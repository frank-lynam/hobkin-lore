# hobkin-lore

Reference packs ("lores") for programming languages and their tools, that an AI agent looks things up in
instead of holding them in context. A whole language reference, every function, class and keyword, worked
examples, and the documentation of its command-line tools: far more than fits in a context window, searchable
in a few hundred tokens a lookup.

A lore is one SQLite file. Installed, it's a skill folder that both
[hobkin](https://github.com/frank-lynam/hobkin) and [Claude Code](https://code.claude.com) read as it is:

```text
jq-lore/
  SKILL.md       when to use it, and how (name and description in front matter)
  lore.py        the lookup tool: one stdlib-only Python file
  jq.lore.db     the lore
  jq.notes.db    what agents learned using it (made on the first note; kept across reinstalls)
```

One file, standard library only, Python 3.9+. No services, no embeddings, no network at lookup time.

## Use a lore

```sh
pip install hobkin-lore
hobkin-lore compile packs/jq                  # a pack source (below) into jq.lore.db
hobkin-lore install jq.lore.db                # as a skill for every hobkin (~/.hobkins/_shared/skills/)
hobkin-lore install jq.lore.db --claude       # for Claude Code (~/.claude/skills/)
hobkin-lore install jq.lore.db --project .    # for one project's Claude Code (.claude/skills/)
hobkin-lore install jq.lore.db --to DIR       # any skills folder
```

The agent sees the skill's one-line description in its list of skills and opens it when a task touches the
language. From then on it looks things up:

```text
$ python3 jq-lore/lore.py symbol map
map  (function)
  map(f)
Runs f on every element of an array and collects the results: same as `[.[] | f]`.
On an object it maps the values and returns an array; use `map_values` to keep an object.
see: map_values, select
examples: map-select (Keep some elements), group-count (Count per group)  ->  examples --symbol map
```

| Command | What it gives |
| --- | --- |
| `info` | What the lore covers, its counts, where it came from |
| `cheatsheet` | The short page an agent reads once per task |
| `toc [ID]` | The outline, or one branch of it |
| `search WORDS [--kind K]` | Ranked hits with snippets (sections, symbols, tools, examples, notes); a word that's exactly a symbol's name puts that symbol first |
| `symbol NAME` | One function, class, keyword, operator...: signature, behaviour, see-also, examples, notes; a near miss suggests the right name |
| `show ID [--from N]` | A section, 80 lines at a time |
| `examples [WORDS] [--symbol NAME] [--id ID]` | Worked examples, each marked whether the language's own tool ran it as shown |
| `tools`, `tool NAME` | The language's command-line tools: purpose, usage, flags, real runs |
| `note TEXT [--on NAME]` | Keep something learned the hard way; it shows on that entry and in search from then on |
| `notes [WORDS]` | What's been noted |

Every output is short and paged: a lookup can't flood an agent's context. Outside a skill folder, point it at a
lore with `--lore FILE` or `LORE=FILE`.

## Make a lore: the pack source

A lore is compiled from a pack source: a folder of plain files you can read, review and keep in git.
[`packs/jq`](packs/jq) is a small hand-written one.

| File | What it holds |
| --- | --- |
| `pack.json` | `name` (it names the skill: `NAME-lore`), `title`, `description`, `language`, `version`, `sources` (where it came from), `check` (below) |
| `sections.jsonl` | The reference's text, one section per line: `id`, `parent`, `title`, `body` (Markdown), `source` |
| `symbols.jsonl` | One per function, class, keyword...: `name`, `kind` (function, method, class, property, event, keyword, operator, statement, type, variable, constant, directive, builtin, other), `signature`, `summary`, `body`, `section`, `source`, `see` |
| `examples.jsonl` | `id`, `title`, `code`, `input` (stdin), `expect` (its output), `notes`, `symbols` (which it shows) |
| `tools.jsonl` | One per command-line tool: `name`, `purpose`, `usage`, `body`, `flags` (`flag`, `meaning`), `runs` (`command`, `output`) |
| `cheatsheet.md` | The essentials: a screenful at most |
| `skill.md` | How to work in this language (testing, conventions), added to the skill's instructions |

`source` on every section and symbol says where in the original documentation it came from, so a reviewer can
check it.

**Checked examples.** With `check` in `pack.json`, `compile` runs every example through the language's own
tool and records whether it ran as shown: `{"command": "jq -c -f {file}", "ext": ".jq"}` writes the example's
code to `{file}`, feeds its `input` on stdin, and compares the output with `expect`. A failing example fails the
compile (exit 1) and is marked as failed in the lore, so an agent knows not to trust it. `--no-check` skips
this, for instance where the tool isn't installed.

**One schema at a time.** A lore records its schema version, and `lore.py` refuses a lore of another version
with "rebuild it": lores are built artifacts, rebuilt from their source, never converted.

## Next: building lores from any documentation

Hand-writing a pack is fine for a small language; a real one (PeopleCode, a vendor's DSL) has thousands of
pages. Next comes `hobkin-lore build`: gather the sources (PDF, HTML, man pages, each tool's `--help` and real
runs of it), have an LLM distill them into a pack source, every entry citing the source passage it came from,
check those citations and the examples, and record every model answer so a rebuild is reproducible and free.
The pack source it writes is the same format as above, so a person can review and fix it before compiling.

## Tests

```sh
python3 -m unittest discover -v tests
```

## Releasing

Releases go to PyPI from GitHub Actions with Trusted Publishing (no token stored), the same way as hobkin:
bump `__version__` in `hobkin_lore.py`, commit and push, then publish a GitHub release tagged `vX.Y.Z` to match.
The workflow tests on Python 3.9, 3.12 and 3.14, checks the tag, builds and uploads. Running it by hand
(Actions → Publish → Run workflow) goes to TestPyPI instead.
