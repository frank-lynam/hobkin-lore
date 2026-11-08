#!/usr/bin/env python3
"""Check harness: runs one Playwright (Python, sync API) example against the fake PIA in fixture/.

The example's code runs with `page` (a fresh Playwright page), `base` (the fake server's URL, e.g.
http://127.0.0.1:PORT), and `expect` (Playwright's assertions). What it prints is its output.
Exit 77 (skip) when Playwright or a Chromium isn't available here: the example is then "unchecked", not failed.
The fixture imitates the structure of PeopleSoft pages (frames, ids, processing, message boxes); it is not
PeopleSoft's markup."""
import http.server
import shutil
import sys
import threading
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixture"
ROUTES = [("/psp/ps/?cmd=login", "signon.html"), ("/psp/ps/EMPLOYEE/HRMS/h/", "homepage.html"),
          ("/psp/", "portal.html"), ("/psc/", "content.html")]

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    print("skipped: Playwright for Python isn't installed here (pip install playwright)")
    sys.exit(77)


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        page = next((f for prefix, f in ROUTES if self.path.startswith(prefix)), None)
        if page is None:
            self.send_error(404)
            return
        raw = (FIXTURE / page).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    code = Path(sys.argv[1]).read_text(encoding="utf-8")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=shutil.which("chromium") or None, args=["--no-sandbox"])
        except Exception as e:  # no browser here
            print(f"skipped: no Chromium for Playwright ({str(e).splitlines()[0]})")
            sys.exit(77)
        page = browser.new_page()
        page.set_default_timeout(10000)
        try:
            exec(compile(code, "example", "exec"), {"page": page, "base": base, "expect": expect})
        finally:
            browser.close()
            srv.shutdown()


if __name__ == "__main__":
    main()
