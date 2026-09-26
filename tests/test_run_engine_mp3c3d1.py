"""Phase 1B.2R MP-3C3D1 — server-owned mode routing for durable plain chat.

Fixes: the durable `/messages/stream` unsupported-mode gate previously read the
*global* `enable_deep_research` / `enable_multi_agent` settings — flags that
describe what the LEGACY pipeline is configured to do, not what a specific
durable request asked for. Any self-host deployment with either enabled
globally would have durable chat 422 on every message, including plain chat,
the instant `run_messages_stream_durable_enabled` turned on.

This file proves the fix in isolation: routing now uses one server-owned
`DURABLE_STREAM_MODE` ("chat" — the only mode this route can currently serve),
independent of those global flags, still fed into the existing MP-3A semantic
identity + encrypted execution snapshot, still fail-closed for anything else.
No later MP-3C3D slice (citations, memory, deep research, multi-agent
execution) is implemented or tested here.
"""

from __future__ import annotations

import json
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import Message, MessageRole, ResearchSession, Run, RunEvent, RunStep
from app.run_engine import messages_stream as ms
from app.run_engine import submission as sub
from app.run_engine.errors import IdempotencyKeyConflict
from app.run_engine.worker import RunWorker
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_factory, mk_session

_HMAC = "mp3c3d1-unit-test-hmac-key-0123456789"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_messages_stream_max_concurrent_per_user", 4)
    ms._active_slots.clear()
    from app.services.rate_limiter import reset_rate_limiter
    reset_rate_limiter()
    yield
    ms._active_slots.clear()


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


# ── helpers (mirrors tests/test_run_engine_mp3c3b.py's private helpers) ──


def _prep(eng, *, uid=7, sid=1, model="model-x", system_prompt="SP"):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        if db.query(ResearchSession).filter(ResearchSession.id == sid).first() is None:
            db.add(ResearchSession(id=sid, user_id=uid, title="t", model=model,
                                   system_prompt=system_prompt))
            db.commit()
    finally:
        db.close()


def _ctx(eng, *, uid=7, family="fam-d1"):
    db = mk_session(eng)
    try:
        return make_ctx(db, user_id=uid, sid=family)
    finally:
        db.close()


def _submit(eng, ctx, *, message="hello there", key=None):
    db = mk_session(eng)
    try:
        return ms.resolve_and_submit(db, ctx, session_id=1, message=message,
                                     idempotency_key=key or uuid.uuid4().hex)
    finally:
        db.close()


class _DeadSup:
    def is_durable_path_ready(self):
        return False


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app), app


# ═══════════ 1. global-flag independence (the actual bug fix) ═══════


@pytest.mark.parametrize("dr, ma", [(True, True), (True, False), (False, True), (False, False)])
def test_plain_chat_accepted_regardless_of_global_capability_flags(monkeypatch, eng, dr, ma):
    monkeypatch.setattr(settings, "enable_deep_research", dr)
    monkeypatch.setattr(settings, "enable_multi_agent", ma)
    assert ms.unsupported_mode_code(has_image=False) is None
    _prep(eng)
    run, created = _submit(eng, _ctx(eng), message=f"hi dr={dr} ma={ma}")
    assert created is True
    assert run.status == "queued"


# ═══════════ 2. mode preserved in snapshot + reaches the worker ═════


def test_run_step_snapshot_records_mode_chat(eng):
    _prep(eng)
    run, _ = _submit(eng, _ctx(eng), message="check snapshot")
    db = mk_session(eng)
    try:
        step = (db.query(RunStep).filter(RunStep.run_id == run.id)
                .order_by(RunStep.seq.asc()).first())
        assert json.loads(step.input_ref)["mode"] == "chat"
    finally:
        db.close()


def test_worker_receives_snapshotted_mode_chat(eng):
    _prep(eng)
    run, _ = _submit(eng, _ctx(eng), message="drive it")
    captured: dict = {}

    class _Prov:
        async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
            yield {"type": "done", "response": "ok"}

    def _chat_provider(chat_input, **_k):
        captured["mode"] = chat_input.mode
        return _Prov()

    RunWorker(mk_factory(eng), worker_id="w-d1",
             chat_provider=_chat_provider).advance_chat_runs()
    assert captured.get("mode") == "chat"


# ═══════════ 3. idempotency covers the final selected mode ══════════


def test_identical_retry_reuses_one_run(eng):
    _prep(eng)
    ctx = _ctx(eng)
    key = "retry-mode-1"
    run1, c1 = _submit(eng, ctx, message="same text", key=key)
    run2, c2 = _submit(eng, ctx, message="same text", key=key)
    assert c1 is True and c2 is False and run1.id == run2.id
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 1
        assert db.query(RunStep).filter(RunStep.run_id == run1.id).count() == 1
    finally:
        db.close()


def test_semantic_identity_includes_resolved_mode():
    explicit = sub.build_chat_submission(
        session_id=1, message="hi", image_url=None, model="m", mode="chat",
        system_prompt=None, provider="p",
    )
    default = sub.build_chat_submission(
        session_id=1, message="hi", image_url=None, model="m", mode=None,
        system_prompt=None, provider="p",
    )
    assert explicit.resolved_mode == "chat" == default.resolved_mode
    # this route's switch from mode=None to mode=DURABLE_STREAM_MODE is a
    # no-op for existing idempotency, not a behavior change.
    assert sub.semantic_identity(explicit) == sub.semantic_identity(default)


def test_behavior_changing_mode_cannot_reuse_identity(eng):
    _prep(eng)
    ctx = _ctx(eng)
    key = "mode-swap-key"
    _submit(eng, ctx, message="same text", key=key)
    db = mk_session(eng)
    try:
        different_mode = sub.build_chat_submission(
            session_id=1, message="same text", image_url=None, model="model-x",
            mode="deep_research", system_prompt="SP", provider=settings.llm_provider,
        )
        with pytest.raises(IdempotencyKeyConflict):
            sub.create_chat_run(db, ctx, submission=different_mode, idempotency_key=key)
    finally:
        db.close()


# ═══════════ 4. unsupported / injected mode rejected, no override ═══


def test_injected_mode_rejected_before_persistence(eng):
    # "deep_research" is checked separately below (MP-3C3D4C): its bounded
    # reason is now a dedicated config-dependent slug
    # ("deep_research_not_executable"), computed via the ONE authoritative
    # readiness predicate, rather than the generic "mode_unsupported_"
    # prefix every OTHER unrecognized/unimplemented mode still gets.
    for bad in ("banana", "unknown_mode",
                "../../etc/passwd", "x" * 100):
        code = ms.unsupported_mode_code(has_image=False, mode=bad)
        assert code is not None and code.startswith("mode_unsupported")
        assert len(code) <= 64          # bounded even for a pathological input

    # MP-3C3D5C closeout: "multi_agent" is NO LONGER generic/unimplemented — it
    # has the SAME dedicated, config-AND-implementation-dependent reason shape
    # as deep_research (computed fresh via the one authoritative
    # multi_agent_readiness.multi_agent_executable() predicate). The loop above
    # still carried the pre-D5C generic expectation for it. Corrected to the
    # superseding contract — the assertion stays equally exact, just for the
    # right code (mirrors the identical reconciliation already made in
    # test_run_engine_mp3c3b.py). Historical test reconciliation only; no
    # production behavior changed by this edit.
    assert ms.unsupported_mode_code(has_image=False, mode="multi_agent") == (
        "multi_agent_not_executable"
    )

    dr_code = ms.unsupported_mode_code(has_image=False, mode="deep_research")
    assert dr_code == "deep_research_not_executable"  # flags OFF by default in this fixture
    assert len(dr_code) <= 64
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
        assert db.query(Message).count() == 0
    finally:
        db.close()


def test_injected_mode_canary_never_reaches_bounded_code(caplog):
    canary = "SECRET-CANARY-VALUE"
    with caplog.at_level("INFO"):
        code = ms.unsupported_mode_code(has_image=False, mode=canary + "!!!")
    assert code is not None
    assert canary not in code
    assert canary not in caplog.text


def test_unknown_extra_body_field_cannot_override_mode():
    # MP-3C3D3A superseded this test's original premise: ChatRequest now has
    # a REAL, intentional `mode` field (the whole point of that phase) — a
    # client CAN set it, and the route validates/canonicalizes it via
    # submission.validate_request_mode (see tests/test_run_engine_mp3c3d3a.py
    # for that full contract). What must STILL hold, and what this test now
    # verifies, is the narrower property: a client cannot smuggle in
    # UNRELATED/internal-sounding fields (run kind, handler name, identity
    # hash) that ChatRequest never declared — pydantic's default
    # extra="ignore" drops them silently, same as before this phase.
    from app.schemas.documents import ChatRequest
    payload = ChatRequest.model_validate({
        "message": "hello",
        "mode": "chat",
        "run_kind": "chat_stream",           # not a declared field — must be dropped
        "handler": "advance_chat_runs",      # not a declared field — must be dropped
        "identity_hash": "a" * 64,           # not a declared field — must be dropped
    })
    assert payload.mode == "chat"
    assert not hasattr(payload, "run_kind")
    assert not hasattr(payload, "handler")
    assert not hasattr(payload, "identity_hash")
    assert payload.message == "hello"
    # the durable route still derives DURABLE_STREAM_MODE when mode is
    # omitted, exactly as before this phase.
    assert ms.unsupported_mode_code(
        has_image=payload.image_url is not None, mode=ms.DURABLE_STREAM_MODE,
    ) is None


def test_image_url_remains_rejected_before_persistence(eng):
    assert ms.unsupported_mode_code(has_image=True) == "mode_unsupported_image"
    # image rejection takes priority over mode, regardless of what mode is
    assert ms.unsupported_mode_code(has_image=True, mode="deep_research") == (
        "mode_unsupported_image"
    )
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
    finally:
        db.close()


# ═══════════ 5. dependency/readiness ordering + zero direct exec ════


def test_unhealthy_worker_returns_503_before_persistence(eng):
    with pytest.raises(ms.DurableStreamError) as ei:
        ms.assert_durable_stream_ready(_DeadSup())
    assert ei.value.http_status == 503
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
    finally:
        db.close()


def test_submission_creates_no_execution(eng):
    _prep(eng)
    run, _ = _submit(eng, _ctx(eng), message="no exec please")
    db = mk_session(eng)
    try:
        r = db.query(Run).filter(Run.id == run.id).first()
        assert r.status == "queued"
        assert db.query(Message).filter(Message.role == MessageRole.assistant).count() == 0
        assert db.query(RunEvent).filter(RunEvent.run_id == run.id,
                                         RunEvent.category == "text").count() == 0
    finally:
        db.close()


# ═══════════ 6. legacy path unaffected when the flag is off ═════════


def test_durable_flag_off_preserves_legacy_and_global_flag_behavior(monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "run_engine_enabled", False)
    monkeypatch.setattr(settings, "run_worker_enabled", False)
    # global flags at their realistic self-host defaults — must still govern
    # the LEGACY pipeline exactly as before; this fix must not touch that.
    monkeypatch.setattr(settings, "enable_deep_research", True)
    monkeypatch.setattr(settings, "enable_multi_agent", True)
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
    monkeypatch.setattr(mod, "save_assistant_message",
                        lambda **kw: type("M", (), {"id": 7})())
    monkeypatch.setattr(mod, "run_memory_extraction", lambda **kw: None)
    monkeypatch.setattr(mod, "_stream_chat_durable",
                        lambda *a, **k: called.__setitem__("durable", 1))

    c, app = _client()
    with c:
        from app.services.auth_service import get_current_user
        from app.services.cookie_service import require_csrf
        from app.database import get_db
        app.dependency_overrides[get_current_user] = lambda: type("U", (), {"id": 7})()
        app.dependency_overrides[require_csrf] = lambda: None
        try:
            r = c.post("/api/sessions/1/messages/stream", json={"message": "hi"})
        finally:
            app.dependency_overrides.clear()
    assert r.status_code == 200
    assert "event: complete" in r.text
    assert called["legacy"] == 1 and called["durable"] == 0
    assert "X-Run-Id" not in r.headers
