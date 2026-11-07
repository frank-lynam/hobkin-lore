"""The workspace: code that lives in a database, worked from a git repo through a lore's operations. Against a
fake PeopleSoft-style CLI (tests/fakeps.py) over a fake database (a folder): init, test, list, pull, status,
validate, deploy, and two developers deploying over each other."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import hobkin_lore as hl  # noqa: E402

FAKEPS = Path(__file__).resolve().parent / "fakeps.py"
OPS = {
    "variables": {"db": "the database to work in"},
    "secrets": {"password": "the database password"},
    "layout": {"ext": {"PeopleCode": ".pcode", "SQL": ".sql", "*": ".txt"}},
    "operations": {
        "list": {"command": f"{FAKEPS} list -db {{db}} -type {{type}}", "parse": r"^(?P<type>\S+) (?P<name>\S+)$"},
        "project_list": {"command": f"{FAKEPS} project -db {{db}} -name {{project}}",
                         "parse": r"^(?P<type>\S+) (?P<name>\S+)$"},
        "pull": {"command": f"{FAKEPS} export -db {{db}} -type {{type}} -name {{name}} -out {{file}}",
                 "output": "file"},
        "deploy": {"command": f"{FAKEPS} import -db {{db}} -type {{type}} -name {{name}} -in {{file}} "
                              f"-pw {{password}}"},
        "validate": {"command": f"{FAKEPS} validate -in {{file}}"},
        "compile": {"command": f"{FAKEPS} compile -db {{db}} -type {{type}} -name {{name}}"},
    },
}


def run(*args, cwd, env=None):
    r = subprocess.run([sys.executable, str(ROOT / "hobkin_lore.py"), *args], capture_output=True, text=True,
                       cwd=cwd, env={**os.environ, **(env or {})})
    return r.returncode, r.stdout + r.stderr


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # the fake database: two PeopleCode programs and a SQL object, and a project holding one of them
        self.db = self.tmp / "db" / "HRDEV"
        for t, n, text in (("PeopleCode", "JOB.EMPLID.FieldChange", "Local string &a = \"x\";\n"),
                           ("PeopleCode", "JOB.DEPTID.FieldEdit", "If None(&d) Then Error \"x\"; End-If;\n"),
                           ("SQL", "HR_ACTIVE", "SELECT EMPLID FROM PS_JOB\n")):
            (self.db / t).mkdir(parents=True, exist_ok=True)
            (self.db / t / n).write_text(text)
        (self.db / "projects").mkdir()
        (self.db / "projects" / "HR_FIX.txt").write_text("PeopleCode JOB.EMPLID.FieldChange\nSQL HR_ACTIVE\n")
        self.env = {"FAKEDB": str(self.tmp / "db"), "LORE_PASSWORD": "s3cret"}
        os.environ.update(self.env)
        # a lore with these operations (the jq sample's reference, standing in for PeopleCode's)
        src = self.tmp / "pack"
        shutil.copytree(ROOT / "packs" / "jq", src)
        (src / "operations.json").write_text(json.dumps(OPS))
        self.lore = self.tmp / "ps.lore.db"
        hl.compile_pack(src, self.lore, check=False, log=lambda *_: None)

    def tearDown(self):
        for k in self.env:
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp)

    def repo(self, name):
        """A developer's repo, set up as a workspace on the dev database."""
        d = self.tmp / name
        d.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        code, out = run("ws", "init", str(self.lore), cwd=d)
        self.assertEqual(code, 0, out)
        cfg = json.loads((d / ".lore-workspace" / "config.json").read_text())
        cfg["variables"]["db"] = "HRDEV"
        (d / ".lore-workspace" / "config.json").write_text(json.dumps(cfg))
        return d

    def test_init_writes_config_gitignore_and_tasks(self):
        d = self.repo("dev1")
        cfg = json.loads((d / ".lore-workspace" / "config.json").read_text())
        self.assertEqual(cfg["secrets"], {"password": "LORE_PASSWORD"})        # the name of a variable, never a value
        self.assertIn(".lore-workspace/manifest.json", (d / ".gitignore").read_text())
        tasks = json.loads((d / ".vscode" / "tasks.json").read_text())
        self.assertIn("lore: deploy this file", [t["label"] for t in tasks["tasks"]])
        code, out = run("ws", "init", str(self.lore), cwd=d)
        self.assertEqual(code, 2)                                                 # not twice
        code, out = run("ws", "test", "--type", "PeopleCode", cwd=d)
        self.assertEqual(code, 0, out)
        self.assertIn("list PeopleCode: 2 objects", out)
        self.assertIn("validate: ok", out)

    def test_pull_status_validate_deploy(self):
        d = self.repo("dev1")
        code, out = run("ws", "pull", "--project", "HR_FIX", cwd=d)
        self.assertEqual(code, 0, out)
        self.assertIn("pulled 2", out)
        f = d / "src" / "PeopleCode" / "JOB.EMPLID.FieldChange.pcode"
        self.assertEqual(f.read_text(), 'Local string &a = "x";\n')
        self.assertTrue((d / "src" / "SQL" / "HR_ACTIVE.sql").exists())
        self.assertEqual(run("ws", "pull", "--type", "PeopleCode", cwd=d)[0], 0)
        self.assertTrue((d / "src" / "PeopleCode" / "JOB.DEPTID.FieldEdit.pcode").exists())
        self.assertIn("everything is as pulled", run("ws", "status", cwd=d)[1])
        # a change: validated (a syntax error stops the deploy), then deployed
        f.write_text("SYNTAX ERROR\n")
        code, out = run("ws", "deploy", str(f), cwd=d)
        self.assertEqual(code, 2)
        self.assertIn("syntax error", out)
        self.assertNotIn("SYNTAX", (self.db / "PeopleCode" / "JOB.EMPLID.FieldChange").read_text())
        f.write_text('Local string &a = "y";\n')
        self.assertIn("changed here", run("ws", "status", cwd=d)[1])
        code, out = run("ws", "deploy", str(f), "--compile", cwd=d)
        self.assertEqual(code, 0, out)
        self.assertEqual((self.db / "PeopleCode" / "JOB.EMPLID.FieldChange").read_text(), 'Local string &a = "y";\n')
        self.assertIn("everything is as pulled", run("ws", "status", "--remote", cwd=d)[1])
        # a pull never overwrites work not yet deployed
        f.write_text("work in progress\n")
        code, out = run("ws", "pull", "PeopleCode", "JOB.EMPLID.FieldChange", cwd=d)
        self.assertIn("kept (changed here) 1", out)
        self.assertEqual(f.read_text(), "work in progress\n")

    def test_two_developers_deploying_over_each_other(self):
        d1, d2 = self.repo("dev1"), self.repo("dev2")
        for d in (d1, d2):
            self.assertEqual(run("ws", "pull", "PeopleCode", "JOB.EMPLID.FieldChange", cwd=d)[0], 0)
        f1 = d1 / "src" / "PeopleCode" / "JOB.EMPLID.FieldChange.pcode"
        f2 = d2 / "src" / "PeopleCode" / "JOB.EMPLID.FieldChange.pcode"
        f1.write_text('Local string &a = "from dev1";\n')
        self.assertEqual(run("ws", "deploy", str(f1), cwd=d1)[0], 0)
        # dev2 changed the same object from the same base: their deploy stops, and nothing is overwritten
        f2.write_text('Local string &a = "from dev2";\n')
        self.assertIn("CONFLICT: changed here and changed in the database", run("ws", "status", "--remote", cwd=d2)[1])
        code, out = run("ws", "deploy", str(f2), cwd=d2)
        self.assertEqual(code, 1)
        self.assertIn("CONFLICT", out)
        self.assertIn("code --merge", out)
        self.assertIn("from dev1", (self.db / "PeopleCode" / "JOB.EMPLID.FieldChange").read_text())
        c = d2 / ".lore-workspace" / "conflicts" / "src" / "PeopleCode" / "JOB.EMPLID.FieldChange.pcode"
        self.assertEqual((c / "base").read_text(), 'Local string &a = "x";\n')
        self.assertIn("from dev1", (c / "theirs").read_text())
        self.assertIn("from dev2", (c / "mine").read_text())
        # merged by hand, then forced: deployed, and the conflict files cleared
        f2.write_text('Local string &a = "from both";\n')
        code, out = run("ws", "deploy", str(f2), "--force", cwd=d2)
        self.assertEqual(code, 0, out)
        self.assertIn("from both", (self.db / "PeopleCode" / "JOB.EMPLID.FieldChange").read_text())
        self.assertFalse(c.exists())
        # a brand-new object that someone else already created is a conflict too
        new = d1 / "src" / "SQL" / "HR_NEW.sql"
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text("SELECT 1\n")
        (self.db / "SQL" / "HR_NEW").write_text("SELECT 2\n")
        self.assertEqual(run("ws", "deploy", str(new), cwd=d1)[0], 1)

    def test_secrets_are_masked_and_values_cant_add_words(self):
        d = self.repo("dev1")
        run("ws", "pull", "SQL", "HR_ACTIVE", cwd=d)
        f = d / "src" / "SQL" / "HR_ACTIVE.sql"
        f.write_text("SELECT 2\n")
        code, out = run("ws", "deploy", str(f), cwd=d, env={"LORE_PASSWORD": "wrong-password"})
        self.assertEqual(code, 2)
        self.assertIn("login failed", out)
        self.assertNotIn("wrong-password", out)                                   # masked in the error
        self.assertIn("*****", out)
        ws = hl.Workspace(d)
        ws.cfg["variables"]["db"] = "HRDEV -rm everything; reboot"
        argv = ws.argv("list", type="PeopleCode")
        self.assertEqual(len(argv), 6)                       # a value is one word, however many spaces it has
        self.assertIn("HRDEV -rm everything; reboot", argv)
        with self.assertRaises(hl.LoreError):
            ws.path_for("PeopleCode", "../../etc/passwd")

    def test_the_skill_says_how_and_broken_operations_dont_compile(self):
        lore = hl.Lore(self.lore)
        md = hl.skill_md(lore.meta, True, True)
        self.assertIn("## Working with the code in the database", md)
        self.assertIn("ws deploy FILE", md)
        bad = self.tmp / "bad"
        shutil.copytree(ROOT / "packs" / "jq", bad)
        (bad / "operations.json").write_text(json.dumps({"operations": {
            "pull": {"command": "tool export {nonsense}"}, "explode": {"command": "x"}}}))
        with self.assertRaises(hl.LoreError) as e:
            hl.compile_pack(bad, self.tmp / "bad.lore.db", check=False, log=lambda *_: None)
        self.assertIn("{nonsense}", str(e.exception))
        self.assertIn("'explode'", str(e.exception))


if __name__ == "__main__":
    unittest.main()
