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

Building a pack source from documentation (with any OpenAI-format model), and reviewing it:
    hobkin-lore build NAME [DOCS...] [--tool CMD]... --base-url URL --model M [--check CMD]
                                                PDF, HTML, Markdown, folders, URLs, tools' --help and man
                                                pages -> packs/NAME, every entry citing the source lines it
                                                rests on; model answers recorded, so rebuilds are cheap
    hobkin-lore review DIR                      a local page: each entry beside its source; accept, edit, reject

Code that lives in a database (a lore whose tools can pull and deploy it), worked from a git repo:
    hobkin-lore ws init LORE | list | pull | status [--remote] | deploy FILE... | validate | compile | test

Serving lores to any MCP client (VS Code, Cursor, Claude Desktop...), over stdio:
    hobkin-lore mcp [LORE...]                   (in an installed skill: python3 lore.py mcp)

Standard library only; Python 3.9+. See README.md for the pack source format.
"""

from __future__ import annotations

__version__ = "0.2.0"

import argparse
import base64
import difflib
import hashlib
import html.parser
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---- map: `grep -n '^# ----' hobkin_lore.py` lists these sections.
#   format        the lore's schema, the pack source files, reading and checking them
#   compile       pack source -> .lore.db (running the examples through the pack's check command)
#   lookup        the commands an agent uses: info, cheatsheet, toc, search, symbol, show, examples, tools, tool
#   notes         what agents learn, in a notes file beside the lore (never inside it)
#   install       the skill folder for hobkin and Claude Code: SKILL.md, lore.py, the lore
#   build         any documentation -> a pack source, with a model: gather, chunk, distill, verify, finish
#   review        a local web page to review a built pack source, entry by entry, beside the source it cites
#   workspace     code that lives in a database, in a git repo: pull, status, deploy (conflict-checked), validate
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

SOURCE_FILES = ("pack.json", "sections.jsonl", "symbols.jsonl", "tools.jsonl", "examples.jsonl", "operations.json",
                "files/",
                "cheatsheet.md", "skill.md")


OPERATIONS = {         # what the lore's operations.json may define; {placeholders} each one is given
    "list": "the objects of a {type}, one per line of output (parsed with `parse`)",
    "project_list": "the objects in a {project}, one per line of output (parsed with `parse`)",
    "pull": "copies object {type} {name} from the database into {file} (or prints it: \"output\": \"stdout\"); "
            "\"missing\": a regex for its message when there's no such object",
    "deploy": "copies {file} into the database as object {type} {name}",
    "validate": "checks {file} (object {type} {name}) without changing the database",
    "compile": "compiles object {type} {name} in the database",
    "build_project": "builds {project} in the database",
    "export_project": "exports {project} into the folder {dir}",
}
OBJECT_KEYS = ("type", "name", "file", "dir", "project")


def check_operations(ops: dict) -> list[str]:
    """Problems with an operations.json: unknown operations, placeholders nothing will fill, parse patterns that
    don't compile. (`build` and `compile` refuse a broken one; `review` shows what's wrong.)"""
    problems = []
    known = set(OBJECT_KEYS) | set((ops.get("variables") or {})) | set((ops.get("secrets") or {}))
    for name, op in (ops.get("operations") or {}).items():
        if name not in OPERATIONS:
            problems.append(f"operation {name!r}: not one of {', '.join(OPERATIONS)}")
            continue
        if not isinstance(op, dict) or not str(op.get("command") or "").strip():
            problems.append(f"operation {name!r}: needs a command")
            continue
        try:
            words = shlex.split(op["command"])
        except ValueError as e:
            problems.append(f"operation {name!r}: {e}")
            continue
        for w in words:
            for ph in re.findall(r"\{(\w+)\}", w):
                if ph not in known:
                    problems.append(f"operation {name!r}: {{{ph}}} isn't a variable, a secret or one of "
                                    f"{', '.join(OBJECT_KEYS)}")
        if op.get("missing"):
            try:
                re.compile(op["missing"])
            except re.error as e:
                problems.append(f"operation {name!r}: missing: {e}")
        if op.get("parse"):
            try:
                rx = re.compile(op["parse"])
                if "name" not in rx.groupindex:
                    problems.append(f"operation {name!r}: parse needs a (?P<name>...) group")
            except re.error as e:
                problems.append(f"operation {name!r}: parse: {e}")
    if (ops.get("operations") or {}).get("pull") is None and ops.get("operations"):
        problems.append("no pull operation: nothing can come from the database")
    return problems


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


FILES_LIMIT = 20 * 1024 * 1024   # a lore's helper files, all together (a lore stays one file you can hand over)


def read_files(folder: Path) -> list[dict]:
    """The pack source's files/ folder: helpers that ship with the lore (a check harness, test fixtures, small
    scripts for its tools). Stored inside the lore, written into the skill folder on install."""
    if not folder.is_dir():
        return []
    out, total = [], 0
    for f in sorted(folder.rglob("*")):
        rel = f.relative_to(folder)
        if not f.is_file() or any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        data = f.read_bytes()
        total += len(data)
        if total > FILES_LIMIT:
            raise LoreError(f"files/ holds more than {FILES_LIMIT // 1024 // 1024} MB: keep a lore's files small")
        out.append({"path": rel.as_posix(), "data": data, "exec": bool(f.stat().st_mode & 0o111)})
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
    kept = lambda rows: [r for r in rows if (r.get("review") or {}).get("status") != "rejected"]  # noqa: E731
    p = {"pack": pack,       # what a reviewer rejected (hobkin-lore review) stays in the source, out of the lore
         "sections": kept(read_jsonl(src / "sections.jsonl")), "symbols": kept(read_jsonl(src / "symbols.jsonl")),
         "tools": kept(read_jsonl(src / "tools.jsonl")), "examples": kept(read_jsonl(src / "examples.jsonl")),
         "cheatsheet": (src / "cheatsheet.md").read_text(encoding="utf-8") if (src / "cheatsheet.md").exists() else "",
         "skill": (src / "skill.md").read_text(encoding="utf-8") if (src / "skill.md").exists() else "",
         "operations": json.loads((src / "operations.json").read_text(encoding="utf-8"))
         if (src / "operations.json").exists() else {},
         "files": read_files(src / "files")}
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
    errors += [f"operations.json: {e}" for e in check_operations(p["operations"])] if p["operations"] else []
    if errors:
        raise LoreError("the pack source has problems:\n  " + "\n  ".join(errors))
    p["warnings"] = warnings
    return p


# ---- compile: pack source -> .lore.db (running the examples through the pack's check command)

def run_check(check: dict, example: dict, timeout: float | None = None,
              pack_dir: Path | None = None) -> tuple[str, str]:
    """Run one example through the pack's check command: `{file}` in the command is a file holding the code,
    the example's input goes to stdin. Passes when the command succeeds and, if the example has `expect`, its
    output matches (check "compare": exact, words or json). `{pack}` is the pack source folder (for a harness
    in its files/), "timeout" in seconds. Exit 77 means the check can't run here: "unchecked", not "fail".
    Returns ("pass" | "fail" | "error" | "unchecked", the output)."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / ("example" + check.get("ext", ".txt"))
        f.write_text(example["code"], encoding="utf-8")
        cmd = check["command"].replace("{file}", shlex.quote(str(f)))
        if pack_dir is not None:
            cmd = cmd.replace("{pack}", shlex.quote(str(Path(pack_dir).resolve())))
        timeout = timeout or float(check.get("timeout", 20))
        try:
            r = subprocess.run(cmd, shell=True, input=example.get("input") or "", capture_output=True, text=True,
                               timeout=timeout, cwd=tmp)
        except subprocess.TimeoutExpired:
            return "error", f"timed out after {timeout:g}s"
        out = (r.stdout + (("\n" + r.stderr) if r.stderr.strip() else "")).strip()
        if r.returncode == 77:          # the harness can't run here (no browser, no service): not a failure
            return "unchecked", out
        if r.returncode != 0:
            return "fail", out or f"exit {r.returncode}"
        if example.get("expect") is not None and not same_output(r.stdout, str(example["expect"]),
                                                                 check.get("compare", "exact")):
            return "fail", f"expected:\n{str(example['expect']).strip()}\ngot:\n{r.stdout.strip()}"
        return "pass", out


def json_stream(text: str) -> list | None:
    """A stream of JSON values (as jq and many tools print them: one after another, maybe comma-separated),
    or None if it isn't one."""
    dec, i, vals = json.JSONDecoder(), 0, []
    while True:
        while i < len(text) and (text[i].isspace() or text[i] == ","):
            i += 1
        if i >= len(text):
            return vals
        try:
            v, i = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            return None
        vals.append(v)


def same_output(got: str, expect: str, compare: str) -> bool:
    """exact: the same text (ends trimmed); words: the same apart from spacing; json: the same JSON values."""
    if compare == "json":
        a, b = json_stream(got), json_stream(expect)
        if a is not None and b is not None:
            return a == b
    if compare in ("json", "words"):
        return got.split() == expect.split() or "".join(got.split()) == "".join(expect.split())
    return got.strip() == expect.strip()


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
            "operations": json.dumps(p["operations"]) if p["operations"] else "",
            "files": json.dumps([{"path": f["path"], "exec": f["exec"],
                                  "data": base64.b64encode(f["data"]).decode()} for f in p["files"]])
            if p["files"] else "",
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
    runner = shlex.split(chk["command"].replace("{pack}", str(src.resolve())))[0] if chk else ""
    can_check = bool(check and chk and shutil.which(runner))     # (a harness in files/ is found through {pack})
    if check and chk and not can_check:
        log(f"note: {runner} isn't installed here, so the examples aren't checked")
    results = {"pass": 0, "fail": 0, "error": 0, "unchecked": 0}
    for e in p["examples"]:
        state, output = run_check(chk, e, pack_dir=src) if can_check else ("unchecked", "")
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
             f"notes: {len(lore.notes.all())} (in {lore.notes.path.name})"
             + (f"; files: {len(lore_files(m))} (installed in the skill folder's files/)" if m.get("files") else ""),
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

def lore_files(meta: dict) -> list[dict]:
    return [{"path": f["path"], "exec": f.get("exec", False), "data": base64.b64decode(f["data"])}
            for f in json.loads(meta.get("files") or "[]")]


def file_note(f: dict) -> str:
    """What a helper file is: its first comment or docstring line, if it has one."""
    try:
        text = f["data"][:2000].decode("utf-8")
    except UnicodeDecodeError:
        return ""
    for line in text.splitlines():
        t = re.sub(r"^<!--|-->$", "", line.strip()).strip().strip('"').strip("'").lstrip("#/*").strip()
        if t and not t.startswith("!") and not t.lower().startswith(("<!doctype", "<html", "-*-")):
            return t[:140]
    return ""


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
    files = lore_files(meta)
    if files:
        lines += ["## Files that come with it", "",
                  "In this skill's folder, under `files/` (use them by their full path):", ""]
        lines += [f"- `files/{f['path']}`" + (f": {file_note(f)}" if file_note(f) else "") for f in files[:40]]
        lines += [""]
    ops = json.loads(meta.get("operations") or "{}")
    if ops.get("operations"):
        have = ", ".join(sorted(ops["operations"]))
        lines += ["## Working with the code in the database", "",
                  f"This lore can also run {meta['language']}'s tools for you (operations: {have}), on code that "
                  "lives in a database, from a git repo laid out as `src/TYPE/NAME`. In such a repo (it has a "
                  "`.lore-workspace/` folder):", "",
                  "| Command | Does |", "| --- | --- |",
                  "| `ws status [--remote]` | What changed here, and (with --remote) in the database since your pull |",
                  "| `ws list --type T` / `--project P` | Objects in the database |",
                  "| `ws pull TYPE NAME` / `--type T` / `--project P` / `--file F` | From the database into files "
                  "(never over your unsaved work) |",
                  "| `ws validate FILE` | Check a file with the tools, without changing the database |",
                  "| `ws deploy FILE... [--compile]` | Into the (dev) database, after checking nobody deployed over it "
                  "since your pull |",
                  "| `ws test` | Try the operations safely (list, pull, validate) |", "",
                  "Pull before you change something, validate before you deploy, and commit what you deploy. When "
                  "deploy reports a CONFLICT (someone else deployed the same object since your pull), don't force "
                  "it: tell the person you're working for, show them the three versions it saved, and merge with "
                  "them. Deploy only to the database the workspace is set up for, never anywhere else.", ""]
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
    shutil.rmtree(folder / "files", ignore_errors=True)       # the lore's files, as this version of it has them
    for f in lore_files(meta):
        target = (folder / "files" / f["path"]).resolve()
        if not str(target).startswith(str((folder / "files").resolve()) + os.sep):
            raise LoreError(f"refusing a file outside the skill folder: {f['path']!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f["data"])
        if f["exec"]:
            target.chmod(target.stat().st_mode | 0o111)
    (folder / "SKILL.md").write_text(skill_md(meta, has_tools, has_examples), encoding="utf-8")
    return folder


# ---- build: any documentation -> a pack source, with a model (gather, chunk, distill, verify, finish)

CHUNK_CHARS = 12000     # a chunk the model reads whole: big enough for a page of reference, small enough to read well
QUOTE_STATES = ("unreviewed", "accepted", "edited", "rejected")
KEEP_ON_REBUILD = ("accepted", "edited", "rejected")     # a reviewer's decision outlives a rebuild


class PageText(html.parser.HTMLParser):
    """HTML as plain text that keeps its structure: headings as #, code blocks fenced, list items as -."""
    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.pre = [], 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in ("h1", "h2", "h3", "h4"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "pre":
            self.pre += 1
            self.out.append("\n```\n")
        elif tag in ("p", "div", "section", "tr", "dt", "dd", "br", "table"):
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "code" and not self.pre:
            self.out.append("`")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n")
        elif tag in ("h1", "h2", "h3", "h4", "p", "dt", "dd"):
            self.out.append("\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data if self.pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        t = "".join(self.out)
        return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in t.splitlines())).strip() + "\n"


def html_text(raw: str) -> str:
    p = PageText()
    p.feed(raw)
    return p.text()


SCRIPT_EXTS = (".sh", ".bash", ".ksh", ".zsh", ".dms", ".dat", ".sql", ".ps1", ".bat", ".cmd", ".py", ".pl",
               ".pc", ".pcode", ".mak", ".cfg", ".ini", ".log", ".transcript")
HELP_FLAGS = ("--help", "-h", "-help", "-H", "-?", "help", "/?")   # tools of every era: GNU, Oracle, Windows
HELP_LIMIT = 60          # help pages read per tool, subcommands included
SUBCOMMANDS_SYSTEM = """You read a command-line tool's help text and list the commands that print the help of
its subcommands, modes or topics, so all of the tool's options can be documented. Only commands that print help
and do nothing else: never one that lists, exports, builds, connects or changes anything. Use the help flag this
tool uses. Reply with only a JSON object: {"invocations": [["subcommand", "--help"], ["help", "topic"], ...]}
(the arguments after the tool's name; an empty list when it has no subcommands)."""


def safe_help(args: list) -> bool:
    """Whether an invocation only asks for help: words for subcommands, then a help flag (or `help WORD...`).
    Plain code decides, not the model: a wrong suggestion can never run an operation."""
    if not args or not all(isinstance(a, str) for a in args) or len(args) > 4:
        return False
    word = re.compile(r"[A-Za-z][\w.-]{0,40}")
    if args[0] == "help":
        return all(word.fullmatch(a) for a in args[1:])
    return args[-1] in HELP_FLAGS and all(word.fullmatch(a) for a in args[:-1])


def run_help(argv: list[str]) -> str:
    """A help page: never with stdin (a tool waiting for a login just gets end-of-file), never for long."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return ""
    except OSError as e:
        raise LoreError(f"tool {argv[0]!r}: {e}") from None
    return plain(r.stdout + r.stderr)


def looks_like_help(text: str) -> bool:
    return bool(text.strip()) and (re.search(r"usage|options|synopsis|commands|flags|parameters", text, re.I)
                                   is not None or len(text.strip().splitlines()) >= 4)


def tool_help(cmd: str, model, log=print, limit: int = HELP_LIMIT) -> list[str]:
    """A tool's help: the first of the usual help flags that gives some, then (with a model) its subcommands'
    help, two levels down, at most HELP_LIMIT pages. Only help is ever run."""
    base = shlex.split(cmd)
    pages, flag = [], None
    for f in HELP_FLAGS:
        text = run_help(base + [f])
        if looks_like_help(text):
            pages.append((f, [f], text))
            flag = f
            break
    if not pages:
        log(f"  {cmd}: no help from any of {' '.join(HELP_FLAGS)}")
        return []
    if model is not None:
        seen, queue = {tuple(pages[0][1])}, [(pages[0][1], pages[0][2], 0)]
        while queue and len(pages) < limit:
            args, text, depth = queue.pop(0)
            if depth >= 2:
                continue
            ask = model.ask("subcommands", SUBCOMMANDS_SYSTEM,
                            f"Tool: {cmd}\nIts help flag: {flag}\nHelp printed by: {cmd} {' '.join(args)}\n\n"
                            f"{text[:CHUNK_CHARS]}")
            for inv in ask.get("invocations") or []:
                if not isinstance(inv, list):
                    continue
                inv = [str(a) for a in inv]
                if tuple(inv) in seen or len(pages) >= limit:
                    continue
                seen.add(tuple(inv))
                if not safe_help(inv):
                    log(f"  {cmd}: not running {' '.join(inv)!r} (not a help request)")
                    continue
                sub = run_help(base + inv)
                if looks_like_help(sub) and sub.strip() != text.strip():
                    pages.append((" ".join(inv), inv, sub))
                    queue.append((inv, sub, depth + 1))
        if len(pages) > 1:
            log(f"  {cmd}: {len(pages)} help pages (subcommands found)")
    return [f"# {cmd} {label}\n\n{text}" for label, _, text in pages]


def plain(text: str) -> str:
    """Terminal output as plain text: no colour or bold codes (man still emits them), no overstrike."""
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|.\x08", "", text)


def slug(text: str, n: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "x"


def gather(sources: list[str], tools: list[str], work: Path, log=print, model: Model | None = None,
           help_limit: int = HELP_LIMIT) -> list[dict]:
    """Every source as text in work/sources/, listed in work/sources.json: files (PDF, HTML, Markdown, text,
    scripts), folders of them, URLs, and for each tool its help (its subcommands' too, found with the model) and
    man page."""
    out_dir = work / "sources"
    out_dir.mkdir(parents=True, exist_ok=True)
    listed: list[dict] = []

    def keep(name: str, kind: str, text: str):
        if not text.strip():
            log(f"  {name}: no text, skipped")
            return
        sid = f"{len(listed) + 1:03d}-{slug(name, 40)}"
        (out_dir / f"{sid}.txt").write_text(text, encoding="utf-8")
        listed.append({"id": sid, "name": name, "kind": kind, "chars": len(text)})
        log(f"  {name}: {len(text):,} characters ({kind})")

    def one_file(path: Path):
        ext = path.suffix.lower()
        if ext == ".pdf":
            if not shutil.which("pdftotext"):
                raise LoreError(f"{path}: reading PDFs needs pdftotext (poppler-utils)")
            r = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True)
            keep(path.name, "pdf", r.stdout.replace("\f", "\n\n"))
        elif ext in (".html", ".htm", ".xhtml"):
            keep(path.name, "html", html_text(path.read_text(encoding="utf-8", errors="replace")))
        elif ext in (".md", ".txt", ".rst", ".adoc", "") or ext.startswith(".man"):
            keep(path.name, "text", path.read_text(encoding="utf-8", errors="replace"))
        elif ext in SCRIPT_EXTS:           # how the tools are really used: flags, sequences, idioms
            keep(path.name, "script", f"# Script {path.name}, as it is used\n\n```\n"
                 + path.read_text(encoding="utf-8", errors="replace") + "\n```\n")

    for src in sources:
        if re.match(r"https?://", src):
            req = urllib.request.Request(src, headers={"User-Agent": f"hobkin-lore/{__version__}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
            keep(src, "html" if "<html" in raw[:2000].lower() else "text",
                 html_text(raw) if "<html" in raw[:2000].lower() else raw)
            continue
        path = Path(src).expanduser()
        if path.is_dir():
            for f in sorted(p for p in path.rglob("*") if p.is_file()):
                one_file(f)
        elif path.is_file():
            one_file(path)
        else:
            raise LoreError(f"no such source: {src}")
    for cmd in tools:
        name = shlex.split(cmd)[0]
        parts = tool_help(cmd, model, log, help_limit)
        if shutil.which("man"):
            r = subprocess.run(["man", "-P", "cat", name], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL,
                               env={**os.environ, "MANWIDTH": "100", "MAN_KEEP_FORMATTING": "0",
                                    "GROFF_NO_SGR": "1"})
            if r.returncode == 0 and r.stdout.strip():
                parts.append(f"# man {name}\n\n" + plain(r.stdout))
        keep(f"tool: {cmd}", "tool", "\n\n".join(parts))
    (work / "sources.json").write_text(json.dumps(listed, indent=1), encoding="utf-8")
    return listed


def chunk(text: str, size: int = CHUNK_CHARS) -> list[tuple[int, int, str]]:
    """Pieces of about `size` characters, cut at headings (else blank lines): (first line, last line, text),
    lines counted from 1."""
    lines = text.splitlines()
    out, start, cur = [], 0, 0

    def is_head(i):
        return bool(re.match(r"#{1,4} \S", lines[i])) or (i + 1 < len(lines) and re.fullmatch(r"[=-]{3,}",
                                                                                                lines[i + 1]))
    for i in range(len(lines)):
        cur += len(lines[i]) + 1
        nxt = i + 1
        if nxt >= len(lines):
            break
        if cur >= size * 0.6 and is_head(nxt) or cur >= size and not lines[nxt].strip() or cur >= size * 1.5:
            out.append((start + 1, nxt, "\n".join(lines[start:nxt])))
            start, cur = nxt, 0
    if start < len(lines):
        out.append((start + 1, len(lines), "\n".join(lines[start:])))
    return [c for c in out if c[2].strip()]


class Model:
    """An OpenAI-format chat endpoint (OpenRouter, a gateway, Ollama, vLLM...), over urllib. Every answer is
    recorded in `answers` keyed by (task, prompt, model): a rebuild asks again only what changed."""

    def __init__(self, base_url: str, model: str, key: str | None, answers: Path, timeout: float = 300):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model, self.key, self.answers, self.timeout = model, key, answers, timeout
        self.answers.mkdir(parents=True, exist_ok=True)
        self.calls = 0
        self.json_mode = True

    def _post(self, messages: list[dict]) -> str:
        body = {"model": self.model, "messages": messages}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.key}"} if self.key else {})})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
                self.calls += 1
                return data["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code == 400 and self.json_mode and "response_format" in detail:
                    self.json_mode = False          # this endpoint has no JSON mode: the prompt asks for JSON
                    return self._post(messages)
                if e.code in (408, 429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(min(60, 2 ** attempt * 3))
                    continue
                raise LoreError(f"model call failed: HTTP {e.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < 4:
                    time.sleep(min(60, 2 ** attempt * 3))
                    continue
                raise LoreError(f"model call failed: {e}") from None
        raise LoreError("model call failed")

    def ask(self, task: str, system: str, user: str) -> dict:
        key = hashlib.sha256(json.dumps([task, system, user, self.model]).encode()).hexdigest()[:24]
        f = self.answers / f"{task}-{key}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))["output"]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(2):
            raw = self._post(messages)
            m = re.search(r"\{.*\}", raw, re.S)
            try:
                out = json.loads(m.group(0) if m else raw)
                problem = None if isinstance(out, dict) else "not an object"
            except json.JSONDecodeError as e:
                problem = str(e)
            if not problem:
                break
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"That wasn't one JSON object ({problem}). "
                                                     "Reply again with only the JSON object."}]
        else:
            raise LoreError(f"{task}: the model's answer wasn't JSON")
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"task": task, "model": self.model, "key": key, "at": time.strftime(
            "%Y-%m-%d %H:%M:%S"), "output": out}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, f)
        return out


DISTILL_SYSTEM = """You turn one passage of documentation into reference entries for a programming language
and its tools, for an AI that will look them up while writing code. Write entries in your own words, short
and exact: every fact a programmer needs (names, arguments and their order, return values, defaults, errors,
edge cases), none of the prose. Include only what this passage documents; leave lists empty otherwise.

An example's "code" is only the program text in the language, exactly as it would be saved in a file: never
the shell command that runs it (no `tool '...'` wrapper, no prompt); put such a command in "notes".

A passage may be a tool's help text or a script that uses the tools: from help, document each tool and every
flag; from scripts, take real usage as examples (with "notes" saying what it does) and as evidence for flags.

Every entry needs "quote": a short passage (5 to 30 words) copied exactly, character for character, from the
passage, that supports the entry; one stretch of text, without "..." in it. Entries whose quote isn't in the
passage are flagged for a person to check.

Reply with only a JSON object:
{"sections": [{"title": "...", "parent": "title of the section it belongs under, or null",
               "body": "the section's content as Markdown", "quote": "..."}],
 "symbols": [{"name": "...", "kind": "KIND", "signature": "...", "summary": "one line",
              "body": "details: arguments, returns, errors, edge cases", "see": ["related names"], "quote": "..."}],
 "examples": [{"title": "...", "code": "...", "input": "input it reads, if the documentation shows one",
               "expect": "its output, only if the documentation shows it exactly", "symbols": ["names it uses"],
               "notes": "the command that runs it, if the documentation shows one",
               "quote": "..."}],
 "tools": [{"name": "command name", "purpose": "one line", "usage": "synopsis", "body": "how to use it",
            "flags": [{"flag": "-x, --long ARG", "meaning": "..."}], "quote": "..."}]}

KIND is one of: """ + ", ".join(SYMBOL_KINDS) + "."

FINISH_SYSTEM = """You write the two short texts an AI reads before using a programming language's reference.
Reply with only a JSON object:
{"description": "two sentences: what the language is and what this reference covers",
 "cheatsheet": "the essentials as a Markdown list, at most 25 lines: syntax rules, the mistakes people make
most, how to run and check code with its tools",
 "skill": "how to work in this language with its tools, a short Markdown paragraph or list: how to run, test
or check what you write, and how to read the errors"}"""


CALLABLE = {"function", "builtin", "method", "other"}   # one name a model may call any of these


def uses(code: str, name: str) -> bool:
    """Whether code mentions a name as a whole word (names like @csv or .[] by their text)."""
    if re.fullmatch(r"\w+", name):
        return re.search(rf"(?<![\w$]){re.escape(name)}(?!\w)", code) is not None
    return name in code


def norm(s: str) -> str:
    """For finding a quote: case, spacing and Markdown's backticks and stars don't matter."""
    return re.sub(r"\s+", " ", re.sub(r"[`*]", "", s)).strip().lower()


def locate(quote: str, chunk_text: str, first_line: int) -> tuple[int, int] | None:
    """The lines (in the source) a quote spans, or None if it isn't in the chunk (whitespace and case aside)."""
    pieces = [norm(p) for p in re.split(r"\.\.\.|…", quote) if norm(p)]
    if not pieces or sum(len(p) for p in pieces) < 8:
        return None
    lines = chunk_text.splitlines()
    flat, starts = "", []
    for i, line in enumerate(lines):
        starts.append(len(flat))
        n = norm(line)
        if n:                          # a blank line adds no space: a quote may run across a paragraph break
            flat += n + " "
    at = flat.find(pieces[0])          # a shortened quote ("a ... b"): each piece verbatim, in order, close by
    if at < 0:
        return None
    end = at + len(pieces[0])
    for p in pieces[1:]:
        nxt = flat.find(p, end)
        if nxt < 0 or nxt - end > 600:
            return None
        end = nxt + len(p)
    a = max(i for i, st in enumerate(starts) if st <= at)
    b = max(i for i, st in enumerate(starts) if st <= end - 1)
    return first_line + a, first_line + b


def distill_all(model: Model, srcs: list[dict], work: Path, language: str, jobs: int, limit: int | None,
                log=print) -> list[dict]:
    """[chunk] and [distill]: every chunk of every source through the model (in parallel). Returns the raw entries,
    each with its chunk."""
    chunks = []
    for s in srcs:
        text = (work / "sources" / f"{s['id']}.txt").read_text(encoding="utf-8")
        for a, b, t in chunk(text):
            chunks.append({"id": f"{s['id']}:{a}-{b}", "source": s, "first": a, "last": b, "text": t})
    if limit:
        chunks = chunks[:limit]
    (work / "chunks.json").write_text(json.dumps([{k: c[k] for k in ("id", "first", "last")} | {
        "source": c["source"]["id"], "name": c["source"]["name"]} for c in chunks], indent=1), encoding="utf-8")
    log(f"distilling {len(chunks)} chunks ({jobs} at a time)...")
    done = [0]

    def one(c):
        user = (f"Language: {language}\nSource: {c['source']['name']}, lines {c['first']}-{c['last']}\n\n"
                f"Passage:\n{c['text']}")
        out = model.ask("distill", DISTILL_SYSTEM, user)
        done[0] += 1
        if done[0] % 10 == 0 or done[0] == len(chunks):
            log(f"  {done[0]}/{len(chunks)}")
        return c, out
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max(1, jobs)) as ex:
        results = list(ex.map(one, chunks))
    entries = []
    for c, out in results:
        for kind in ("sections", "symbols", "examples", "tools"):
            for e in out.get(kind) or []:
                if isinstance(e, dict):
                    entries.append({"kind": kind, "entry": e, "chunk": c})
    return entries


def verify_and_merge(entries: list[dict]) -> dict[str, list[dict]]:
    """[verify] and merge: each entry's quote looked up in its chunk (a line-range citation if found, a flag if
    not), duplicates merged, ids given, sections nested, symbols tied to their chunk's section."""
    packs: dict[str, list[dict]] = {"sections": [], "symbols": [], "examples": [], "tools": []}
    top: dict[str, dict] = {}
    sec_by_title: dict[tuple, str] = {}
    sec_of_chunk: dict[str, str] = {}
    ids: set = set()

    def unique(base):
        i, n = base, 2
        while i in ids:
            i, n = f"{base}-{n}", n + 1
        ids.add(i)
        return i
    for item in entries:
        e, c, kind = item["entry"], item["chunk"], item["kind"]
        src = c["source"]
        span = locate(str(e.get("quote") or ""), c["text"], c["first"])
        flags = [] if span else ["quote not found in the source"]
        cite = f"{src['name']}, lines {span[0]}-{span[1]}" if span else f"{src['name']}, lines {c['first']}-{c['last']}"
        review = {"status": "unreviewed", "flags": flags, "quote": e.get("quote"), "chunk": c["id"]}
        if kind == "sections":
            if not e.get("title") or not e.get("body"):
                continue
            if src["id"] not in top:
                tid = unique(slug(src["name"]))
                top[src["id"]] = {"id": tid, "parent": None, "title": src["name"], "body": f"From {src['name']}.",
                                  "source": src["name"], "review": {"status": "unreviewed", "flags": []}}
                packs["sections"].append(top[src["id"]])
            parent = sec_by_title.get((src["id"], norm(e.get("parent") or ""))) or top[src["id"]]["id"]
            sid = unique(f"{top[src['id']]['id']}/{slug(e['title'])}")
            sec_by_title[(src["id"], norm(e["title"]))] = sid
            sec_of_chunk.setdefault(c["id"], sid)
            packs["sections"].append({"id": sid, "parent": parent, "title": e["title"], "body": e["body"],
                                      "source": cite, "review": review})
        elif kind == "symbols":
            name, sk = str(e.get("name") or "").strip(), e.get("kind")
            if not name:
                continue
            if sk not in SYMBOL_KINDS:
                review["flags"].append(f"kind {sk!r} wasn't one of the known kinds")
                sk = "other"
            same = next((y for y in packs["symbols"] if y["name"] == name and (
                y["kind"] == sk or {y["kind"], sk} <= CALLABLE)), None)   # a model's "builtin" vs "function"
            if same:              # documented in two places: keep the fuller, and both citations
                if len(e.get("body") or "") > len(same.get("body") or ""):
                    same.update({k: e.get(k) or same.get(k) for k in ("signature", "summary", "body")})
                same["see"] = sorted(set(same["see"]) | set(e.get("see") or []))
                same["source"] += f"; {cite}"
                if not flags:
                    same["review"]["flags"] = [f for f in same["review"]["flags"] if "quote" not in f]
                continue
            packs["symbols"].append({"name": name, "kind": sk, "signature": e.get("signature"),
                                     "summary": e.get("summary"), "body": e.get("body"),
                                     "section": c["id"], "source": cite,
                                     "see": [s for s in (e.get("see") or []) if isinstance(s, str)],
                                     "review": review})
        elif kind == "examples":
            if not e.get("code"):
                continue
            if any(x["code"].strip() == str(e["code"]).strip() and (x["input"] or "").strip() == str(
                    e.get("input") or "").strip() for x in packs["examples"]):
                continue                  # the same example in two places (a manual and its man page)
            packs["examples"].append({"id": unique(f"ex-{slug(e.get('title') or e['code'][:30], 40)}"),
                                      "title": e.get("title"), "code": e["code"], "input": e.get("input") or None,
                                      "expect": e.get("expect") or None, "notes": e.get("notes") or None,
                                      "symbols": [s for s in (e.get("symbols") or []) if isinstance(s, str)],
                                      "source": cite, "review": review})
        elif kind == "tools":
            if not e.get("name"):
                continue
            same = next((t for t in packs["tools"] if t["name"] == e["name"]), None)
            if same:
                have = {f.get("flag") for f in same["flags"]}
                same["flags"] += [f for f in e.get("flags") or [] if isinstance(f, dict) and f.get("flag") not in have]
                continue
            packs["tools"].append({"name": e["name"], "purpose": e.get("purpose"), "usage": e.get("usage"),
                                   "body": e.get("body"), "flags": [f for f in e.get("flags") or []
                                                                   if isinstance(f, dict)],
                                   "runs": [], "source": cite, "review": review})
    names = {y["name"] for y in packs["symbols"]}
    for y in packs["symbols"]:             # tie each symbol to its chunk's first section; drop dangling see-alsos
        y["section"] = sec_of_chunk.get(y["section"]) or None
        y["see"] = [s for s in y["see"] if s in names and s != y["name"]]
    for e in packs["examples"]:            # only symbols the code really uses: a model's lists are loose
        e["symbols"] = [s for s in e["symbols"] if s in names and uses(e["code"], s)]
    return packs


def entry_key(kind: str, e: dict) -> str:
    return f"{e['name']}\t{e.get('kind', '')}" if kind in ("symbols", "tools") else e.get("id", "")


def by_hand(r: dict) -> bool:
    """An entry a person wrote (no review record: build never makes one without) or reviewed (accepted, edited,
    rejected): a rebuild keeps it as it is."""
    return "review" not in r or (r.get("review") or {}).get("status") in KEEP_ON_REBUILD


def write_pack(out: Path, pack: dict, packs: dict[str, list[dict]], cheatsheet: str, skill: str, log=print,
               ops: dict | None = None) -> None:
    """The pack source, keeping what people did: entries written by hand, and entries accepted, edited or rejected
    in review, stay as they are; everything else is the new build. So a build can extend a hand-written pack."""
    out.mkdir(parents=True, exist_ok=True)
    kept = 0
    for kind, rows in packs.items():
        old = {entry_key(kind, r): r for r in read_jsonl(out / f"{kind}.jsonl")}
        merged, seen = [], set()
        for r in rows:
            k = entry_key(kind, r)
            prev = old.get(k)
            if prev and by_hand(prev):
                r, kept = prev, kept + 1
            merged.append(r)
            seen.add(k)
        merged += [r for k, r in old.items() if k not in seen and by_hand(r)]   # not in the new build: kept
        tmp = out / f"{kind}.jsonl.tmp"
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in merged), encoding="utf-8")
        os.replace(tmp, out / f"{kind}.jsonl")
    (out / "pack.json").write_text(json.dumps(pack, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    work = out / ".build"
    work.mkdir(parents=True, exist_ok=True)
    texts = [("cheatsheet.md", cheatsheet), ("skill.md", skill)]
    if ops:
        texts.append(("operations.json", json.dumps(ops, indent=2, ensure_ascii=False)))
    for fname, text in texts:
        # replaced only if nobody changed it since the last build wrote it
        last, cur = work / f"generated-{fname}", out / fname
        if not cur.exists() or (last.exists() and cur.read_text(encoding="utf-8") == last.read_text(encoding="utf-8")):
            cur.write_text(text.strip() + "\n", encoding="utf-8")
        else:
            log(f"kept {fname}: it was edited by hand")
        last.write_text(text.strip() + "\n", encoding="utf-8")
    if kept:
        log(f"kept {kept} entries written or reviewed by hand")


OPERATIONS_SYSTEM = """You set up how a team works on code that lives in a database, through its command-line
tools. From the tools' documentation and the scripts that use them, write the command template for each operation
the tools can do. Reply with only a JSON object:
{"variables": {"name": "what it is (e.g. the database name, the operator id)"},
 "secrets": {"name": "what it is (e.g. the database password)"},
 "layout": {"ext": {"OBJECTTYPE": ".ext", "*": ".txt"}},
 "operations": {"OPERATION": {"command": "tool --flag {placeholder} ...", "output": "file" or "stdout",
                              "parse": "a regex for one line of list output, with (?P<name>...) and maybe (?P<type>...)",
                              "notes": "anything a person setting it up should check"}}}

Operations (leave out any the tools can't do):
""" + "\n".join(f"- {k}: {v}" for k, v in OPERATIONS.items()) + """

A command is one tool invocation, words separated by spaces, no shell (no pipes, no redirection, no &&). Use
{placeholders} for the object ({type}, {name}, {file}, {dir}, {project}) and for every site-specific value
(database, server, user) as a variable, and every password as a secret: never a real value. Use only flags the
documentation shows."""


def draft_operations(model: Model, language: str, packs: dict, log=print) -> dict:
    """[operations] The model drafts operations.json from the tool entries and script examples; plain code
    checks it. A draft that doesn't hold together is kept with its problems noted, for review."""
    tools = "\n\n".join(f"## {t['name']}\n{t.get('purpose') or ''}\nusage: {t.get('usage') or ''}\n"
                         f"{t.get('body') or ''}\n" + "\n".join(f"  {f.get('flag')}  {f.get('meaning')}"
                                                               for f in t.get("flags") or [])
                         for t in packs["tools"])
    used = "\n".join(f"- {e['code'][:300]}  ({e.get('notes') or ''})" for e in packs["examples"]
                     if any(t["name"] in e["code"] for t in packs["tools"]))[:20000]
    ops = model.ask("operations", OPERATIONS_SYSTEM, f"Language: {language}\n\nTools:\n{tools[:40000]}\n\n"
                                                     f"How the scripts use them:\n{used or '(none)'}")
    ops = {k: v for k, v in ops.items() if k in ("variables", "secrets", "layout", "operations")}
    problems = check_operations(ops)
    if problems:
        ops["problems"] = problems
        log("operations drafted, with problems to fix in review: " + "; ".join(problems))
    else:
        log(f"operations drafted: {', '.join(sorted(ops.get('operations') or {}))} (review them before use)")
    return ops


def build(name: str, sources: list[str], tools: list[str], out: Path, model: Model, title: str | None = None,
          language: str | None = None, version: str | None = None, check: dict | None = None, jobs: int = 4,
          limit: int | None = None, log=print, help_limit: int = HELP_LIMIT) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name):
        raise LoreError("the name: lowercase letters, digits and hyphens (it names the skill)")
    work = out / ".build"
    title, language = title or name, language or title or name
    log("gathering sources...")
    srcs = gather(sources, tools, work, log, model, help_limit)
    if not srcs:
        raise LoreError("no text in any of the sources")
    entries = distill_all(model, srcs, work, language, jobs, limit, log)
    packs = verify_and_merge(entries)
    names = "\n".join(f"- {y['name']} ({y['kind']}): {y.get('summary') or ''}" for y in packs["symbols"][:400])
    tool_lines = "\n".join(f"- {t['name']}: {t.get('purpose') or ''}" for t in packs["tools"])
    heads = "\n".join(f"- {s['title']}" for s in packs["sections"][:200])
    fin = model.ask("finish", FINISH_SYSTEM, f"Language: {language}\n\nSections:\n{heads}\n\nSymbols:\n{names}"
                                             f"\n\nTools:\n{tool_lines or '(none documented)'}")
    ops = {}
    if packs["tools"]:
        ops = draft_operations(model, language, packs, log)
    pack = {"name": name, "title": title, "language": language, "version": version or "",
            "description": fin.get("description") or f"The {language} reference.",
            "sources": [s["name"] for s in srcs]}
    if check:
        pack["check"] = check
    write_pack(out, pack, packs, str(fin.get("cheatsheet") or ""), str(fin.get("skill") or ""), log, ops)
    flagged = sum(1 for rows in packs.values() for r in rows if (r.get("review") or {}).get("flags"))
    return {"out": str(out), "sources": len(srcs), "chunks": len(json.loads((work / "chunks.json").read_text())),
            **{k: len(v) for k, v in packs.items()}, "flagged": flagged, "model_calls": model.calls}


# ---- review: a local web page to review a built pack source, entry by entry, beside the source it cites

REVIEW_FIELDS = {
    "sections": ["id", "parent", "title", "body"],
    "symbols": ["name", "kind", "signature", "summary", "body", "see"],
    "examples": ["id", "title", "code", "input", "expect", "notes", "symbols"],
    "tools": ["name", "purpose", "usage", "body", "flags"],
}


class Review:
    """The pack source on disk is the state: every read comes from the files, every change is written back."""

    def __init__(self, src: Path):
        if not (src / "pack.json").exists():
            raise LoreError(f"{src} isn't a pack source (no pack.json)")
        self.src = src
        self.lock = threading.Lock()

    def rows(self, kind: str) -> list[dict]:
        return read_jsonl(self.src / f"{kind}.jsonl")

    def save(self, kind: str, rows: list[dict]) -> None:
        tmp = self.src / f"{kind}.jsonl.tmp"
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        os.replace(tmp, self.src / f"{kind}.jsonl")

    def summary(self) -> dict:
        out = {"pack": json.loads((self.src / "pack.json").read_text(encoding="utf-8")), "kinds": {}}
        for kind in REVIEW_FIELDS:
            rows = self.rows(kind)
            st = lambda r: (r.get("review") or {}).get("status", "unreviewed")  # noqa: E731
            out["kinds"][kind] = {"total": len(rows), "flagged": sum(1 for r in rows if (r.get("review") or {})
                                                                     .get("flags") and st(r) == "unreviewed"),
                                  **{s: sum(1 for r in rows if st(r) == s) for s in QUOTE_STATES}}
        return out

    def listing(self, kind: str, status: str = "", q: str = "") -> list[dict]:
        out = []
        for r in self.rows(kind):
            rv = r.get("review") or {}
            if status == "flagged":
                if not rv.get("flags") or rv.get("status", "unreviewed") != "unreviewed":
                    continue
            elif status and rv.get("status", "unreviewed") != status:
                continue
            title = r.get("title") or r.get("name") or r.get("id")
            if q and q.lower() not in json.dumps(r, ensure_ascii=False).lower():
                continue
            out.append({"key": entry_key(kind, r), "title": title, "status": rv.get("status", "unreviewed"),
                        "flags": rv.get("flags") or [], "line": r.get("summary") or r.get("purpose") or
                        (r.get("body") or r.get("code") or "")[:120]})
        out.sort(key=lambda e: (e["status"] != "unreviewed", not e["flags"], str(e["title"]).lower()))
        return out

    def entry(self, kind: str, key: str) -> dict:
        r = next((r for r in self.rows(kind) if entry_key(kind, r) == key), None)
        if r is None:
            raise LoreError(f"no {kind} entry {key!r}")
        return {"entry": r, "fields": REVIEW_FIELDS[kind], "context": self.context(r)}

    def context(self, r: dict) -> dict | None:
        """The cited source lines (a few around them), and which hold the quote."""
        rv = r.get("review") or {}
        m = re.match(r"(.+):(\d+)-(\d+)$", rv.get("chunk") or "")
        f = self.src / ".build" / "sources" / f"{m.group(1)}.txt" if m else None
        if not m or not f.exists():
            return None
        a, b = int(m.group(2)), int(m.group(3))
        lines = f.read_text(encoding="utf-8").splitlines()
        span = locate(str(rv.get("quote") or ""), "\n".join(lines[a - 1:b]), a)
        lo, hi = (span[0] - 8, span[1] + 8) if span else (a, min(b, a + 60))
        lo, hi = max(1, lo), min(len(lines), hi)
        return {"first": lo, "lines": lines[lo - 1:hi], "quote": list(span) if span else None,
                "chunk": [a, b], "source": next((s["name"] for s in json.loads(
                    (self.src / ".build" / "sources.json").read_text()) if s["id"] == m.group(1)), m.group(1))}

    def update(self, kind: str, key: str, fields: dict | None, status: str | None) -> dict:
        with self.lock:
            rows = self.rows(kind)
            r = next((r for r in rows if entry_key(kind, r) == key), None)
            if r is None:
                raise LoreError(f"no {kind} entry {key!r}")
            rv = r.setdefault("review", {})
            changed = False
            for k, v in (fields or {}).items():
                if k in REVIEW_FIELDS[kind] and r.get(k) != v:
                    r[k], changed = v, True
            if status:
                if status not in QUOTE_STATES:
                    raise LoreError(f"status: one of {', '.join(QUOTE_STATES)}")
                rv["status"] = status
            elif changed:
                rv["status"] = "edited"
            rv["at"] = time.strftime("%Y-%m-%d %H:%M")
            self.save(kind, rows)
            return r

    def check_example(self, key: str) -> dict:
        pack = json.loads((self.src / "pack.json").read_text(encoding="utf-8"))
        if not pack.get("check"):
            return {"state": "unchecked", "output": "this pack has no check command (pack.json \"check\")"}
        if not shutil.which(pack["check"]["command"].split()[0]):
            return {"state": "unchecked", "output": f"{pack['check']['command'].split()[0]} isn't installed here"}
        r = next((r for r in self.rows("examples") if entry_key("examples", r) == key), None)
        if r is None:
            raise LoreError(f"no example {key!r}")
        state, output = run_check(pack["check"], r, pack_dir=self.src)
        return {"state": state, "output": output}

    def text(self, name: str, value: str | None = None) -> str:
        if name not in ("cheatsheet.md", "skill.md", "operations.json"):
            raise LoreError("only cheatsheet.md, skill.md and operations.json")
        f = self.src / name
        if value is not None and name == "operations.json":
            try:
                problems = check_operations(json.loads(value))
            except json.JSONDecodeError as e:
                raise LoreError(f"not JSON: {e}") from None
            if problems:
                raise LoreError("; ".join(problems))
        if value is not None:
            f.write_text(value, encoding="utf-8")
        return f.read_text(encoding="utf-8") if f.exists() else ""


REVIEW_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>lore review</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1b1f24;--muted:#5d6672;--line:#d5dae1;--accent:#2f5d8f;--ok:#2c7a46;--bad:#a8322d;--warn:#94660b;--mark:#fff1b8}
@media (prefers-color-scheme:dark){:root{--bg:#15181c;--panel:#1d2126;--ink:#e6e9ed;--muted:#9aa4b0;--line:#343a42;--accent:#7fb0e8;--ok:#7ccf98;--bad:#f0928b;--warn:#e7c06b;--mark:#5a4a12}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header b{font-size:15px}.muted{color:var(--muted)}main{display:grid;grid-template-columns:minmax(260px,360px) 1fr;height:calc(100vh - 52px)}
@media (max-width:800px){main{grid-template-columns:1fr;height:auto}}
#side{border-right:1px solid var(--line);overflow:auto;padding:10px}#detail{overflow:auto;padding:14px 18px}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}.tabs button,.filters select,.filters input{font:inherit}
button{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;padding:4px 10px;cursor:pointer}
button.on{border-color:var(--accent);color:var(--accent);font-weight:600}button.ok{border-color:var(--ok);color:var(--ok)}button.bad{border-color:var(--bad);color:var(--bad)}
.filters{display:flex;gap:6px;margin-bottom:8px}.filters input{flex:1;min-width:0;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
.item{padding:6px 8px;border-radius:6px;cursor:pointer;border:1px solid transparent}.item:hover{background:var(--panel)}.item.sel{border-color:var(--accent);background:var(--panel)}
.item .t{font-weight:600;overflow-wrap:anywhere}.item .l{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip{font-size:11px;border-radius:999px;padding:0 6px;border:1px solid var(--line);margin-left:4px}.chip.accepted{color:var(--ok);border-color:var(--ok)}.chip.rejected{color:var(--bad);border-color:var(--bad)}.chip.edited{color:var(--accent);border-color:var(--accent)}.chip.flag{color:var(--warn);border-color:var(--warn)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media (max-width:1100px){.grid{grid-template-columns:1fr}}
label{display:block;font-size:12px;color:var(--muted);margin:8px 0 2px}textarea,input.f{width:100%;font:13px/1.4 ui-monospace,monospace;padding:6px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
pre.src{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere;margin:0}
pre.src span{display:block}pre.src .hit{background:var(--mark)}pre.src .n{color:var(--muted);user-select:none;display:inline-block;width:4em}
.acts{display:flex;gap:8px;margin:12px 0;flex-wrap:wrap}.flags{color:var(--warn)}#msg{margin-left:auto}
</style></head><body>
<header><b id="title">lore review</b><span id="counts" class="muted"></span><span id="msg" class="muted"></span></header>
<main><div id="side"><div class="tabs" id="tabs"></div>
<div class="filters"><select id="status"><option value="">all</option><option value="flagged">flagged</option><option>unreviewed</option><option>accepted</option><option>edited</option><option>rejected</option></select><input id="q" placeholder="filter"></div>
<div id="list"></div></div><div id="detail"><p class="muted">Pick an entry. Flagged ones (their quote isn't in the source) come first.</p></div></main>
<script>
const TOKEN = "__TOKEN__";
const KINDS = ["symbols","sections","examples","tools","texts"];
let kind = "symbols", current = null;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"})[c]);
async function api(path, body) {
  const r = await fetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json","X-Lore-Token":TOKEN}, body:JSON.stringify(body)} : {});
  const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status); return j;
}
function say(t) { $("msg").textContent = t; setTimeout(() => { if ($("msg").textContent === t) $("msg").textContent = ""; }, 4000); }
async function loadSummary() {
  const s = await api("/api/summary");
  $("title").textContent = `${s.pack.title} lore: review`;
  $("tabs").innerHTML = KINDS.map((k) => { const c = s.kinds[k];
    return `<button data-k="${k}" class="${k===kind?"on":""}">${k}${c ? ` <span class="muted">${c.total - c.unreviewed}/${c.total}</span>` : ""}</button>`; }).join("");
  $("tabs").querySelectorAll("button").forEach((b) => b.onclick = () => { kind = b.dataset.k; current = null; loadSummary(); loadList(); });
  const f = Object.values(s.kinds).reduce((a, c) => a + c.flagged, 0), u = Object.values(s.kinds).reduce((a, c) => a + c.unreviewed, 0);
  $("counts").textContent = `${u} unreviewed, ${f} flagged`;
}
async function loadList() {
  if (kind === "texts") { $("list").innerHTML = ["cheatsheet.md","skill.md","operations.json"].map((n) => `<div class="item" data-n="${n}"><div class="t">${n}</div></div>`).join("");
    $("list").querySelectorAll(".item").forEach((d) => d.onclick = () => openText(d.dataset.n)); return; }
  const rows = await api(`/api/list?kind=${kind}&status=${encodeURIComponent($("status").value)}&q=${encodeURIComponent($("q").value)}`);
  $("list").innerHTML = rows.map((r) => `<div class="item${current===r.key?" sel":""}" data-k="${esc(r.key)}"><div class="t">${esc(r.title)}<span class="chip ${r.status}">${r.status}</span>${r.flags.length?'<span class="chip flag">flag</span>':""}</div><div class="l">${esc(r.line)}</div></div>`).join("") || '<p class="muted">nothing here</p>';
  $("list").querySelectorAll(".item").forEach((d) => d.onclick = () => openEntry(d.dataset.k));
}
function field(name, v) {
  const val = typeof v === "string" || v == null ? (v ?? "") : JSON.stringify(v, null, 1);
  const big = ["body","code","input","expect","notes","flags"].includes(name) || val.length > 80;
  return `<label>${name}</label>` + (big ? `<textarea data-f="${name}" rows="${Math.min(18, Math.max(3, val.split("\n").length + 1))}">${esc(val)}</textarea>` : `<input class="f" data-f="${name}" value="${esc(val)}">`);
}
async function openEntry(key) {
  current = key; loadList();
  const d = await api(`/api/entry?kind=${kind}&key=${encodeURIComponent(key)}`), e = d.entry, rv = e.review || {}, c = d.context;
  const src = c ? `<label>${esc(c.source)}, lines ${c.first}-${c.first + c.lines.length - 1}${c.quote ? "" : " (the quote isn't here: this is the chunk it came from)"}</label><pre class="src">${c.lines.map((l, i) => { const n = c.first + i, hit = c.quote && n >= c.quote[0] && n <= c.quote[1];
      return `<span class="${hit?"hit":""}"><span class="n">${n}</span>${esc(l)}</span>`; }).join("")}</pre>` : '<p class="muted">no source context (hand-written, or the build folder is gone)</p>';
  $("detail").innerHTML = `<h2 style="margin:0 0 4px">${esc(e.title || e.name || e.id)} <span class="chip ${rv.status||"unreviewed"}">${rv.status||"unreviewed"}</span></h2>
    ${rv.flags && rv.flags.length ? `<p class="flags">${rv.flags.map(esc).join("; ")}</p>` : ""}
    <p class="muted">quote: “${esc(rv.quote || "")}” · cited as: ${esc(e.source || "")}</p>
    <div class="acts"><button class="ok" id="acc">Accept</button><button id="sav">Save edits</button><button class="bad" id="rej">Reject</button>
      ${kind==="examples" ? '<button id="chk">Run the check</button>' : ""}<span id="chkout" class="muted"></span></div>
    <div class="grid"><div>${d.fields.map((f) => field(f, e[f])).join("")}</div><div>${src}</div></div>`;
  const fields = () => { const o = {}; $("detail").querySelectorAll("[data-f]").forEach((el) => { let v = el.value;
    if (["see","symbols","flags"].includes(el.dataset.f)) { try { v = JSON.parse(v || "[]"); } catch { v = v.split(",").map((s) => s.trim()).filter(Boolean); } }
    else if (v === "" && ["parent","signature","input","expect","notes"].includes(el.dataset.f)) v = null;
    o[el.dataset.f] = v; }); return o; };
  const act = async (status, withFields) => { try { await api("/api/update", {kind, key, status, fields: withFields ? fields() : null});
    say(status ? status : "saved"); await loadSummary(); await openEntry(key); } catch (err) { say("error: " + err.message); } };
  $("acc").onclick = () => act("accepted", true); $("rej").onclick = () => act("rejected", false); $("sav").onclick = () => act(null, true);
  if ($("chk")) $("chk").onclick = async () => { $("chkout").textContent = "running..."; const r = await api("/api/check", {key});
    $("chkout").textContent = `${r.state}${r.output ? ": " + r.output.slice(0, 300) : ""}`; };
}
async function openText(name) {
  const t = await api(`/api/text?name=${name}`);
  $("detail").innerHTML = `<h2>${name}</h2><textarea id="tx" rows="24">${esc(t.text)}</textarea><div class="acts"><button id="sv">Save</button></div>`;
  $("sv").onclick = async () => { await api("/api/text", {name, text: $("tx").value}); say("saved"); };
}
$("status").onchange = loadList; let tq; $("q").oninput = () => { clearTimeout(tq); tq = setTimeout(loadList, 250); };
loadSummary().then(loadList);
</script></body></html>"""


def review_server(src: Path, host: str = "127.0.0.1", port: int = 8765):
    """The review page and its little API. Changes need the token embedded in the page, so another site open
    in the same browser can't post to it."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit
    rv, token = Review(src), secrets.token_urlsafe(16)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, obj, ctype="application/json; charset=utf-8"):
            raw = obj.encode() if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    return self.send(200, REVIEW_PAGE.replace("__TOKEN__", token), "text/html; charset=utf-8")
                if u.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return None
                if u.path == "/api/summary":
                    return self.send(200, rv.summary())
                if u.path == "/api/list":
                    return self.send(200, rv.listing(q.get("kind", "symbols"), q.get("status", ""), q.get("q", "")))
                if u.path == "/api/entry":
                    return self.send(200, rv.entry(q.get("kind", ""), q.get("key", "")))
                if u.path == "/api/text":
                    return self.send(200, {"text": rv.text(q.get("name", ""))})
                self.send(404, {"error": "not found"})
            except (LoreError, KeyError) as e:
                self.send(400, {"error": str(e)})

        def do_POST(self):
            if self.headers.get("X-Lore-Token") != token:
                return self.send(403, {"error": "no token: use the review page"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/update":
                    return self.send(200, rv.update(body["kind"], body["key"], body.get("fields"), body.get("status")))
                if self.path == "/api/check":
                    return self.send(200, rv.check_example(body["key"]))
                if self.path == "/api/text":
                    return self.send(200, {"text": rv.text(body["name"], body["text"])})
                self.send(404, {"error": "not found"})
            except (LoreError, KeyError, json.JSONDecodeError) as e:
                self.send(400, {"error": str(e)})

    srv = ThreadingHTTPServer((host, port), H)
    srv.token = token
    return srv


# ---- workspace: code that lives in a database, worked from a git repo through the lore's tools

WS_DIR = ".lore-workspace"
MISSING = r"not found|no such|does not exist|unknown object|doesn't exist|no rows"   # a pull's "there's none"
class Workspace:
    """A git repo (or any folder) holding code that lives in a database: files laid out as ROOT/TYPE/NAME.EXT,
    worked through the lore's operations. `.lore-workspace/config.json` says which lore and the site's
    variables; the manifest records what each file was pulled from (a hash, and a copy of it), so a deploy can
    tell when the database changed since."""

    def __init__(self, start: Path | None = None):
        d = (start or Path.cwd()).resolve()
        while not (d / WS_DIR / "config.json").exists():
            if d.parent == d:
                raise LoreError("not in a workspace (no .lore-workspace/config.json here or above): "
                                "lore.py ws init")
            d = d.parent
        self.root = d
        self.cfg = json.loads((d / WS_DIR / "config.json").read_text(encoding="utf-8"))
        lore_path = Path(self.cfg["lore"]).expanduser()
        if not lore_path.is_absolute():
            lore_path = (d / lore_path).resolve()
        self.lore = Lore(lore_path)
        self.ops = json.loads(self.lore.meta.get("operations") or "{}")
        for k, v in (self.cfg.get("operations") or {}).items():      # site overrides, without a rebuild
            self.ops.setdefault("operations", {})[k] = v
        problems = check_operations(self.ops)
        if problems:
            raise LoreError("the operations don't hold together:\n  " + "\n  ".join(problems))
        self.src = d / self.cfg.get("root", "src")
        self.mpath = d / WS_DIR / "manifest.json"
        self.manifest = json.loads(self.mpath.read_text()) if self.mpath.exists() else {}

    # --- running an operation
    def secrets(self) -> dict:
        out = {}
        for key, env in (self.cfg.get("secrets") or {}).items():
            if env and os.environ.get(env):
                out[key] = os.environ[env]
        return out

    def mask(self, text: str) -> str:
        for v in self.secrets().values():
            if v:
                text = text.replace(v, "*****")
        return text

    def argv(self, op: str, **kw) -> list[str]:
        """An operation's command, filled in: split into words first, so a value is always exactly one word."""
        spec = (self.ops.get("operations") or {}).get(op)
        if not spec:
            raise LoreError(f"this lore has no {op!r} operation ({OPERATIONS.get(op, '')})")
        values = {**{k: str(v) for k, v in (self.cfg.get("variables") or {}).items()}, **self.secrets(),
                  **{k: str(v) for k, v in kw.items()}}
        argv = []
        for word in shlex.split(spec["command"]):    # split first, fill in after: a value can't add words
            missing = [ph for ph in re.findall(r"\{(\w+)\}", word) if ph not in values]
            if missing:
                raise LoreError(f"{op}: no value for {', '.join('{' + m + '}' for m in missing)} (set it under "
                                f"variables or secrets in {WS_DIR}/config.json)")
            argv.append(re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], word))
        return argv

    def run(self, op: str, **kw) -> subprocess.CompletedProcess:
        argv = self.argv(op, **kw)
        try:
            r = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, cwd=self.root,
                               timeout=float(self.cfg.get("timeout", 600)))
        except FileNotFoundError:
            raise LoreError(f"{op}: {argv[0]} isn't installed (or isn't on PATH)") from None
        except subprocess.TimeoutExpired:
            raise LoreError(f"{op}: timed out") from None
        if r.returncode != 0:
            raise LoreError(f"{op} failed (exit {r.returncode}): {self.mask((r.stderr or r.stdout).strip())[:2000]}")
        return r

    # --- objects and files
    def ext(self, otype: str) -> str:
        exts = (self.ops.get("layout") or {}).get("ext") or {}
        return exts.get(otype, exts.get("*", ".txt"))

    def path_for(self, otype: str, name: str) -> Path:
        if not re.fullmatch(r"[\w.$#@ -]+", otype) or ".." in name.split("/") or name.startswith("/"):
            raise LoreError(f"can't make a path from {otype!r} {name!r}")
        return self.src / otype / (name + self.ext(otype))

    def object_of(self, path: Path) -> tuple[str, str]:
        rel = path.resolve().relative_to(self.src.resolve())
        otype, rest = rel.parts[0], "/".join(rel.parts[1:])
        ext = self.ext(otype)
        return otype, rest[: -len(ext)] if ext and rest.endswith(ext) else rest

    def key(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def fetch(self, otype: str, name: str) -> bytes | None:
        """The database's copy of an object (None if it isn't there)."""
        spec = self.ops["operations"]["pull"]
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / ("object" + self.ext(otype))
            try:
                r = self.run("pull", type=otype, name=name, file=f, dir=tmp)
            except LoreError as e:
                if re.search(spec.get("missing") or MISSING, str(e), re.I):
                    return None
                raise
            content = r.stdout.encode() if spec.get("output") == "stdout" else (
                f.read_bytes() if f.exists() else b"")
            if spec.get("missing") and re.search(spec["missing"], r.stdout + r.stderr, re.I):
                return None
            return content or None      # nothing came back: there's no such object

    def objects(self, otype: str | None = None, project: str | None = None) -> list[tuple[str, str]]:
        op = "project_list" if project else "list"
        spec = (self.ops.get("operations") or {}).get(op) or {}
        r = self.run(op, type=otype or "", project=project or "")
        rx = re.compile(spec.get("parse") or r"^(?P<name>\S+)")
        out = []
        for line in r.stdout.splitlines():
            m = rx.search(line)
            if m and m.group("name"):
                out.append((m.groupdict().get("type") or otype or "", m.group("name").strip()))
        return out

    def save_manifest(self):
        self.mpath.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.mpath.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.manifest, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.mpath)

    def record_base(self, path: Path, otype: str, name: str, content: bytes):
        base = self.root / WS_DIR / "base" / self.key(path)
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_bytes(content)
        self.manifest[self.key(path)] = {"type": otype, "name": name, "hash": hashlib.sha256(content).hexdigest(),
                                         "at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # --- the commands
    def pull(self, objs: list[tuple[str, str]], force: bool = False, log=print) -> dict:
        """Objects from the database into their files. A file changed here since its last pull is left alone
        (unless --force), so a pull never loses work."""
        done = {"pulled": 0, "same": 0, "kept": 0, "missing": 0}
        for otype, name in objs:
            path = self.path_for(otype, name)
            content = self.fetch(otype, name)
            if content is None:
                done["missing"] += 1
                log(f"  {otype} {name}: not in the database")
                continue
            m = self.manifest.get(self.key(path))
            if path.exists() and not force:
                here = hashlib.sha256(path.read_bytes()).hexdigest()
                if m is None or here != m["hash"]:
                    if here != hashlib.sha256(content).hexdigest():
                        done["kept"] += 1
                        log(f"  {self.key(path)}: changed here, not overwritten (deploy it, or pull --force)")
                        continue
            same = path.exists() and path.read_bytes() == content
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            self.record_base(path, otype, name, content)
            done["same" if same else "pulled"] += 1
        self.save_manifest()
        return done

    def status(self, remote: bool = False) -> list[tuple[str, str]]:
        """Each file: same, changed here, changed in the database (with remote), both (a conflict), new here, or
        gone here."""
        out = []
        tracked = set(self.manifest)
        for k, m in sorted(self.manifest.items()):
            path = self.root / k
            here = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            local = "gone here" if here is None else ("changed here" if here != m["hash"] else "")
            db = ""
            if remote:
                cur = self.fetch(m["type"], m["name"])
                if cur is None:
                    db = "gone from the database"
                elif hashlib.sha256(cur).hexdigest() != m["hash"]:
                    db = "changed in the database"
            state = "CONFLICT: " + " and ".join(x for x in (local, db) if x) if local and db else (local or db or "same")
            out.append((k, state))
        if self.src.exists():
            for f in sorted(self.src.rglob("*")):
                if f.is_file() and self.key(f) not in tracked:
                    out.append((self.key(f), "new here (never pulled)"))
        return out

    def deploy(self, files: list[Path], force: bool = False, validate: bool = True, compile_: bool = False,
               log=print) -> dict:
        """Files into the database. Before writing, the database's copy is compared with what was pulled: if
        someone deployed over it since, this stops, and leaves base, theirs and yours side by side for a merge by
        hand (then deploy --force). Nothing is ever overwritten without that check or --force."""
        done = {"deployed": 0, "conflicts": 0}
        has = (self.ops.get("operations") or {})
        for f in files:
            path = f.resolve()
            otype, name = self.object_of(path)
            k, mine = self.key(path), path.read_bytes()
            if validate and "validate" in has:
                self.run("validate", type=otype, name=name, file=path, dir=path.parent)
            if not force:
                cur = self.fetch(otype, name)
                m = self.manifest.get(k)
                base_hash = m["hash"] if m else None
                cur_hash = hashlib.sha256(cur).hexdigest() if cur is not None else None
                if cur_hash != base_hash and cur_hash != hashlib.sha256(mine).hexdigest():
                    cdir = self.root / WS_DIR / "conflicts" / k
                    cdir.mkdir(parents=True, exist_ok=True)
                    base = self.root / WS_DIR / "base" / k
                    (cdir / "base").write_bytes(base.read_bytes() if base.exists() else b"")
                    (cdir / "theirs").write_bytes(cur or b"")
                    (cdir / "mine").write_bytes(mine)
                    done["conflicts"] += 1
                    who = "isn't in the database any more" if cur is None else (
                        "was deployed by someone else since your pull" if m else "already exists in the database")
                    log(f"  CONFLICT {k}: it {who}. Not deployed.\n"
                        f"    merge by hand: code --merge {cdir}/mine {cdir}/theirs {cdir}/base {k}\n"
                        f"    (or: git merge-file {k} {cdir}/base {cdir}/theirs), then: lore.py ws deploy {k} --force")
                    continue
            self.run("deploy", type=otype, name=name, file=path, dir=path.parent)
            if compile_ and "compile" in has:
                self.run("compile", type=otype, name=name, file=path, dir=path.parent)
            self.record_base(path, otype, name, mine)
            shutil.rmtree(self.root / WS_DIR / "conflicts" / k, ignore_errors=True)
            done["deployed"] += 1
            log(f"  {k}: deployed")
        self.save_manifest()
        return done


def portable(path: Path, root: Path, vscode: bool = False) -> str:
    """A path for a file the team commits: relative inside the repo, ~ (or VS Code's ${userHome}) inside the
    home folder, absolute otherwise. Everyone who installed the lore in the same place can use it as is."""
    home = Path.home().resolve()
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        pass
    try:
        rest = path.relative_to(home).as_posix()
        return ("${userHome}/" if vscode else "~/") + rest
    except ValueError:
        return str(path)


def ws_init(root: Path, lore_path: Path, src: str = "src") -> Path:
    """A workspace in `root`: config (which lore, the site's variables and secrets to fill in), .gitignore lines
    for what's per developer, and VS Code tasks for the commands."""
    lore = Lore(lore_path)
    ops = json.loads(lore.meta.get("operations") or "{}")
    if not ops.get("operations"):
        raise LoreError(f"{lore_path.name} has no operations (its pack source needs operations.json)")
    d = root / WS_DIR
    if (d / "config.json").exists():
        raise LoreError(f"{root} is already a workspace ({WS_DIR}/config.json)")
    d.mkdir(parents=True)
    lore_ref = portable(lore_path.resolve(), root)
    cfg = {"lore": lore_ref, "root": src,
           "variables": {k: "" for k in (ops.get("variables") or {})},
           "secrets": {k: f"LORE_{k.upper()}" for k in (ops.get("secrets") or {})},
           "operations": {}, "timeout": 600}
    (d / "config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    gi = root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    for want in (f"{WS_DIR}/base/", f"{WS_DIR}/conflicts/", f"{WS_DIR}/manifest.json"):
        if want not in lines:
            lines.append(want)
    gi.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tasks = root / ".vscode" / "tasks.json"
    if not tasks.exists():
        tasks.parent.mkdir(exist_ok=True)
        beside = lore_path.resolve().parent / "lore.py"           # an installed skill: its own lore.py
        lore_py = portable(beside if beside.exists() else Path(__file__).resolve(), root, vscode=True)
        mk = lambda label, args: {"label": label, "type": "process", "command": "python3",  # noqa: E731
                                  "args": [lore_py, "ws", *args], "problemMatcher": []}
        tasks.write_text(json.dumps({"version": "2.0.0", "tasks": [
            mk("lore: status", ["status"]), mk("lore: status (against the database)", ["status", "--remote"]),
            mk("lore: pull this file", ["pull", "--file", "${file}"]),
            mk("lore: validate this file", ["validate", "${file}"]),
            mk("lore: deploy this file", ["deploy", "${file}"]),
            mk("lore: deploy this file (after merging a conflict)", ["deploy", "${file}", "--force"]),
        ]}, indent=2) + "\n", encoding="utf-8")
    return d / "config.json"


def ws_test(ws: Workspace, otype: str | None, log=print) -> bool:
    """Each operation tried safely: list (or a project's), then one object pulled to a temporary file and
    validated. Nothing is deployed or changed."""
    ok = True
    types = [otype] if otype else [t for t in ((ws.ops.get("layout") or {}).get("ext") or {}) if t != "*"][:1] or [""]
    try:
        objs = ws.objects(types[0])
        log(f"list {types[0]}: {len(objs)} objects" + (f", e.g. {objs[0][0]} {objs[0][1]}" if objs else ""))
    except LoreError as e:
        log(f"list: FAILED: {e}")
        return False
    if not objs:
        log("nothing listed: give --type of a kind of object the database has")
        return False
    t, n = objs[0]
    try:
        content = ws.fetch(t or types[0], n)
        log(f"pull {t} {n}: {'ok, ' + str(len(content)) + ' bytes' if content else 'FAILED: nothing came back'}")
        ok = ok and bool(content)
    except LoreError as e:
        log(f"pull: FAILED: {e}")
        return False
    if "validate" in ws.ops["operations"] and content:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / ("object" + ws.ext(t or types[0]))
            f.write_bytes(content)
            try:
                ws.run("validate", type=t or types[0], name=n, file=f, dir=tmp)
                log("validate: ok (the object as it is in the database)")
            except LoreError as e:
                log(f"validate: FAILED: {e}")
                ok = False
    return ok


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
    b = sub.add_parser("build", help="build a pack source from documentation, with a model")
    b.add_argument("name", help="the lore's name (lowercase, hyphens): it names the skill, NAME-lore")
    b.add_argument("sources", nargs="*", help="PDF, HTML, Markdown or text files, folders of them, or URLs")
    b.add_argument("--tool", action="append", default=[], metavar="CMD",
                   help="a command-line tool of the language: its help (every subcommand's, found by the model; "
                        "only help is ever run) and man page are read (repeatable)")
    b.add_argument("--out", help="the pack source folder to write (default: packs/NAME)")
    b.add_argument("--title")
    b.add_argument("--language", help="the language's name, if it isn't the title")
    b.add_argument("--version", dest="lang_version", help="the language's version the documentation is for")
    b.add_argument("--check", metavar="CMD", help="a command that runs an example: {file} is its code, its input "
                   "goes on stdin, e.g. 'jq -c -f {file}'")
    b.add_argument("--check-ext", default=".txt", help="the example file's extension, for --check")
    b.add_argument("--check-compare", choices=["exact", "words", "json"], default="exact",
                   help="how an example's output is compared with its expected output (json: as JSON values)")
    b.add_argument("--base-url", default=os.environ.get("LORE_BASE_URL"),
                   help="an OpenAI-format endpoint (default: $LORE_BASE_URL), e.g. https://openrouter.ai/api/v1")
    b.add_argument("--model", default=os.environ.get("LORE_MODEL"), help="the model (default: $LORE_MODEL)")
    b.add_argument("--key-env", default="LORE_API_KEY", help="the variable holding the API key (default: "
                   "LORE_API_KEY; none needed for a local endpoint)")
    b.add_argument("--jobs", type=int, default=4, help="chunks read at once (default 4)")
    b.add_argument("--help-limit", type=int, default=HELP_LIMIT,
                   help=f"help pages read per tool, subcommands included (default {HELP_LIMIT})")
    b.add_argument("--limit", type=int, help="only the first N chunks (to try a build cheaply)")
    r = sub.add_parser("review", help="review a built pack source in the browser")
    r.add_argument("src", help="the pack source folder")
    r.add_argument("--port", type=int, default=8765)
    r.add_argument("--host", default="127.0.0.1")
    w = sub.add_parser("ws", aliases=["workspace"],
                       help="code that lives in a database, in a git repo: pull, status, deploy, validate")
    wsub = w.add_subparsers(dest="ws_cmd")
    wi = wsub.add_parser("init", help="make this folder a workspace for a lore that has operations")
    wi.add_argument("lore_file", nargs="?", metavar="LORE", help="the .lore.db (default: --lore, $LORE, or the "
                    "one beside lore.py)")
    wi.add_argument("--root", default="src", help="the folder the objects go in (default: src)")
    wl = wsub.add_parser("list", help="objects in the database")
    wl.add_argument("--type")
    wl.add_argument("--project")
    wp = wsub.add_parser("pull", help="objects from the database into files")
    wp.add_argument("object", nargs="*", metavar="TYPE NAME")
    wp.add_argument("--type", help="every object of this type")
    wp.add_argument("--project", help="every object in this project")
    wp.add_argument("--file", action="append", default=[], help="the object this file holds (repeatable)")
    wp.add_argument("--force", action="store_true", help="overwrite files changed here")
    wst = wsub.add_parser("status", help="what changed here (and, with --remote, in the database)")
    wst.add_argument("--remote", action="store_true", help="also compare with the database (one pull per object)")
    wd = wsub.add_parser("deploy", help="files into the database, after a conflict check")
    wd.add_argument("files", nargs="+", metavar="FILE")
    wd.add_argument("--force", action="store_true", help="skip the conflict check (after merging by hand)")
    wd.add_argument("--no-validate", action="store_true", help="don't validate first")
    wd.add_argument("--compile", action="store_true", help="compile each object after deploying it")
    wv = wsub.add_parser("validate", help="check files with the tools, without changing the database")
    wv.add_argument("files", nargs="+", metavar="FILE")
    wc = wsub.add_parser("compile", help="compile objects in the database")
    wc.add_argument("files", nargs="+", metavar="FILE")
    wt = wsub.add_parser("test", help="try the operations safely: list, pull one object, validate it")
    wt.add_argument("--type")
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


def cmd_ws(args) -> int:
    if args.ws_cmd == "init":
        cfg = ws_init(Path.cwd(), Path(args.lore_file).expanduser() if args.lore_file else find_lore(args.lore),
                      args.root)
        c = json.loads(cfg.read_text())
        print(f"workspace set up: {cfg}")
        if c["variables"]:
            print(f"  fill in the variables there: {', '.join(c['variables'])}")
        if c["secrets"]:
            print("  and set the secrets as environment variables: "
                  + ", ".join(f"{v} ({k})" for k, v in c["secrets"].items()))
        print("  then: lore.py ws test")
        return 0
    ws = Workspace()
    if args.ws_cmd == "list":
        for t, n in ws.objects(args.type, args.project):
            print(f"{t} {n}".strip())
        return 0
    if args.ws_cmd == "pull":
        objs = []
        if len(args.object) % 2:
            raise LoreError("pull TYPE NAME [TYPE NAME...]")
        objs += list(zip(args.object[::2], args.object[1::2]))
        objs += [ws.object_of(Path(f)) for f in args.file]
        if args.type or args.project:
            objs += ws.objects(args.type, args.project)
        if not objs:
            raise LoreError("pull what? TYPE NAME, --type T, --project P or --file F")
        r = ws.pull(objs, force=args.force)
        print(f"pulled {r['pulled']}, unchanged {r['same']}, kept (changed here) {r['kept']}, "
              f"missing {r['missing']}")
        return 0
    if args.ws_cmd == "status":
        rows = [(k, st) for k, st in ws.status(args.remote) if st != "same"]
        print("\n".join(f"{st:<40} {k}" for k, st in rows) or "everything is as pulled")
        return 1 if any(st.startswith("CONFLICT") for _, st in rows) else 0
    if args.ws_cmd == "deploy":
        r = ws.deploy([Path(f) for f in args.files], force=args.force, validate=not args.no_validate,
                      compile_=args.compile)
        print(f"deployed {r['deployed']}" + (f", {r['conflicts']} CONFLICTS (not deployed)" if r["conflicts"] else ""))
        return 1 if r["conflicts"] else 0
    if args.ws_cmd in ("validate", "compile"):
        for f in args.files:
            t, n = ws.object_of(Path(f))
            r = ws.run(args.ws_cmd, type=t, name=n, file=Path(f).resolve(), dir=Path(f).resolve().parent)
            print(f"{f}: ok" + (f"\n{ws.mask(r.stdout.strip())}" if r.stdout.strip() else ""))
        return 0
    if args.ws_cmd == "test":
        return 0 if ws_test(ws, args.type) else 1
    raise LoreError("ws what? init, list, pull, status, deploy, validate, compile, test")


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
        if args.cmd == "build":
            if not args.base_url or not args.model:
                raise LoreError("build needs a model: --base-url and --model (or LORE_BASE_URL and LORE_MODEL)")
            if not args.sources and not args.tool:
                raise LoreError("build from what? give documentation (files, folders, URLs) and/or --tool CMD")
            out = Path(args.out or f"packs/{args.name}")
            model = Model(args.base_url, args.model, os.environ.get(args.key_env), out / ".build" / "answers")
            check = {"command": args.check, "ext": args.check_ext,
                     "compare": args.check_compare} if args.check else None
            r = build(args.name, args.sources, args.tool, out, model, args.title, args.language, args.lang_version,
                      check, args.jobs, args.limit, help_limit=args.help_limit)
            print(f"{r['out']}: {r['sources']} sources, {r['chunks']} chunks -> {r['sections']} sections, "
                  f"{r['symbols']} symbols, {r['examples']} examples, {r['tools']} tools; {r['flagged']} flagged "
                  f"for review; {r['model_calls']} model calls (the rest were recorded)"
                  f"\n  next: hobkin-lore review {r['out']}   then: hobkin-lore compile {r['out']}")
            return 0
        if args.cmd == "review":
            srv = review_server(Path(args.src), args.host, args.port)
            print(f"reviewing {args.src}: http://{args.host}:{srv.server_address[1]}/  (Ctrl-C to stop)")
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            return 0
        if args.cmd in ("ws", "workspace"):
            return cmd_ws(args)
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
