"""Phase 1B.2R MP-4C decision 2 - canonical Run-bound memory/directive proposals.

Exercises the REAL authority end to end: the trusted registry, the approval
gateway, the at-most-once DispatchRecord ledger, the approvals decision path,
and the REAL write handlers (app.services.memory_service.save_memory /
app.services.directive_service.save_directive). No HTTP, no live provider,
no Ollama.
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import (
    AgentDirective, DispatchRecord, Memory, Message, MessageRole, PendingApproval,
    ResearchSession, Run, RunSignal, RunStep,
)
from app.routes.approvals import _decide, _owned_approval
from app.run_engine import submission as sub
from app.run_engine.worker import RunWorker
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_factory, mk_session

_HMAC = "mp4c-save-unit-test-hmac-key-0123456789"
_MEM_CANARY = "MEM-CANARY the user prefers bullet points 7f3a"
_DIR_CANARY = "DIR-CANARY always answer in plain words 5b2c"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_engine_memory_write_enabled", True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 30)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)
    monkeypatch.setattr(settings, "run_chat_max_tool_cycles", 3)
    monkeypatch.setattr(settings, "run_chat_tool_marker_max_bytes", 4096)
    monkeypatch.setattr(settings, "run_engine_memory_write_max_chars", 2000)


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


class FakeChat:
    """Streaming provider. One script per provider invocation."""

    def __init__(self, scripts, *, sleep=0.0):
        self.scripts = [list(s) for s in scripts]
        self.sleep = sleep
        self.calls = []
        self.n = 0

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        self.calls.append({"messages": [dict(m) for m in messages],
                           "system_prompt": system_prompt, "model_name": model_name})
        toks = self.scripts[self.n] if self.n < len(self.scripts) else []
        self.n += 1
        for t in toks:
            yield {"type": "token", "token": t}
        yield {"type": "done", "response": "".join(x for x in toks if isinstance(x, str))}


def _seed(eng, *, uid=7, sid="fam-alpha", session_id=1, message="hi"):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        if db.query(ResearchSession).filter(ResearchSession.id == session_id).first() is None:
            db.add(ResearchSession(id=session_id, user_id=uid, title="t"))
            db.commit()
        ctx = make_ctx(db, user_id=uid, sid=sid)
        submission = sub.build_chat_submission(
            session_id=session_id, message=message, image_url=None, provider="prov-1",
            model="model-1", mode="chat", system_prompt="SYS-PROMPT", options={})
        run, created = sub.create_chat_run(db, ctx, submission=submission,
                                           idempotency_key=uuid.uuid4().hex)
        assert created
        return run.id
    finally:
        db.close()


def _worker(eng, provider=None):
    return RunWorker(mk_factory(eng), worker_id="w-save",
                     chat_provider=(lambda ci, **_k: provider) if provider is not None else None)


def _run(eng, rid):
    db = mk_session(eng)
    try:
        return db.query(Run).filter(Run.id == rid).first()
    finally:
        db.close()


def _steps(eng, rid):
    db = mk_session(eng)
    try:
        return (db.query(RunStep).filter(RunStep.run_id == rid)
                .order_by(RunStep.seq.asc()).all())
    finally:
        db.close()


def _tool_step(eng, rid):
    for s in _steps(eng, rid):
        if s.kind == "tool_dispatch":
            return s
    return None


def _memories(eng, uid=7):
    db = mk_session(eng)
    try:
        return db.query(Memory).filter(Memory.user_id == uid).all()
    finally:
        db.close()


def _directives(eng, uid=7):
    db = mk_session(eng)
    try:
        return db.query(AgentDirective).filter(AgentDirective.user_id == uid).all()
    finally:
        db.close()


def _approvals(eng, uid=7):
    db = mk_session(eng)
    try:
        return db.query(PendingApproval).filter(PendingApproval.user_id == uid).all()
    finally:
        db.close()


def _dispatch(eng, tool_call_id):
    db = mk_session(eng)
    try:
        return (db.query(DispatchRecord)
                .filter(DispatchRecord.tool_call_id == tool_call_id).one_or_none())
    finally:
        db.close()


def _assistant_msgs(eng, session_id=1):
    db = mk_session(eng)
    try:
        return (db.query(Message).filter(Message.session_id == session_id,
                                         Message.role == MessageRole.assistant)
                .order_by(Message.id.asc()).all())
    finally:
        db.close()


def _decide_run(eng, aid, *, approve):
    db = mk_session(eng)
    try:
        row = db.query(PendingApproval).filter(PendingApproval.id == aid).one()
        return _decide(db, row, approve=approve)
    finally:
        db.close()


def _leak_blob(eng, rid, *, caplog_text=""):
    db = mk_session(eng)
    try:
        parts = [caplog_text or ""]
        r = db.query(Run).filter(Run.id == rid).first()
        parts.append(r.status_reason or "")
        from app.models.models import RunEvent
        for e in db.query(RunEvent).filter(RunEvent.run_id == rid).all():
            parts.append(e.data or "")
        for s in db.query(RunStep).filter(RunStep.run_id == rid).all():
            parts.append(s.input_ref or "")
            parts.append(s.output_ref or "")
            parts.append(s.error_code or "")
        for sg in db.query(RunSignal).filter(RunSignal.run_id == rid).all():
            parts.append(sg.payload or "")
        return "\n".join(parts)
    finally:
        db.close()


def _save_script(kind, content, *, prefix="Sure. ", suffix="TAIL-MUST-NOT-APPEAR"):
    opener = {"memory": ("[SAVE", "_MEMORY: "),
              "directive": ("[SAVE", "_DIRECTIVE: ")}[kind]
    mid = max(1, len(content) // 2)
    return [prefix, opener[0], opener[1],
            content[:mid], content[mid:], "]", "\n", suffix]


def _park(eng, prov):
    rid = _seed(eng)
    _worker(eng, prov).advance_chat_runs()
    return rid


# ── 1. approve exactly once -> the real handler persists the exact row ──

def test_memory_proposal_approve_persists_exact_row_once(eng, caplog):
    content = _MEM_CANARY
    prov = FakeChat([_save_script("memory", content), ["I saved ", "that."]])
    with caplog.at_level("DEBUG"):
        rid = _park(eng, prov)
        r = _run(eng, rid)
        assert r.status == "waiting_for_approval" and r.pending_approval_id

        appr = _approvals(eng)[0]
        assert appr.tool_id == "memory_write"
        assert appr.state == "requested"
        assert appr.run_id == rid and appr.session_id == 1
        # the redacted approval view never carries the content
        assert content not in (appr.display_summary or "")
        assert content not in (appr.purpose_redacted or "")
        assert _memories(eng) == []          # NOTHING written before approval

        # the owner-only preview shows the EXACT content
        from app.approval.action_preview import (ActionPreviewError,
                                                 build_save_action_preview)
        pdb = mk_session(eng)
        try:
            row = pdb.query(PendingApproval).filter(PendingApproval.id == r.pending_approval_id).one()
            pv = build_save_action_preview(pdb, row, actor_user_id=7)
            assert pv["kind"] == "memory" and pv["content"] == content
            assert pv["content_chars"] == len(content)
            with pytest.raises(ActionPreviewError):
                build_save_action_preview(pdb, row, actor_user_id=99)
        finally:
            pdb.close()

        dec = _decide_run(eng, r.pending_approval_id, approve=True)
        assert dec["state"] == "approved"
        assert _memories(eng) == []          # deciding executes nothing

        _worker(eng, prov).advance_chat_runs()

    rows = _memories(eng)
    assert len(rows) == 1
    assert rows[0].content == content
    assert rows[0].category == "preference"
    assert rows[0].session_id == 1
    r = _run(eng, rid)
    assert r.status == "succeeded"
    step = _tool_step(eng, rid)
    assert step.status == "succeeded"
    assert _dispatch(eng, step.tool_call_id).outcome == "succeeded"
    msgs = _assistant_msgs(eng)
    assert len(msgs) == 1 and msgs[0].content == "Sure. I saved that."

    # exactly once: another full sweep writes no second row / dispatch
    _worker(eng, prov).advance_chat_runs()
    assert len(_memories(eng)) == 1

    blob = _leak_blob(eng, rid, caplog_text=caplog.text)
    assert content not in blob
    assert "SAVE_MEMORY" not in blob and "TAIL-MUST-NOT-APPEAR" not in blob


def test_directive_proposal_approve_persists_exact_row_once(eng, caplog):
    content = _DIR_CANARY
    prov = FakeChat([_save_script("directive", content), ["Noted."]])
    caplog.set_level("DEBUG")
    rid = _park(eng, prov)
    r = _run(eng, rid)
    assert r.status == "waiting_for_approval"
    assert _approvals(eng)[0].tool_id == "directive_write"
    assert _directives(eng) == []
    _decide_run(eng, r.pending_approval_id, approve=True)
    _worker(eng, prov).advance_chat_runs()
    rows = _directives(eng)
    assert len(rows) == 1
    assert rows[0].content == content and rows[0].is_active is True
    assert _run(eng, rid).status == "succeeded"
    step = _tool_step(eng, rid)
    assert _dispatch(eng, step.tool_call_id).outcome == "succeeded"
    assert content not in caplog.text


# ── 2. deny / expire -> zero writes ────────────────────────────────────

def test_deny_writes_zero_rows_and_denies_the_run(eng):
    prov = FakeChat([_save_script("memory", _MEM_CANARY), ["must not run"]])
    rid = _park(eng, prov)
    aid = _run(eng, rid).pending_approval_id
    assert _decide_run(eng, aid, approve=False)["state"] == "denied"
    _worker(eng, prov).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "denied"
    assert _memories(eng) == []
    assert _directives(eng) == []
    assert len(prov.calls) == 1               # no continuation generation
    # the bounded user notice says nothing was written
    from app.run_engine.messages_stream import _terminal_frame
    db = mk_session(eng)
    try:
        rr = db.query(Run).filter(Run.id == rid).one()
        frame = _terminal_frame(db, rr)
        assert "Nothing was written" in frame
        assert _MEM_CANARY not in frame
    finally:
        db.close()


def test_expired_approval_writes_zero_rows(eng):
    prov = FakeChat([_save_script("directive", _DIR_CANARY), ["must not run"]])
    rid = _park(eng, prov)
    aid = _run(eng, rid).pending_approval_id
    db = mk_session(eng)
    try:
        row = db.query(PendingApproval).filter(PendingApproval.id == aid).one()
        row.expires_at = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)
        db.commit()
    finally:
        db.close()
    _worker(eng, prov).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "failed" and r.status_reason == "approval_expired"
    assert _memories(eng) == [] and _directives(eng) == []
    assert len(prov.calls) == 1


# ── 3. changed arguments / wrong user -> zero writes ───────────────────

def test_changed_arguments_write_nothing(eng):
    prov = FakeChat([_save_script("memory", _MEM_CANARY), ["must not run"]])
    rid = _park(eng, prov)
    r = _run(eng, rid)
    step = _tool_step(eng, rid)
    db = mk_session(eng)
    try:
        s = db.query(RunStep).filter(RunStep.id == step.id).one()
        ref = json.loads(s.input_ref)
        ref["args_hash"] = "0" * 64          # tamper the bound args hash
        s.input_ref = json.dumps(ref)
        db.commit()
    finally:
        db.close()
    _decide_run(eng, r.pending_approval_id, approve=True)
    _worker(eng, prov).advance_chat_runs()
    rr = _run(eng, rid)
    assert rr.status == "failed" and rr.status_reason == "gateway_error"
    assert _memories(eng) == []
    assert _dispatch(eng, step.tool_call_id) is None   # rejected before the gateway


def test_wrong_user_cannot_see_or_decide_the_proposal(eng):
    from fastapi import HTTPException
    prov = FakeChat([_save_script("memory", _MEM_CANARY)])
    rid = _park(eng, prov)
    aid = _run(eng, rid).pending_approval_id
    db = mk_session(eng)
    try:
        class _Other:
            id = 99
        class _Owner:
            id = 7
        with pytest.raises(HTTPException):
            _owned_approval(db, aid, _Other())
        assert _owned_approval(db, aid, _Owner()).id == aid
    finally:
        db.close()
    # no owner decision happened -> still parked, still zero writes
    assert _run(eng, rid).status == "waiting_for_approval"
    assert _memories(eng) == []


# ── 4. refuse BEFORE any proposal: sensitive / oversized / flag off ────

def test_sensitive_content_creates_no_proposal_and_writes_nothing(eng):
    secret = "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q7R8S9T0"   # 40-char token
    prov = FakeChat([_save_script("memory", secret)])
    rid = _park(eng, prov)
    r = _run(eng, rid)
    assert r.status == "failed" and r.status_reason == "save_blocked_sensitive"
    assert _approvals(eng) == [] and _memories(eng) == []
    assert secret not in _leak_blob(eng, rid)


def test_oversized_content_creates_no_proposal(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_engine_memory_write_max_chars", 10)
    prov = FakeChat([_save_script("memory", "0123456789ABCDEFGHIJ")])
    rid = _park(eng, prov)
    r = _run(eng, rid)
    assert r.status == "failed" and r.status_reason == "save_content_too_large"
    assert _approvals(eng) == [] and _memories(eng) == []


def test_flag_off_defers_and_writes_nothing(eng, monkeypatch, caplog):
    monkeypatch.setattr(settings, "run_engine_memory_write_enabled", False)
    content = _MEM_CANARY
    prov = FakeChat([_save_script("memory", content)])
    with caplog.at_level("DEBUG"):
        rid = _park(eng, prov)
        r = _run(eng, rid)
        assert r.status == "failed" and r.status_reason == "nonexec_marker_deferred"
        assert _approvals(eng) == [] and _memories(eng) == []
        blob = _leak_blob(eng, rid, caplog_text=caplog.text)
        assert content not in blob and "SAVE_MEMORY" not in blob

# ── 5. HTTP-route integration: preview + approved write ────────────────
#
# IN-PROCESS integration: the real FastAPI app + real route functions via
# TestClient, with get_db pointed at this test's engine and get_current_user
# overridden so a second identity can attempt the same approval. No server
# process, no browser, no live provider.

@pytest.fixture
def api(eng):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app
    from app.services.auth_service import get_current_user
    from app.services.cookie_service import require_csrf

    holder = {"uid": 7}

    def _db():
        s = mk_session(eng)
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_csrf] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=holder["uid"])
    with TestClient(app) as c:
        yield c, holder
    app.dependency_overrides.clear()


def test_preview_route_shows_exact_content_to_the_owner_only(eng, api):
    client, holder = api
    content = _MEM_CANARY
    prov = FakeChat([_save_script("memory", content)])
    rid = _park(eng, prov)
    aid = _run(eng, rid).pending_approval_id
    assert aid

    holder["uid"] = 7
    r = client.get("/api/approvals/%s/action" % aid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool_id"] == "memory_write" and body["kind"] == "memory"
    assert body["content"] == content and body["content_chars"] == len(content)

    detail = client.get("/api/approvals/%s" % aid)
    assert detail.status_code == 200
    assert content not in detail.text          # redacted detail, never the content

    holder["uid"] = 99
    other = client.get("/api/approvals/%s/action" % aid)
    assert other.status_code == 404, other.text
    assert content not in other.text
    assert client.get("/api/approvals/%s" % aid).status_code == 404
    # no decision happened: still parked, zero writes
    assert _run(eng, rid).status == "waiting_for_approval"
    assert _memories(eng) == []


def test_approved_write_via_http_route_persists_exactly_one_row(eng, api):
    client, holder = api
    content = _DIR_CANARY
    prov = FakeChat([_save_script("directive", content), ["Noted."]])
    rid = _park(eng, prov)
    aid = _run(eng, rid).pending_approval_id
    assert aid

    holder["uid"] = 99
    assert client.post("/api/approvals/%s/approve" % aid, json={}).status_code == 404
    assert client.post("/api/approvals/%s/deny" % aid, json={}).status_code == 404
    assert _directives(eng) == []              # wrong user decides nothing

    holder["uid"] = 7
    ok = client.post("/api/approvals/%s/approve" % aid, json={})
    assert ok.status_code == 200, ok.text
    assert ok.json()["state"] == "approved"
    assert _directives(eng) == []              # the route never executes or writes
    again = client.post("/api/approvals/%s/approve" % aid, json={})
    assert again.status_code == 200 and again.json()["idempotent"] is True

    _worker(eng, prov).advance_chat_runs()
    rows = _directives(eng)
    assert len(rows) == 1 and rows[0].content == content
    assert _run(eng, rid).status == "succeeded"


# ── 6. duplicate-marker suppression (Task 7) + grammar injection (Task 8) ──

def _chash(kind, content):
    from app.run_engine import save_markers as sm
    args = ({"content": content, "category": "preference"} if kind == "memory"
            else {"content": content})
    return sm.save_content_hash(kind, args)


def test_duplicate_after_approve_is_suppressed_with_no_new_rows(eng, caplog):
    content = _MEM_CANARY
    prov = FakeChat([_save_script("memory", content), _save_script("memory", content)])
    caplog.set_level("DEBUG")
    rid = _park(eng, prov)
    aid = _run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    _worker(eng, prov).advance_chat_runs()          # write + resume -> duplicate marker
    r = _run(eng, rid)
    assert r.status == "failed" and r.status_reason == "save_duplicate_approved"
    assert len(_approvals(eng)) == 1                # no second proposal
    assert len(_memories(eng)) == 1                 # exactly one write
    assert "duplicate_marker_suppressed" in caplog.text
    assert _chash("memory", content) in caplog.text  # hash IS logged ...
    assert content not in caplog.text                # ... content is NOT
    assert content not in _leak_blob(eng, rid)


def test_duplicate_after_deny_is_suppressed(eng):
    content = _MEM_CANARY
    prov_a = FakeChat([_save_script("memory", content)])
    rid_a = _park(eng, prov_a)
    _decide_run(eng, _run(eng, rid_a).pending_approval_id, approve=False)
    _worker(eng, prov_a).advance_chat_runs()
    assert _run(eng, rid_a).status == "denied"
    rid_b = _seed(eng)
    _worker(eng, FakeChat([_save_script("memory", content)])).advance_chat_runs()
    rb = _run(eng, rid_b)
    assert rb.status == "failed" and rb.status_reason == "save_duplicate_denied"
    assert len(_approvals(eng)) == 1 and _memories(eng) == []


def test_duplicate_after_expiry_is_suppressed(eng):
    content = _MEM_CANARY
    prov_a = FakeChat([_save_script("memory", content)])
    rid_a = _park(eng, prov_a)
    aid = _run(eng, rid_a).pending_approval_id
    db = mk_session(eng)
    try:
        row = db.query(PendingApproval).filter(PendingApproval.id == aid).one()
        row.expires_at = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)
        db.commit()
    finally:
        db.close()
    _worker(eng, prov_a).advance_chat_runs()
    assert _run(eng, rid_a).status_reason == "approval_expired"
    rid_b = _seed(eng)
    _worker(eng, FakeChat([_save_script("memory", content)])).advance_chat_runs()
    rb = _run(eng, rid_b)
    assert rb.status == "failed" and rb.status_reason == "save_duplicate_expired"
    assert len(_approvals(eng)) == 1 and _memories(eng) == []


def test_duplicate_while_pending_creates_exactly_one_proposal(eng):
    content = _MEM_CANARY
    rid_a = _park(eng, FakeChat([_save_script("memory", content)]))
    assert _run(eng, rid_a).status == "waiting_for_approval"
    rid_b = _seed(eng)
    _worker(eng, FakeChat([_save_script("memory", content)])).advance_chat_runs()
    rb = _run(eng, rid_b)
    assert rb.status == "failed" and rb.status_reason == "save_duplicate_unresolved"
    assert len(_approvals(eng)) == 1                # exactly one proposal exists
    assert _run(eng, rid_a).status == "waiting_for_approval"
    assert _memories(eng) == []


def test_changed_content_allows_a_new_proposal(eng):
    rid_a = _park(eng, FakeChat([_save_script("memory", _MEM_CANARY)]))
    assert _run(eng, rid_a).status == "waiting_for_approval"
    other = "MEM-CANARY a different preference 9c1f"
    rid_b = _seed(eng)
    _worker(eng, FakeChat([_save_script("memory", other)])).advance_chat_runs()
    assert _run(eng, rid_b).status == "waiting_for_approval"
    assert len(_approvals(eng)) == 2                # materially changed -> new proposal


def test_content_hash_is_stable_across_whitespace_and_kind():
    from app.run_engine import save_markers as sm
    h1 = sm.save_content_hash("memory", {"content": "  the user  prefers tea ",
                                         "category": "preference"})
    h2 = sm.save_content_hash("memory", {"category": "preference",
                                         "content": "the user prefers tea"})
    h3 = sm.save_content_hash("directive", {"content": "the user prefers tea"})
    assert h1 == h2 and h1 != h3


def _submission(system_prompt, mode="chat"):
    from app.run_engine import submission as sub
    return sub.build_chat_submission(
        session_id=1, message="hi", image_url=None, model="llama3.2:3b",
        mode=mode, system_prompt=system_prompt, provider="ollama", options={})


def test_durable_system_prompt_contains_grammar_when_flag_on(eng):
    from app.run_engine import save_markers as sm
    s = _submission("BASE PROMPT")
    assert sm.MEMORY_MARKER_GRAMMAR_V1 in (s.resolved_system_prompt or "")
    assert s.resolved_system_prompt.startswith("BASE PROMPT")
    # non-chat durable modes do NOT receive the memory grammar
    s2 = _submission("BASE PROMPT", mode="deep_research")
    assert sm.MEMORY_MARKER_GRAMMAR_V1 not in (s2.resolved_system_prompt or "")


def test_durable_system_prompt_omits_grammar_when_flag_off(eng, monkeypatch):
    from app.run_engine import save_markers as sm
    monkeypatch.setattr(settings, "run_engine_memory_write_enabled", False)
    s = _submission("BASE PROMPT")
    assert s.resolved_system_prompt == "BASE PROMPT"
    assert sm.MEMORY_MARKER_GRAMMAR_V1 not in (s.resolved_system_prompt or "")
    assert _submission(None).resolved_system_prompt is None

