"""Tests the relay against a fake Gemini. Run: python relay/test_relay.py"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

seen = []


class FakeGemini(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        seen.append((self.path, self.headers.get("x-goog-api-key"), json.loads(body)))
        out = json.dumps({"candidates": [{"content": {"parts": [{"text": "hi [1]"}]}}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        seen.append((self.path, self.headers.get("x-goog-api-key"), None))
        out = b'{"models":[{"name":"models/gemma-4-x"}]}'
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


upstream = serve(FakeGemini)
os.environ.update(UPSTREAM=f"http://127.0.0.1:{upstream.server_port}", GEMINI_API_KEY="REAL-KEY",
                  RELAY_TOKEN="tok123", ALLOWED_MODELS="flash-lite, gemma-4-x")
sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402  (reads env at import)

main.Handler.log_message = lambda *a: None
relay = serve(main.Handler)
BASE = f"http://127.0.0.1:{relay.server_port}"


def req(method, path, body=None, token="tok123", headers=None):
    r = urllib.request.Request(BASE + path, data=body, method=method)
    if token:
        r.add_header("x-goog-api-key", token)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


GOOD = json.dumps({"contents": [{"parts": [{"text": "q"}]}]}).encode()
J = {"Content-Type": "application/json"}
failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += not cond


s, b = req("POST", "/v1beta/models/flash-lite:generateContent", GOOD, headers=J)
check("allowed call is forwarded", s == 200 and b["candidates"][0]["content"]["parts"][0]["text"] == "hi [1]")
check("upstream gets the REAL key, not the relay token", seen[-1][1] == "REAL-KEY")
check("upstream gets the same path and body", seen[-1][0] == "/v1beta/models/flash-lite:generateContent" and seen[-1][2]["contents"])
n = len(seen)
check("wrong token -> 401", req("POST", "/v1beta/models/flash-lite:generateContent", GOOD, token="nope", headers=J)[0] == 401)
check("no token -> 401", req("POST", "/v1beta/models/flash-lite:generateContent", GOOD, token=None, headers=J)[0] == 401)
check("model not on allowlist -> 403", req("POST", "/v1beta/models/gemini-ultra:generateContent", GOOD, headers=J)[0] == 403)
check("other method on model -> 404", req("POST", "/v1beta/models/flash-lite:countTokens", GOOD, headers=J)[0] == 404)
check("query string smuggling -> 404", req("POST", "/v1beta/models/flash-lite:generateContent?key=x", GOOD, headers=J)[0] == 404)
check("path traversal -> 404", req("POST", "/v1beta/models/../files:generateContent", GOOD, headers=J)[0] == 404)
check("non-JSON body -> 400", req("POST", "/v1beta/models/flash-lite:generateContent", b"hello", headers=J)[0] == 400)
check("body over 64 KB -> 413", req("POST", "/v1beta/models/flash-lite:generateContent", b"{" + b" " * 70000 + b"}", headers=J)[0] == 413)
check("DELETE -> 405", req("DELETE", "/v1beta/models/flash-lite")[0] == 405)
check("rejected requests never reach Gemini", len(seen) == n)
s, b = req("GET", "/v1beta/models")
check("model list works with token", s == 200 and b["models"][0]["name"] == "models/gemma-4-x")
check("model list without token -> 401", req("GET", "/v1beta/models", token=None)[0] == 401)
check("health needs no token", req("GET", "/health", token=None) == (200, {"ok": True}))
check("unknown GET -> 404", req("GET", "/v1beta/files")[0] == 404)

print("all passed" if not failures else f"{failures} failed")
sys.exit(1 if failures else 0)
