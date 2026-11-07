#!/usr/bin/env python3
"""hobkin-lore: reference packs ("lores") for languages and their tools, that an agent looks things up in
instead of holding them in context. One stdlib-only file; a lore is one SQLite file.

A lore holds a language's reference (sections, every function/class/keyword as a symbol, examples) and its
tools' documentation (what each command-line tool is for, its flags, real runs), searchable with SQLite's
full-text search. Installed, it's a skill folder that hobkin and Claude Code both read: SKILL.md, this file
(as lore.py, the lookup tool) and the .lore.db.

Making a lore:
    hobkin-lore compile SRC [-o NAME.lore.db]   compile a pack source (a folder: pack.json, sections.jsonl,
                                                symbols.jsonl, tools.jsonl, examples.jsonl, cheatsheet.md,
                                                skill.md) into a lore; examples are run through the pack's
                                                check command when it has one (--no-check to skip)
    hobkin-lore install LORE [--hobkin | --claude | --project DIR | --to DIR]
                                                a skill folder for it: hobkin's shared skills (the default),
                                                ~/.claude/skills, DIR/.claude/skills, or any folder

Looking things up (an agent runs these, as `python3 lore.py ...` in the skill folder, or `hobkin-lore ...`
with --lore FILE or $LORE):
    info                    what this lore covers, and its counts
    cheatsheet              the short always-useful page
    toc [ID]                the outline, or one branch of it
    search QUERY [--kind K] ranked hits with snippets (K: section, symbol, tool, example, note)
    symbol NAME             one function/class/keyword/...: signature, behaviour, examples, notes
    show ID [--from N]      a section, a page at a time
    examples [QUERY] [--symbol NAME]   worked examples (with their checked result)
    tools                   the language's command-line tools
    tool NAME               one tool: purpose, usage, flags, real runs
    note TEXT [--on REF]    keep something learned the hard way (kept apart from the lore; survives rebuilds)
    notes [QUERY]           what's been noted

Serving lores to any MCP client (VS Code, Cursor, Claude Desktop...), over stdio:
    hobkin-lore mcp [LORE...]                   (in an installed skill: python3 lore.py mcp)

Standard library only; Python 3.9+. See README.md for the pack source format.
"""

from __future__ import annotations

__version__ = "0.1.0"

import argparse
import difflib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

# ---- map: `grep -n '^# ----' hobkin_lore.py` lists these sections.
#   format        the lore's schema, the pack source files, reading and checking them
#   compile       pack source -> .lore.db (running the examples through the pack's check command)
#   lookup        the commands an agent uses: info, cheatsheet, toc, search, symbol, show, examples, tools, tool
#   notes         what agents learn, in a notes file beside the lore (never inside it)
#   install       the skill folder for hobkin and Claude Code: SKILL.md, lore.py, the lore
#   mcp           the lookups as an MCP server over stdio (VS Code, Cursor, Claude Desktop, any MCP client)
#   command line  argument parsing, main


# ---- format: the lore's schema, the pack source files, reading and checking them

SCHEMA_VERSION = 1      # a lore of another version is rebuilt from its source, never converted
PAGE_LINES = 80         # `show` gives this many lines at a time
BODY_LINES = 40         # longest body `symbol`/`tool`/`examples` print before pointing at `show`
SYMBOL_KINDS = ("function", "method", "class", "property", "event", "keyword", "operator", "statement", "type",
                "variable", "constant", "directive", "builtin", "other")

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sections (id TEXT PRIMARY KEY, parent TEXT, title TEXT NOT NULL, body TEXT NOT NULL, source TEXT,
                       ord INTEGER);
CREATE TABLE symbols (name TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT, summary TEXT, body TEXT,
                      section TEXT, source TEXT, see TEXT, PRIMARY KEY (name, kind));
CREATE TABLE tools (name TEXT PRIMARY KEY, purpose TEXT, usage TEXT, body TEXT, flags TEXT, runs TEXT);
CREATE TABLE examples (id TEXT PRIMARY KEY, title TEXT, code TEXT NOT NULL, input TEXT, expect TEXT, notes TEXT,
                       symbols TEXT, checked TEXT, check_output TEXT);
CREATE VIRTUAL TABLE fts USING fts5(kind UNINDEXED, ref UNINDEXED, title, body,
                                    tokenize = "unicode61 tokenchars '_@$'");
"""
NOTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, at TEXT, by TEXT, ref TEXT, text TEXT NOT NULL);
"""

SOURCE_FILES = ("pack.json", "sections.jsonl", "symbols.jsonl", "tools.jsonl", "examples.jsonl",
                "cheatsheet.md", "skill.md")


class LoreError(Exception):
    """Something the user can fix: printed without a traceback."""


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise LoreError(f"{path.name}:{n}: not JSON ({e.msg})") from None
    return out


def read_source(src: Path) -> dict:
    """A pack source folder, read and checked. Errors stop the compile; warnings are returned."""
    if not (src / "pack.json").exists():
        raise LoreError(f"{src} has no pack.json (a pack source folder holds {', '.join(SOURCE_FILES)})")
    pack = json.loads((src / "pack.json").read_text(encoding="utf-8"))
    for key in ("name", "title", "description"):
        if not pack.get(key):
            raise LoreError(f"pack.json needs {key!r}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", pack["name"]):
        raise LoreError("pack.json name: lowercase letters, digits and hyphens (it names the skill)")
    p = {"pack": pack,
         "sections": read_jsonl(src / "sections.jsonl"), "symbols": read_jsonl(src / "symbols.jsonl"),
         "tools": read_jsonl(src / "tools.jsonl"), "examples": read_jsonl(src / "examples.jsonl"),
         "cheatsheet": (src / "cheatsheet.md").read_text(encoding="utf-8") if (src / "cheatsheet.md").exists() else "",
         "skill": (src / "skill.md").read_text(encoding="utf-8") if (src / "skill.md").exists() else ""}
    errors, warnings = [], []
    ids = [s.get("id") for s in p["sections"]]
    for s in p["sections"]:
        if not s.get("id") or not s.get("title") or s.get("body") is None:
            errors.append(f"section {s.get('id')!r}: needs id, title and body")
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        errors.append(f"sections: duplicate ids {sorted(dup)}")
    known = set(ids)
    for s in p["sections"]:
        if s.get("parent") and s["parent"] not in known:
            errors.append(f"section {s['id']!r}: parent {s['parent']!r} doesn't exist")
    names = set()
    for y in p["symbols"]:
        if not y.get("name") or not y.get("kind"):
            errors.append(f"symbol {y.get('name')!r}: needs name and kind")
            continue
        if y["kind"] not in SYMBOL_KINDS:
            errors.append(f"symbol {y['name']!r}: kind {y['kind']!r} isn't one of {', '.join(SYMBOL_KINDS)}")
        if (y["name"], y["kind"]) in names:
            errors.append(f"symbol {y['name']!r} ({y['kind']}) twice")
        names.add((y["name"], y["kind"]))
        if y.get("section") and y["section"] not in known:
            errors.append(f"symbol {y['name']!r}: section {y['section']!r} doesn't exist")
        if not y.get("source"):
            warnings.append(f"symbol {y['name']!r}: no source (where in the documentation it comes from)")
    symbol_names = {n for n, _ in names}
    for y in p["symbols"]:
        for ref in y.get("see") or []:
            if ref not in symbol_names and ref not in known:
                warnings.append(f"symbol {y['name']!r}: see {ref!r} is neither a symbol nor a section")
    eids = [e.get("id") for e in p["examples"]]
    for e in p["examples"]:
        if not e.get("id") or not e.get("code"):
            errors.append(f"example {e.get('id')!r}: needs id and code")
        for ref in e.get("symbols") or []:
            if ref not in symbol_names:
                warnings.append(f"example {e.get('id')!r}: symbol {ref!r} isn't in symbols.jsonl")
    if len(set(eids)) != len(eids):
        errors.append("examples: duplicate ids")
    for t in p["tools"]:
        if not t.get("name"):
            errors.append("a tool without a name")
    if errors:
        raise LoreError("the pack source has problems:\n  " + "\n  ".join(errors))
    p["warnings"] = warnings
    return p


# ---- compile: pack source -> .lore.db (running the examples through the pack's check command)

def run_check(check: dict, example: dict, timeout: float = 20) -> tuple[str, str]:
    """Run one example through the pack's check command: `{file}` in the command is a file holding the code,
    the example's input goes to stdin. Passes when the command succeeds and, if the example has `expect`, its
    output matches (whitespace at the ends ignored). Returns ("pass" | "fail" | "error", what it printed)."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / ("example" + check.get("ext", ".txt"))
        f.write_text(example["code"], encoding="utf-8")
        cmd = check["command"].replace("{file}", str(f))
        try:
            r = subprocess.run(cmd, shell=True, input=example.get("input") or "", capture_output=True, text=True,
                               timeout=timeout, cwd=tmp)
        except subprocess.TimeoutExpired:
            return "error", f"timed out after {timeout:g}s"
        out = (r.stdout + (("\n" + r.stderr) if r.stderr.strip() else "")).strip()
        if r.returncode != 0:
            return "fail", out or f"exit {r.returncode}"
        if example.get("expect") is not None and r.stdout.strip() != str(example["expect"]).strip():
            return "fail", f"expected:\n{str(example['expect']).strip()}\ngot:\n{r.stdout.strip()}"
        return "pass", out


def compile_pack(src: Path, out: Path, check: bool = True, log=print) -> dict:
    p = read_source(src)
    pack = p["pack"]
    out = out.resolve()
    tmp = out.with_suffix(".building")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
    except sqlite3.OperationalError as e:
        con.close()
        tmp.unlink()
        raise LoreError(f"this Python's SQLite can't build a lore ({e}); it needs FTS5") from None
    meta = {"schema": str(SCHEMA_VERSION), "name": pack["name"], "title": pack["title"],
            "description": pack["description"], "language": pack.get("language", pack["title"]),
            "version": pack.get("version", ""), "sources": json.dumps(pack.get("sources", [])),
            "check": json.dumps(pack.get("check")) if pack.get("check") else "",
            "cheatsheet": p["cheatsheet"], "skill": p["skill"], "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "built_by": f"hobkin-lore {__version__}"}
    con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    for i, s in enumerate(p["sections"]):
        con.execute("INSERT INTO sections VALUES (?,?,?,?,?,?)",
                    (s["id"], s.get("parent"), s["title"], s["body"], s.get("source"), i))
        con.execute("INSERT INTO fts VALUES ('section', ?, ?, ?)", (s["id"], s["title"], s["body"]))
    for y in p["symbols"]:
        con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?)",
                    (y["name"], y["kind"], y.get("signature"), y.get("summary"), y.get("body"), y.get("section"),
                     y.get("source"), json.dumps(y.get("see") or [])))
        con.execute("INSERT INTO fts VALUES ('symbol', ?, ?, ?)",
                    (y["name"], f"{y['name']} {y.get('signature') or ''}",
                     " ".join(filter(None, [y.get("summary"), y.get("body")]))))
    for t in p["tools"]:
        con.execute("INSERT INTO tools VALUES (?,?,?,?,?,?)",
                    (t["name"], t.get("purpose"), t.get("usage"), t.get("body"), json.dumps(t.get("flags") or []),
                     json.dumps(t.get("runs") or [])))
        flags = " ".join(f"{f.get('flag', '')} {f.get('meaning', '')}" for f in t.get("flags") or [])
        con.execute("INSERT INTO fts VALUES ('tool', ?, ?, ?)",
                    (t["name"], t["name"], " ".join(filter(None, [t.get("purpose"), t.get("usage"), t.get("body"),
                                                                  flags]))))
    chk = pack.get("check")
    can_check = bool(check and chk and shutil.which(chk["command"].split()[0]))
    if check and chk and not can_check:
        log(f"note: {chk['command'].split()[0]} isn't installed here, so the examples aren't checked")
    results = {"pass": 0, "fail": 0, "error": 0, "unchecked": 0}
    for e in p["examples"]:
        state, output = run_check(chk, e) if can_check else ("unchecked", "")
        results[state] += 1
        if state in ("fail", "error"):
            log(f"example {e['id']}: {state}\n{textwrap.indent(output, '    ')}")
        con.execute("INSERT INTO examples VALUES (?,?,?,?,?,?,?,?,?)",
                    (e["id"], e.get("title"), e["code"], e.get("input"),
                     None if e.get("expect") is None else str(e["expect"]), e.get("notes"),
                     json.dumps(e.get("symbols") or []), state, output[:4000]))
        con.execute("INSERT INTO fts VALUES ('example', ?, ?, ?)",
                    (e["id"], e.get("title") or e["id"], " ".join(filter(None, [e["code"], e.get("notes")]))))
    con.commit()
    con.execute("INSERT INTO fts(fts) VALUES ('optimize')")
    con.commit()
    con.close()
    os.replace(tmp, out)
    for w in p["warnings"]:
        log(f"warning: {w}")
    return {"lore": str(out), "sections": len(p["sections"]), "symbols": len(p["symbols"]),
            "tools": len(p["tools"]), "examples": len(p["examples"]), "checked": results,
            "warnings": len(p["warnings"])}


# ---- lookup: the commands an agent uses: info, cheatsheet, toc, search, symbol, show, examples, tools, tool

def find_lore(given: str | None) -> Path:
    """The lore to read: --lore, else $LORE, else the one .lore.db beside this file (an installed skill)."""
    if given or os.environ.get("LORE"):
        path = Path(given or os.environ["LORE"]).expanduser()
        if not path.exists():
            raise LoreError(f"no lore at {path}")
        return path
    here = sorted(Path(__file__).resolve().parent.glob("*.lore.db"))
    if len(here) == 1:
        return here[0]
    raise LoreError("which lore? give --lore FILE (or set LORE)" if not here else
                    f"several lores here ({', '.join(h.name for h in here)}): give --lore FILE")


class Lore:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            self.meta = dict(self.con.execute("SELECT key, value FROM meta"))
        except sqlite3.DatabaseError as e:
            raise LoreError(f"{path} isn't a lore ({e})") from None
        if self.meta.get("schema") != str(SCHEMA_VERSION):
            raise LoreError(f"{path.name} was built for lore schema {self.meta.get('schema')}; this tool reads "
                            f"schema {SCHEMA_VERSION}. Rebuild it from its source with this version.")
        self.notes = Notes(notes_path(path))

    def q(self, sql: str, *args):
        return self.con.execute(sql, args).fetchall()


def page(text: str, start: int, lines: int, more: str) -> str:
    rows = text.splitlines()
    chunk = rows[start:start + lines]
    out = "\n".join(chunk)
    if start + lines < len(rows):
        out += f"\n\n[lines {start + 1}-{start + len(chunk)} of {len(rows)}; more: {more} --from {start + lines}]"
    return out


def cmd_info(lore: Lore, args) -> str:
    m = lore.meta
    n = lambda t: lore.q(f"SELECT COUNT(*) FROM {t}")[0][0]  # noqa: E731
    checked = dict(lore.q("SELECT checked, COUNT(*) FROM examples GROUP BY checked"))
    lines = [f"{m['title']}{' ' + m['version'] if m.get('version') else ''}: {m['description']}",
             f"{n('sections')} sections, {n('symbols')} symbols, {n('tools')} tools, {n('examples')} examples"
             + (f" ({checked.get('pass', 0)} checked and passing)" if checked.get("pass") else ""),
             f"notes: {len(lore.notes.all())} (in {lore.notes.path.name})",
             f"built {m.get('built_at', '?')} by {m.get('built_by', '?')}"]
    srcs = json.loads(m.get("sources") or "[]")
    if srcs:
        lines.append("from: " + "; ".join(s if isinstance(s, str) else s.get("title", str(s)) for s in srcs))
    return "\n".join(lines)


def cmd_cheatsheet(lore: Lore, args) -> str:
    return lore.meta.get("cheatsheet") or "(this lore has no cheatsheet)"


def cmd_toc(lore: Lore, args) -> str:
    rows = lore.q("SELECT id, parent, title FROM sections ORDER BY ord")
    kids: dict = {}
    for i, parent, title in rows:
        kids.setdefault(parent, []).append((i, title))
    root = args.id
    if root and root not in {r[0] for r in rows}:
        raise LoreError(f"no section {root!r}" + suggest(root, [r[0] for r in rows]))
    out, depth_limit = [], 3 if root else 2

    def walk(parent, depth):
        for i, title in kids.get(parent, []):
            more = f"  (+{len(kids[i])})" if i in kids and depth + 1 >= depth_limit else ""
            out.append(f"{'  ' * depth}{title}  [{i}]{more}")
            if depth + 1 < depth_limit:
                walk(i, depth + 1)
    if root:
        title = next(t for i, _, t in rows if i == root)
        out.append(f"{title}  [{root}]")
        walk(root, 1)
    else:
        walk(None, 0)
    return "\n".join(out) or "(no sections)"


def fts_query(text: str, op: str) -> str:
    """The user's words as an FTS5 query: each one quoted (so punctuation can't break the syntax) and matched as a
    prefix (identifiers are whole tokens), joined with AND or OR."""
    words = re.findall(r"[\w@$]+", text)
    return f" {op} ".join('"' + w.replace('"', "") + '"*' for w in words)   # a prefix: "group" finds group_by


def cmd_search(lore: Lore, args) -> str:
    query = " ".join(args.query)
    kinds = (args.kind,) if args.kind else ("section", "symbol", "tool", "example")
    hits = []
    if args.kind != "note":
        for op in ("AND", "OR"):            # every word first; any word if that finds nothing
            q = fts_query(query, op)
            if not q:
                break
            ph = ",".join("?" * len(kinds))
            hits = lore.q(f"SELECT kind, ref, title, snippet(fts, 3, '[', ']', ' ... ', 14) FROM fts "
                          f"WHERE fts MATCH ? AND kind IN ({ph}) ORDER BY bm25(fts, 0, 0, 4.0, 1.0) LIMIT ?",
                          q, *kinds, args.n)
            if hits:
                break
    out = []
    if args.kind in (None, "symbol"):       # a word that is exactly a symbol's name: that symbol first
        words = {w.lower() for w in re.findall(r"[^\s()\[\];,\"']+", query)}
        exact = [r[0] for r in lore.q("SELECT DISTINCT name FROM symbols") if r[0].lower() in words]
        for name in exact:
            summary = lore.q("SELECT signature, summary FROM symbols WHERE name = ? LIMIT 1", name)[0]
            out.append(f"symbol: {name}  ->  symbol {name}\n    {summary[0] or ''}: {summary[1] or ''}")
        hits = [h for h in hits if not (h[0] == "symbol" and h[1] in exact)]
    for kind, ref, title, snip in hits:
        hint = {"section": f"show {ref}", "symbol": f"symbol {ref}", "tool": f"tool {ref}",
                "example": f"examples --id {ref}"}[kind]
        out.append(f"{kind}: {ref if kind == 'symbol' else title.strip()}  ->  {hint}\n    {' '.join(snip.split())}")
    if args.kind in (None, "note"):
        out += [f"note on {r or '(general)'}: {t}" for r, t in lore.notes.search(query)[:5]]
    return "\n".join(out) or f"nothing for {query!r}. Try other words, or browse: toc"


def suggest(name: str, names: list[str]) -> str:
    close = difflib.get_close_matches(name, names, n=5, cutoff=0.6)
    low = [n for n in names if name.lower() in n.lower() and n not in close][:5]
    near = close + low
    return f"; did you mean: {', '.join(near)}?" if near else ""


def cmd_symbol(lore: Lore, args) -> str:
    rows = lore.q("SELECT name, kind, signature, summary, body, section, source, see FROM symbols "
                  "WHERE name = ? OR lower(name) = lower(?) ORDER BY name = ? DESC, kind", args.name, args.name,
                  args.name)
    if not rows:
        names = [r[0] for r in lore.q("SELECT name FROM symbols")]
        raise LoreError(f"no symbol {args.name!r}{suggest(args.name, names)} (or: search {args.name})")
    out = []
    for name, kind, sig, summary, body, section, source, see in rows:
        out.append(f"{name}  ({kind})")
        if sig:
            out.append(f"  {sig}")
        if summary:
            out.append(summary)
        if body:
            b = body.splitlines()
            out.append("\n".join(b[:BODY_LINES]) + (f"\n[... {len(b) - BODY_LINES} more lines: show {section}]"
                                                      if len(b) > BODY_LINES and section else ""))
        refs = json.loads(see or "[]")
        if refs:
            out.append("see: " + ", ".join(refs))
        ex = lore.q("SELECT id, title FROM examples WHERE EXISTS (SELECT 1 FROM json_each(examples.symbols) "
                    "WHERE value = ?) LIMIT 5", name)
        if ex:
            out.append("examples: " + ", ".join(f"{i} ({t})" if t else i for i, t in ex)
                       + f"  ->  examples --symbol {name}")
        if section:
            out.append(f"section: {section}")
        if source:
            out.append(f"source: {source}")
        notes = lore.notes.on(name)
        if notes:
            out.append("notes:\n" + "\n".join(f"  - {t}" for t in notes))
        out.append("")
    return "\n".join(out).rstrip()


def cmd_show(lore: Lore, args) -> str:
    row = lore.q("SELECT id, title, body, source, parent FROM sections WHERE id = ?", args.id)
    if not row:
        ids = [r[0] for r in lore.q("SELECT id FROM sections")]
        raise LoreError(f"no section {args.id!r}{suggest(args.id, ids)}")
    i, title, body, source, parent = row[0]
    kids = lore.q("SELECT id, title FROM sections WHERE parent = ? ORDER BY ord", i)
    head = f"# {title}  [{i}]" + (f"\n(in {parent})" if parent else "")
    text = body + ("\n\nSubsections: " + ", ".join(f"{t} [{k}]" for k, t in kids) if kids else "")
    text += f"\n\nsource: {source}" if source else ""
    notes = lore.notes.on(i)
    if notes:
        text += "\n\nnotes:\n" + "\n".join(f"  - {t}" for t in notes)
    return head + "\n\n" + page(text, args.from_, PAGE_LINES, f"show {i}")


def cmd_examples(lore: Lore, args) -> str:
    if args.id:
        rows = lore.q("SELECT id, title, code, input, expect, notes, checked FROM examples WHERE id = ?", args.id)
    elif args.symbol:
        rows = lore.q("SELECT id, title, code, input, expect, notes, checked FROM examples WHERE EXISTS "
                      "(SELECT 1 FROM json_each(examples.symbols) WHERE value = ?) LIMIT ?", args.symbol, args.n)
    elif args.query:
        ids = [r[0] for r in lore.q("SELECT ref FROM fts WHERE fts MATCH ? AND kind = 'example' "
                                    "ORDER BY bm25(fts, 0, 0, 4.0, 1.0) LIMIT ?",
                                    fts_query(" ".join(args.query), "OR"), args.n)] if fts_query(
            " ".join(args.query), "OR") else []
        rows = [lore.q("SELECT id, title, code, input, expect, notes, checked FROM examples WHERE id = ?", i)[0]
                for i in ids]
    else:
        rows = lore.q("SELECT id, title, code, input, expect, notes, checked FROM examples LIMIT ?", args.n)
    if not rows:
        return "no examples found"
    out = []
    for i, title, code, inp, expect, notes, checked in rows:
        state = {"pass": "checked: runs as shown", "fail": "checked: FAILED (don't trust it)",
                 "error": "check errored", "unchecked": "not checked"}.get(checked, checked)
        out.append(f"## {title or i}  [{i}]  ({state})\n{code.rstrip()}")
        if inp:
            out.append(f"input:\n{inp.rstrip()}")
        if expect is not None:
            out.append(f"output:\n{expect.rstrip()}")
        if notes:
            out.append(notes.rstrip())
        out.append("")
    return "\n".join(out).rstrip()


def cmd_tools(lore: Lore, args) -> str:
    rows = lore.q("SELECT name, purpose FROM tools ORDER BY name")
    return "\n".join(f"{n}: {p or ''}  ->  tool {n}" for n, p in rows) or "(this lore documents no tools)"


def cmd_tool(lore: Lore, args) -> str:
    row = lore.q("SELECT name, purpose, usage, body, flags, runs FROM tools WHERE name = ?", args.name)
    if not row:
        names = [r[0] for r in lore.q("SELECT name FROM tools")]
        raise LoreError(f"no tool {args.name!r}{suggest(args.name, names)} (tools lists them)")
    name, purpose, usage, body, flags, runs = row[0]
    out = [f"# {name}", purpose or ""]
    if usage:
        out.append(f"usage: {usage}")
    if body:
        out.append(body.rstrip())
    fl = json.loads(flags or "[]")
    if fl:
        width = min(28, max(len(f.get("flag", "")) for f in fl))
        out.append("flags:\n" + "\n".join(f"  {f.get('flag', ''):<{width}}  {f.get('meaning', '')}" for f in fl))
    rs = json.loads(runs or "[]")
    for r in rs[:6]:
        out.append(f"$ {r.get('command', '')}\n{(r.get('output') or '').rstrip()}")
    notes = lore.notes.on(name)
    if notes:
        out.append("notes:\n" + "\n".join(f"  - {t}" for t in notes))
    return page("\n\n".join(x for x in out if x), args.from_, PAGE_LINES * 2, f"tool {name}")


# ---- notes: what agents learn, in a notes file beside the lore (never inside it)

def notes_path(lore_path: Path) -> Path:
    return Path(os.environ.get("LORE_NOTES") or str(lore_path).replace(".lore.db", "") + ".notes.db")


class Notes:
    """Notes live apart from the lore, so a rebuilt or reinstalled lore keeps what agents learned. Opened (and
    created) only when written to."""

    def __init__(self, path: Path):
        self.path = path

    def _con(self, write=False):
        if not write and not self.path.exists():
            return None
        con = sqlite3.connect(self.path, timeout=10)
        con.executescript(NOTES_SCHEMA)
        return con

    def add(self, text: str, ref: str | None, by: str | None) -> int:
        con = self._con(write=True)
        cur = con.execute("INSERT INTO notes (at, by, ref, text) VALUES (?,?,?,?)",
                          (time.strftime("%Y-%m-%d %H:%M"), by, ref, text))
        con.commit()
        con.close()
        return cur.lastrowid

    def all(self) -> list:
        con = self._con()
        if not con:
            return []
        rows = con.execute("SELECT id, at, by, ref, text FROM notes ORDER BY id").fetchall()
        con.close()
        return rows

    def on(self, ref: str) -> list[str]:
        return [t for _, _, _, r, t in self.all() if r == ref]

    def search(self, query: str) -> list[tuple]:
        words = [w.lower() for w in re.findall(r"[\w@$]+", query)]
        return [(r, t) for _, _, _, r, t in self.all()
                if words and all(w in f"{r or ''} {t}".lower() for w in words)]


def cmd_note(lore: Lore, args) -> str:
    text = " ".join(args.text).strip()
    if not text:
        raise LoreError("note what? (note TEXT [--on SYMBOL|SECTION|TOOL])")
    n = lore.notes.add(text, args.on, os.environ.get("HOBKIN_ID") or os.environ.get("USER"))
    return f"noted (#{n}{' on ' + args.on if args.on else ''}); it shows up in search, and on {args.on or 'notes'}"


def cmd_notes(lore: Lore, args) -> str:
    rows = lore.notes.all()
    if args.query:
        words = [w.lower() for w in args.query]
        rows = [r for r in rows if all(w in f"{r[3] or ''} {r[4]}".lower() for w in words)]
    return "\n".join(f"#{i} {at}{' ' + by if by else ''}{' on ' + ref if ref else ''}: {t}"
                     for i, at, by, ref, t in rows) or "no notes yet"


# ---- install: the skill folder for hobkin and Claude Code: SKILL.md, lore.py, the lore

def skill_md(meta: dict, has_tools: bool, has_examples: bool) -> str:
    """SKILL.md in the format hobkin and Claude Code both read: name and description in front matter, then the
    instructions. The description says when to use it, since that's all an agent sees before opening it."""
    name, title = f"{meta['name']}-lore", meta["title"]
    desc = (f"The {title} reference: look up {meta['language']} syntax, functions and keywords"
            + (", and its command-line tools," if has_tools else "")
            + f" instead of guessing. Use it whenever you write, read, debug or explain {meta['language']}.")
    run = 'python3 "<this skill\'s folder>/lore.py"'
    lines = [
        "---", f"name: {name}", f"description: {json.dumps(desc)}", "---", "",
        f"# {title}{' ' + meta['version'] if meta.get('version') else ''}", "",
        meta["description"], "",
        "This is a searchable reference, not something to read through. Look up exactly what you need, when you "
        "need it. Every command below is",
        "", f"    {run} COMMAND ...", "",
        "using this skill's folder (its full path is given when the skill is opened). Output is short and paged.",
        "",
        "## Commands", "",
        "| Command | What it gives |", "| --- | --- |",
        "| `cheatsheet` | The essentials: read this first, once per task |",
        "| `search WORDS [--kind section\\|symbol\\|tool\\|example\\|note]` | Ranked hits with snippets |",
        "| `symbol NAME` | One function, class, keyword...: signature, behaviour, examples, notes |",
        "| `toc [ID]` | The outline, or one branch of it |",
        "| `show ID [--from N]` | A section, a page at a time |",
        "| `examples [WORDS] [--symbol NAME]` | Worked examples" + (" (checked: they run as shown)" if has_examples
                                                                    else "") + " |",
    ]
    if has_tools:
        lines += ["| `tools`, `tool NAME` | The command-line tools: purpose, flags, real runs |"]
    lines += [
        "| `note TEXT [--on NAME]` | Keep something you learned the hard way, for next time |",
        "| `info` | What this reference covers |", "",
        "## How to work", "",
        "1. Read the `cheatsheet` once at the start of a task.",
        f"2. Before you use a function, class, keyword or event you aren't certain of, look it up with `symbol` "
        f"(or `search` when you don't know its name). Don't rely on memory for {meta['language']}'s details: "
        "names, argument order, return values and edge cases are where mistakes come from.",
        "3. Prefer a checked example over your own guess at an idiom.",
    ]
    if has_tools:
        lines += ["4. Use the language's tools (`tools`, then `tool NAME`) to run, check or test what you write, "
                  "and read their errors with the tool's page at hand."]
    lines += [f"{5 if has_tools else 4}. Say which entries you relied on (the ids in brackets), so a reviewer "
              "can check them.",
              f"{6 if has_tools else 5}. When the reference was wrong, missing or surprising, `note` it "
              "(with `--on NAME`), so the next lookup shows it.", ""]
    if meta.get("skill"):
        lines += ["## About this language", "", meta["skill"].rstrip(), ""]
    return "\n".join(lines)


def install_dir(args) -> Path:
    if args.to:
        return Path(args.to).expanduser()
    if args.claude:
        return Path("~/.claude/skills").expanduser()
    if args.project:
        return Path(args.project).expanduser() / ".claude" / "skills"
    return Path(os.environ.get("HOBKIN_HOME", "~/.hobkins")).expanduser() / "_shared" / "skills"


def install(lore_path: Path, base: Path) -> Path:
    lore = Lore(lore_path)
    meta = lore.meta
    has_tools = bool(lore.q("SELECT COUNT(*) FROM tools")[0][0])
    has_examples = bool(lore.q("SELECT COUNT(*) FROM examples WHERE checked = 'pass'")[0][0])
    lore.con.close()
    folder = base / f"{meta['name']}-lore"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.lore.db"):          # a reinstall replaces the lore; notes (*.notes.db) stay
        if old.name != f"{meta['name']}.lore.db":
            old.unlink()
    shutil.copyfile(lore_path, folder / f"{meta['name']}.lore.db")
    shutil.copyfile(Path(__file__).resolve(), folder / "lore.py")
    (folder / "SKILL.md").write_text(skill_md(meta, has_tools, has_examples), encoding="utf-8")
    return folder


# ---- mcp: the lookups as an MCP server (stdio), for VS Code, Cursor, Claude Desktop and any other MCP client

MCP_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")    # newest first; we answer in the client's if we know it


def mcp_tools(lores: dict[str, Lore]) -> list[dict]:
    """The tools: the lookup commands, each described for a model that hasn't opened the skill. With several
    lores, each tool takes `lore` (one of their names)."""
    names = list(lores)
    langs = ", ".join(lores[n].meta["language"] for n in names)
    which = {"lore": {"type": "string", "enum": names, "description": "Which reference: " + ", ".join(
        f"{n} ({lores[n].meta['title']})" for n in names)}} if len(names) > 1 else {}
    s = lambda props=None, required=(): {"type": "object", "properties": {**which, **(props or {})},  # noqa: E731
                                         "required": (["lore"] if which else []) + list(required)}
    text = lambda d: {"type": "string", "description": d}  # noqa: E731
    num = lambda d: {"type": "integer", "description": d}  # noqa: E731
    tools = [
        ("cheatsheet", f"The essentials of {langs}: read once at the start of a task, before writing code.", s()),
        ("search", f"Search the {langs} reference: sections, symbols, tools, examples and notes, ranked, with "
                   "snippets. A word that is exactly a symbol's name puts that symbol first.",
         s({"query": text("words to look for, e.g. a function name or what you want to do"),
            "kind": {"type": "string", "enum": ["section", "symbol", "tool", "example", "note"],
                     "description": "only this kind of entry"},
            "limit": num("how many hits (default 8)")}, ["query"])),
        ("symbol", f"One {langs} function, class, keyword, operator...: signature, behaviour, see-also, examples "
                   "and notes. Look up anything you aren't certain of before using it.",
         s({"name": text("its exact name; a near miss suggests the right one")}, ["name"])),
        ("toc", "The reference's outline, or one branch of it (section ids in brackets).",
         s({"id": text("a section id, for its branch")})),
        ("show", "One section of the reference, a page at a time.",
         s({"id": text("the section id"), "from": num("the line to start at, for the next page")}, ["id"])),
        ("examples", "Worked examples, each marked whether the language's own tool ran it as shown.",
         s({"query": text("words to look for"), "symbol": text("examples that use this symbol"),
            "id": text("one example by id")})),
        ("tools", f"The command-line tools that come with {langs}.", s()),
        ("tool", "One command-line tool: purpose, usage, flags and real runs with their output.",
         s({"name": text("the tool's name")}, ["name"])),
        ("note", "Keep something learned the hard way (the reference was wrong, missing or surprising), so the "
                 "next lookup shows it.",
         s({"text": text("what you learned"), "on": text("the symbol, section or tool it's about")}, ["text"])),
        ("info", "What this reference covers, its counts and where it came from.", s()),
    ]
    return [{"name": n, "description": d, "inputSchema": sch} for n, d, sch in tools]


def mcp_call(lores: dict[str, Lore], name: str, a: dict) -> str:
    """Run one tool: the same code as the command line, so both always agree."""
    lore = lores[a["lore"]] if len(lores) > 1 else next(iter(lores.values()))
    words = lambda v: str(v).split() if v else []  # noqa: E731
    ns = {"cheatsheet": {}, "info": {}, "tools": {},
          "search": {"query": words(a.get("query")), "kind": a.get("kind"), "n": int(a.get("limit") or 8)},
          "symbol": {"name": a.get("name", "")},
          "toc": {"id": a.get("id")},
          "show": {"id": a.get("id", ""), "from_": int(a.get("from") or 0)},
          "examples": {"query": words(a.get("query")), "symbol": a.get("symbol"), "id": a.get("id"), "n": 5},
          "tool": {"name": a.get("name", ""), "from_": int(a.get("from") or 0)},
          "note": {"text": words(a.get("text")), "on": a.get("on")}}
    if name not in ns:
        raise LoreError(f"no tool {name!r}")
    return LOOKUPS[name](lore, argparse.Namespace(**ns[name]))


def mcp_serve(lores: dict[str, Lore], stdin=None, stdout=None) -> None:
    """MCP over stdio: one JSON-RPC message per line in, one per line out. Nothing else goes to stdout."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    tools = mcp_tools(lores)
    first = next(iter(lores.values())).meta
    title = " + ".join(lo.meta["title"] for lo in lores.values())

    def send(msg):
        stdout.write(json.dumps({"jsonrpc": "2.0", **msg}, ensure_ascii=False) + "\n")
        stdout.flush()

    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send({"id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:                          # a notification (initialized, cancelled...): nothing to answer
            continue
        if method == "initialize":
            asked = params.get("protocolVersion")
            send({"id": mid, "result": {
                "protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": f"{first['name']}-lore" if len(lores) == 1 else "hobkin-lore",
                               "title": f"{title} reference", "version": __version__},
                "instructions": (f"A searchable reference for {title}. Before writing or fixing code in it, read "
                                 "cheatsheet once, then look up each function, keyword or tool you aren't certain "
                                 "of (symbol, or search when you don't know the name). Prefer checked examples "
                                 "to guesses, and note what the reference got wrong.")}})
        elif method == "ping":
            send({"id": mid, "result": {}})
        elif method == "tools/list":
            send({"id": mid, "result": {"tools": tools}})
        elif method == "tools/call":
            try:
                text, err = mcp_call(lores, params.get("name", ""), params.get("arguments") or {}), False
            except (LoreError, KeyError, ValueError) as e:
                text, err = str(e) if not isinstance(e, KeyError) else f"which lore? {e} isn't one", True
            send({"id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": err}})
        else:
            send({"id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})


# ---- command line: argument parsing, main

LOOKUPS = {"info": cmd_info, "cheatsheet": cmd_cheatsheet, "toc": cmd_toc, "search": cmd_search,
           "symbol": cmd_symbol, "show": cmd_show, "examples": cmd_examples, "tools": cmd_tools, "tool": cmd_tool,
           "note": cmd_note, "notes": cmd_notes}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hobkin-lore" if Path(sys.argv[0]).name != "lore.py" else "lore.py",
                                 description="Reference packs (lores) for languages and their tools.")
    ap.add_argument("--version", action="version", version=f"hobkin-lore {__version__}")
    ap.add_argument("--lore", help="the .lore.db to read (default: $LORE, or the one beside lore.py)")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("compile", help="compile a pack source folder into a lore")
    c.add_argument("src")
    c.add_argument("-o", "--out", help="where to write it (default: NAME.lore.db here)")
    c.add_argument("--no-check", action="store_true", help="don't run the examples through the check command")
    i = sub.add_parser("install", help="install a lore as a skill (hobkin's shared skills by default)")
    i.add_argument("lore_file", metavar="LORE")
    where = i.add_mutually_exclusive_group()
    where.add_argument("--hobkin", action="store_true", help="~/.hobkins/_shared/skills (the default)")
    where.add_argument("--claude", action="store_true", help="~/.claude/skills (Claude Code, for you)")
    where.add_argument("--project", metavar="DIR", help="DIR/.claude/skills (Claude Code, for one project)")
    where.add_argument("--to", metavar="DIR", help="any skills folder")
    sub.add_parser("info", help="what this lore covers")
    sub.add_parser("cheatsheet", help="the essentials")
    t = sub.add_parser("toc", help="the outline, or one branch of it")
    t.add_argument("id", nargs="?")
    s = sub.add_parser("search", help="ranked hits with snippets")
    s.add_argument("query", nargs="+")
    s.add_argument("--kind", choices=["section", "symbol", "tool", "example", "note"])
    s.add_argument("-n", type=int, default=8, help="how many hits (default 8)")
    y = sub.add_parser("symbol", help="one function/class/keyword/...")
    y.add_argument("name")
    sh = sub.add_parser("show", help="a section, a page at a time")
    sh.add_argument("id")
    sh.add_argument("--from", dest="from_", type=int, default=0, help="the line to start at")
    e = sub.add_parser("examples", help="worked examples")
    e.add_argument("query", nargs="*")
    e.add_argument("--symbol")
    e.add_argument("--id")
    e.add_argument("-n", type=int, default=5)
    m = sub.add_parser("mcp", help="serve lores as an MCP server over stdio (for VS Code, Cursor, Claude Desktop...)")
    m.add_argument("lores", nargs="*", metavar="LORE", help="the .lore.db files (default: --lore, $LORE, or the one "
                   "beside lore.py)")
    sub.add_parser("tools", help="the command-line tools")
    tl = sub.add_parser("tool", help="one tool")
    tl.add_argument("name")
    tl.add_argument("--from", dest="from_", type=int, default=0)
    n = sub.add_parser("note", help="keep something learned the hard way")
    n.add_argument("text", nargs="+")
    n.add_argument("--on", help="the symbol, section or tool it's about")
    ns = sub.add_parser("notes", help="what's been noted")
    ns.add_argument("query", nargs="*")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    try:
        if args.cmd == "compile":
            src = Path(args.src)
            name = json.loads((src / "pack.json").read_text())["name"] if (src / "pack.json").exists() else "pack"
            r = compile_pack(src, Path(args.out or f"{name}.lore.db"), check=not args.no_check)
            c = r["checked"]
            print(f"{r['lore']}: {r['sections']} sections, {r['symbols']} symbols, {r['tools']} tools, "
                  f"{r['examples']} examples ({c['pass']} pass, {c['fail']} fail, {c['error']} error, "
                  f"{c['unchecked']} unchecked), {r['warnings']} warnings")
            return 1 if c["fail"] or c["error"] else 0
        if args.cmd == "install":
            folder = install(Path(args.lore_file), install_dir(args))
            print(f"installed: {folder}\n  SKILL.md, lore.py, {Path(args.lore_file).name}"
                  f"\n  try: python3 {folder / 'lore.py'} info"
                  f"\n  as an MCP server (VS Code, Cursor, Claude Desktop): python3 {folder / 'lore.py'} mcp")
            return 0
        if args.cmd == "mcp":
            paths = [Path(p).expanduser() for p in args.lores] or [find_lore(args.lore)]
            lores = {}
            for p in paths:
                lo = Lore(p)
                if lo.meta["name"] in lores:
                    raise LoreError(f"two lores named {lo.meta['name']!r}")
                lores[lo.meta["name"]] = lo
            mcp_serve(lores)
            return 0
        if args.cmd in LOOKUPS:
            print(LOOKUPS[args.cmd](Lore(find_lore(args.lore)), args))
            return 0
        ap.print_help()
        return 0
    except LoreError as e:
        print(f"lore: {e}", file=sys.stderr)
        return 2


def cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli()
