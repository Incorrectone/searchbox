"""Gemini relay: forwards the API's summary calls to Gemini from a supported country.

Why it exists: the app VM is in Azure East Asia (Hong Kong), and the Gemini API
refuses requests from Hong Kong ("User location is not supported"). This relay
runs on Cloud Run in Mumbai (asia-south1), so Google sees an Indian Google IP.

What it allows, and nothing else:
  POST /v1beta/models/<allowed model>:generateContent   (body <= 64 KB)
  GET  /v1beta/models                                   (list model IDs; for setup)
  GET  /health                                          (no token needed)

Every Gemini call needs the shared relay token in `x-goog-api-key`. The API
already sends its "API key" in that header, so the API needs no code change:
its ApiKey setting becomes the relay token, and the real Gemini key lives only
here (Secret Manager -> env var). A stolen relay token can only make summary
calls to the allowed models; it can't be turned into the Gemini key.

Standard library only: no dependencies to audit or update.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("UPSTREAM", "https://generativelanguage.googleapis.com")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "").strip()
ALLOWED_MODELS = {m.strip() for m in os.environ.get("ALLOWED_MODELS", "").split(",") if m.strip()}
MAX_BODY = 64 * 1024
UPSTREAM_TIMEOUT = 20  # seconds; the API gives up at 25

GENERATE = re.compile(r"^/v1beta/models/([A-Za-z0-9._-]{1,100}):generateContent$")


def token_ok(sent: str | None) -> bool:
    """Constant-time compare, so the token can't be guessed byte by byte from timing."""
    if not RELAY_TOKEN or not sent:
        return False
    return hmac.compare_digest(sent.strip().encode(), RELAY_TOKEN.encode())


def call_upstream(method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(UPSTREAM + path, data=body, method=method)
    req.add_header("x-goog-api-key", GEMINI_API_KEY)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:  # 4xx/5xx from Google: pass status + reason through
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError) as e:
        return 502, json.dumps({"error": {"code": 502, "message": f"relay upstream error: {e}"}}).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "relay"
    sys_version = ""

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, json.dumps({"error": {"code": status, "message": message}}).encode())

    def do_GET(self) -> None:
        if self.path == "/health":
            ready = bool(GEMINI_API_KEY and RELAY_TOKEN and ALLOWED_MODELS)
            self._send(200 if ready else 503, json.dumps({"ok": ready}).encode())
        elif not token_ok(self.headers.get("x-goog-api-key")):
            self._error(401, "bad relay token")
        elif self.path == "/v1beta/models":
            self._send(*call_upstream("GET", "/v1beta/models?pageSize=200", None))
        else:
            self._error(404, "not allowed")

    def do_POST(self) -> None:
        if not token_ok(self.headers.get("x-goog-api-key")):
            return self._error(401, "bad relay token")
        m = GENERATE.match(self.path)  # no query string allowed either
        if not m:
            return self._error(404, "not allowed")
        if m.group(1) not in ALLOWED_MODELS:
            return self._error(403, f"model {m.group(1)} is not allowed")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._error(411, "Content-Length required")
        if not 0 < length <= MAX_BODY:
            return self._error(413, "body too large")
        body = self.rfile.read(length)
        try:
            json.loads(body)
        except ValueError:
            return self._error(400, "body must be JSON")
        self._send(*call_upstream("POST", self.path, body))

    def do_PUT(self) -> None:
        self._error(405, "method not allowed")

    do_DELETE = do_PATCH = do_PUT

    def log_message(self, fmt: str, *args) -> None:
        # One line per request to Cloud Logging; never the token, key or body.
        print(f'{self.command} {self.path.split("?")[0]} -> {args[1] if len(args) > 1 else "?"}', flush=True)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))  # Cloud Run sets PORT
    missing = [n for n, v in [("GEMINI_API_KEY", GEMINI_API_KEY), ("RELAY_TOKEN", RELAY_TOKEN),
                              ("ALLOWED_MODELS", ALLOWED_MODELS)] if not v]
    if missing:
        print(f"WARNING: missing {', '.join(missing)}; every call will fail", flush=True)
    print(f"relay listening on :{port}, models: {sorted(ALLOWED_MODELS)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
