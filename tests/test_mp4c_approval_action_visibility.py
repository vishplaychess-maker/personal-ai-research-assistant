"""MP-4C audit - can the authenticated approver inspect the EXACT Python action?

EXECUTED EVIDENCE for three properties, plus a pinned non-control:

  A. NON-DISCLOSURE - the approval-detail API exposes the tool identity and the
     action hash but never the raw code, which lives ONLY in the sealed
     `tool_args` run payload.
  B. AUTHORIZATION - only the owning user can read/decide; another user gets 404.
  C. BINDING - the code IS bound to the same Run / ToolCall / args_hash /
     approval the gateway later consumes, and opening the payload fails closed
     on any context mismatch.
  D. The code never reaches RunEvents or the display fields.
  E. `redact()` does NOT treat `code` as sensitive - so the control is "never
     place raw args in display fields", not redaction.

This file does NOT implement a code preview. Whether an approver may see the
exact code is an unspecified product policy and is reported as a decision.

Run: PYTHONPATH=backend python -m pytest tests/test_mp4c_approval_action_visibility.py -q
"""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models.models import (
    ActionLogEntry,
    ExecutionTurn,
    PendingApproval,
    Run,
    RunEvent,
    RunStep,
    ToolCall,
    TrustedTool,
    User,
)
from app.approval.hashing import args_hash
from app.approval.service import reconstruct_envelope
from app.run_engine.errors import (
    RunPayloadAccessError,
    RunPayloadContextMismatch,
)
from tests.auth_helpers import auth_headers, register_and_login
from tests.run_engine_helpers import mk_engine, mk_session

CODE = "print('MP4C-EXACT-ACTION-CANARY-4f2a')"
PY_TOOL = "python_sandbox"


# ── A/B: the approval-detail API (in-process, real migrations) ───────


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _cleanup_api_rows():
    def _wipe():
        db = SessionLocal()
        try:
            for m in (ActionLogEntry, PendingApproval, ToolCall, ExecutionTurn,
                      TrustedTool):
                db.query(m).delete(synchronize_session=False)
            db.query(User).filter(User.username.like("vis_%")).delete(
                synchronize_session=False)
            db.commit()
        finally:
            db.close()
    _wipe()
    yield
    _wipe()


def _user(client, tag):
    uid, token = register_and_login(client, username=f"vis_{tag}")
    return uid, auth_headers(token)


def _csrf(client):
    tok = client.cookies.get("research_assistant_csrf_token")
    return {"X-CSRF-Token": tok} if tok else {}


def _mint(user_id):
    """Reuse the proven in-process minter from tests/test_approvals_api.py."""
    from tests.test_approvals_api import _mint_approval
    return _mint_approval(user_id, tool_id=PY_TOOL, args={"code": CODE})


def test_approval_detail_never_returns_the_raw_code(client):
    uid, headers = _user(client, "owner")
    aid = _mint(uid)

    resp = client.get(f"/api/approvals/{aid}", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    blob = json.dumps(body)

    # the approver DOES get the action IDENTITY ...
    assert body["tool_id"] == PY_TOOL
    assert body["state"] == "requested"
    assert isinstance(body["action_hash"], str) and len(body["action_hash"]) == 64
    assert "code" not in json.dumps(body["display_summary"]).lower()
    # ... but never the code itself, anywhere in the response.
    assert CODE not in blob
    assert "MP4C-EXACT-ACTION-CANARY" not in blob

    # NON-VACUOUS: the approval really IS bound to that exact code.
    db = SessionLocal()
    try:
        row = db.get(PendingApproval, aid)
        assert row.args_hash == args_hash({"code": CODE})
        assert reconstruct_envelope(row).args_hash == args_hash({"code": CODE})
    finally:
        db.close()


def test_another_user_cannot_read_or_decide_the_approval(client):
    uid_a, headers_a = _user(client, "a")
    _uid_b, headers_b = _user(client, "b")
    aid = _mint(uid_a)

    own = client.get(f"/api/approvals/{aid}", headers=headers_a)
    assert own.status_code == 200
    assert CODE not in own.text

    # cross-user READ is non-disclosing
    other = client.get(f"/api/approvals/{aid}", headers=headers_b)
    assert other.status_code == 404, other.text
    assert CODE not in other.text

    # B's list does not contain A's approval
    listed = client.get("/api/approvals", headers=headers_b).json()
    assert all(i["id"] != aid for i in listed["items"])

    # cross-user DECISION is refused and the row is untouched
    for path in ("approve", "deny"):
        r = client.post(f"/api/approvals/{aid}/{path}", json={},
                        headers={**headers_b, **_csrf(client)})
        assert r.status_code == 404, (path, r.status_code, r.text)

    db = SessionLocal()
    try:
        assert db.get(PendingApproval, aid).state == "requested"
    finally:
        db.close()


# ── C/D: hermetic worker-level binding + non-disclosure ──────────────


@pytest.fixture
def hermes(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp4c-visibility-key-0123456789")
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 30)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)
    monkeypatch.setattr(settings, "run_chat_max_tool_cycles", 3)
    monkeypatch.setattr(settings, "run_chat_tool_marker_max_bytes", 4096)
    monkeypatch.setattr(settings, "run_engine_sandbox", False)


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _park(eng, monkeypatch):
    from tests.test_mp4c_r2b2c2_python_routing import _park_python
    rid, _prov, _tr = _park_python(eng, monkeypatch, code=CODE)
    return rid


def test_the_code_is_bound_to_the_same_run_call_hash_and_approval(eng, hermes, monkeypatch):
    rid = _park(eng, monkeypatch)
    db = mk_session(eng)
    try:
        run = db.query(Run).filter(Run.id == rid).one()
        step = next(s for s in db.query(RunStep).filter(RunStep.run_id == rid).all()
                    if s.kind == "tool_dispatch")
        approval = db.get(PendingApproval, run.pending_approval_id)
        call = db.get(ToolCall, step.tool_call_id)
        ref = json.loads(step.input_ref)

        assert approval is not None and call is not None
        assert approval.run_id == rid
        assert approval.tool_id == PY_TOOL
        assert approval.tool_call_id == step.tool_call_id
        assert approval.args_hash == args_hash({"code": CODE})
        assert ref["args_hash"] == approval.args_hash
        assert call.args_hash == approval.args_hash
        assert reconstruct_envelope(approval).action_hash() == approval.action_hash

        # the EXACT code is recoverable only server-side, from the sealed payload,
        # and only with the trusted run/step context.
        from app.run_engine import mcp_flow
        assert mcp_flow.open_mcp_args(db, run, step) == {"code": CODE}
    finally:
        db.close()


def test_opening_the_payload_with_mismatched_context_fails_closed(eng, hermes, monkeypatch):
    from app.run_engine.payloads import open_payload

    rid = _park(eng, monkeypatch)
    db = mk_session(eng)
    try:
        run = db.query(Run).filter(Run.id == rid).one()
        step = next(s for s in db.query(RunStep).filter(RunStep.run_id == rid).all()
                    if s.kind == "tool_dispatch")

        with pytest.raises(RunPayloadContextMismatch):
            open_payload(db, step.input_payload_id,
                         expected_tenant_id=run.tenant_id,
                         expected_user_id=run.user_id,
                         expected_run_id="some-other-run",
                         expected_step_id=step.id,
                         expected_payload_type="tool_args",
                         expected_action_hash=None)

        with pytest.raises(RunPayloadAccessError):
            open_payload(db, step.input_payload_id,
                         expected_tenant_id=run.tenant_id,
                         expected_user_id=run.user_id + 1,
                         expected_run_id=run.id,
                         expected_step_id=step.id,
                         expected_payload_type="tool_args",
                         expected_action_hash=None)
    finally:
        db.close()


def test_the_code_never_reaches_run_events_or_the_display_fields(eng, hermes, monkeypatch):
    rid = _park(eng, monkeypatch)
    db = mk_session(eng)
    try:
        events = db.query(RunEvent).filter(RunEvent.run_id == rid).all()
        blob = json.dumps([{"t": e.type, "d": e.data} for e in events])
        assert CODE not in blob

        approval = db.get(PendingApproval,
                          db.query(Run).filter(Run.id == rid).one().pending_approval_id)
        assert CODE not in (approval.display_summary or "")
        assert CODE not in (approval.trusted_envelope_json or "")
        assert CODE not in (approval.purpose_redacted or "")
    finally:
        db.close()


# ── E: redaction is NOT the control ─────────────────────────────────


def test_redaction_alone_would_not_hide_a_code_key():
    """Pins the real control: args are never placed in display fields.
    `redact` treats `code` as ordinary content, so a future change that put raw
    args into `display_summary` would DISCLOSE them."""
    from app.approval.redaction import redact
    assert redact({"code": CODE}) == {"code": CODE}
