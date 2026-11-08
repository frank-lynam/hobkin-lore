# hobkin-lore runbook: from documentation and tools to a working PeopleSoft workspace

This is a step-by-step procedure for an AI agent (or a person) setting hobkin-lore up at a site. Follow the
steps in order. Each step says what to run, what success looks like, and when to stop and ask a person.
Commands are for a Linux shell with Python 3.9 or newer.

Words used here:

- **Lore**: one `.lore.db` file holding a reference (a language, or a set of command-line tools).
- **Pack source**: the folder of plain files a lore is compiled from (`packs/NAME/`). `build` writes it,
  a person reviews it, `compile` turns it into the lore.
- **Skill folder**: an installed lore (`SKILL.md`, `lore.py`, the `.lore.db`), which Claude Code and hobkin read.
- **Operations**: the command templates in a tools lore that pull and deploy code (`operations.json`).
- **Workspace**: a git repo where developers work on code that lives in the database (`src/TYPE/NAME.ext`).

## Rules that always apply

1. **Never point a workspace at a production database.** Only development databases. If you are not sure which
   database a name refers to, stop and ask.
2. **Never put a password in a file.** Passwords go in environment variables only (`LORE_PASSWORD` and the like).
   Never print them, never commit them.
3. **Never run `ws deploy --force` on your own.** `--force` is only for after a person has merged a conflict.
4. **Never edit `.lore.db` files.** Change the pack source and compile again.
5. **When a step's "success looks like" doesn't match, stop** and report what you saw. Don't improvise around it.

## Step 0: install and check

```sh
pip install hobkin-lore            # or: pip install --user hobkin-lore
hobkin-lore --version              # success: hobkin-lore 0.2.0 (or newer)
```

Check it works offline with the sample that ships in the source package:

```sh
pip download --no-deps --no-binary :all: hobkin-lore -d /tmp/hl && tar xzf /tmp/hl/hobkin_lore-*.tar.gz -C /tmp/hl
hobkin-lore compile /tmp/hl/hobkin_lore-*/packs/jq --no-check -o /tmp/hl/jq.lore.db
hobkin-lore --lore /tmp/hl/jq.lore.db symbol map
```

Success: `compile` prints `9 sections, 40 symbols, 1 tools, 12 examples` and `symbol map` prints `map(f)` and a
description. If `compile` says the SQLite can't build a lore (no FTS5), stop: this Python can't be used.

## Step 1: the model

`build` needs an OpenAI-format chat endpoint (the site's model gateway). Set three variables:

```sh
export LORE_BASE_URL=https://GATEWAY/v1      # up to /v1, without /chat/completions
export LORE_MODEL=MODEL-NAME                  # the model name the gateway expects
export LORE_API_KEY=...                       # your key (never written to any file)
```

Check it answers: run Step 2 with `--limit 2` first (two chunks only). Success: it ends with
`... model calls` and no `model call failed`. If it fails with HTTP 401/403, the key is wrong; with 404, the
URL is wrong (it must end in `/v1` or whatever comes before `/chat/completions`). Stop and ask if unsure.

## Step 2: the PeopleCode lore (the language)

Gather the PeopleCode documentation (PeopleBooks: the PeopleCode Language Reference, PeopleCode API Reference,
PeopleCode Developer's Guide) as HTML folders or PDFs, in one folder, for example `~/docs/peoplebooks/`.

```sh
hobkin-lore build peoplecode ~/docs/peoplebooks/ --title PeopleCode --version "8.60" --out packs/peoplecode --jobs 4
```

Use the PeopleTools version the documentation is for. A big documentation set takes a while (each chunk is
one model call); the run prints progress. **If it stops part way** (network, gateway limits), run the same
command again: answers already received are recorded and not asked again.

Success: the last line is like
`packs/peoplecode: N sources, N chunks -> N sections, N symbols, N examples, N tools; N flagged for review`.

## Step 3: the tools lore (the CLI, Data Mover, SQL*Plus)

One lore for all the tools developers use with the database, built from their help and from the team's
existing scripts. Put copies of scripts that use the tools (`.sh`, `.dms`, `.sql`, captured terminal sessions
as `.log` or `.transcript`) in a folder, for example `~/docs/ps-scripts/`. **Remove any passwords from them
first.**

```sh
hobkin-lore build pstools ~/docs/ps-scripts/ --tool "THE-CLI-COMMAND" --tool "psdmtx" --tool "sqlplus" \
    --title "PeopleSoft tools" --out packs/pstools --help-limit 120
```

Replace `THE-CLI-COMMAND` with the command developers run for Application Designer operations (and add a
`--tool` for each other command they use). `build` only ever runs help requests (`--help`, `-h`, `-help`,
`-?`, `help WORD` and the like); it never runs an operation, never connects to a database, and gives tools no
input. Lines like `not running '...' (not a help request)` are normal.

Success: the summary line shows `N tools` greater than 0 and the line
`operations drafted: list, pull, deploy, ...`. If it says `operations drafted, with problems to fix in
review`, continue: Step 4 fixes them. If it shows `0 tools`, the help wasn't found: check the command name
runs (`THE-CLI-COMMAND -help`), and stop and ask if it doesn't.

## Step 4: review (a person does this, with you)

```sh
hobkin-lore review packs/pstools          # then open http://127.0.0.1:8765/ in a browser
```

The page lists every entry beside the source text it came from. **Flagged** entries (their quote wasn't
found in the source) come first. For each: accept it, fix it and save, or reject it. Then open
**texts → operations.json** and check every command with someone who uses the tools daily:

- Each operation is one command with `{placeholders}`: `{type}`, `{name}`, `{file}`, `{dir}`, `{project}`,
  plus the site values under `variables` (database name...) and passwords under `secrets`.
- `list` and `project_list` need `parse`: a regular expression that matches one line of the command's
  output, with `(?P<name>...)` and, if the line shows it, `(?P<type>...)`.
- `pull` writes the object to `{file}` (or prints it, with `"output": "stdout"`). If the tool's message for
  "no such object" doesn't say "not found" or similar, add `"missing": "its wording"`.
- Delete any line `"problems"` once they're fixed. The page refuses to save operations that don't hold
  together, and says why.

Do the same for `packs/peoplecode` (no operations there). Reviewing everything at once isn't required:
at minimum, review all flagged entries and the operations.

## Step 5: compile and install

```sh
hobkin-lore compile packs/peoplecode
hobkin-lore compile packs/pstools --no-check
hobkin-lore install peoplecode.lore.db --claude      # ~/.claude/skills/peoplecode-lore/
hobkin-lore install pstools.lore.db --claude         # ~/.claude/skills/pstools-lore/
```

Success: each `compile` prints its counts; each `install` prints `installed: ...`. If `compile` lists
"problems" with `operations.json`, go back to Step 4. Installing again later (after a rebuild) keeps the
notes agents have made.

For VS Code's Copilot, Cursor or Claude Desktop instead of Claude Code, use the MCP server: see "In VS Code,
Cursor, Claude Desktop" in the README.

## Step 6: the workspace (a git repo for the code)

In the team's repo (a new one is fine):

```sh
cd ~/repos/peoplesoft
python3 ~/.claude/skills/pstools-lore/lore.py ws init
```

Success: `workspace set up: .../.lore-workspace/config.json`, then what to fill in. Edit
`.lore-workspace/config.json`:

- `variables`: the **development** database name and any other site values. (Rule 1.)
- `secrets`: leave as is. It names the environment variables to set, e.g. `LORE_PASSWORD`. Set them in
  your shell, not in any file.
- `operations`: leave empty unless this site needs a command different from the lore's; anything here
  overrides the lore's operation of the same name.

Then try the operations. **This changes nothing in the database:**

```sh
export LORE_PASSWORD=...                      # if the operations use {password}
python3 ~/.claude/skills/pstools-lore/lore.py ws test --type PeopleCode
```

Success: three lines ending in `ok`: `list ...: N objects`, `pull ...: ok, N bytes`, `validate: ok`. If one
says FAILED, the message says which command and what the tool printed. Fix that operation (Step 4, or the
config's `operations`) and test again. Don't continue until `ws test` passes.

Commit the workspace setup (`.lore-workspace/config.json`, `.gitignore`, `.vscode/tasks.json`). The
manifest, base copies and conflicts are per developer and are ignored by git on purpose.

## Step 7: daily work

All commands run inside the repo (`lore.py` is `~/.claude/skills/pstools-lore/lore.py`; VS Code has the same
commands under **Terminal → Run Task**: `lore: ...`).

| To | Run |
| --- | --- |
| See what changed here | `lore.py ws status` |
| ...and in the database since your pull | `lore.py ws status --remote` |
| Get objects | `lore.py ws pull TYPE NAME`, `--type TYPE`, `--project PROJECT`, `--file src/...` |
| Check a file | `lore.py ws validate src/...` |
| Put a file in the dev database | `lore.py ws deploy src/... --compile` |

Then commit with git as usual. When writing PeopleCode, look things up in the PeopleCode lore first
(`python3 ~/.claude/skills/peoplecode-lore/lore.py symbol NAME`); Claude Code does this on its own once the
skills are installed.

## Step 8 (optional): testing PeopleSoft pages with Playwright

hobkin-lore comes with a starter lore for writing Playwright (Python) tests of PeopleSoft pages: how pages are
framed, which ids to use, how to wait for the server. It's in the source package.

```sh
pip download --no-deps --no-binary :all: hobkin-lore -d /tmp/hl && tar xzf /tmp/hl/hobkin_lore-*.tar.gz -C /tmp/hl
cp -r /tmp/hl/hobkin_lore-*/packs/peoplesoft-pia packs/peoplesoft-pia      # keep it with the other packs
hobkin-lore compile packs/peoplesoft-pia
hobkin-lore install peoplesoft-pia.lore.db --claude
```

Success: `compile` prints `8 examples` with `8 pass` (when Playwright for Python and Chromium are installed)
or `8 unchecked` (when they aren't; that's fine). Never `fail`.

Then make it match this site:

1. With a developer, open a real classic component and a real Fluid page and compare with the lore
   (`lore.py show pia/ids`, `pia/toolbar`, `pia/roundtrips`): the frame id, the processing indicator, the
   toolbar ids, the message box. Where this site differs, `lore.py note "what it really is" --on ENTRY`
   (it shows up in every later lookup), or fix the entry in `packs/peoplesoft-pia/` and compile again.
2. If the team has existing Playwright (or Selenium) tests of PeopleSoft pages, add them:
   `hobkin-lore build peoplesoft-pia ~/repos/ps-tests/ --out packs/peoplesoft-pia`. The build adds what it
   learns from them; everything already in the pack (written by hand or reviewed) is kept as it is. Then
   review (Step 4) and compile.
3. Tests run against a development or test environment, never production (Rule 1), with credentials from
   environment variables (Rule 2).

## When a deploy says CONFLICT

Someone else deployed the same object since your pull. Nothing was overwritten. The message shows a command
like:

```text
code --merge .lore-workspace/conflicts/src/X/mine .lore-workspace/conflicts/src/X/theirs .lore-workspace/conflicts/src/X/base src/X
```

Run it (or show it to the developer): VS Code opens a three-way merge. **A person decides the merge** (Rule 3).
When the file in `src/` holds the merged result, run `lore.py ws deploy src/X --force`.

## Troubleshooting

| You see | It means | Do |
| --- | --- | --- |
| `not in a workspace` | You're not inside the repo | `cd` into it |
| `no value for {db}` | A variable isn't filled in | Fill it in `.lore-workspace/config.json` |
| `no value for {password}` | The secret's environment variable isn't set | `export LORE_PASSWORD=...` |
| `... isn't installed (or isn't on PATH)` | The tool can't be found | Check the command runs in this shell |
| `... failed (exit N): ...` | The tool reported an error | Read the tool's message; the tools lore explains its errors (`lore.py tool NAME`) |
| `kept (changed here) N` on a pull | Files you changed weren't overwritten | Deploy them first, or `pull --force` to drop your changes |
| `the operations don't hold together` | operations.json is broken | Step 4 |
| `this lore has no 'X' operation` | The tools lore doesn't know how to do X | Add it in Step 4, or in the config's `operations` |
| Every pull of a new object is a CONFLICT | `pull` doesn't recognise "no such object" | Add `"missing": "the tool's wording"` to the pull operation |
| `model call failed: HTTP 429` | Gateway rate limit | Use `--jobs 1`, run again later (nothing is lost) |

## Where things are

- `packs/NAME/`: the pack source (commit it: it's how the lore is rebuilt). `packs/NAME/.build/answers/` holds
  the recorded model answers (commit them too, so a rebuild is free); `.build/sources/` holds the extracted
  documentation text (commit only if the documentation's licence allows).
- `NAME.lore.db`: a compiled lore (not committed; rebuild with `compile`).
- `~/.claude/skills/NAME-lore/`: an installed lore. `NAME.notes.db` beside it: notes agents made.
- `.lore-workspace/`: the workspace's config (committed) and per-developer state (not committed).
