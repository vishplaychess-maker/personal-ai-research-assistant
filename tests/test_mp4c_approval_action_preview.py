"""MP-4C - informed-approval EXACT Python action preview (RED-first).

Policy: the authenticated owner must see the exact python_sandbox code bound to
a REQUESTED approval before Approve is enabled. These tests pin:

  * owner sees the exact code (service + route reachability);
  * non-owner 404; generic MCP refused; decided / expired refused;
  * wrong origin / wrong hash / tampered ciphertext / oversize / flag-off refused;
  * the preview writes nothing public (no RunEvent / ActionLog code);
  * the generic approval response still never carries code.

Run: PYTHONPATH=backend python -m pytest tests/test_mp4c_approval_action_preview.py -q
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models.models import (
    ActionLogEntry,
    ExecutionTurn,
    PendingApproval,
    Run,
    RunEvent,
    RunPayload,
    RunStep,
    ToolCall,
    TrustedTool,
    User,
)
from tests.auth_helpers import auth_headers, register_and_login
from tests.run_engine_helpers import mk_engine, mk_session

CODE = "print('MP4C-PREVIEW-EXACT-CODE-91c7')"
PY_TOOL = "python_sandbox"


# ── route-level (in-process, real migrations) ────────────────────────


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
            db.query(User).filter(User.username.like("pv_%")).delete(
                synchronize_session=False)
            db.commit()
        finally:
            db.close()
    _wipe()
    yield
    _wipe()


def _user(client, tag):
    uid, token = register_and_login(client, username=f"pv_{tag}")
    return uid, auth_headers(token)


def _mint(user_id, tool_id=PY_TOOL, args=None):
    from tests.test_approvals_api import _mint_approval
    return _mint_approval(user_id, tool_id=tool_id, args=args or {"code": CODE})


def test_the_preview_route_is_registered():
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/approvals/{approval_id}/action" in paths, sorted(
        p for p in paths if p and "approvals" in p)


def test_non_owner_gets_404(client):
    uid_a, _ha = _user(client, "a")
    _uid_b, headers_b = _user(client, "b")
    aid = _mint(uid_a)
    r = client.get(f"/api/approvals/{aid}/action", headers=headers_b)
    assert r.status_code == 404, r.text
    assert CODE not in r.text


def test_generic_mcp_preview_is_refused(client):
    uid, headers = _user(client, "mcp")
    aid = _mint(uid, tool_id="mcp__srv__echo", args={"x": 1})
    r = client.get(f"/api/approvals/{aid}/action", headers=headers)
    assert r.status_code == 409, r.text
    assert CODE not in r.text


def test_decided_approval_preview_is_refused(client):
    from app.approval.service import record_decision
    uid, headers = _user(client, "dec")
    aid = _mint(uid)
    db = SessionLocal()
    try:
        record_decision(db, aid, approve=True)
    finally:
        db.close()
    r = client.get(f"/api/approvals/{aid}/action", headers=headers)
    assert r.status_code == 409, r.text
    assert CODE not in r.text


def test_expired_approval_preview_is_refused(client):
    uid, headers = _user(client, "exp")
    aid = _mint(uid)
    db = SessionLocal()
    try:
        row = db.get(PendingApproval, aid)
        row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)
        db.commit()
    finally:
        db.close()
    r = client.get(f"/api/approvals/{aid}/action", headers=headers)
    assert r.status_code == 409, r.text
    assert CODE not in r.text


def test_generic_detail_response_still_carries_no_code(client):
    uid, headers = _user(client, "gen")
    aid = _mint(uid)
    r = client.get(f"/api/approvals/{aid}", headers=headers)
    assert r.status_code == 200, r.text
    assert CODE not in r.text
    assert "code" not in json.dumps(r.json().get("display_summary") or {}).lower()


# ── service-level (hermetic engine, real proposal + payload) ─────────


@pytest.fixture
def hermes(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp4c-preview-key-0123456789")
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


def _pair(db, rid):
    run = db.query(Run).filter(Run.id == rid).one()
    approval = db.get(PendingApproval, run.pending_approval_id)
    step = (db.query(RunStep)
            .filter(RunStep.run_id == rid, RunStep.kind == "tool_dispatch").one())
    return run, approval, step


def test_owner_sees_the_exact_code_and_nothing_public_is_written(eng, hermes, monkeypatch):
    from app.approval.action_preview import build_python_action_preview

    rid = _park(eng, monkeypatch)
    db = mk_session(eng)
    try:
        run, approval, _step = _pair(db, rid)
        out = build_python_action_preview(db, approval, actor_user_id=run.user_id)
        assert out["code"] == CODE
        assert out["approval_id"] == approval.id
        assert out["tool_id"] == PY_TOOL
        assert out["code_bytes"] == len(CODE.encode("utf-8"))
        assert out["args_hash"] == approval.args_hash
        assert out["action_hash"] == approval.action_hash

        events = json.dumps([{"t": e.type, "d": e.data}
                             for e in db.query(RunEvent).filter(RunEvent.run_id == rid).all()])
        assert CODE not in events
        logs = json.dumps([dict(r._mapping) for r in db.execute(
            ActionLogEntry.__table__.select()).all()], default=str)
        assert CODE not in logs
        assert CODE not in (approval.display_summary or "")
    finally:
        db.close()


def test_service_refuses_a_non_owner(eng, hermes, monkeypatch):
    from app.approval.action_preview import ActionPreviewError, build_python_action_preview

    rid = _park(eng, monkeypatch)
    db = mk_session(eng)
    try:
        run, approval, _step = _pair(db, rid)
        with pytest.raises(ActionPreviewError):
            build_python_action_preview(db, approval, actor_user_id=run.user_id + 1)
    finally:
        db.close()


def test_service_refuses_a_wrong_origin(eng, hermes, monkeypatch):
    from app.approval.action_preview import ActionPreviewError, build_python_action_preview

    rid = _park(eng, monkeypatch)
    db = mk_session(eng)
    try:
        run, approval, step = _pair(db, rid)
        ref = json.loads(step.input_ref)
        ref["purpose"] = "not_python"
        db.execute(update(RunStep).where(RunStep.id == step.id)
                   .values(input_ref=json.dumps(ref, sort_keys=True)))
        db.commit()
        db.expire_all()
        run, approval, step = _pair(db, rid)
        with pytest.raises(ActionPreviewError):
            build_python_action_preview(db, approval, actor_user_id=run.user_id)
    finally:
        db.close()


def test_service_refuses_a_wrong_args_hash(eng, hermes, monkeypatch):
    from app.approval.action_preview import ActionPreviewError, build_python_action_preview

    rid = _park(eng, monkeypatch)
    db = mk_session(eng)
    try:
        run, approval, _step = _pair(db, rid)
        db.execute(update(PendingApproval).where(PendingApproval.id == approval.id)
                   .values(args_hash="0" * 64))
        db.commit()
        db.expire_all()
        run, approval, _step = _pair(db, rid)
        with pytest.raises(ActionPreviewError):
            build_python_action_preview(db, approval, actor_user_id=run.user_id)
    finally:
        db.close()


def test_service_refuses_tampered_ciphertext(eng, hermes, monkeypatch):
    from app.approval.action_preview import ActionPreviewError, build_python_action_preview

    rid = _park(eng, monkeypatch)
    db = mk_session(eng)
    try:
        run, approval, step = _pair(db, rid)
        payload = db.get(RunPayload, step.input_payload_id)
        assert payload is not None
        db.execute(update(RunPayload).where(RunPayload.id == payload.id)
                   .values(ciphertext="gAAAAA" + "A" * 64))
        db.commit()
        db.expire_all()
        run, approval, step = _pair(db, rid)
        with pytest.raises(ActionPreviewError):
            build_python_action_preview(db, approval, actor_user_id=run.user_id)
    finally:
        db.close()


def test_service_refuses_an_oversize_body_without_truncating(eng, hermes, monkeypatch):
    from app.approval.action_preview import ActionPreviewError, build_python_action_preview
    from app.run_engine import sandbox_policy

    rid = _park(eng, monkeypatch)
    db = mk_session(eng)
    try:
        run, approval, _step = _pair(db, rid)
        monkeypatch.setattr(sandbox_policy, "max_code_bytes", lambda: 4)
        with pytest.raises(ActionPreviewError):
            build_python_action_preview(db, approval, actor_user_id=run.user_id)
    finally:
        db.close()


def test_service_refuses_when_the_flag_is_off(eng, hermes, monkeypatch):
    from app.approval.action_preview import ActionPreviewError, build_python_action_preview

    rid = _park(eng, monkeypatch)
    db = mk_session(eng)
    try:
        run, approval, _step = _pair(db, rid)
        monkeypatch.setattr(settings, "run_engine_sandbox", False)
        with pytest.raises(ActionPreviewError):
            build_python_action_preview(db, approval, actor_user_id=run.user_id)
    finally:
        db.close()
