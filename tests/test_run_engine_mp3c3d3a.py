"""Phase 1B.2R MP-3C3D3A — explicit request-level mode contract and backend
routing foundation.

Covers: the canonical mode validator (submission.validate_request_mode,
STRICT exact-match), legacy omitted-vs-explicit routing
(streaming_service.prepare_chat_context / stream_chat_response) with exact
mocked-entrypoint call-count proof, the URL-scrape/deep-research interaction,
durable recognized-but-unimplemented rejection proven through the REAL
`/messages/stream` route (not just the bare gate function), the direct Run
API's mode gate proven through the REAL `/api/runs` route, idempotency (mode
is already part of the existing MP-3A semantic identity — unchanged, reused,
not reimplemented), and privacy. No live Ollama/ChromaDB/network calls —
deep_research/multi_agent/web-scrape/memory/RAG services and durable-route
auth/worker-readiness dependencies are all monkeypatched or overridden at
their seams; test_streaming.py's live-backend integration suite is
confirmed (this session) to require a running server + Ollama and is out of
scope here.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import Message, MessageRole, ResearchSession, Run, RunStep
from app.run_engine import messages_stream as ms
from app.run_engine import submission as sub
from app.services import streaming_service as ss
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_session

_HMAC = "mp3c3d3a-unit-test-hmac-key-0123456789"

# Closeout correction (2026-09-12): the SAME rejected-values set, used to
# prove /messages/stream and /api/runs behave IDENTICALLY at the public wire
# — both now go through validate_request_mode()/resolve_mode() (which
# delegates to it), one canonical strict contract, no per-endpoint drift.
_PUBLIC_WIRE_REJECTED_MODES = (
    "CHAT", " Chat ", "deep-research", "multi-agent", "Deep_Research",
    "", "   ", "banana", "chat; DROP TABLE runs;--", "x" * 100,
    123, 1.5, True, False, ["chat"], {"mode": "chat"},
)
_PUBLIC_WIRE_ACCEPTED_MODES = ("chat", "deep_research", "multi_agent")


# ═══════════════ A. submission.validate_request_mode (pure, STRICT) ═══


def test_validate_request_mode_omitted_returns_none():
    assert sub.validate_request_mode(None) is None


def test_validate_request_mode_exact_canonical_values_returned_unchanged():
    assert sub.validate_request_mode("chat") == "chat"
    assert sub.validate_request_mode("deep_research") == "deep_research"
    assert sub.validate_request_mode("multi_agent") == "multi_agent"


def test_validate_request_mode_case_and_whitespace_variants_rejected():
    # STRICT exact-match: this is a brand-new request-level field with no
    # prior client relying on lenient parsing (see the function's docstring
    # for the evidence check against resolve_mode's looser, but undocumented
    # and untested, normalization).
    for bad in ("CHAT", " chat", "chat ", "  chat  ", "Chat", "Deep_Research",
               "DEEP_RESEARCH", "MULTI_AGENT", "\tchat\n"):
        with pytest.raises(ValueError):
            sub.validate_request_mode(bad)


def test_validate_request_mode_rejects_unknown_oversized_injection_like():
    bad = (
        "", "banana", "chat; DROP TABLE runs;--", "x" * 100,
        "../../etc/passwd", "__proto__", "chat_stream", "mcp_call",
        "run_worker_enabled", "sha256:deadbeef",
    )
    for value in bad:
        with pytest.raises(ValueError):
            sub.validate_request_mode(value)


def test_validate_request_mode_rejects_non_string():
    for value in (123, 1.5, True, ["chat"], {"mode": "chat"}, {"$ne": None}):
        with pytest.raises(ValueError):
            sub.validate_request_mode(value)


def test_validate_request_mode_error_never_echoes_raw_value():
    canary = "SECRET-MODE-CANARY-<script>alert(1)</script>"
    try:
        sub.validate_request_mode(canary)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert canary not in str(exc)


# ═══════════════ B. Legacy routing (prepare_chat_context /  ═══════════
# ═══════════════    stream_chat_response), exact call counts ═════════


def _prep_session(eng, *, uid=7, sid=1):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        db.add(ResearchSession(id=sid, user_id=uid, title="t", model="model-x"))
        db.commit()
    finally:
        db.close()


def _prepare(eng, *, mode=None, message="hello there"):
    db = mk_session(eng)
    try:
        return ss.prepare_chat_context(
            session_id=1, user_input=message, db=db, user_id=7, mode=mode,
        )
    finally:
        db.close()


def _drain_stream_chat_response(context):
    async def go():
        out = []
        async for ev in ss.stream_chat_response(context):
            out.append(ev)
            if len(out) > 20:  # bounded — a fake multi-agent generator ends fast
                break
        return out
    return asyncio.run(go())


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _mock_deep_research(monkeypatch, *, heuristic: bool, called: dict, urls=None):
    from importlib import import_module
    import app.services.agent_personas as personas
    import app.tools.deep_research as dr
    # NOTE: `import app.tools.web_scraper as ws` does NOT return the real
    # module here — app/tools/__init__.py re-exports the `web_scraper` TOOL
    # (the @tool-decorated StructuredTool), which shadows the submodule
    # attribute on the `app.tools` package namespace. import_module() goes
    # straight to sys.modules and returns the actual module.
    ws = import_module("app.tools.web_scraper")

    monkeypatch.setattr(personas, "detect_research_task", lambda text: heuristic)

    def fake_run(query):
        called["deep_research"] = called.get("deep_research", 0) + 1
        return "DEEP-RESEARCH-CONTEXT-MARKER"
    monkeypatch.setattr(dr, "run_deep_research", fake_run)
    monkeypatch.setattr("app.services.knowledge_graph.ingest_text_async", lambda *a, **k: None)

    monkeypatch.setattr(ws, "extract_urls", lambda text: list(urls or []))
    if urls:
        # web_scraper is a pydantic-backed StructuredTool — `invoke` is an
        # inherited METHOD (not a declared field), so it can't be monkeypatched
        # on the instance directly (pydantic v2 rejects the assignment). Patch
        # the underlying wrapped callable (`func`, a real declared field)
        # instead; StructuredTool.invoke() dispatches to it internally.
        def fake_scrape_func(url):
            called["web_scraper"] = called.get("web_scraper", 0) + 1
            return "SCRAPED-URL-CONTENT-MARKER"
        monkeypatch.setattr(ws.web_scraper, "func", fake_scrape_func)


def _mock_multi_agent(monkeypatch, *, heuristic: bool, called: dict):
    import app.services.agent_personas as personas

    monkeypatch.setattr(personas, "detect_complex_task", lambda text: heuristic)

    async def fake_stream(context):
        called["multi_agent"] = called.get("multi_agent", 0) + 1
        yield ss.format_sse("agent_status", {"marker": "MULTI_AGENT_CALLED"})
    monkeypatch.setattr(ss, "stream_multi_agent_response", fake_stream)


def _mock_plan_and_cache(monkeypatch):
    """stream_chat_response's very first step (plan preview) touches a real
    service — bound to a safe no-op so the generator can run past the
    multi-agent decision point without a live provider."""
    monkeypatch.setattr("app.services.planning_service.generate_plan_for_query",
                        lambda **k: [])


def test_omitted_mode_preserves_exact_old_behavior(eng, monkeypatch):
    _prep_session(eng)
    called = {}
    _mock_deep_research(monkeypatch, heuristic=True, called=called)
    monkeypatch.setattr(settings, "enable_deep_research", False)
    ctx = _prepare(eng, mode=None)
    assert called.get("deep_research", 0) == 0
    assert ctx.mode is None


def test_omitted_mode_with_deep_research_enabled_uses_existing_detector(eng, monkeypatch):
    _prep_session(eng)
    monkeypatch.setattr(settings, "enable_deep_research", True)

    called_on = {}
    _mock_deep_research(monkeypatch, heuristic=True, called=called_on)
    ctx = _prepare(eng, mode=None)
    assert called_on.get("deep_research", 0) == 1
    assert "DEEP-RESEARCH-CONTEXT-MARKER" in ctx.system_prompt

    # heuristic OFF -> not called, even with the global flag still on
    called_off = {}
    _mock_deep_research(monkeypatch, heuristic=False, called=called_off)
    ctx2 = _prepare(eng, mode=None)
    assert called_off.get("deep_research", 0) == 0


def test_omitted_mode_with_multi_agent_enabled_uses_existing_detector(eng, monkeypatch):
    _prep_session(eng)
    called = {}
    _mock_deep_research(monkeypatch, heuristic=False, called=called)
    _mock_multi_agent(monkeypatch, heuristic=True, called=called)
    _mock_plan_and_cache(monkeypatch)
    monkeypatch.setattr(settings, "enable_deep_research", False)
    monkeypatch.setattr(settings, "enable_multi_agent", True)
    ctx = _prepare(eng, mode=None)
    events = _drain_stream_chat_response(ctx)
    assert called.get("multi_agent", 0) == 1
    assert any("MULTI_AGENT_CALLED" in e for e in events)
    # heuristic OFF -> not routed, even with the global flag on
    called.clear()
    _mock_multi_agent(monkeypatch, heuristic=False, called=called)
    events2 = _drain_stream_chat_response(ctx)
    assert called.get("multi_agent", 0) == 0


def test_both_globals_true_preserves_existing_precedence(eng, monkeypatch):
    # documented existing precedence (unchanged): deep-research context is
    # injected at prepare_chat_context time (system_prompt), THEN
    # stream_chat_response's OWN separate multi-agent check still routes the
    # (already deep-research-enriched) context to the multi-agent team.
    _prep_session(eng)
    called = {}
    _mock_deep_research(monkeypatch, heuristic=True, called=called)
    _mock_multi_agent(monkeypatch, heuristic=True, called=called)
    _mock_plan_and_cache(monkeypatch)
    monkeypatch.setattr(settings, "enable_deep_research", True)
    monkeypatch.setattr(settings, "enable_multi_agent", True)
    ctx = _prepare(eng, mode=None)
    assert called.get("deep_research", 0) == 1
    assert "DEEP-RESEARCH-CONTEXT-MARKER" in ctx.system_prompt
    events = _drain_stream_chat_response(ctx)
    assert called.get("multi_agent", 0) == 1
    assert any("MULTI_AGENT_CALLED" in e for e in events)


def test_explicit_chat_skips_both_even_with_both_globals_enabled(eng, monkeypatch):
    _prep_session(eng)
    called = {}
    _mock_deep_research(monkeypatch, heuristic=True, called=called)
    _mock_multi_agent(monkeypatch, heuristic=True, called=called)
    _mock_plan_and_cache(monkeypatch)
    monkeypatch.setattr(settings, "enable_deep_research", True)
    monkeypatch.setattr(settings, "enable_multi_agent", True)
    ctx = _prepare(eng, mode="chat")
    assert called.get("deep_research", 0) == 0
    events = _drain_stream_chat_response(ctx)
    assert called.get("multi_agent", 0) == 0
    assert not any("MULTI_AGENT_CALLED" in e for e in events)


def test_explicit_deep_research_invokes_only_deep_research_path(eng, monkeypatch):
    _prep_session(eng)
    called = {}
    # heuristics deliberately False — explicit mode must bypass them
    _mock_deep_research(monkeypatch, heuristic=False, called=called)
    _mock_multi_agent(monkeypatch, heuristic=False, called=called)
    _mock_plan_and_cache(monkeypatch)
    monkeypatch.setattr(settings, "enable_deep_research", True)
    monkeypatch.setattr(settings, "enable_multi_agent", True)   # even if globally on
    ctx = _prepare(eng, mode="deep_research")
    assert called.get("deep_research", 0) == 1
    assert "DEEP-RESEARCH-CONTEXT-MARKER" in ctx.system_prompt
    events = _drain_stream_chat_response(ctx)
    assert called.get("multi_agent", 0) == 0
    assert not any("MULTI_AGENT_CALLED" in e for e in events)


def test_explicit_multi_agent_invokes_only_multi_agent_branch(eng, monkeypatch):
    _prep_session(eng)
    called = {}
    _mock_deep_research(monkeypatch, heuristic=False, called=called)
    _mock_multi_agent(monkeypatch, heuristic=False, called=called)  # bypassed by explicit mode
    _mock_plan_and_cache(monkeypatch)
    monkeypatch.setattr(settings, "enable_deep_research", True)     # even if globally on
    monkeypatch.setattr(settings, "enable_multi_agent", True)
    ctx = _prepare(eng, mode="multi_agent")
    assert called.get("deep_research", 0) == 0    # never runs for multi_agent mode
    events = _drain_stream_chat_response(ctx)
    assert called.get("multi_agent", 0) == 1
    assert any("MULTI_AGENT_CALLED" in e for e in events)


def test_explicit_modes_never_invoke_two_pipelines(eng, monkeypatch):
    """Trip-wire: once multi_agent mode routes and returns, the single-agent
    generation path (which would eventually call the real provider) must
    NEVER be reached."""
    _prep_session(eng)
    called = {}
    _mock_deep_research(monkeypatch, heuristic=False, called=called)
    _mock_multi_agent(monkeypatch, heuristic=True, called=called)
    _mock_plan_and_cache(monkeypatch)

    def trip_wire(*a, **k):
        raise AssertionError("single-agent provider path reached after multi_agent routing")
    monkeypatch.setattr("app.services.llm_providers.get_provider", trip_wire)

    monkeypatch.setattr(settings, "enable_multi_agent", True)
    ctx = _prepare(eng, mode="multi_agent")
    events = _drain_stream_chat_response(ctx)   # must not raise
    assert any("MULTI_AGENT_CALLED" in e for e in events)


def test_explicit_deep_research_with_url_uses_url_context_not_autonomous_research(eng, monkeypatch):
    """Evidence-based contract: prepare_chat_context's web-scrape step (7)
    runs BEFORE the deep-research step (7b), and 7b is gated on
    `not web_scraped`. This routing phase deliberately preserves that
    short-circuit for explicit mode="deep_research" too — a URL in the
    message uses the existing URL-scrape context path instead of invoking
    autonomous run_deep_research, exactly as it already does for the
    omitted/heuristic-driven case. Not changed here; only proven."""
    _prep_session(eng)
    called = {}
    _mock_deep_research(monkeypatch, heuristic=True, called=called,
                        urls=["https://example.com/article"])
    monkeypatch.setattr(settings, "enable_deep_research", True)
    ctx = _prepare(eng, mode="deep_research",
                   message="summarize https://example.com/article")
    assert called.get("web_scraper", 0) == 1
    assert called.get("deep_research", 0) == 0
    assert "SCRAPED-URL-CONTENT-MARKER" in ctx.system_prompt
    assert "DEEP-RESEARCH-CONTEXT-MARKER" not in ctx.system_prompt


# ═══════════════ C. Route-level validation + legacy capability gate ═══


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app), app


def _post_stream(c, app, *, mode_body: dict, message="hi", headers=None):
    from app.services.auth_service import get_current_user
    from app.services.cookie_service import require_csrf
    with c:
        app.dependency_overrides[get_current_user] = lambda: type("U", (), {"id": 7})()
        app.dependency_overrides[require_csrf] = lambda: None
        try:
            body = {"message": message, **mode_body}
            return c.post("/api/sessions/1/messages/stream", json=body, headers=headers or {})
        finally:
            app.dependency_overrides.clear()


def test_route_rejects_invalid_mode_before_persistence(monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    called = {"n": 0}
    import app.routes.messages as mod
    monkeypatch.setattr(mod, "prepare_chat_context",
                        lambda **kw: called.__setitem__("n", called["n"] + 1))
    c, app = _client()
    for bad_mode in _PUBLIC_WIRE_REJECTED_MODES:
        r = _post_stream(c, app, mode_body={"mode": bad_mode})
        assert r.status_code == 422, bad_mode
    assert called["n"] == 0


def test_route_rejects_explicit_deep_research_when_globally_disabled(monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "enable_deep_research", False)
    called = {"n": 0}
    import app.routes.messages as mod
    monkeypatch.setattr(mod, "prepare_chat_context",
                        lambda **kw: called.__setitem__("n", called["n"] + 1))
    c, app = _client()
    r = _post_stream(c, app, mode_body={"mode": "deep_research"})
    assert r.status_code == 422
    assert called["n"] == 0   # prepare_chat_context (which saves the user Message) never ran


def test_route_rejects_explicit_multi_agent_when_globally_disabled(monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "enable_multi_agent", False)
    called = {"n": 0}
    import app.routes.messages as mod
    monkeypatch.setattr(mod, "prepare_chat_context",
                        lambda **kw: called.__setitem__("n", called["n"] + 1))
    c, app = _client()
    r = _post_stream(c, app, mode_body={"mode": "multi_agent"})
    assert r.status_code == 422
    assert called["n"] == 0


def test_omitted_mode_flag_off_reaches_legacy_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    called = {"legacy": 0, "durable": 0}
    import app.routes.messages as mod

    class _Ctx:
        session_id = 1
        citations = []
        sources_used = False
        memories_used = False

    monkeypatch.setattr(mod, "prepare_chat_context",
                        lambda **kw: called.__setitem__("legacy", called["legacy"] + 1) or _Ctx())

    async def _fake_stream(ctx):
        yield mod.format_sse("start", {})
        yield mod.format_sse("complete", {"message_id": 7, "citations": []})

    monkeypatch.setattr(mod, "stream_chat_response", _fake_stream)
    monkeypatch.setattr(mod, "save_assistant_message", lambda **kw: type("M", (), {"id": 7})())
    monkeypatch.setattr(mod, "run_memory_extraction", lambda **kw: None)
    monkeypatch.setattr(mod, "_stream_chat_durable",
                        lambda *a, **k: called.__setitem__("durable", 1))
    c, app = _client()
    r = _post_stream(c, app, mode_body={})
    assert r.status_code == 200
    assert called["legacy"] == 1 and called["durable"] == 0


# ═══════════════ D. Durable route proof (through the REAL route) ══════


def _configure_durable_ready(monkeypatch, app):
    """Wire every dependency assert_durable_stream_ready() checks so a REAL
    TestClient request reaches the mode gate deterministically — never a 503
    from an unrelated missing dependency masquerading as the mode check."""
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)

    class _ReadySupervisor:
        def is_durable_path_ready(self):
            return True

        async def start(self) -> None:
            pass  # the app's own lifespan calls this unconditionally on startup

        async def stop(self) -> None:
            pass  # ...and this on shutdown, which also nulls app.state.run_worker_supervisor

    # each `with TestClient(...):` block runs the app's real lifespan, which
    # NULLS app.state.run_worker_supervisor on shutdown — re-assign fresh
    # every time this helper runs so a second request in the same test still
    # sees a ready fake, never a real freshly-constructed RunWorkerSupervisor.
    app.state.run_worker_supervisor = _ReadySupervisor()


def _post_stream_durable(c, app, *, mode_body: dict, message="hi", headers=None):
    from app.services.auth_service import get_current_user
    from app.services.cookie_service import require_csrf
    with c:
        app.dependency_overrides[get_current_user] = lambda: type("U", (), {"id": 7})()
        app.dependency_overrides[require_csrf] = lambda: None
        try:
            body = {"message": message, **mode_body}
            return c.post("/api/sessions/1/messages/stream", json=body, headers=headers or {})
        finally:
            app.dependency_overrides.clear()


def test_durable_route_deep_research_exact_422_before_any_side_effect(monkeypatch):
    c, app = _client()
    _configure_durable_ready(monkeypatch, app)
    calls = {"submit": 0, "wire": 0}
    import app.run_engine.messages_stream as ms_mod

    def spy_submit(*a, **k):
        calls["submit"] += 1
        raise AssertionError("resolve_and_submit must never be called for a rejected mode")

    async def spy_wire(*a, **k):
        calls["wire"] += 1
        raise AssertionError("stream_legacy_wire must never be called for a rejected mode")
        yield  # pragma: no cover — unreachable, keeps this an async generator

    monkeypatch.setattr(ms_mod, "resolve_and_submit", spy_submit)
    monkeypatch.setattr(ms_mod, "stream_legacy_wire", spy_wire)

    r = _post_stream_durable(c, app, mode_body={"mode": "deep_research"})
    assert r.status_code == 422
    assert calls["submit"] == 0 and calls["wire"] == 0
    assert "mode_unsupported" not in r.text   # bounded internal code never leaks to the client


def test_durable_route_multi_agent_exact_422_before_any_side_effect(monkeypatch):
    c, app = _client()
    _configure_durable_ready(monkeypatch, app)
    import app.run_engine.messages_stream as ms_mod

    def spy_submit(*a, **k):
        raise AssertionError("resolve_and_submit must never be called for a rejected mode")
    monkeypatch.setattr(ms_mod, "resolve_and_submit", spy_submit)

    r = _post_stream_durable(c, app, mode_body={"mode": "multi_agent"})
    assert r.status_code == 422
    assert "mode_unsupported" not in r.text


def test_durable_route_invalid_mode_fails_before_readiness_would_matter(monkeypatch):
    # readiness IS configured (so a 503 here would prove the wrong thing);
    # the invalid mode must still be rejected with 422, proving the
    # validate_request_mode gate at the very top of the route runs
    # independently of durable readiness.
    c, app = _client()
    _configure_durable_ready(monkeypatch, app)
    r = _post_stream_durable(c, app, mode_body={"mode": "banana"})
    assert r.status_code == 422


def test_durable_route_unready_dependency_never_masks_as_mode_rejection(monkeypatch):
    # the INVERSE proof: readiness NOT configured (flag stays default OFF /
    # no supervisor) must produce 503/404 territory, distinguishable from a
    # 422 mode rejection — an unrelated missing dependency must never be
    # reported as "unsupported mode".
    c, app = _client()
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    # deliberately do NOT configure the rest — no supervisor, no HMAC key
    r = _post_stream_durable(c, app, mode_body={"mode": "chat"})
    assert r.status_code == 503
    assert "mode_unsupported" not in r.text


def test_durable_route_deep_research_never_falls_through_to_legacy(monkeypatch):
    c, app = _client()
    _configure_durable_ready(monkeypatch, app)
    called = {"legacy": 0}
    import app.routes.messages as mod
    monkeypatch.setattr(mod, "prepare_chat_context",
                        lambda **kw: called.__setitem__("legacy", called["legacy"] + 1))
    r = _post_stream_durable(c, app, mode_body={"mode": "deep_research"})
    assert r.status_code == 422
    assert called["legacy"] == 0


def test_durable_route_omitted_and_explicit_chat_reach_durable_chat(monkeypatch):
    """Through the real route: prove omitted mode and explicit "chat" both
    pass the mode gate and reach resolve_and_submit (the actual durable-chat
    submission call) — not merely that the bare gate function returns None."""
    c, app = _client()
    _configure_durable_ready(monkeypatch, app)
    calls = {"submit": [], "wire": 0}
    import app.run_engine.messages_stream as ms_mod

    class _FakeRun:
        id = "fake-run-id-1"

    def fake_submit(db, ctx, *, session_id, message, idempotency_key, mode=None):
        calls["submit"].append(idempotency_key)
        return _FakeRun(), True

    async def fake_wire(request, session_factory, ctx, run_id, *, after_seq=0):
        calls["wire"] += 1
        yield "event: start\ndata: {}\n\n"

    monkeypatch.setattr(ms_mod, "resolve_and_submit", fake_submit)
    monkeypatch.setattr(ms_mod, "stream_legacy_wire", fake_wire)
    monkeypatch.setattr("app.run_engine.context.build_auth_context",
                        lambda db, user, token_payload: object())
    monkeypatch.setattr("app.services.auth_service.decode_access_token",
                        lambda token: {"sub": "7"})

    for i, mode_body in enumerate(({}, {"mode": "chat"})):
        # each `with TestClient(...):` cycle's shutdown nulls
        # app.state.run_worker_supervisor — re-arm the fake before every
        # request, not just once before the loop.
        _configure_durable_ready(monkeypatch, app)
        r = _post_stream_durable(
            c, app, mode_body=mode_body,
            headers={"Idempotency-Key": f"reach-key-{i}", "Authorization": "Bearer faketoken"},
        )
        assert r.status_code == 200, r.text
        assert r.headers.get("x-run-id") == "fake-run-id-1"

    assert calls["submit"] == ["reach-key-0", "reach-key-1"]
    assert calls["wire"] == 2


# ═══════════════ E. Direct Run API route proof (through /api/runs) ════


def _configure_run_api_ready(monkeypatch):
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)


def _run_api_client(eng, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db
    from app.run_engine.context import get_auth_context
    from app.services.cookie_service import require_csrf

    db = mk_session(eng)
    ctx = make_ctx(db, user_id=7, sid="fam-runapi")

    def _get_db_override():
        yield db

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_auth_context] = lambda: ctx
    app.dependency_overrides[require_csrf] = lambda: None
    c = TestClient(app)
    return c, app, db


def _run_api_teardown(app):
    app.dependency_overrides.clear()


@pytest.fixture
def run_api_eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _seed_run_api_session(eng, *, uid=7, sid=1):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        db.add(ResearchSession(id=sid, user_id=uid, title="t", model="model-x",
                               system_prompt="SP"))
        db.commit()
    finally:
        db.close()


def test_direct_run_api_accepts_chat_when_dependencies_configured(run_api_eng, monkeypatch):
    _configure_run_api_ready(monkeypatch)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        r = c.post("/api/runs", json={"session_id": 1, "message": "hi", "mode": "chat"},
                   headers={"Idempotency-Key": "runapi-chat-key"})
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "queued"
    finally:
        _run_api_teardown(app)
        db.close()
    db2 = mk_session(run_api_eng)
    try:
        assert db2.query(Run).count() == 1
    finally:
        db2.close()


def test_direct_run_api_rejects_deep_research_exact_422_before_persistence(run_api_eng, monkeypatch):
    _configure_run_api_ready(monkeypatch)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        r = c.post("/api/runs", json={"session_id": 1, "message": "hi", "mode": "deep_research"},
                   headers={"Idempotency-Key": "runapi-dr-key"})
        assert r.status_code == 422
    finally:
        _run_api_teardown(app)
        db.close()
    db2 = mk_session(run_api_eng)
    try:
        assert db2.query(Run).count() == 0
        assert db2.query(Message).count() == 0
    finally:
        db2.close()


def test_direct_run_api_rejects_multi_agent_exact_422_before_persistence(run_api_eng, monkeypatch):
    _configure_run_api_ready(monkeypatch)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        r = c.post("/api/runs", json={"session_id": 1, "message": "hi", "mode": "multi_agent"},
                   headers={"Idempotency-Key": "runapi-ma-key"})
        assert r.status_code == 422
    finally:
        _run_api_teardown(app)
        db.close()
    db2 = mk_session(run_api_eng)
    try:
        assert db2.query(Run).count() == 0
    finally:
        db2.close()


def test_direct_run_api_invalid_mode_returns_validation_failure(run_api_eng, monkeypatch):
    _configure_run_api_ready(monkeypatch)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        r = c.post("/api/runs", json={"session_id": 1, "message": "hi", "mode": "banana"},
                   headers={"Idempotency-Key": "runapi-bad-key"})
        assert r.status_code == 422
    finally:
        _run_api_teardown(app)
        db.close()
    db2 = mk_session(run_api_eng)
    try:
        assert db2.query(Run).count() == 0
    finally:
        db2.close()


def test_direct_run_api_omitted_mode_defaults_to_chat(run_api_eng, monkeypatch):
    _configure_run_api_ready(monkeypatch)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        r = c.post("/api/runs", json={"session_id": 1, "message": "hi"},
                   headers={"Idempotency-Key": "runapi-omit-key"})
        assert r.status_code == 201
        run_id = r.json()["run_id"]
    finally:
        _run_api_teardown(app)
        db.close()
    db2 = mk_session(run_api_eng)
    try:
        step = (db2.query(RunStep).filter(RunStep.run_id == run_id)
                .order_by(RunStep.seq.asc()).first())
        assert json.loads(step.input_ref)["mode"] == "chat"
    finally:
        db2.close()


# ═══════════════ F. Idempotency + exec snapshot (adapter-level) ═══════


def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    ms._active_slots.clear()
    from app.services.rate_limiter import reset_rate_limiter
    reset_rate_limiter()


@pytest.fixture
def d_eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _durable_prep(eng, *, uid=7, sid=1):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        db.add(ResearchSession(id=sid, user_id=uid, title="t", model="model-x",
                               system_prompt="SP"))
        db.commit()
    finally:
        db.close()


def _durable_ctx(eng, *, uid=7, family="fam-d3a"):
    db = mk_session(eng)
    try:
        return make_ctx(db, user_id=uid, sid=family)
    finally:
        db.close()


def _durable_submit(eng, ctx, *, key=None):
    db = mk_session(eng)
    try:
        return ms.resolve_and_submit(db, ctx, session_id=1, message="hello",
                                     idempotency_key=key or uuid.uuid4().hex)
    finally:
        db.close()


def test_same_key_same_mode_replay_reuses_run(d_eng, monkeypatch):
    _env(monkeypatch)
    _durable_prep(d_eng)
    ctx = _durable_ctx(d_eng)
    key = "replay-key"
    run1, c1 = _durable_submit(d_eng, ctx, key=key)
    run2, c2 = _durable_submit(d_eng, ctx, key=key)
    assert c1 is True and c2 is False and run1.id == run2.id


def test_same_key_changed_mode_conflicts(d_eng, monkeypatch):
    _env(monkeypatch)
    _durable_prep(d_eng)
    ctx = _durable_ctx(d_eng)
    key = "conflict-mode-key"
    _durable_submit(d_eng, ctx, key=key)
    db = mk_session(d_eng)
    try:
        different_mode = sub.build_chat_submission(
            session_id=1, message="hello", image_url=None, model="model-x",
            mode="deep_research", system_prompt="SP", provider=settings.llm_provider,
        )
        from app.run_engine.errors import IdempotencyKeyConflict
        with pytest.raises(IdempotencyKeyConflict):
            sub.create_chat_run(db, ctx, submission=different_mode, idempotency_key=key)
    finally:
        db.close()


def test_canonical_mode_sealed_in_exec_snapshot(d_eng, monkeypatch):
    _env(monkeypatch)
    _durable_prep(d_eng)
    ctx = _durable_ctx(d_eng)
    run, _ = _durable_submit(d_eng, ctx)
    db = mk_session(d_eng)
    try:
        step = (db.query(RunStep).filter(RunStep.run_id == run.id)
                .order_by(RunStep.seq.asc()).first())
        assert json.loads(step.input_ref)["mode"] == "chat"
    finally:
        db.close()


def test_durable_image_rejection_unchanged():
    for mode in ("chat", "deep_research", "multi_agent", None):
        assert ms.unsupported_mode_code(has_image=True, mode=mode) == "mode_unsupported_image"


def test_durable_flag_off_by_default():
    assert settings.run_messages_stream_durable_enabled is False


# ═══════════════ G. Security / injection / canary ═══════════════════════


def test_mode_cannot_inject_internal_fields_rejected_by_schema():
    from pydantic import ValidationError
    from app.schemas.documents import ChatRequest
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"message": "hi", "mode": {"handler": "advance_chat_runs"}})
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"message": "hi", "mode": ["chat"]})


def test_mode_field_cannot_carry_run_kind_handler_or_identity(run_api_eng, monkeypatch):
    # SubmitChatRunBody (the /api/runs body) declares extra="forbid" — prove
    # a client cannot smuggle internal fields alongside a valid mode.
    from pydantic import ValidationError
    from app.routes.runs import SubmitChatRunBody
    with pytest.raises(ValidationError):
        SubmitChatRunBody.model_validate({
            "session_id": 1, "message": "hi", "mode": "chat",
            "run_kind": "chat_stream", "handler": "advance_chat_runs",
            "identity_hash": "a" * 64, "tenant_id": "evil-tenant",
        })


def test_canary_mode_absent_from_logs_events_refs_status_reasons(d_eng, monkeypatch, caplog):
    canary = "MODE-CANARY-" + "x" * 40
    with caplog.at_level("DEBUG"):
        code = ms.unsupported_mode_reason(canary)
    assert code is not None
    assert canary not in code
    assert canary not in caplog.text
    try:
        sub.validate_request_mode(canary)
    except ValueError as exc:
        assert canary not in str(exc)


# ═══════════════ H. Closeout correction — unified public-wire ════════
# ═══════════════    validation across BOTH /messages/stream and       ═
# ═══════════════    /api/runs (no per-endpoint drift)                 ═


def test_resolve_mode_delegates_to_validate_request_mode_strictly():
    # the previous asymmetry: resolve_mode() normalized case/whitespace
    # directly instead of reusing validate_request_mode(). Now it delegates
    # (+ applies ONLY the internal omitted->chat default on top) — same
    # _ALLOWED_MODES, same strict rejection, one authoritative contract.
    assert sub.resolve_mode(None) == "chat"
    assert sub.resolve_mode("chat") == "chat"
    assert sub.resolve_mode("deep_research") == "deep_research"
    assert sub.resolve_mode("multi_agent") == "multi_agent"
    for bad in _PUBLIC_WIRE_REJECTED_MODES:
        if bad is None:
            continue
        with pytest.raises(ValueError):
            sub.resolve_mode(bad)


def test_run_api_rejects_every_public_wire_invalid_mode_before_persistence(
    run_api_eng, monkeypatch,
):
    _configure_run_api_ready(monkeypatch)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        for i, bad_mode in enumerate(_PUBLIC_WIRE_REJECTED_MODES):
            r = c.post("/api/runs",
                      json={"session_id": 1, "message": "hi", "mode": bad_mode},
                      headers={"Idempotency-Key": f"runapi-bad-{i}"})
            assert r.status_code == 422, bad_mode
    finally:
        _run_api_teardown(app)
        db.close()
    db2 = mk_session(run_api_eng)
    try:
        assert db2.query(Run).count() == 0
        assert db2.query(Message).count() == 0
    finally:
        db2.close()


def test_both_public_endpoints_reject_the_identical_invalid_mode_set(
    run_api_eng, monkeypatch,
):
    """Direct parity proof: the SAME bad values reject with 422 on BOTH
    endpoints — no per-endpoint drift. Each endpoint's requests are fully
    completed (including dependency-override teardown) before the other
    endpoint's requests start — `app.dependency_overrides` lives on the ONE
    shared `app.main` singleton both `_client()` and `_run_api_client()`
    import, so interleaving would let one endpoint's teardown wipe the
    other's auth override mid-loop."""
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    c1, app1 = _client()
    stream_results = [
        _post_stream(c1, app1, mode_body={"mode": bad_mode}).status_code
        for bad_mode in _PUBLIC_WIRE_REJECTED_MODES
    ]

    _configure_run_api_ready(monkeypatch)
    _seed_run_api_session(run_api_eng)
    c2, app2, db2 = _run_api_client(run_api_eng, monkeypatch)
    try:
        run_api_results = [
            c2.post("/api/runs",
                   json={"session_id": 1, "message": "hi", "mode": bad_mode},
                   headers={"Idempotency-Key": f"parity-bad-{i}"}).status_code
            for i, bad_mode in enumerate(_PUBLIC_WIRE_REJECTED_MODES)
        ]
    finally:
        _run_api_teardown(app2)
        db2.close()

    for bad_mode, s1, s2 in zip(_PUBLIC_WIRE_REJECTED_MODES, stream_results, run_api_results):
        assert s1 == 422, bad_mode
        assert s2 == 422, bad_mode


def test_run_api_accepted_canonical_modes_pass_validation_then_hit_executability_gate(
    run_api_eng, monkeypatch,
):
    _configure_run_api_ready(monkeypatch)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        r_chat = c.post("/api/runs", json={"session_id": 1, "message": "hi", "mode": "chat"},
                        headers={"Idempotency-Key": "runapi-ok-chat"})
        assert r_chat.status_code == 201   # validated AND executable -> accepted
        r_dr = c.post("/api/runs",
                      json={"session_id": 1, "message": "hi", "mode": "deep_research"},
                      headers={"Idempotency-Key": "runapi-ok-dr"})
        assert r_dr.status_code == 422     # validated (recognized) but not executable -> rejected
        r_ma = c.post("/api/runs",
                      json={"session_id": 1, "message": "hi", "mode": "multi_agent"},
                      headers={"Idempotency-Key": "runapi-ok-ma"})
        assert r_ma.status_code == 422
    finally:
        _run_api_teardown(app)
        db.close()
    db2 = mk_session(run_api_eng)
    try:
        assert db2.query(Run).count() == 1   # only the chat submission persisted
    finally:
        db2.close()


def test_run_api_canary_absent_from_error_body_and_logs(run_api_eng, monkeypatch, caplog):
    # Bounded to <=32 chars so it reaches validate_request_mode/
    # unsupported_mode_reason (proven never to echo raw input) rather than
    # tripping pydantic's OWN max_length=32 field constraint first — that
    # structural, schema-level rejection legitimately echoes the submitted
    # value back to the SAME requester for every field in the app (not a
    # leak; not something this mode-validation correction controls or
    # should special-case away for just one field).
    canary = "CANARY-MODE-INJECT"
    _configure_run_api_ready(monkeypatch)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        with caplog.at_level("DEBUG"):
            r = c.post("/api/runs", json={"session_id": 1, "message": "hi", "mode": canary},
                      headers={"Idempotency-Key": "runapi-canary"})
    finally:
        _run_api_teardown(app)
        db.close()
    assert r.status_code == 422
    assert canary not in r.text
    assert canary not in caplog.text


def test_messages_stream_canary_absent_from_error_body_and_logs(monkeypatch, caplog):
    # bounded <=32 chars — see test_run_api_canary_absent_from_error_body_and_logs
    # for why (must reach OUR validator, not pydantic's own field-length check).
    canary = "CANARY-STREAM-INJECT"
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    c, app = _client()
    with caplog.at_level("DEBUG"):
        r = _post_stream(c, app, mode_body={"mode": canary})
    assert r.status_code == 422
    assert canary not in r.text
    assert canary not in caplog.text


def test_run_api_same_key_different_mode_is_deterministically_rejected(run_api_eng, monkeypatch):
    """The identity-conflict machinery (submission.create_chat_run /
    semantic_identity) DOES treat a changed mode as a conflict — proven
    directly and precisely at the adapter level by
    test_same_key_changed_mode_conflicts (409, unaffected by this
    correction). Through the FULL /api/runs HTTP round-trip specifically,
    that 409 can't be observed today for mode="deep_research"/"multi_agent"
    as the "different mode": the executability gate (unsupported_mode_reason)
    is intentionally MORE SPECIFIC and fires first, before the identity/
    conflict check ever runs — a same-key request with an unimplemented
    second mode is correctly bounded-rejected (422), not silently
    conflated with the original run, never a false success, never a 500.
    This test proves THAT determinism, which is what the full route can
    actually exhibit today; it is not a substitute for the adapter-level
    409 proof."""
    _configure_run_api_ready(monkeypatch)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        r1 = c.post("/api/runs", json={"session_id": 1, "message": "hi", "mode": "chat"},
                   headers={"Idempotency-Key": "runapi-conflict-key"})
        assert r1.status_code == 201
        r2 = c.post("/api/runs",
                    json={"session_id": 1, "message": "hi", "mode": "deep_research"},
                    headers={"Idempotency-Key": "runapi-conflict-key"})
        assert r2.status_code == 422   # unsupported-mode gate, not a false 201 or 500
    finally:
        _run_api_teardown(app)
        db.close()
    db2 = mk_session(run_api_eng)
    try:
        assert db2.query(Run).count() == 1   # the rejected attempt created nothing new
    finally:
        db2.close()


def test_omitted_run_api_mode_still_follows_documented_default(run_api_eng, monkeypatch):
    # unchanged by this correction: /api/runs omitting `mode` still resolves
    # to "chat" via resolve_mode's internal default (now reached via
    # validate_request_mode, same outcome as before).
    _configure_run_api_ready(monkeypatch)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        r = c.post("/api/runs", json={"session_id": 1, "message": "hi"},
                  headers={"Idempotency-Key": "runapi-omit-default"})
        assert r.status_code == 201
        run_id = r.json()["run_id"]
    finally:
        _run_api_teardown(app)
        db.close()
    db2 = mk_session(run_api_eng)
    try:
        step = (db2.query(RunStep).filter(RunStep.run_id == run_id)
                .order_by(RunStep.seq.asc()).first())
        assert json.loads(step.input_ref)["mode"] == "chat"
    finally:
        db2.close()
