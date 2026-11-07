# Working on hobkin-lore

- **Finding your way:** `grep -n '^# ----' hobkin_lore.py` lists its sections (format, compile, lookup, notes,
  install, command line); the map near the top says what each holds. Keep the map in step.
- **One file, standard library only:** `hobkin_lore.py`, Python 3.9+ (no `match`, no `X | Y` outside
  annotations, no f-string quote nesting from 3.12). The same file is copied into every installed skill as
  `lore.py`, so the lookup side must never need anything but the standard library and the lore beside it.
  The build pipeline (next) may call a model over HTTP with urllib, as hobkin does; no SDKs.
- **Claude Code compatibility is a design rule.** An installed lore is a plain skill folder: `SKILL.md` with
  `name` and `description` in YAML front matter (the description quoted: it contains colons), `lore.py`, the
  `.lore.db`. Nothing hobkin-specific in it; hobkin reads the same format.
- **Lores are built artifacts.** A schema change bumps `SCHEMA_VERSION`, and lores of another version are
  rebuilt from their pack source, never converted. No migration code.
- **A lore is read-only at lookup time.** What agents learn goes in the notes file beside it
  (`NAME.notes.db`), which a reinstall or rebuild never touches.
- **Output is bounded.** Every lookup command prints a short, paged answer; nothing dumps a whole table.
- **Tests:** `python3 -m unittest discover -v tests` (they compile `packs/jq` and run its examples through jq
  when it's installed). Add a test for every behaviour you change.
- GitHub: `frank-lynam/hobkin-lore` (remote `origin` uses the `github-hobkin-lore` SSH alias). Commit and push
  finished work.
