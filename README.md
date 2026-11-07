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

For VS Code's Copilot, Cursor or Claude Desktop, see [MCP](#in-vs-code-cursor-claude-desktop-and-other-mcp-clients) below.

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

## In VS Code, Cursor, Claude Desktop and other MCP clients

Claude Code (in the terminal or in VS Code) and hobkin use the skill folder directly. For any other AI tool
that speaks [MCP](https://modelcontextprotocol.io), a lore is also an MCP server over stdio: the same lookups
as tools (`cheatsheet`, `search`, `symbol`, `toc`, `show`, `examples`, `tools`, `tool`, `note`, `info`). An
installed skill's own `lore.py` serves it, so the editor needs nothing installed:

```sh
python3 ~/.claude/skills/jq-lore/lore.py mcp          # its own lore
hobkin-lore mcp jq.lore.db peoplecode.lore.db          # several: each tool then takes `lore`
```

VS Code (`.vscode/mcp.json` in a workspace, or **MCP: Open User Configuration**):

```json
{
  "servers": {
    "jq-lore": {"type": "stdio", "command": "python3", "args": ["/home/you/.claude/skills/jq-lore/lore.py", "mcp"]}
  }
}
```

Cursor (`.cursor/mcp.json`) and Claude Desktop (`claude_desktop_config.json`) take the same command under
`mcpServers`:

```json
{
  "mcpServers": {
    "jq-lore": {"command": "python3", "args": ["/home/you/.claude/skills/jq-lore/lore.py", "mcp"]}
  }
}
```

The server tells the model how to use it (read the cheatsheet once, look up what it isn't sure of), and stdout
carries nothing but the protocol. Notes an agent makes through MCP land in the same notes file as everyone
else's.

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

## Build a lore from any documentation

Hand-writing a pack is fine for a small language; a real one (PeopleCode, a vendor's DSL) has thousands of
pages. `hobkin-lore build` has a model write the pack source from the documentation itself:

```sh
export LORE_API_KEY=...            # any OpenAI-format endpoint: OpenRouter, a gateway, Ollama, vLLM...
hobkin-lore build jq https://jqlang.org/manual/v1.7/ --tool jq --version 1.7 \
    --check 'jq -c -f {file}' --check-ext .jq --check-compare json \
    --base-url https://openrouter.ai/api/v1 --model nvidia/nemotron-3-ultra-550b-a55b
hobkin-lore review packs/jq          # check what it made, in the browser
hobkin-lore compile packs/jq         # then compile and install as usual
```

1. **Gather.** PDFs (with `pdftotext`), HTML (files, folders or URLs; navigation and scripts dropped,
   headings and code kept), Markdown and text, scripts that use the tools (`.sh`, `.dms`, `.sql`, `.ps1`,
   `.bat`, logs and captured terminal sessions: how a team really uses them), and for each `--tool`, its help
   and man page. Each becomes a numbered text source in `packs/NAME/.build/sources/`.

   **Tools nobody outside your network has seen.** `--tool` tries the help flags of every era (`--help`, `-h`,
   `-help`, `-H`, `-?`, `help`, `/?`), then has the model read that help and propose the help pages of its
   subcommands, two levels down (`--help-limit`, default 60 pages). Plain code runs a proposal only if it asks
   for help (subcommand words, then a help flag, or `help WORD`), so a build never lists, exports, connects or
   changes anything, whatever the model suggests. Tools always run with no stdin, so one waiting for a login
   just gets end-of-file.
2. **Chunk** at headings into pieces a model reads whole.
3. **Distill.** Each chunk goes to the model, which writes sections, symbols, examples and tools in its own
   words, each with a short quote from the passage it rests on (`--jobs` at a time).
4. **Verify,** in plain code: each quote is looked up in its chunk (whitespace, case, Markdown marks and a
   shortening "..." aside). Found, it becomes a citation (`manual, lines 1843-1844`); not found, the entry is
   flagged for review. Duplicates (a manual and its man page) are merged, and an example is tied only to the
   symbols its code really uses.
5. **Finish.** The model writes the description, the cheatsheet and the skill text from the merged entries.

Every model answer is recorded in `.build/answers/`, keyed by the prompt and model: a rebuild asks again only
about what changed. A reviewer's decisions outlive a rebuild: entries accepted, edited or rejected stay as the
reviewer left them, and a hand-edited cheatsheet or skill text is kept. `--limit N` tries a build on the first
N chunks. With `--check`, `compile` runs the examples; `--check-compare json` compares their output as JSON
values (spacing aside), `words` ignoring spacing, `exact` (the default) as text.

For a sense of scale: built from jq's online manual and its man page, with Nemotron 3 Ultra, it made 236
sections, 178 symbols and 280 examples (252 of them run as documented) in 23 chunks, for under a dollar, with
48 entries flagged for review. `packs/jq` is the hand-written comparison: 40 symbols, 12 examples.

## Review

`hobkin-lore review packs/NAME` serves a page on `127.0.0.1:8765` (local only, and changes need a token
embedded in the page). Each entry is shown beside the source lines it cites, its quote highlighted; flagged
and unreviewed entries come first. Accept, reject, or edit and save: the pack source's JSONL is updated in
place, and `compile` leaves rejected entries out. Examples can be run through the pack's check command, and
the cheatsheet and skill text edited, from the same page.

## Tests

```sh
python3 -m unittest discover -v tests
```

## Releasing

Releases go to PyPI from GitHub Actions with Trusted Publishing (no token stored), the same way as hobkin:
bump `__version__` in `hobkin_lore.py`, commit and push, then publish a GitHub release tagged `vX.Y.Z` to match.
The workflow tests on Python 3.9, 3.12 and 3.14, checks the tag, builds and uploads. Running it by hand
(Actions → Publish → Run workflow) goes to TestPyPI instead.
