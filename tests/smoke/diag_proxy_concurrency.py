"""D6B-R1 proxy-concurrency reproduction (TEST-ONLY).

One-variable experiment for the proven boundary: an SSE request through the Vite
dev proxy ALONE completes with a terminal frame, but the real browser (which also
issues concurrent /api/health polls through the same proxy) does not receive one.

This script holds one durable SSE submission open through the proxy and, while it
is in flight, fires N concurrent GET /api/health requests through that same proxy.
It reports whether the SSE still received its terminal frame.

--concurrent 0 reproduces the passing control; --concurrent 3 reproduces the
browser-like load. Same instance, same route, same auth: ONE variable changes.

Usage:
    python tests/smoke/diag_proxy_concurrency.py --base http://127.0.0.1:P \
        --origin http://127.0.0.1:P --concurrent 3
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.smoke.helpers import assert_loopback_url  # noqa: E402

USERNAME = "d6b_smoke_user"
PASSWORD = "d6b-smoke-password-1"
TERMINAL = ("complete", "error", "cancelled")


def _opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _cookies(opener) -> dict:
    out = {}
    for h in opener.handlers:
        jar = getattr(h, "cookiejar", None)
        if jar is not None:
            for c in jar:
                out[c.name] = c.value
    return out


def _req(opener, url, *, origin, method="GET", payload=None, headers=None, timeout=60):
    assert_loopback_url(url)
    data = None if payload is None else json.dumps(payload).encode()
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Origin", origin)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with opener.open(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def run(base: str, origin: str, concurrent: int) -> dict:
    out = {"base": base, "concurrent": concurrent}
    op = _opener()
    st, body = _req(op, base + "/api/auth/login", origin=origin, method="POST",
                    payload={"username": USERNAME, "password": PASSWORD})
    if st != 200:
        out["outcome"] = "login_failed"; out["login_status"] = st; return out
    token = json.loads(body)["access_token"]
    csrf = _cookies(op).get("research_assistant_csrf_token") or ""
    st, body = _req(op, base + "/api/sessions", origin=origin, method="POST",
                    payload={"title": "d6b-r1-conc"},
                    headers={"Authorization": "Bearer " + token, "X-CSRF-Token": csrf})
    if st not in (200, 201):
        out["outcome"] = "session_failed"; out["session_status"] = st; return out
    sid = json.loads(body)["id"]

    result = {"terminal": None, "bytes": 0, "events": [], "outcome": "unknown"}

    def sse():
        r = urllib.request.Request(
            "%s/api/sessions/%s/messages/stream" % (base, sid),
            data=json.dumps({"message": "D6B-R1 concurrency probe"}).encode(),
            method="POST")
        r.add_header("Content-Type", "application/json")
        r.add_header("Origin", origin)
        r.add_header("Accept", "text/event-stream")
        r.add_header("Idempotency-Key", str(uuid.uuid4()))
        r.add_header("Authorization", "Bearer " + token)
        if csrf:
            r.add_header("X-CSRF-Token", csrf)
        buf = ""
        try:
            with op.open(r, timeout=90) as resp:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        result["outcome"] = "eof"; break
                    result["bytes"] += len(chunk)
                    buf += chunk.decode("utf-8", "replace")
                    parts = buf.split("\n\n")
                    buf = parts.pop()
                    for raw in parts:
                        for line in raw.split("\n"):
                            if line.startswith("event: "):
                                name = line[7:].strip()
                                result["events"].append(name)
                                if name in TERMINAL:
                                    result["terminal"] = name
        except Exception as exc:  # noqa: BLE001
            result["outcome"] = "exception:" + type(exc).__name__

    t = threading.Thread(target=sse, daemon=True)
    t.start()
    time.sleep(0.25)                       # let the SSE establish upstream
    probe = _opener()
    probe_st, _ = _req(probe, base + "/api/auth/login", origin=origin, method="POST",
                       payload={"username": USERNAME, "password": PASSWORD})
    probe_token = ""
    for _ in range(concurrent):
        _req(probe, base + "/api/health", origin=origin, timeout=20)
    t.join(timeout=90)

    out.update(result)
    out["probe_login_status"] = probe_st
    if result["terminal"] is None and result["outcome"] == "eof":
        out["outcome"] = "EOF_WITHOUT_TERMINAL"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--origin", required=True)
    ap.add_argument("--concurrent", type=int, default=3)
    a = ap.parse_args(argv)
    print(json.dumps(run(a.base, a.origin, a.concurrent), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
