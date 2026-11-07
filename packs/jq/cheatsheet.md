jq essentials
- A program is a filter: JSON in, zero or more JSON values out. `.` is the input.
- `.a.b`, `.[0]`, `.[]` (every element), `.a?` (no error if it doesn't apply).
- `a | b` pipes each output of a into b; `a, b` outputs both; `[f]` collects f's outputs into an array.
- `{name, id: .uid}` builds objects; `{(.k): .v}` computes a key.
- Function arguments are separated by `;` not `,`: `sub("a"; "b")`.
- Only false and null are falsey. `a // b` is a default for null/false.
- `map(f)` = `[.[] | f]`; `select(cond)` filters; `|=` updates in place.
- Output text, not JSON: `jq -r`. Pass shell values with `--arg` (string) or `--argjson` (JSON), never by splicing them into the filter.
- Check a filter quickly: `echo 'JSON' | jq -c 'FILTER'`.
