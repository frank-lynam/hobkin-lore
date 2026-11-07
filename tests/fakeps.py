#!/usr/bin/env python3
"""A fake PeopleSoft-style CLI for the workspace tests: a "database" that is a folder ($FAKEDB/DB/TYPE/NAME),
with list, project, export, import (password-protected), validate and compile."""
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
cmd, opts = args[0], dict(zip(args[1::2], args[2::2]))
root = Path(os.environ["FAKEDB"]) / opts.get("-db", "?")
if cmd == "list":
    for f in sorted((root / opts["-type"]).glob("*")):
        print(f"{opts['-type']} {f.name}")
elif cmd == "project":
    print((root / "projects" / f"{opts['-name']}.txt").read_text(), end="")
elif cmd == "export":
    src = root / opts["-type"] / opts["-name"]
    if not src.exists():
        print(f"error: object not found: {opts['-type']} {opts['-name']}", file=sys.stderr)
        sys.exit(2)
    shutil.copyfile(src, opts["-out"])
elif cmd == "import":
    if opts.get("-pw") != "s3cret":
        print(f"error: login failed with password {opts.get('-pw')}", file=sys.stderr)
        sys.exit(3)
    (root / opts["-type"]).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(opts["-in"], root / opts["-type"] / opts["-name"])
elif cmd == "validate":
    if "SYNTAX ERROR" in Path(opts["-in"]).read_text():
        print(f"{opts['-in']}: line 1: syntax error", file=sys.stderr)
        sys.exit(1)
    print("valid")
elif cmd == "compile":
    print(f"compiled {opts['-type']} {opts['-name']}")
else:
    print("usage: fakeps list|project|export|import|validate|compile ...", file=sys.stderr)
    sys.exit(64)
