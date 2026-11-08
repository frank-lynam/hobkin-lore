Write tests in Python with Playwright's sync API (or match the team's existing tests). Before relying on an id
or behaviour from this reference, check it on the real page (codegen, or the page's HTML): PeopleTools versions
differ. When a page doesn't match what this reference says, `note` it with `--on` the entry, so the next lookup
shows the truth for this site. Run each new test at least once and read the trace of any failure before
changing selectors. `files/run_snippet.py` runs a snippet against a small imitation of PIA's structure: useful
for trying a selector idea without a PeopleSoft environment, never a replacement for the real page.
