"""Phase 1B.2R MP-3A — durable Run API contract + semantic HMAC + persisted text
events + SSE replay/reconnect. Service-level (in-memory SQLite) + a small HTTP
contract slice. No LLM, no MCP, no /messages/stream.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import (
    DispatchRecord,
    Message,
    ResearchSession,
    Run,
    RunEvent,
    RunPayload,
    RunSignal,
    RunStep,
)
from app.run_engine import submission as sub
from app.run_engine.engine import append_control_event, get_run, submit_run, transition_run
from app.run_engine.errors import (
    IdempotencyKeyConflict,
    RunNotFound,
    SessionBindingMismatch,
)
from app.run_engine.events import read_run_events
from app.run_engine.sse import parse_last_event_id, stream_run_events
from app.run_engine.text_events import TextEventBuffer, append_text_event
from tests.run_engine_helpers import (
    Clock,
    ensure_user,
    make_ctx,
    mk_engine,
    mk_factory,
    mk_session,
    seed_family,
)

_HMAC = "unit-test-hmac-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    # make the SSE poll loop instant
    monkeypatch.setattr("app.run_engine.sse.asyncio.sleep", _noop_sleep)


async def _noop_sleep(_):
    return None


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _seed(db, *, uid=7, sid_int=1):
    ensure_user(db, uid)
    if db.query(ResearchSession).filter(ResearchSession.id == sid_int).first() is None:
        db.add(ResearchSession(id=sid_int, user_id=uid, title="t"))
        db.commit()


def _submission(message="hello", model="m1", mode="chat", system_prompt="sp", session_id=1):
    return sub.build_chat_submission(
        session_id=session_id, message=message, image_url=None, model=model, mode=mode,
        system_prompt=system_prompt, options={},
    )


def _counts(db, run_id):
    return {
        "runs": db.query(Run).count(),
        "messages": db.query(Message).count(),
        "steps": db.query(RunStep).filter(RunStep.run_id == run_id).count(),
        "payloads": db.query(RunPayload).filter(RunPayload.run_id == run_id).count(),
        "events": db.query(RunEvent).filter(RunEvent.run_id == run_id).count(),
    }


# ── semantic idempotency HMAC ──────────────────────────────────────


def test_identical_semantic_retry_returns_one_run(eng):
    db = mk_session(eng)
    try:
        _seed(db)
        ctx = make_ctx(db, sid="fam-alpha")
        r1, c1 = sub.create_chat_run(db, ctx, submission=_submission(), idempotency_key="K1")
        before = _counts(db, r1.id)
        r2, c2 = sub.create_chat_run(db, ctx, submission=_submission(), idempotency_key="K1")
        assert r1.id == r2.id and c1 is True and c2 is False
        assert _counts(db, r1.id) == before
        assert before["messages"] == 1 and before["steps"] == 1 and before["payloads"] == 1
        assert r1.status == "queued"
    finally:
        db.close()


@pytest.mark.parametrize("changed", [
    dict(message="different prompt"),
    dict(model="m2"),
    dict(mode="deep_research"),
])
def test_changed_submission_same_key_conflicts(eng, changed):
    db = mk_session(eng)
    try:
        _seed(db)
        ctx = make_ctx(db, sid="fam-alpha")
        sub.create_chat_run(db, ctx, submission=_submission(), idempotency_key="K")
        with pytest.raises(IdempotencyKeyConflict):
            sub.create_chat_run(db, ctx, submission=_submission(**changed), idempotency_key="K")
    finally:
        db.close()


def test_client_cannot_inject_identity_fields():
    from pydantic import ValidationError
    from app.routes.runs import SubmitChatRunBody

    with pytest.raises(ValidationError):
        SubmitChatRunBody(session_id=1, message="x", client_fingerprint="evil")
    with pytest.raises(ValidationError):
        SubmitChatRunBody(session_id=1, message="x", identity_hash="deadbeef")


def test_missing_hmac_key_fails_when_feature_enabled(monkeypatch):
    monkeypatch.delenv("RUN_IDEMPOTENCY_HMAC_KEY", raising=False)
    with pytest.raises(sub.RunFeatureMisconfigured):
        sub.require_run_public_api()
    with pytest.raises(sub.RunFeatureMisconfigured):
        sub.semantic_identity(_submission())


def test_disabled_feature_is_non_disclosing(monkeypatch):
    monkeypatch.setattr(settings, "run_public_api_enabled", False)
    with pytest.raises(sub.RunFeatureDisabled):
        sub.require_run_public_api()


def test_secret_canary_absent_from_identity_and_events(eng):
    canary = "CANARY-sk-PROMPT-31337"
    db = mk_session(eng)
    try:
        _seed(db)
        ctx = make_ctx(db, sid="fam-alpha")
        run, _ = sub.create_chat_run(
            db, ctx, submission=_submission(message=f"secret {canary} here"),
            idempotency_key="K",
        )
        assert canary not in (run.idempotency_identity_hash or "")
        assert len(run.idempotency_identity_hash) == 64
        step = db.query(RunStep).filter(RunStep.run_id == run.id).first()
        assert canary not in (step.input_ref or "")
        for ev in db.query(RunEvent).filter(RunEvent.run_id == run.id).all():
            assert canary not in (ev.data or "")
        # it IS in the sealed payload (encrypted) — check it is NOT plaintext there
        pl = db.query(RunPayload).filter(RunPayload.run_id == run.id).first()
        assert canary not in pl.ciphertext
        # ...and the Message row DOES hold it (existing privacy model, by design)
        assert canary in db.query(Message).first().content
    finally:
        db.close()


# ── submission atomicity + isolation ──────────────────────────────


def test_no_execution_happens_in_submission(eng):
    db = mk_session(eng)
    try:
        _seed(db)
        ctx = make_ctx(db, sid="fam-alpha")
        run, _ = sub.create_chat_run(db, ctx, submission=_submission(), idempotency_key="K")
        assert run.status == "queued"
        assert db.query(DispatchRecord).count() == 0
        step = db.query(RunStep).filter(RunStep.run_id == run.id).first()
        assert step.status == "pending" and step.output_payload_id is None
    finally:
        db.close()


def test_cross_user_and_cross_session_isolation(eng):
    db = mk_session(eng)
    try:
        _seed(db, uid=7)
        ensure_user(db, 999)
        ctx_a = make_ctx(db, user_id=7, sid="fam-alpha")
        run, _ = sub.create_chat_run(db, ctx_a, submission=_submission(), idempotency_key="K")

        with pytest.raises(RunNotFound):                       # different user
            get_run(db, make_ctx(db, user_id=999, sid="fam-alpha"), run.id)

        seed_family(db, user_id=7, family_id="fam-beta")
        with pytest.raises(SessionBindingMismatch):            # same user, other family
            get_run(db, make_ctx(db, user_id=7, sid="fam-beta", seed=False), run.id)

        # same key, different user -> a SEPARATE run (scope includes user_id)
        _seed(db, uid=999, sid_int=2)
        run_b, _ = sub.create_chat_run(
            db, make_ctx(db, user_id=999, sid="fam-alpha"),
            submission=_submission(session_id=2), idempotency_key="K")
        assert run_b.id != run.id
    finally:
        db.close()


# ── persisted text events + batching ──────────────────────────────


def _text_of(eng, run_id):
    db = mk_session(eng)
    try:
        rows = (db.query(RunEvent)
                .filter(RunEvent.run_id == run_id, RunEvent.category == "text")
                .order_by(RunEvent.run_seq.asc()).all())
        return "".join(json.loads(r.data)["text"] for r in rows), len(rows)
    finally:
        db.close()


def _new_run(eng, *, uid=7, sid="fam-alpha"):
    db = mk_session(eng)
    try:
        _seed(db, uid=uid)
        run, _ = sub.create_chat_run(db, make_ctx(db, user_id=uid, sid=sid),
                                     submission=_submission(), idempotency_key="K")
        return run.id
    finally:
        db.close()


def test_text_batches_preserve_exact_utf8(eng):
    rid = _new_run(eng)
    src = "héllo wörld — 日本語 😀🎉 ∑∫" * 7
    buf = TextEventBuffer(mk_factory(eng), rid, tenant_id=None, user_id=7,
                          flush_bytes=8, flush_ms=100_000, max_bytes=16)
    for ch in src:
        buf.publish(ch)
    buf.close()
    joined, n = _text_of(eng, rid)
    assert joined == src and n >= 1


def test_threshold_and_final_flush(eng):
    rid = _new_run(eng)
    buf = TextEventBuffer(mk_factory(eng), rid, tenant_id=None, user_id=7,
                          flush_bytes=10, flush_ms=100_000, max_bytes=64)
    buf.publish("abc")                     # 3 bytes < 10 -> nothing yet
    assert _text_of(eng, rid)[1] == 0
    buf.publish("defghijk")               # 11 bytes -> flush
    assert _text_of(eng, rid)[1] >= 1
    buf.publish("tail")                   # buffered
    buf.close()                           # final partial flush
    joined, _ = _text_of(eng, rid)
    assert joined == "abcdefghijktail"


def test_time_flush(eng):
    rid = _new_run(eng)
    clk = Clock()
    buf = TextEventBuffer(mk_factory(eng), rid, tenant_id=None, user_id=7,
                          now_fn=clk, flush_bytes=10_000, flush_ms=50, max_bytes=64)
    buf.publish("hi")
    buf.tick()
    assert _text_of(eng, rid)[1] == 0     # not enough elapsed
    clk.advance(1)                        # +1000ms >= 50ms
    buf.tick()
    assert _text_of(eng, rid)[1] == 1


def test_per_event_max_size_chunks_on_codepoint_boundary(eng):
    rid = _new_run(eng)
    src = "😀" * 30                        # 120 bytes, 4 bytes/char
    buf = TextEventBuffer(mk_factory(eng), rid, tenant_id=None, user_id=7,
                          flush_bytes=10_000, flush_ms=100_000, max_bytes=10)
    buf.publish(src)
    buf.close()
    db = mk_session(eng)
    try:
        rows = (db.query(RunEvent)
                .filter(RunEvent.run_id == rid, RunEvent.category == "text")
                .order_by(RunEvent.run_seq.asc()).all())
        assert len(rows) > 1
        for r in rows:
            piece = json.loads(r.data)["text"]
            assert len(piece.encode("utf-8")) <= 10
        assert "".join(json.loads(r.data)["text"] for r in rows) == src
    finally:
        db.close()


def test_append_text_event_rejects_oversize(eng):
    from app.run_engine.errors import ControlEventTooLarge

    rid = _new_run(eng)
    db = mk_session(eng)
    try:
        with pytest.raises(ControlEventTooLarge):
            append_text_event(db, run_id=rid, tenant_id=None, user_id=7,
                              text="x" * (int(settings.run_text_event_max_bytes) + 1))
    finally:
        db.close()


def test_control_and_text_share_one_ordered_sequence(eng):
    rid = _new_run(eng)
    buf = TextEventBuffer(mk_factory(eng), rid, tenant_id=None, user_id=7,
                          flush_bytes=1, flush_ms=1, max_bytes=64)
    buf.publish("one")
    db = mk_session(eng)
    try:
        append_control_event(db, make_ctx(db, sid="fam-alpha"), rid, type="run.status",
                             data={"from_status": "queued", "to_status": "running"})
    finally:
        db.close()
    buf.publish("two")
    buf.close()
    db = mk_session(eng)
    try:
        rows = read_run_events(db, make_ctx(db, sid="fam-alpha"), rid, after_seq=0)
        seqs = [r.run_seq for r in rows]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
        cats = [r.category for r in rows]
        assert "control" in cats and "text" in cats
    finally:
        db.close()


# ── SSE replay / reconnect ────────────────────────────────────────


class _FakeRequest:
    def __init__(self, disconnect_after=None):
        self.n = 0
        self._after = disconnect_after

    async def is_disconnected(self):
        self.n += 1
        return self._after is not None and self.n > self._after


async def _collect(gen, limit=10_000):
    out = []
    async for frame in gen:
        out.append(frame)
        if len(out) >= limit:
            break
    return out


def _ids(frames):
    ids = []
    for f in frames:
        for line in f.splitlines():
            if line.startswith("id: "):
                ids.append(int(line[4:]))
    return ids


def _terminal_run(eng, rid):
    db = mk_session(eng)
    try:
        ctx = make_ctx(db, sid="fam-alpha")
        run = db.query(Run).filter(Run.id == rid).first()
        v = run.version
        for a, b in (("queued", "running"), ("running", "succeeded")):
            transition_run(db, ctx, rid, to=b, expected_version=v)
            v += 1
    finally:
        db.close()


def test_reconnect_replays_by_seq_without_gap_or_duplicate(eng):
    rid = _new_run(eng)
    buf = TextEventBuffer(mk_factory(eng), rid, tenant_id=None, user_id=7,
                          flush_bytes=1, flush_ms=1, max_bytes=64)
    for i in range(6):
        buf.publish(f"chunk{i} ")
    buf.close()
    _terminal_run(eng, rid)

    db_ctx = make_ctx(mk_session(eng), sid="fam-alpha")
    a = asyncio.run(_collect(stream_run_events(_FakeRequest(), mk_factory(eng), db_ctx,
                                               rid, after_seq=0)))
    all_ids = _ids(a)
    assert all_ids == sorted(all_ids) == list(range(all_ids[0], all_ids[0] + len(all_ids)))
    assert len(all_ids) == len(set(all_ids))

    cut = all_ids[len(all_ids) // 2]
    b = asyncio.run(_collect(stream_run_events(_FakeRequest(), mk_factory(eng), db_ctx,
                                               rid, after_seq=cut)))
    b_ids = _ids(b)
    assert b_ids and min(b_ids) > cut
    assert set(b_ids).isdisjoint({i for i in all_ids if i <= cut})


def test_disconnect_closes_connection_only_no_cancel(eng):
    rid = _new_run(eng)                       # status = queued, non-terminal
    ctx = make_ctx(mk_session(eng), sid="fam-alpha")
    frames = asyncio.run(_collect(
        stream_run_events(_FakeRequest(disconnect_after=1), mk_factory(eng), ctx, rid,
                          after_seq=0)))
    db = mk_session(eng)
    try:
        run = db.query(Run).filter(Run.id == rid).first()
        assert run.status == "queued"         # untouched
        assert db.query(RunSignal).filter(RunSignal.run_id == rid).count() == 0
    finally:
        db.close()


def test_revoked_family_closes_stream_safely(eng):
    rid = _new_run(eng)
    ctx = make_ctx(mk_session(eng), sid="fam-alpha")
    db = mk_session(eng)
    try:
        from sqlalchemy import text as _t
        db.execute(_t("UPDATE refresh_sessions SET revoked_at=CURRENT_TIMESTAMP "
                      "WHERE family_id='fam-alpha'"))
        db.commit()
    finally:
        db.close()
    frames = asyncio.run(_collect(
        stream_run_events(_FakeRequest(disconnect_after=3), mk_factory(eng), ctx, rid,
                          after_seq=0)))
    assert any("event: error" in f for f in frames)
    db = mk_session(eng)
    try:
        assert db.query(Run).filter(Run.id == rid).first().status == "queued"
    finally:
        db.close()


def test_bounded_page_reads(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_event_read_max_page", 5)
    rid = _new_run(eng)
    buf = TextEventBuffer(mk_factory(eng), rid, tenant_id=None, user_id=7,
                          flush_bytes=1, flush_ms=1, max_bytes=64)
    for i in range(40):
        buf.publish(f"e{i} ")
    buf.close()
    db = mk_session(eng)
    try:
        page = read_run_events(db, make_ctx(db, sid="fam-alpha"), rid, after_seq=0)
        assert len(page) == 5                 # bounded regardless of ledger size
    finally:
        db.close()
    _terminal_run(eng, rid)
    ctx = make_ctx(mk_session(eng), sid="fam-alpha")
    frames = asyncio.run(_collect(stream_run_events(_FakeRequest(), mk_factory(eng), ctx,
                                                    rid, after_seq=0)))
    ids = _ids(frames)
    assert len(ids) == len(set(ids)) and ids == sorted(ids)   # multi-page, no dupes


def test_heartbeat_is_a_comment_not_a_persisted_event(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_sse_heartbeat_s", 0)
    rid = _new_run(eng)                       # non-terminal, no events after seq 2
    ctx = make_ctx(mk_session(eng), sid="fam-alpha")
    db = mk_session(eng)
    try:
        before = db.query(RunEvent).filter(RunEvent.run_id == rid).count()
    finally:
        db.close()
    frames = asyncio.run(_collect(
        stream_run_events(_FakeRequest(disconnect_after=4), mk_factory(eng), ctx, rid,
                          after_seq=1000),   # past the tail -> only heartbeats
        limit=50))
    assert any(f.startswith(": heartbeat") for f in frames)
    db = mk_session(eng)
    try:
        assert db.query(RunEvent).filter(RunEvent.run_id == rid).count() == before
    finally:
        db.close()


def test_parse_last_event_id():
    assert parse_last_event_id("7") == 7
    assert parse_last_event_id(None) == 0
    assert parse_last_event_id("garbage") == 0
    assert parse_last_event_id("-3") == 0


# ── HTTP contract (real app, real auth/CSRF) ──────────────────────


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    with TestClient(app) as c:
        yield c


def _login(client, tag):
    from tests.auth_helpers import auth_headers, register_and_login
    import datetime as _d
    uid, token = register_and_login(
        client, username=f"{tag}_{_d.datetime.now().timestamp():.6f}".replace(".", ""))
    h = auth_headers(token)
    csrf = client.cookies.get("research_assistant_csrf_token")
    if csrf:
        h["X-CSRF-Token"] = csrf
    return uid, h


def _make_session(client, headers):
    r = client.post("/api/sessions", json={"title": "mp3a"}, headers=headers)
    assert r.status_code in (200, 201), r.text[:300]
    return r.json()["id"]


def test_http_flag_off_returns_404(client, monkeypatch):
    monkeypatch.setattr(settings, "run_public_api_enabled", False)
    _uid, h = _login(client, "mp3a_off")
    r = client.post("/api/runs", json={"session_id": 1, "message": "hi"},
                    headers={**h, "Idempotency-Key": "k1"})
    assert r.status_code == 404


def test_http_submit_status_cancel_and_idempotency(client, monkeypatch):
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    # MP-3C1: a chat_stream submission is refused unless the durable driver can run it
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    _uid, h = _login(client, "mp3a_ok")
    sid = _make_session(client, h)

    # missing Idempotency-Key -> 422
    r = client.post("/api/runs", json={"session_id": sid, "message": "hello"}, headers=h)
    assert r.status_code == 422

    hk = {**h, "Idempotency-Key": "run-key-1"}
    r1 = client.post("/api/runs", json={"session_id": sid, "message": "hello"}, headers=hk)
    assert r1.status_code == 201, r1.text[:300]
    run_id = r1.json()["run_id"]
    assert r1.json()["status"] == "queued" and r1.json()["created"] is True

    r2 = client.post("/api/runs", json={"session_id": sid, "message": "hello"}, headers=hk)
    assert r2.status_code == 201 and r2.json()["run_id"] == run_id and r2.json()["created"] is False

    r3 = client.post("/api/runs", json={"session_id": sid, "message": "CHANGED"}, headers=hk)
    assert r3.status_code == 409

    g = client.get(f"/api/runs/{run_id}", headers=h)
    assert g.status_code == 200 and g.json()["status"] == "queued"

    c = client.post(f"/api/runs/{run_id}/cancel", headers=h)
    assert c.status_code == 200 and c.json()["cancellation_requested"] is True

    # another user cannot see it
    _uid2, h2 = _login(client, "mp3a_other")
    assert client.get(f"/api/runs/{run_id}", headers=h2).status_code == 404
