PeopleSoft pages with Playwright: essentials (verify ids on your PeopleTools version)
- Classic components live in the iframe `#ptifrmtgtframe` (TargetContent): `f = page.frame_locator("#ptifrmtgtframe")`.
- Or skip the frame: open the component's `/psc/...` URL instead of `/psp/...`.
- Field ids are `RECORD_FIELD$row` (`JOB_DEPTID$1`). `$` breaks `#id` CSS: use `[id='JOB_DEPTID$1']`.
- Toolbar and message ids start with `#`: `[id='#ICSave']`, `[id='#ICList']`, `[id='#ICSearch']`, `[id='#ICOK']`.
- PeopleCode runs when a field changes and loses focus: `fill`, then `press("Tab")`, then wait.
- Wait for the processing indicator to hide (`#WAIT_win0` in classic pages), never a fixed sleep; then assert with `expect` (it retries).
- Message boxes may be in a modal iframe (`#ptModFrame_0`): read the text, then click OK.
- Sign in once (`#userid`, `#pwd`) and reuse `storage_state`; credentials from environment variables.
- Dev or test environments only. Traces in CI; `playwright show-trace` on failure; `playwright codegen` on the `/psc/` URL.
