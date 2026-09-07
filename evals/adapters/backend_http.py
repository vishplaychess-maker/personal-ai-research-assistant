"""Running-backend HTTP adapter (Gate B/C).

Drives the ACTUAL user-facing path:
  POST /api/auth/register  -> POST /api/auth/login (JWT + double-submit CSRF)
  -> POST /api/sessions     -> POST /api/sessions/{id}/messages/stream (SSE)

Route names and the auth/CSRF contract are taken from the repository
(routes/auth.py, routes/sessions.py, routes/messages.py, cookie_service).

Safety model (default-closed — this is the only eval code that can register a
user and make a live model call)
--------------------------------------------------------------------------
`_authorise_target()` must pass before anything is sent:

* `EVAL_BACKEND_URL` must be set and use an http/https scheme. Any other
  scheme (file:, ftp:, gopher:, ...) is refused.
* The target is classified local vs remote by resolving its host the same way
  `app.services.browser_agent.assert_url_allowed` does (loopback IPs only ->
  local). Localhost / loopback / a host named in `EVAL_BACKEND_ALLOW_HOSTS`
  is permitted by default.
* A NON-LOCAL target requires `EVAL_ALLOW_REMOTE_BACKEND=true` AND is then
  additionally run through the repo SSRF guard (`assert_url_allowed`, guarded
  import) so a remote target still cannot be a private / cloud-metadata
  address unless the operator allow-listed it there.
* A live model call requires `EVAL_ALLOW_LLM_CALLS=true` regardless of target.
* `PAID_ENABLED` defaults to false. While false, only a LOCAL target is
  allowed — a remote backend's provider cannot be verified as free/local from
  here and there is no implicit paid fallback.
* Credentials: a pre-provisioned `EVAL_BACKEND_USERNAME` / `EVAL_BACKEND_PASSWORD`
  pair is used when both are set. Otherwise an ephemeral account named
  `eval-ephemeral-<utc-ts>-<rand>` is registered. There is no user-delete
  endpoint in the API, so these rows persist: an operator running Gate C
  against a long-lived dev DB should periodically prune
  `users WHERE username LIKE 'eval-ephemeral-%'` (and their sessions/messages).
* connect / read / write / pool timeouts on the client; a stream-idle gap and
  a total wall-clock deadline on the SSE read.
* Credentials, JWTs, cookies and any backend error body are redacted before
  they can reach a report (`redact` / `_redact_str`); prompts and response
  bodies are redacted by the runner's `redact()` pass over every payload.

Unreachable / unauthorised target -> `AdapterUnavailable` so the runner
records `unverified` (never a pass). It never sends a paid request itself.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import os
import secrets
import socket
import time
from urllib.parse import urlparse

from evals.provenance import (
    AdapterUnavailable,
    Provenance,
    provenance_defaults,
    redact,
    rubric_version,
)
from evals.provenance import _redact_str  # intentional: single redaction impl
from evals.schema import Task

CSRF_COOKIE = "research_assistant_csrf_token"

# Timeouts (seconds).
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 30.0
_WRITE_TIMEOUT = 10.0
_POOL_TIMEOUT = 5.0
_STREAM_IDLE_TIMEOUT = 45.0   # max gap between two SSE lines
_STREAM_TOTAL_TIMEOUT = 180.0  # max wall-clock for one streamed answer

_LOCAL_HOST_LITERALS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _allow_hosts() -> set[str]:
    return {
        h.strip().lower()
        for h in os.environ.get("EVAL_BACKEND_ALLOW_HOSTS", "").split(",")
        if h.strip()
    }


def _host_is_local(host: str) -> bool:
    """True when `host` is loopback / localhost. Mirrors the resolution
    technique in app.services.browser_agent.assert_url_allowed."""
    host = (host or "").lower()
    if host in _LOCAL_HOST_LITERALS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False  # cannot resolve -> not provably local
    addrs = [sockaddr[0] for *_, sockaddr in infos]
    return bool(addrs) and all(
        ipaddress.ip_address(a).is_loopback for a in addrs
    )


def _ssrf_check_remote(url: str) -> None:
    """For an authorised remote target, reuse the repo SSRF guard so it still
    cannot be a private / link-local / cloud-metadata address. Guarded import:
    if backend/app is not importable, fall back to a stdlib check."""
    try:
        from app.services.browser_agent import (  # type: ignore
            BrowserSecurityError,
            assert_url_allowed,
        )
    except Exception:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise AdapterUnavailable(f"cannot resolve remote backend host {host!r}: {exc}")
        for *_, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise AdapterUnavailable(
                    f"remote backend {host!r} resolves to non-public {ip}; refused"
                )
        return
    try:
        assert_url_allowed(url)
    except BrowserSecurityError as exc:
        raise AdapterUnavailable(f"remote backend refused by SSRF guard: {exc}") from exc


def _authorise_target() -> str:
    """Validate every precondition and return the normalised base URL.
    Pure except for env + DNS; unit-tested directly."""
    raw = os.environ.get("EVAL_BACKEND_URL", "").strip()
    if not raw:
        raise AdapterUnavailable("EVAL_BACKEND_URL not set (pass --backend BASE_URL)")

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise AdapterUnavailable(f"unsupported backend URL scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise AdapterUnavailable(f"backend URL has no host: {raw!r}")

    is_local = host in _allow_hosts() or _host_is_local(host)

    if not is_local:
        if not _truthy("EVAL_ALLOW_REMOTE_BACKEND"):
            raise AdapterUnavailable(
                f"non-local backend {host!r} refused; set EVAL_ALLOW_REMOTE_BACKEND=true "
                "to target a non-loopback URL"
            )
        _ssrf_check_remote(raw)

    if not _truthy("EVAL_ALLOW_LLM_CALLS"):
        raise AdapterUnavailable(
            "backend mode issues a live model call; set EVAL_ALLOW_LLM_CALLS=true to allow it"
        )

    if not _truthy("PAID_ENABLED") and not is_local:
        raise AdapterUnavailable(
            "PAID_ENABLED is not true and the target is not local — the running "
            "backend's provider cannot be verified as free/local from here and there "
            "is no implicit paid fallback; refused"
        )

    return raw.rstrip("/")


def _client(base: str):
    import httpx

    timeout = httpx.Timeout(
        connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT,
        write=_WRITE_TIMEOUT, pool=_POOL_TIMEOUT,
    )
    try:
        c = httpx.Client(base_url=base, timeout=timeout, follow_redirects=False)
        r = c.get("/api/health")
        if r.status_code != 200:
            raise AdapterUnavailable(f"/api/health -> HTTP {r.status_code}")
    except AdapterUnavailable:
        raise
    except Exception as exc:
        raise AdapterUnavailable(f"backend unreachable at {base}: {_redact_str(str(exc))}") from exc
    return c


def _ephemeral_username() -> str:
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"eval-ephemeral-{ts}-{secrets.token_hex(4)}"


def _auth(c) -> tuple[dict, str]:
    """Pre-provisioned creds if both env vars are set, else register an
    ephemeral account. Returns (headers, csrf_token). Credentials never leave
    this function; only Bearer/CSRF headers are returned and those are stripped
    from reports by redact()."""
    pre_user = os.environ.get("EVAL_BACKEND_USERNAME", "").strip()
    pre_pass = os.environ.get("EVAL_BACKEND_PASSWORD", "")
    if pre_user and pre_pass:
        uname, pw, ephemeral = pre_user, pre_pass, False
    else:
        uname = _ephemeral_username()
        pw = "Eval-" + secrets.token_hex(12) + "!aA1"
        ephemeral = True

    if ephemeral:
        reg = c.post("/api/auth/register", json={"username": uname, "password": pw})
        if reg.status_code not in (200, 201):
            raise AdapterUnavailable(
                f"register -> HTTP {reg.status_code}: {_redact_str(reg.text[:160])}"
            )
    login = c.post("/api/auth/login", json={"username": uname, "password": pw})
    if login.status_code != 200:
        raise AdapterUnavailable(
            f"login -> HTTP {login.status_code}: {_redact_str(login.text[:160])}"
        )
    token = (login.json() or {}).get("access_token", "")
    csrf = c.cookies.get(CSRF_COOKIE, "")
    headers = {"Authorization": f"Bearer {token}"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers, csrf


def _stream_message(c, headers: dict, session_id: int, message: str) -> str:
    """POST the streaming endpoint and reassemble the final content, enforcing
    an idle-gap timeout and a total wall-clock deadline."""
    text_parts: list[str] = []
    final = ""
    started = time.monotonic()
    last_line = started
    with c.stream("POST", f"/api/sessions/{session_id}/messages/stream",
                  headers=headers, json={"message": message}) as resp:
        if resp.status_code != 200:
            raise AdapterUnavailable(f"stream -> HTTP {resp.status_code}")
        event = None
        for line in resp.iter_lines():
            now = time.monotonic()
            if now - started > _STREAM_TOTAL_TIMEOUT:
                raise AdapterUnavailable(
                    f"stream exceeded total deadline ({_STREAM_TOTAL_TIMEOUT}s)"
                )
            if now - last_line > _STREAM_IDLE_TIMEOUT:
                raise AdapterUnavailable(
                    f"stream idle > {_STREAM_IDLE_TIMEOUT}s with no data"
                )
            last_line = now
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event == "token":
                    text_parts.append(data.get("token", ""))
                elif event == "complete":
                    final = data.get("content", "")
    return final or "".join(text_parts)


def run(task: Task) -> tuple[dict, dict]:
    if task.blocked_by:
        raise AdapterUnavailable(f"{task.id} blocked_by {task.blocked_by}")

    base = _authorise_target()
    c = _client(base)
    try:
        headers, _csrf = _auth(c)
        s = c.post("/api/sessions", headers=headers, json={"title": f"eval {task.id}"})
        if s.status_code not in (200, 201):
            raise AdapterUnavailable(f"create session -> HTTP {s.status_code}")
        session_id = s.json()["id"]
        content = _stream_message(c, headers, session_id, task.input)
    finally:
        c.close()

    prov = Provenance(
        fixture_kind="backend_capture", evidence_level="product_backend",
        captured_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        capture_mode="backend",
        application_path="POST /api/sessions/{id}/messages/stream -> streaming_service.stream_chat_response",
        provider="(running backend's configured provider)", model=None,
        grader_version=rubric_version(task.grader.rubric if task.grader else None),
        from_product_code=True, tools_mocked=[], human_reviewed=False,
        **provenance_defaults(),
    ).to_dict()
    payload = {"task_id": task.id, "response_text": content, "citations": [],
               "status_code": 200, "response_json": None, "execution_attempted": None,
               "provenance": prov}
    return redact(payload), prov
