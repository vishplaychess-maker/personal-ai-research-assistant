"""D6B-R1 diagnostic SSE client (TEST-ONLY).

Performs ONE real durable plain-chat submission through the real routes and
records bounded wire metadata for the SSE boundary trace:

- HTTP status, content-type, content-length if present
- byte count and timestamp of every body chunk
- SSE event name + id per frame (never token text; only a bounded length)
- whether a terminal frame (complete/error/cancelled) arrived
- EOF vs error, and elapsed time

It records NO Authorization header, cookie, CSRF value, JWT, prompt text or
response text. The assistant text is reduced to a length only.

Usage:
    python tests/smoke/diag_client.py --base http://127.0.0.1:PORT \
        --origin http://127.0.0.1:VITEPORT --label direct
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import sys
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

FRAME_RE = re.compile(r"(?:^|\n)(event|id|data): ?([^\n]*)")
TERMINAL = ("complete", "error", "cancelled")


def _opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _cookies(opener) -> dict:
    out: dict = {}
    for cj in opener.handlers:
        jar = getattr(cj, "cookiejar", None)
        if jar is None:
            continue
        for c in jar:
            out[c.name] = c.value
    return out


def _json(opener, url: str, *, origin: str, method: str = "GET", payload=None,
          headers=None, timeout: float = 30.0):
    assert_loopback_url(url)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Origin", origin)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"detail": body[:200]}


def run(base: str, origin: str, label: str, timeout_s: float = 90.0) -> dict:
    report: dict = {"label": label, "base": base, "events": [], "chunks": 0,
                    "bytes": 0, "terminal": None, "outcome": "unknown"}
    opener = _opener()

    status, body = _json(opener, base + "/api/auth/login", origin=origin,
                         method="POST", payload={"username": USERNAME,
                                                "password": PASSWORD})
    report["login_status"] = status
    token = (body or {}).get("access_token")
    if status != 200 or not token:
        report["outcome"] = "login_failed"
        return report

    ck = _cookies(opener)
    csrf = ck.get("research_assistant_csrf_token")
    report["has_refresh_cookie"] = "research_assistant_refresh_token" in ck
    report["has_csrf_cookie"] = csrf is not None

    status, sess = _json(opener, base + "/api/sessions", origin=origin,
                         method="POST", payload={"title": "d6b-r1-diag"},
                         headers={"Authorization": "Bearer " + token,
                                  "X-CSRF-Token": csrf or ""})
    report["session_status"] = status
    sid = (sess or {}).get("id")
    if not sid:
        report["outcome"] = "session_failed"
        return report

    idem = str(uuid.uuid4())
    prompt = "D6B-R1 diagnostic probe"
    start = time.monotonic()
    req = urllib.request.Request(
        "%s/api/sessions/%s/messages/stream" % (base, sid),
        data=json.dumps({"message": prompt}).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", origin)
    req.add_header("Accept", "text/event-stream")
    req.add_header("Idempotency-Key", idem)
    req.add_header("Authorization", "Bearer " + token)
    if csrf:
        req.add_header("X-CSRF-Token", csrf)

    buf = ""
    terminal_seen = None
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            report["stream_status"] = resp.status
            report["content_type"] = resp.headers.get("Content-Type")
            report["x_run_id_present"] = bool(resp.headers.get("X-Run-Id"))
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    report["outcome"] = "eof"
                    break
                report["chunks"] += 1
                report["bytes"] += len(chunk)
                buf += chunk.decode("utf-8", "replace")
                parts = buf.split("\n\n")
                buf = parts.pop()
                for raw in parts:
                    ev = {"event": "", "id": None, "data_len": 0}
                    for m in FRAME_RE.finditer(raw):
                        f, v = m.group(1), m.group(2)
                        if f == "event":
                            ev["event"] = v
                        elif f == "id":
                            ev["id"] = v
                        elif f == "data":
                            ev["data_len"] += len(v)
                    if ev["event"] or ev["data_len"]:
                        report["events"].append(ev)
                    if ev["event"] in TERMINAL:
                        terminal_seen = ev["event"]
                        report["terminal"] = ev["event"]
    except Exception as exc:  # noqa: BLE001 - bounded, no message text
        report["outcome"] = "exception:" + type(exc).__name__

    report["elapsed_ms"] = int((time.monotonic() - start) * 1000)
    report["event_names"] = [e["event"] for e in report["events"]]
    report["token_frames"] = sum(1 for e in report["events"] if e["event"] == "token")
    report["token_data_bytes"] = sum(e["data_len"] for e in report["events"]
                                      if e["event"] == "token")
    if terminal_seen is None and report["outcome"] == "eof":
        report["outcome"] = "EOF_WITHOUT_TERMINAL"
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="D6B-R1 diagnostic SSE client")
    ap.add_argument("--base", required=True)
    ap.add_argument("--origin", required=True)
    ap.add_argument("--label", default="diag")
    args = ap.parse_args(argv)
    report = run(args.base, args.origin, args.label)
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
