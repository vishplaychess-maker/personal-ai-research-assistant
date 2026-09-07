"""Running-backend HTTP adapter (Gate B/C).

Drives the ACTUAL user-facing path:
  POST /api/auth/register  -> POST /api/auth/login (JWT + double-submit CSRF)
  -> POST /api/sessions     -> POST /api/sessions/{id}/messages/stream (SSE)

Route names and the auth/CSRF contract are taken from the repository
(routes/auth.py, routes/sessions.py, routes/messages.py, cookie_service).

Safety model (default-closed — this is the only eval code that can register a
user and make a live model call)
--------------------------------------------------------------------------
Two INDEPENDENT authorization decisions, each its own gate:

GATE 1 — target authorization  (`_authorise_target()`): may the eval client
contact this backend AT ALL, for ANY request (health / register / login /
create session — none of which invoke a model)?
  * `EVAL_BACKEND_URL` must be set and use an http/https scheme. Any other
    scheme (file:, ftp:, gopher:, ...) is refused.
  * Host is classified local vs non-local by resolving it the same way
    `app.services.browser_agent.assert_url_allowed` does (loopback only ->
    local). localhost / loopback / a host in `EVAL_BACKEND_ALLOW_HOSTS` is
    permitted by default.
  * A NON-LOCAL target requires `EVAL_ALLOW_REMOTE_BACKEND=true` AND is then
    run through the repo SSRF/URL guard (`assert_url_allowed`, guarded import;
    stdlib fallback) so it still cannot be a private / link-local / reserved /
    cloud-metadata address unless allow-listed there.
  * Redirects are NOT followed (`follow_redirects=False`) so a 3xx to an
    unvalidated location fails instead of being chased.
  * Gate 1 does NOT consult `EVAL_ALLOW_LLM_CALLS` or `PAID_ENABLED`. An
    authorized staging/test backend may run every non-model check with paid
    calls still disabled.

GATE 2 — live-model authorization  (`_authorise_live_model()`): may THIS run
trigger a live model invocation (the SSE stream)?
  * Always requires `EVAL_ALLOW_LLM_CALLS=true`. Paid calls stay disabled by
    default; `EVAL_ALLOW_REMOTE_BACKEND=true` does NOT authorize a model call.
  * LOCAL target: `EVAL_ALLOW_LLM_CALLS=true` is the operator's assertion that
    their own local stack (GLM router :8320 / Ollama) is free. Allowed.
  * NON-LOCAL target: a local env var is not proof of a remote server's
    routing. The live-model portion is allowed ONLY when the effective model
    can be verified free/local — the model id is known, its provider is known
    and `app.schemas.sessions.is_free_model(model, provider)` is True, AND the
    id is in `EVAL_FREE_MODEL_ALLOWLIST` (explicit, comma-separated). Never
    inferred from the name alone; never a silent paid fallback.
  * KNOWN LIMITATION (documented, not worked around): the Thunder AI API's
    `GET /api/sessions/{id}/model` returns only a bare model string — no
    provider, no routing/fallback policy. So for a remote backend the provider
    is unknown here and Gate 2 REFUSES the live-model portion as `unverified`.
    Gate 1 checks (connectivity / auth / session) still run and still pass.
    Gate C stays manual (`workflow_dispatch`) and non-gating.

Credentials: pre-provisioned `EVAL_BACKEND_USERNAME` / `EVAL_BACKEND_PASSWORD`
when both are set; otherwise an ephemeral `eval-ephemeral-<utc-ts>-<rand>`
account (no user-delete endpoint exists — prune
`users WHERE username LIKE 'eval-ephemeral-%'` periodically). connect / read /
write / pool + stream-idle + stream-total timeouts. Bearer/CSRF headers and
any backend error body are redacted before they can reach a report; the runner
`redact()`s every payload again. Failure messages name the reason and never
echo an env value or token.

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


def _target_is_local(host: str) -> bool:
    return host.lower() in _allow_hosts() or _host_is_local(host)


def _authorise_target() -> str:
    """GATE 1 — target authorization. May the eval client contact this backend
    for ANY request (health / register / login / create session — none invoke a
    model)? Returns the normalised base URL. Does NOT authorise a live model
    call — that is `_authorise_live_model()`. Pure except for env + DNS.

    Independent of `EVAL_ALLOW_LLM_CALLS` and `PAID_ENABLED` by design: an
    authorized staging/test backend may run every non-model check with paid
    calls still disabled.
    """
    raw = os.environ.get("EVAL_BACKEND_URL", "").strip()
    if not raw:
        raise AdapterUnavailable("EVAL_BACKEND_URL not set (pass --backend BASE_URL)")

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise AdapterUnavailable(f"unsupported backend URL scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise AdapterUnavailable(f"backend URL has no host: {raw!r}")

    if not _target_is_local(host):
        if not _truthy("EVAL_ALLOW_REMOTE_BACKEND"):
            raise AdapterUnavailable(
                f"non-local backend {host!r} refused; set EVAL_ALLOW_REMOTE_BACKEND=true "
                "to authorise a non-loopback target (this does NOT authorise a model call)"
            )
        _ssrf_check_remote(raw)  # repo SSRF/URL guard; rejects private/metadata/rebind-to-private

    return raw.rstrip("/")


def _free_model_allowlist() -> set[str]:
    return {
        m.strip()
        for m in os.environ.get("EVAL_FREE_MODEL_ALLOWLIST", "").split(",")
        if m.strip()
    }


def _is_free_model_mirror(model_id: str, provider: str | None) -> bool:
    """Byte-for-byte mirror of app.schemas.sessions.is_free_model, used ONLY
    when backend/app is not importable (e.g. hermetic Gate A). Not weaker —
    identical logic. Keep in sync if the repo helper changes."""
    name_lower = (model_id or "").lower()
    provider_lower = (provider or "").lower()
    if provider_lower in ("ollama", "local", "glm"):
        return True
    if provider_lower == "openrouter":
        return True
    if ":free" in name_lower:
        return True
    return False


def _is_free_model(model_id: str, provider: str | None) -> bool:
    """Reuse the repo's own classifier (`app.schemas.sessions.is_free_model`)
    when importable, else the in-sync mirror above. An unknown provider is not
    verifiable -> False. This is only ONE of the conditions GATE 2 requires; the
    explicit `EVAL_FREE_MODEL_ALLOWLIST` is always also enforced, so a name-only
    `:free` match can never by itself authorise a remote model call."""
    if provider is None:
        return False
    try:
        from app.schemas.sessions import is_free_model  # guarded import
        return bool(is_free_model(model_id, provider))
    except Exception:
        return _is_free_model_mirror(model_id, provider)


def _authorise_live_model(base_url: str, *, effective_model: str | None,
                          effective_provider: str | None) -> None:
    """GATE 2 — live-model authorization. Raises `AdapterUnavailable`
    (classified, credential-safe) when THIS run may not trigger a model
    invocation. Independent of GATE 1.
    """
    if not _truthy("EVAL_ALLOW_LLM_CALLS"):
        raise AdapterUnavailable(
            "live model call not authorised: set EVAL_ALLOW_LLM_CALLS=true "
            "(EVAL_ALLOW_REMOTE_BACKEND does not authorise a model call)"
        )

    host = (urlparse(base_url).hostname or "").lower()
    if _target_is_local(host):
        # The operator's own local stack (GLM router / Ollama). EVAL_ALLOW_LLM_CALLS
        # is their explicit assertion that local routing is free.
        return

    # Remote: a local env var is not proof of a remote server's routing. Require
    # the effective model to be *verifiable* free/local. Never inferred from the
    # name; never a silent paid fallback.
    if not _truthy("EVAL_ALLOW_REMOTE_BACKEND"):
        raise AdapterUnavailable(
            "remote live-model call requires EVAL_ALLOW_REMOTE_BACKEND=true (GATE 1)"
        )
    if not effective_model:
        raise AdapterUnavailable(
            "remote live-model routing UNVERIFIED: backend exposed no effective model id; "
            "live-model evaluation refused (non-model checks still ran)"
        )
    if effective_provider is None:
        raise AdapterUnavailable(
            "remote live-model routing UNVERIFIED: effective provider unknown for model "
            f"{effective_model!r} (GET /api/sessions/{{id}}/model exposes no provider or "
            "routing/fallback policy); cannot prove free-only routing; live-model "
            "evaluation refused (non-model checks still ran)"
        )
    if not _is_free_model(effective_model, effective_provider):
        raise AdapterUnavailable(
            f"remote model {effective_model!r} (provider {effective_provider!r}) not "
            "verified free/local by app.schemas.sessions.is_free_model; refused (no paid fallback)"
        )
    if effective_model not in _free_model_allowlist():
        raise AdapterUnavailable(
            f"remote model {effective_model!r} not in EVAL_FREE_MODEL_ALLOWLIST; "
            "refused as unverified"
        )
    # verified: known free model + known provider + explicit operator allowlist


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

    base = _authorise_target()  # GATE 1
    c = _client(base)
    try:
        # ── non-model checks (GATE 1 only) ────────────────────────────────
        headers, _csrf = _auth(c)
        s = c.post("/api/sessions", headers=headers, json={"title": f"eval {task.id}"})
        if s.status_code not in (200, 201):
            raise AdapterUnavailable(f"create session -> HTTP {s.status_code}")
        session_id = s.json()["id"]

        # ── live-model check (GATE 2) ─────────────────────────────────────
        # The API exposes only a bare model string (no provider / routing
        # policy), so effective_provider is None -> for a remote target GATE 2
        # refuses the live-model portion as unverified. Non-model checks above
        # have already run and passed.
        effective_model = None
        try:
            mr = c.get(f"/api/sessions/{session_id}/model", headers=headers)
            if mr.status_code == 200:
                effective_model = (mr.json() or {}).get("model")
        except Exception:
            effective_model = None
        _authorise_live_model(base, effective_model=effective_model,
                              effective_provider=None)

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
