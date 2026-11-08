"""hobkin-lore build and review: documentation (HTML, a tool's --help) through a model into a pack source whose
entries cite the source lines they rest on, flagged when they don't; rebuilds ask the model nothing new and keep
a reviewer's decisions; the review page reads and writes the pack source."""

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hobkin_lore as hl  # noqa: E402
from fake_model import FakeModel  # noqa: E402

DOC = """<html><body><nav>skip me</nav>
<h1>Toylang</h1><p>Toylang is a tiny language.</p>
<h2>Strings</h2><p>Strings are written between double quotes and may span lines.</p>
<h3>upper(s)</h3><p>Returns the string s with every letter in upper case.</p>
<h3>lower(s)</h3><p>Returns the string s with every letter in lower case.</p>
<pre>upper("abc")</pre>
</body></html>"""

TOOL = """#!/bin/sh
case "$1 $2" in
  "work --help") echo "usage: toyrun work PROJECT [--out DIR]"; echo "  --out DIR   where the export goes" ;;
  "list --help") echo "usage: toyrun list [--type T]"; echo "  --type T   only objects of type T" ;;
  "work "*|"list "*) touch "$(dirname "$0")/DID_WORK"; echo "working" ;;
  "--help "*|"-h "*) echo "usage: toyrun FILE [-v]"; echo "  -v   say what it does"; echo "Commands:";
                     echo "  work  export a project"; echo "  list  list objects" ;;
  *) touch "$(dirname "$0")/DID_WORK"; echo "working" ;;
esac
"""


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "doc.html").write_text(DOC)
        tool = self.tmp / "toyrun"
        tool.write_text(TOOL)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        self.tool = str(tool)
        self.fake = FakeModel()
        self.out = self.tmp / "packs" / "toy"

    def tearDown(self):
        self.fake.close()
        shutil.rmtree(self.tmp)

    def build(self):
        model = hl.Model(self.fake.url, "fake", None, self.out / ".build" / "answers")
        return hl.build("toy", [str(self.tmp / "doc.html")], [self.tool], self.out, model, title="Toylang",
                        log=lambda *_: None), model

    def test_a_tools_help_is_found_subcommands_too_and_nothing_else_runs(self):
        pages = hl.tool_help(self.tool, hl.Model(self.fake.url, "fake", None, self.tmp / "answers"),
                             log=lambda *_: None)
        text = "\n".join(pages)
        self.assertIn("usage: toyrun FILE", text)
        self.assertIn("--out DIR", text)                         # a subcommand's help, found through the model
        self.assertIn("--type T", text)
        self.assertFalse((self.tmp / "DID_WORK").exists())       # the model's "work PROJECTX" was never run
        self.assertFalse(hl.safe_help(["work", "PROJECTX"]))
        self.assertFalse(hl.safe_help(["--delete-everything"]))
        self.assertFalse(hl.safe_help(["export", "/tmp/x", "--help"]))
        self.assertTrue(hl.safe_help(["export", "--help"]))
        self.assertTrue(hl.safe_help(["help", "export"]))
        self.assertTrue(hl.safe_help(["-?"]))

    def test_scripts_are_sources(self):
        (self.tmp / "nightly.sh").write_text("toyrun work PAYROLL --out /tmp/x\n")
        srcs = hl.gather([str(self.tmp / "nightly.sh")], [], self.tmp / "work", log=lambda *_: None)
        self.assertEqual(srcs[0]["kind"], "script")
        self.assertIn("toyrun work PAYROLL", (self.tmp / "work" / "sources" / f"{srcs[0]['id']}.txt").read_text())

    def test_html_text_keeps_structure(self):
        t = hl.html_text(DOC)
        self.assertNotIn("skip me", t)
        self.assertIn("## Strings", t)
        self.assertIn("### upper(s)", t)
        self.assertIn('```\nupper("abc")\n```', t)

    def test_locate_finds_quotes_the_way_models_write_them(self):
        text = "Intro line.\n\n   @uri:\n\n      Applies percent-encoding, by mapping all\n   reserved characters.\n"
        self.assertEqual(hl.locate("@uri: Applies percent-encoding, by mapping all reserved characters.", text, 10),
                         (12, 15))                                # across a blank line and a line break
        self.assertEqual(hl.locate("@uri: ... reserved characters", text, 10), (12, 15))   # shortened with ...
        self.assertEqual(hl.locate("`@uri`: **Applies** percent-encoding", text, 1), (3, 5))  # Markdown marks
        self.assertIsNone(hl.locate("something the documentation never says", text, 1))
        self.assertIsNone(hl.locate("short", text, 1))
        self.assertEqual(hl.plain("\x1b[1mbold\x1b[22m and o\x08ok"), "bold and ok")    # man's formatting

    def test_output_comparison_modes(self):
        self.assertTrue(hl.same_output("[0,1]\n", "[0, 1]", "json"))
        self.assertTrue(hl.same_output("0\n3\n6\n", "0, 3, 6", "json"))           # a stream, shown with commas
        self.assertFalse(hl.same_output("[0,2]", "[0, 1]", "json"))
        self.assertTrue(hl.same_output("a  b\n", "a b", "words"))
        self.assertFalse(hl.same_output("[0,1]", "[0, 1]", "exact"))

    def test_examples_tie_only_to_symbols_their_code_uses(self):
        self.assertTrue(hl.uses("map(abs)", "map"))
        self.assertFalse(hl.uses("map_values(.)", "map"))
        self.assertTrue(hl.uses(".[] | @csv", "@csv"))
        self.assertFalse(hl.uses("length", "len"))

    def test_chunks_cover_every_line_once(self):
        text = "\n".join(f"# head {i}\n" + "word " * 400 for i in range(20))
        chunks = hl.chunk(text, size=3000)
        self.assertGreater(len(chunks), 3)
        self.assertEqual(chunks[0][0], 1)
        for (a1, b1, _), (a2, _, _) in zip(chunks, chunks[1:]):
            self.assertEqual(a2, b1 + 1)                       # contiguous, no gaps, no overlaps
        self.assertEqual(chunks[-1][1], len(text.splitlines()))

    def test_build_cites_flags_and_compiles(self):
        r, model = self.build()
        self.assertEqual(r["sources"], 2)                       # the HTML, and the tool's --help
        syms = {y["name"]: y for y in hl.read_jsonl(self.out / "symbols.jsonl")}
        self.assertIn("upper", syms)
        self.assertRegex(syms["upper"]["source"], r"doc\.html, lines \d+-\d+")
        self.assertEqual(syms["upper"]["review"]["flags"], [])
        self.assertEqual(syms["invented"]["review"]["flags"], ["quote not found in the source"])
        tools = hl.read_jsonl(self.out / "tools.jsonl")
        self.assertEqual(tools[0]["name"], "toyrun")
        ops = json.loads((self.out / "operations.json").read_text())       # drafted from the tool, and sound
        self.assertNotIn("problems", ops)
        self.assertEqual(sorted(ops["operations"]), ["list", "pull"])
        pack = json.loads((self.out / "pack.json").read_text())
        self.assertEqual(pack["description"], "Toylang, a tiny language for tests.")
        self.assertIn("double quotes", (self.out / "cheatsheet.md").read_text())
        # the pack source compiles, and the lore answers lookups
        lore = self.tmp / "toy.lore.db"
        hl.compile_pack(self.out, lore, check=False, log=lambda *_: None)
        lo = hl.Lore(lore)
        out = hl.cmd_symbol(lo, type("A", (), {"name": "upper"}))
        self.assertIn("upper case", out)
        self.assertIn("doc.html, lines", out)

    def test_a_rebuild_asks_nothing_and_keeps_the_reviewers_decisions(self):
        self.build()
        first = len(self.fake.requests)
        rows = hl.read_jsonl(self.out / "symbols.jsonl")
        for r in rows:
            if r["name"] == "invented":
                r["review"]["status"] = "rejected"
            if r["name"] == "lower":
                r["summary"], r["review"]["status"] = "lower-cases it (reviewed)", "edited"
        (self.out / "symbols.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (self.out / "cheatsheet.md").write_text("- my own cheatsheet\n")
        r, model = self.build()
        self.assertEqual(len(self.fake.requests), first)         # every answer came from the record
        self.assertEqual(model.calls, 0)
        rows = {y["name"]: y for y in hl.read_jsonl(self.out / "symbols.jsonl")}
        self.assertEqual(rows["invented"]["review"]["status"], "rejected")
        self.assertEqual(rows["lower"]["summary"], "lower-cases it (reviewed)")
        self.assertEqual((self.out / "cheatsheet.md").read_text(), "- my own cheatsheet\n")
        lore = self.tmp / "toy.lore.db"
        hl.compile_pack(self.out, lore, check=False, log=lambda *_: None)
        names = [r[0] for r in hl.Lore(lore).q("SELECT name FROM symbols")]
        self.assertNotIn("invented", names)                       # rejected: in the source, not in the lore
        self.assertIn("upper", names)

    def test_a_build_extends_a_hand_written_pack(self):
        self.out.mkdir(parents=True)
        (self.out / "symbols.jsonl").write_text(json.dumps({"name": "handmade", "kind": "function",
                                                            "summary": "written by a person"}) + "\n")
        self.build()
        rows = {y["name"]: y for y in hl.read_jsonl(self.out / "symbols.jsonl")}
        self.assertEqual(rows["handmade"]["summary"], "written by a person")   # kept as written
        self.assertIn("upper", rows)                                            # and the build's added

    def test_the_review_page(self):
        self.build()
        srv = hl.review_server(self.out, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        get = lambda p: json.loads(urllib.request.urlopen(base + p).read())  # noqa: E731

        def post(p, body, token=srv.token):
            req = urllib.request.Request(base + p, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Lore-Token": token})
            try:
                return json.loads(urllib.request.urlopen(req).read()), 200
            except urllib.error.HTTPError as e:
                return json.loads(e.read()), e.code
        try:
            page = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("lore review", page)
            self.assertIn(srv.token, page)
            flagged = get("/api/list?kind=symbols&status=flagged")
            self.assertEqual([e["title"] for e in flagged], ["invented"])
            key = flagged[0]["key"]
            e = get("/api/entry?kind=symbols&key=" + urllib.request.quote("upper\tfunction"))
            self.assertTrue(e["context"]["quote"])                 # the quote's lines in the source
            self.assertTrue(any("upper case" in l for l in e["context"]["lines"]))
            self.assertEqual(post("/api/update", {"kind": "symbols", "key": key, "status": "rejected"},
                                  token="wrong")[1], 403)
            r, code = post("/api/update", {"kind": "symbols", "key": key, "status": "rejected"})
            self.assertEqual((code, r["review"]["status"]), (200, "rejected"))
            r, code = post("/api/update", {"kind": "symbols", "key": "upper\tfunction",
                                           "fields": {"summary": "upper-cases a string"}})
            self.assertEqual(r["review"]["status"], "edited")
            saved = {y["name"]: y for y in hl.read_jsonl(self.out / "symbols.jsonl")}
            self.assertEqual(saved["upper"]["summary"], "upper-cases a string")
            self.assertEqual(saved["invented"]["review"]["status"], "rejected")
            s = get("/api/summary")
            self.assertEqual(s["kinds"]["symbols"]["rejected"], 1)
            r, code = post("/api/text", {"name": "cheatsheet.md", "text": "- new\n"})
            self.assertEqual((self.out / "cheatsheet.md").read_text(), "- new\n")
            self.assertEqual(post("/api/text", {"name": "../pack.json", "text": "x"})[1], 400)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
