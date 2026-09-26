"""Test-only ASGI entrypoint for the MP-3C3D6B real-browser durable-chat smoke.

This module is NEVER imported by `app/**` or by any production startup path. It
is imported only by the uvicorn process that `tests/smoke/run_smoke.py` starts.

What it does, and nothing more:

1. Refuses to start unless the launcher set `THUNDER_SMOKE_ROOT` to an existing
   directory AND the configured SQLite database lives inside it. That is a
   fail-closed guard so this module can never be pointed at real user data.
2. Installs the outbound-network tripwire for this process.
3. Imports the REAL application object (`app.main.app`) — real routes, real
   lifespan, real middleware.
4. Pre-sets `app.state.run_worker_supervisor` so the real lifespan adopts a real
   `RunWorkerSupervisor` whose REAL `RunWorker` was handed a deterministic
   in-process provider through the pre-existing `chat_provider` seam.

Boundary: the fake provider is installed by mutating an in-process attribute
before the app starts. It is NOT selectable through an environment variable, a
config field, a route, a header, a request body, or any production startup path.
There is no production switch that turns a fake provider on.
"""

from __future__ import annotations

import os
from pathlib import Path


def _smoke_root() -> Path:
    raw = (os.getenv("THUNDER_SMOKE_ROOT") or "").strip()
    if not raw:
        raise RuntimeError(
            "THUNDER_SMOKE_ROOT is not set — durable_smoke_app is a TEST-ONLY "
            "entrypoint and must be started by tests/smoke/run_smoke.py"
        )
    root = Path(raw).resolve()
    if not root.is_dir():
        raise RuntimeError("THUNDER_SMOKE_ROOT must be an existing directory")
    return root


def _assert_db_inside(root: Path, database_url: str) -> Path:
    if not database_url.startswith("sqlite:"):
        raise RuntimeError("the smoke requires an isolated sqlite database")
    tail = database_url.split("sqlite:", 1)[1]
    while tail.startswith("/"):
        tail = tail[1:]
    if not tail:
        raise RuntimeError("the smoke requires a file-backed sqlite database")
    db_path = Path(tail)
    if not db_path.is_absolute():
        db_path = (Path.cwd() / db_path)
    resolved = db_path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise RuntimeError(
            "refusing to start: the smoke database is outside THUNDER_SMOKE_ROOT"
        ) from None
    return resolved


SMOKE_ROOT = _smoke_root()

from app.config import settings  # noqa: E402

DB_PATH = _assert_db_inside(SMOKE_ROOT, settings.database_url)

from tests.smoke.fakes import (  # noqa: E402
    DeterministicChatProviderFactory, NetworkTripwire,
)

TRIPWIRE = NetworkTripwire(log_path=SMOKE_ROOT / "tripwire.jsonl").install()

from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402  (the REAL application, real lifespan)
from app.run_engine.worker import RunWorker  # noqa: E402
from app.run_engine.worker_supervisor import RunWorkerSupervisor  # noqa: E402

#: The one deterministic provider for this process. The launcher reads the
#: recorded invocation count as evidence that no real provider was ever used.
#:
#: D6B-R1: the default bound is exactly ONE invocation (the D6B acceptance
#: invariant). The diagnostic comparison matrix needs two independent
#: submissions against one instance, so a TEST-ONLY knob exists. It is read by
#: this test-only module only; nothing in `app/**` reads it, and the acceptance
#: run always uses the default.
def _max_invocations() -> int:
    raw = (os.getenv("THUNDER_SMOKE_MAX_PROVIDER_INVOCATIONS") or "").strip()
    if not raw:
        return 1
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


CHAT_PROVIDER = DeterministicChatProviderFactory(
    record_path=SMOKE_ROOT / "provider_calls.jsonl",
    max_invocations=_max_invocations(),
)


# D6C-J2/J3 - tests/smoke-scoped scenario switch (MCP approve / deny).
#
# Read ONLY by this test-only module. It never enters backend Settings,
# production environment parsing, the public API, a frontend request, a
# header/query/body, or normal application startup.
SCENARIO = (os.getenv("THUNDER_SMOKE_SCENARIO") or "").strip().lower()
MCP_SCENARIO = SCENARIO in ("mcp_approve", "mcp_deny")
CANCEL_SCENARIO = SCENARIO == "cancel"
DEEP_RESEARCH_SCENARIO = SCENARIO == "deep_research"
MULTI_AGENT_SCENARIO = SCENARIO in ("multi_agent", "multi_agent_longtokens")
# MP-4C - tests/smoke-scoped Python approval scenarios (durable chat + real
# approval/dispatch/resume, or real DENIAL). `python_deny` REUSES the exact
# same fixtures and the same `_build_worker` arm as `python_approve`; only
# the browser action differs. Only the proposal provider and the executor
# callback are deterministic.
PYTHON_SCENARIO = SCENARIO in ("python_approve", "python_deny")
PYTHON_DENY_SCENARIO = SCENARIO == "python_deny"

if MCP_SCENARIO:
    from tests.smoke.mcp_fakes import MarkerProvider, FakeMcpTransport

    # ONE shared stateful provider and ONE shared fake transport per smoke
    # process - never recreated per poll/sweep.
    MCP_PROVIDER = MarkerProvider()
    MCP_TRANSPORT = FakeMcpTransport()
else:
    MCP_PROVIDER = None
    MCP_TRANSPORT = None

# MP-4C - tests/smoke-scoped Python approval fixtures. ONE shared provider and
# ONE shared counting executor per smoke process. `python_executor` is the
# existing RunWorker seam; the fake never starts a subprocess and only
# *receives* the code the gateway reopened from the sealed tool_args payload.
if PYTHON_SCENARIO:
    from tests.smoke.python_fakes import (
        CountingMcpTransport,
        CountingPythonExecutor,
        PythonMarkerProvider,
    )

    PY_PROVIDER = PythonMarkerProvider(
        record_path=SMOKE_ROOT / "python_provider_calls.jsonl")
    PY_EXECUTOR = CountingPythonExecutor(
        record_path=SMOKE_ROOT / "python_executor_calls.jsonl")
    # Loud tripwire: the Python path must never reach the MCP transport.
    PY_TRANSPORT = CountingMcpTransport(
        record_path=SMOKE_ROOT / "python_mcp_transport_calls.jsonl")
else:
    PY_PROVIDER = None
    PY_EXECUTOR = None
    PY_TRANSPORT = None

# D6C-J4 - tests/smoke-scoped cancellable provider. ONE shared instance per smoke
# process, never recreated per poll/sweep. Every selection / invocation / chunk /
# stall / cancellation / close is appended to a JSONL file inside the smoke root
# so the launcher (a different process) reads counts as evidence.
if CANCEL_SCENARIO:
    from tests.smoke.cancel_fakes import CancellableProviderFactory

    CANCEL_PROVIDER = CancellableProviderFactory(
        record_path=SMOKE_ROOT / "cancel_provider_calls.jsonl",
        max_invocations=1,
    )
else:
    CANCEL_PROVIDER = None

# D6C-J6 - tests/smoke-scoped Deep Research fixtures. Only the EXTERNAL research and
# provider operations are faked; they are injected through the pre-existing
# RunWorker seams (worker.py:2049-2050 already prefers an injected adapter). ONE
# shared instance per smoke process, so invocation counts are process-stable.
if DEEP_RESEARCH_SCENARIO:
    from tests.smoke.research_fakes import (
        DeepResearchFetchAdapter,
        DeepResearchProviderFactory,
        DeepResearchSearchAdapter,
    )

    DR_SEARCH = DeepResearchSearchAdapter(record_path=SMOKE_ROOT / "dr_search.jsonl")
    DR_FETCH = DeepResearchFetchAdapter(record_path=SMOKE_ROOT / "dr_fetch.jsonl")
    DR_PROVIDER = DeepResearchProviderFactory(
        record_path=SMOKE_ROOT / "dr_provider.jsonl", max_invocations=1)
else:
    DR_SEARCH = None
    DR_FETCH = None
    DR_PROVIDER = None

# D6C-J7 - tests/smoke-scoped Multi-Agent fixtures. ONLY the three role provider
# outputs are deterministic; the context seam patches just the retrieval primitive
# _build_context_snapshot already calls, so the REAL sealed-context/citation pipeline
# runs with no ChromaDB and no embedding model.
if MULTI_AGENT_SCENARIO:
    from tests.smoke.ma_fakes import MultiAgentProviderFactory, install_context_seam

    install_context_seam()
    MA_PROVIDER = MultiAgentProviderFactory(record_path=SMOKE_ROOT / "ma_provider.jsonl")
else:
    MA_PROVIDER = None


def _build_worker() -> RunWorker:
    if MULTI_AGENT_SCENARIO:
        # Real durable Multi-Agent; only the three role outputs are deterministic.
        return RunWorker(SessionLocal, chat_provider=MA_PROVIDER)
    if DEEP_RESEARCH_SCENARIO:
        # Real durable Deep Research; only search / fetch / synthesis are deterministic.
        return RunWorker(SessionLocal, chat_provider=DR_PROVIDER,
                         research_search_adapter=DR_SEARCH,
                         research_fetch_adapter=DR_FETCH)
    if CANCEL_SCENARIO:
        # Chat-only cancellable provider: yields a visible prefix, then stays open
        # so a real browser Cancel is observed through the production on_idle guard
        # path (chat_stream._adrive) rather than an arbitrary test race.
        return RunWorker(SessionLocal, chat_provider=CANCEL_PROVIDER)
    if MCP_SCENARIO:
        # mcp_transport is the existing RunWorker seam; the fake never drives the
        # gateway, never mutates approval state and fails closed pre-consumption.
        return RunWorker(SessionLocal, chat_provider=MCP_PROVIDER,
                         mcp_transport=MCP_TRANSPORT)
    if PYTHON_SCENARIO:
        # Real durable chat + real Python approval/dispatch/resume; only the
        # proposal provider and the executor callback are deterministic.
        return RunWorker(SessionLocal, chat_provider=PY_PROVIDER,
                         python_executor=PY_EXECUTOR,
                         mcp_transport=PY_TRANSPORT)
    return RunWorker(SessionLocal, chat_provider=CHAT_PROVIDER)


# Pre-set BEFORE the lifespan runs: `app.main.lifespan` adopts an existing
# `app.state.run_worker_supervisor` instead of constructing its own, then starts
# it exactly once and stops it on shutdown. No production code is modified.
app.state.run_worker_supervisor = RunWorkerSupervisor(
    session_factory=SessionLocal,
    worker_factory=_build_worker,
)


# ── D6B-R1 test-only ASGI boundary tracer ───────────────────────────
#
# Wraps the real app, forwards lifespan and every request UNCHANGED, and records
# only bounded SSE boundary metadata: response status, content type, body chunk
# count/bytes, the `more_body` flag of each body message, the SSE event names and
# ids seen in the body, and the time of `http.disconnect`. It NEVER records
# headers carrying credentials, token text, or payloads.
#
# Enabled only when THUNDER_SMOKE_ASGI_TRACE is set (TEST-ONLY). Nothing in
# `app/**` reads or provides this switch.


def _parse_frames(buf: str) -> list:
    out = []
    for raw in buf.split("\n\n"):
        name = None
        ident = None
        for line in raw.split("\n"):
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("id: "):
                ident = line[4:].strip()
        if name:
            out.append({"event": name, "id": ident})
    return out


def _wrap_asgi_trace(inner, log_path):
    import json as _json
    import time as _time

    async def wrapper(scope, receive, send):
        if scope.get("type") != "http":
            return await inner(scope, receive, send)
        path = scope.get("path") or ""
        if not path.endswith("/messages/stream"):
            return await inner(scope, receive, send)

        t0 = _time.monotonic()
        rec = {"path": path, "status": None, "content_type": None,
               "body_chunks": 0, "body_bytes": 0, "last_more_body": None,
               "disconnect_ms": None, "final_ms": None, "events": []}
        buf = ""

        async def _receive():
            msg = await receive()
            if msg.get("type") == "http.disconnect" and rec["disconnect_ms"] is None:
                rec["disconnect_ms"] = int((_time.monotonic() - t0) * 1000)
            return msg

        async def _send(msg):
            if msg["type"] == "http.response.start":
                rec["status"] = msg.get("status")
                for k, v in (msg.get("headers") or []):
                    if k.lower() == b"content-type":
                        rec["content_type"] = v.decode("latin-1")
            elif msg["type"] == "http.response.body":
                body = msg.get("body") or b""
                rec["body_chunks"] += 1
                rec["body_bytes"] += len(body)
                rec["last_more_body"] = bool(msg.get("more_body"))
                if body:
                    nonlocal buf
                    buf += body.decode("utf-8", "replace")
                    if "\n\n" in buf:
                        parts = buf.split("\n\n")
                        buf = parts.pop()
                        for part in parts:
                            for fr in _parse_frames(part):
                                rec["events"].append(fr)
                rec["final_ms"] = int((_time.monotonic() - t0) * 1000)
            await send(msg)

        try:
            await inner(scope, _receive, _send)
        finally:
            rec["total_ms"] = int((_time.monotonic() - t0) * 1000)
            rec["event_names"] = [e["event"] for e in rec["events"]]
            rec["terminal_sent"] = any(
                e["event"] in ("complete", "error", "cancelled") for e in rec["events"])
            try:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(_json.dumps(rec, sort_keys=True) + "\n")
            except OSError:
                pass

    return wrapper


if (os.getenv("THUNDER_SMOKE_ASGI_TRACE") or "").strip() == "1":
    app = _wrap_asgi_trace(app, SMOKE_ROOT / "asgi_trace.jsonl")

# D6C-J1 - deterministic test-only server-side truncated SSE / early EOF.
# Armed ONLY by this test-only module from a tests/smoke-scoped switch; nothing in
# `app/**` reads it and it is not selectable by header, query, body or production
# config. It cuts the FIRST `POST .../messages/stream` response after the first
# complete `event: token` frame; the reconnect passes through untouched.
if (os.getenv("THUNDER_SMOKE_TRUNCATE_FIRST_STREAM") or "").strip() == "1":
    import json as _json

    from tests.smoke.sse_fault_injector import SSEStreamTruncator

    _cut_log = SMOKE_ROOT / "injector_cuts.jsonl"

    def _record_cut(rec: dict) -> None:
        try:
            with _cut_log.open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps(rec, sort_keys=True) + "\n")
        except OSError:
            pass

    app = SSEStreamTruncator(app, enabled=True, on_cut=_record_cut)
