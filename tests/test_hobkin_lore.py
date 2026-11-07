"""hobkin-lore: compile a pack source, look things up the way an agent does, keep notes, and install the skill
folder that hobkin and Claude Code read. Standard library only: python3 -m unittest discover tests"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import hobkin_lore as hl  # noqa: E402

JQ = ROOT / "packs" / "jq"


def run(*args, lore=None, cwd=None, script=None):
    """The command line, as a separate process (as an agent runs it)."""
    env = {**os.environ}
    env.pop("LORE", None)
    if lore:
        env["LORE"] = str(lore)
    r = subprocess.run([sys.executable, str(script or ROOT / "hobkin_lore.py"), *args], capture_output=True,
                       text=True, env=env, cwd=cwd)
    return r.returncode, r.stdout + r.stderr


class LoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.lore = cls.tmp / "jq.lore.db"
        cls.result = hl.compile_pack(JQ, cls.lore, log=lambda *_: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    # --- compile
    def test_compiles_everything_in_the_source(self):
        r = self.result
        self.assertEqual((r["sections"], r["tools"], r["examples"]), (9, 1, 12))
        self.assertGreaterEqual(r["symbols"], 36)
        self.assertEqual(r["warnings"], 0)
        con = sqlite3.connect(self.lore)
        meta = dict(con.execute("SELECT key, value FROM meta"))
        self.assertEqual(meta["schema"], str(hl.SCHEMA_VERSION))
        self.assertIn("jq essentials", meta["cheatsheet"])

    @unittest.skipUnless(shutil.which("jq"), "jq isn't installed")
    def test_examples_are_checked_by_the_languages_own_tool(self):
        self.assertEqual(self.result["checked"], {"pass": 12, "fail": 0, "error": 0, "unchecked": 0})

    def test_a_wrong_example_fails_the_compile(self):
        if not shutil.which("jq"):
            self.skipTest("jq isn't installed")
        src = self.tmp / "bad"
        shutil.copytree(JQ, src)
        with open(src / "examples.jsonl", "a") as f:
            f.write(json.dumps({"id": "wrong", "code": ".a + 1", "input": '{"a": 1}', "expect": "3"}) + "\n")
        code, out = run("compile", str(src), "-o", str(self.tmp / "bad.lore.db"))
        self.assertEqual(code, 1)
        self.assertIn("example wrong: fail", out)
        self.assertIn("expected:\n    3\n    got:\n    2", out)

    def test_bad_sources_are_refused_with_the_reason(self):
        src = self.tmp / "broken"
        shutil.copytree(JQ, src)
        with open(src / "symbols.jsonl", "a") as f:
            f.write(json.dumps({"name": "x", "kind": "gizmo", "section": "nowhere"}) + "\n")
        with self.assertRaises(hl.LoreError) as e:
            hl.compile_pack(src, self.tmp / "broken.lore.db", log=lambda *_: None)
        self.assertIn("kind 'gizmo'", str(e.exception))
        self.assertIn("section 'nowhere' doesn't exist", str(e.exception))
        self.assertFalse((self.tmp / "broken.lore.db").exists())       # nothing half-built is left behind

    # --- lookup
    def look(self, *args):
        code, out = run(*args, lore=self.lore)
        self.assertEqual(code, 0, out)
        return out

    def test_symbol_exact_and_case_insensitive(self):
        out = self.look("symbol", "map")
        self.assertIn("map(f)", out)
        self.assertIn("examples: map-select", out)
        self.assertIn("map(f)", self.look("symbol", "MAP"))

    def test_a_misspelled_symbol_suggests_the_right_one(self):
        code, out = run("symbol", "mapp", lore=self.lore)
        self.assertEqual(code, 2)
        self.assertIn("did you mean: map", out)

    def test_search_ranks_a_named_symbol_first_and_survives_punctuation(self):
        out = self.look("search", 'sub("a"; "b")')
        self.assertTrue(out.startswith("symbol: sub"), out)
        self.assertIn("symbol: @csv", self.look("search", "csv"))
        self.assertIn("group_by", self.look("search", "group"))            # prefixes match identifiers
        self.assertIn("nothing for", self.look("search", "zzzqqq"))
        self.assertIn("tool: jq", self.look("search", "--kind", "tool", "raw", "output"))

    def test_toc_show_and_paging(self):
        toc = self.look("toc")
        self.assertIn("Paths: .foo, .[n], .[]  [basics/paths]", toc)
        self.assertIn("[basics/paths]", self.look("toc", "basics"))
        self.assertIn(".[]", self.look("show", "basics/paths"))
        # a long section pages
        lines = "\n".join(f"line {i}" for i in range(200))
        src = self.tmp / "long"
        shutil.copytree(JQ, src)
        with open(src / "sections.jsonl", "a") as f:
            f.write(json.dumps({"id": "long", "title": "Long", "body": lines}) + "\n")
        hl.compile_pack(src, self.tmp / "long.lore.db", check=False, log=lambda *_: None)
        code, out = run("show", "long", lore=self.tmp / "long.lore.db")
        self.assertIn("more: show long --from 80", out)
        self.assertNotIn("line 100", out)
        code, out = run("show", "long", "--from", "80", lore=self.tmp / "long.lore.db")
        self.assertIn("line 100", out)

    def test_examples_tools_cheatsheet_info(self):
        self.assertIn("map(select(.age > 30))", self.look("examples", "--symbol", "select"))
        self.assertIn("--argjson NAME JSON", self.look("tool", "jq"))
        self.assertIn("Cannot index number", self.look("tool", "jq"))
        self.assertIn("`;` not `,`", self.look("cheatsheet"))
        self.assertIn("12 examples", self.look("info"))

    def test_a_lore_of_another_schema_is_refused(self):
        other = self.tmp / "old.lore.db"
        shutil.copyfile(self.lore, other)
        con = sqlite3.connect(other)
        con.execute("UPDATE meta SET value = '0' WHERE key = 'schema'")
        con.commit()
        con.close()
        code, out = run("info", lore=other)
        self.assertEqual(code, 2)
        self.assertIn("Rebuild it from its source", out)

    # --- install, notes
    def test_install_makes_a_skill_folder_that_works_on_its_own(self):
        skills = self.tmp / "skills"
        code, out = run("install", str(self.lore), "--to", str(skills))
        self.assertEqual(code, 0, out)
        folder = skills / "jq-lore"
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["SKILL.md", "jq.lore.db", "lore.py"])
        md = (folder / "SKILL.md").read_text()
        head = md.split("---")[1]
        self.assertIn("name: jq-lore", head)
        desc = next(l for l in head.splitlines() if l.startswith("description:"))
        self.assertTrue(json.loads(desc.split(":", 1)[1]))                 # a quoted (YAML-safe) string
        self.assertLessEqual(len(json.loads(desc.split(":", 1)[1])), 1024)
        self.assertIn('lore.py" COMMAND', md)
        # run from the skill folder, from anywhere, with no LORE set: it finds its own lore
        code, out = run("symbol", "select", script=folder / "lore.py", cwd=self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn("select(f)", out)
        # a note, kept across a reinstall
        code, out = run("note", "surprising", "thing", "--on", "select", script=folder / "lore.py")
        self.assertEqual(code, 0, out)
        run("install", str(self.lore), "--to", str(skills))
        code, out = run("symbol", "select", script=folder / "lore.py")
        self.assertIn("surprising thing", out)
        self.assertIn("note on select: surprising thing", run("search", "surprising", script=folder / "lore.py")[1])

    def test_install_targets(self):
        ns = lambda **k: type("A", (), {"to": None, "claude": False, "project": None, "hobkin": False, **k})  # noqa
        home = Path.home()
        self.assertEqual(hl.install_dir(ns(claude=True)), home / ".claude" / "skills")
        self.assertEqual(hl.install_dir(ns(project="/p")), Path("/p/.claude/skills"))
        os.environ["HOBKIN_HOME"] = str(self.tmp / "hh")
        try:
            self.assertEqual(hl.install_dir(ns()), self.tmp / "hh" / "_shared" / "skills")
        finally:
            del os.environ["HOBKIN_HOME"]

    # --- mcp
    def mcp(self, *messages, args=()):
        """Talk to `hobkin-lore mcp` over stdio: the replies, by id."""
        lines = "".join(json.dumps({"jsonrpc": "2.0", **m}) + "\n" for m in messages)
        r = subprocess.run([sys.executable, str(ROOT / "hobkin_lore.py"), "mcp", *(args or [str(self.lore)])],
                           input=lines, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = [json.loads(l) for l in r.stdout.splitlines()]          # stdout holds nothing but JSON-RPC
        return {m["id"]: m for m in out}

    def test_mcp_handshake_tools_and_calls(self):
        init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
        got = self.mcp({"id": 1, "method": "initialize", "params": init},
                       {"method": "notifications/initialized"},
                       {"id": 2, "method": "tools/list"},
                       {"id": 3, "method": "tools/call", "params": {"name": "symbol", "arguments": {"name": "select"}}},
                       {"id": 4, "method": "tools/call", "params": {"name": "symbol", "arguments": {"name": "slect"}}},
                       {"id": 5, "method": "tools/call", "params": {"name": "search",
                                                                     "arguments": {"query": "group", "limit": 3}}},
                       {"id": 6, "method": "ping"}, {"id": 7, "method": "nope"})
        r = got[1]["result"]
        self.assertEqual((r["protocolVersion"], r["serverInfo"]["name"]), ("2025-06-18", "jq-lore"))
        self.assertIn("tools", r["capabilities"])
        names = [t["name"] for t in got[2]["result"]["tools"]]
        self.assertEqual(names, ["cheatsheet", "search", "symbol", "toc", "show", "examples", "tools", "tool",
                                 "note", "info"])
        for t in got[2]["result"]["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])
        self.assertFalse(got[3]["result"]["isError"])
        self.assertIn("select(f)", got[3]["result"]["content"][0]["text"])
        self.assertTrue(got[4]["result"]["isError"])                    # a tool error, not a protocol error
        self.assertIn("did you mean: select", got[4]["result"]["content"][0]["text"])
        self.assertIn("group_by", got[5]["result"]["content"][0]["text"])
        self.assertEqual(got[6]["result"], {})
        self.assertEqual(got[7]["error"]["code"], -32601)
        self.assertEqual(len(got), 7)                                   # the notification got no reply

    def test_mcp_an_unknown_protocol_version_gets_ours(self):
        got = self.mcp({"id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}})
        self.assertEqual(got[1]["result"]["protocolVersion"], hl.MCP_VERSIONS[0])

    def test_mcp_several_lores_take_a_lore_argument(self):
        other_src = self.tmp / "jq2"
        shutil.copytree(JQ, other_src)
        pack = json.loads((other_src / "pack.json").read_text())
        pack["name"], pack["title"] = "jqtwo", "jq two"
        (other_src / "pack.json").write_text(json.dumps(pack))
        hl.compile_pack(other_src, self.tmp / "jqtwo.lore.db", check=False, log=lambda *_: None)
        got = self.mcp({"id": 1, "method": "tools/list"},
                       {"id": 2, "method": "tools/call", "params": {"name": "info", "arguments": {"lore": "jqtwo"}}},
                       {"id": 3, "method": "tools/call", "params": {"name": "info", "arguments": {}}},
                       args=[str(self.lore), str(self.tmp / "jqtwo.lore.db")])
        search = next(t for t in got[1]["result"]["tools"] if t["name"] == "search")
        self.assertEqual(search["inputSchema"]["properties"]["lore"]["enum"], ["jq", "jqtwo"])
        self.assertIn("lore", search["inputSchema"]["required"])
        self.assertIn("jq two", got[2]["result"]["content"][0]["text"])
        self.assertTrue(got[3]["result"]["isError"])

    def test_mcp_from_an_installed_skill(self):
        skills = self.tmp / "skills-mcp"
        hl.install(self.lore, skills)
        r = subprocess.run([sys.executable, str(skills / "jq-lore" / "lore.py"), "mcp"], capture_output=True,
                           text=True, timeout=60, input=json.dumps({"jsonrpc": "2.0", "id": 1,
                                                                    "method": "tools/list"}) + "\n")
        self.assertIn('"name": "symbol"', r.stdout)


if __name__ == "__main__":
    unittest.main()
