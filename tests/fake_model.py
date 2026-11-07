"""A fake OpenAI-format model for the build tests: it 'reads' the passage it's sent and answers with entries
derived from it (### headings become symbols, ## headings sections, code blocks examples, tool help a tool),
plus one entry whose quote isn't in the passage, so verification has something to flag."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def answer(system: str, user: str) -> dict:
    if "two short texts" in system:
        return {"description": "Toylang, a tiny language for tests.", "cheatsheet": "- strings use double quotes",
                "skill": "Run code with toyrun."}
    passage = user.split("Passage:\n", 1)[1]
    lines = passage.splitlines()
    out = {"sections": [], "symbols": [], "examples": [], "tools": []}
    for i, line in enumerate(lines):
        nxt = next((l for l in lines[i + 1:] if l.strip() and not l.startswith("#")), "")
        if line.startswith("### "):
            name = line[4:].split("(")[0].strip()
            out["symbols"].append({"name": name, "kind": "function", "signature": line[4:].strip(),
                                   "summary": nxt.strip(), "body": nxt.strip(), "see": [], "quote": nxt.strip()})
        elif line.startswith("## "):
            out["sections"].append({"title": line[3:].strip(), "parent": None, "body": nxt.strip(),
                                    "quote": nxt.strip()})
    for code in re.findall(r"```\n(.*?)\n```", passage, re.S):
        out["examples"].append({"title": f"example {code[:12]}", "code": code.strip(), "symbols": ["upper"],
                                "quote": code.strip()})
    if "usage: toyrun" in passage:
        out["tools"].append({"name": "toyrun", "purpose": "runs toylang", "usage": "toyrun FILE",
                             "flags": [{"flag": "-v", "meaning": "verbose"}], "quote": "usage: toyrun FILE [-v]"})
    out["symbols"].append({"name": "invented", "kind": "function", "summary": "made up",
                           "quote": "this sentence appears nowhere in the documentation at all"})
    return out


class FakeModel:
    def __init__(self):
        self.requests = []
        fm = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fm.requests.append(body)
                msgs = body["messages"]
                content = json.dumps(answer(msgs[0]["content"], msgs[-1]["content"]))
                raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
