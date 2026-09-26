"""Phase 1B.2 — /api/approvals + /api/session-grants HTTP contract.

Runs in-process against app.main:app via TestClient (its lifespan runs the real
migrations). PendingApproval rows are minted in-process through the trusted
service layer — the API never accepts trusted envelope / hash / identity fields.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models.models import (
    ActionLogEntry,
    ExecutionTurn,
    PendingApproval,
    SessionGrant,
    ToolCall,
    TrustedTool,
    User,
)
from tests.auth_helpers import register_and_login, auth_headers

from app.approval import FixedClock, open_tool_call, open_turn, registry
from app.approval.hashing import args_hash
from app.approval.service import (
    create_session_grant,
    reconstruct_envelope,
    record_decision,
    request_approval,
)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _cleanup():
    def _wipe():
        db = SessionLocal()
        try:
            for m in (ActionLogEntry, PendingApproval, SessionGrant, ToolCall,
                      ExecutionTurn, TrustedTool):
                db.query(m).delete(synchronize_session=False)
            db.query(User).filter(User.username.like("appr_%")).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
    _wipe()
    yield
    _wipe()


def _user(client, tag="appr"):
    uid, token = register_and_login(client, username=f"{tag}_{dt.datetime.now().timestamp():.6f}".replace(".", ""))
    return uid, auth_headers(token)


def _csrf(client):
    tok = client.cookies.get("research_assistant_csrf_token")
    return {"X-CSRF-Token": tok} if tok else {}


def _mint_approval(user_id: int, *, session_id=3, tool_id="mcp__srv__echo",
                   args=None, with_turn=True):
    """Create a gateway-style PendingApproval for ``user_id`` via the trusted
    service layer (no client input)."""
    args = args or {"x": 1}
    db = SessionLocal()
    try:
        # real-time clock so the TTL is in the future relative to the API's SystemClock
        for _ in range(2):
            registry.register_or_refresh(
                db, owner_user_id=user_id, tool_id=tool_id, tool_name="echo",
                mcp_server_id=3, mcp_server_name="srv", executable_identity="python -m srv @1",
                non_secret_launch_config=registry.non_secret_launch_config("python", ["-m", "srv"]),
                config_ref_id=registry.config_ref_for_server(3), input_schema={"type": "object"},
                unlock_if_unchanged=True,
            )
        tt = registry.resolve_trusted(db, owner_user_id=user_id, tool_id=tool_id)
        turn = open_turn(db, user_id=user_id, session_id=session_id, run_id="r1") if with_turn else None
        call = open_tool_call(db, turn=turn, call_index=0, tool_id=tool_id,
                              args_hash=args_hash(args)) if with_turn else None
        env, row = request_approval(
            db, tool_id=tool_id, source_type="mcp", args=args,
            server_fingerprint=tt.server_fingerprint, tool_schema_hash=tt.tool_schema_hash,
            user_id=user_id, session_id=session_id, run_id="r1",
            tool_version=tt.tool_version or None, mcp_server_id="3",
            execution_turn_id=turn.id if turn else None,
            tool_call_id=call.id if call else None,
            proposal_dedupe_key=f"dk-{user_id}-{tool_id}-{args_hash(args)[:16]}",
            ttl_seconds=3600,
        )
        db.commit()
        return row.id
    finally:
        db.close()


def _mint_grant(user_id: int, *, session_id=3):
    """Create a SessionGrant the ONLY supported way — the trusted domain
    function with a server-controlled ``session_expires_at`` (there is no public
    grant-creation route). Returns (approval_id, grant_id)."""
    aid = _mint_approval(user_id, session_id=session_id)
    db = SessionLocal()
    try:
        row = db.get(PendingApproval, aid)
        record_decision(db, aid, approve=True)
        db.refresh(row)
        env = reconstruct_envelope(row)
        now = dt.datetime.now(dt.timezone.utc)
        g = create_session_grant(
            db, approval=row, envelope=env,
            session_expires_at=now + dt.timedelta(minutes=30),
            explicit_grant_decision=True,
        )
        db.commit()
        return aid, g.id
    finally:
        db.close()


# ── auth / ownership ────────────────────────────────────────────────


def test_unauthenticated_is_rejected(client):
    assert client.get("/api/approvals").status_code == 401
    assert client.post("/api/approvals/x/approve").status_code == 401


def test_list_and_inspect_own_approval(client):
    uid, h = _user(client)
    aid = _mint_approval(uid)
    r = client.get("/api/approvals", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["limit"] == 50 and body["offset"] == 0
    assert body["items"][0]["id"] == aid and body["items"][0]["state"] == "requested"
    one = client.get(f"/api/approvals/{aid}", headers=h)
    assert one.status_code == 200 and one.json()["tool_id"] == "mcp__srv__echo"
    assert "action_nonce" not in one.json()  # nonce is never exposed


def test_cross_user_access_is_404(client):
    uid_a, ha = _user(client, "appr_a")
    aid = _mint_approval(uid_a)
    uid_b, hb = _user(client, "appr_b")
    assert client.get(f"/api/approvals/{aid}", headers=hb).status_code == 404
    r = client.post(f"/api/approvals/{aid}/approve", headers={**hb, **_csrf(client)})
    assert r.status_code == 404
    # A's approval is untouched
    db = SessionLocal()
    try:
        assert db.get(PendingApproval, aid).state == "requested"
    finally:
        db.close()


# ── decide: idempotency + conflict ─────────────────────────────────


def test_approve_is_idempotent_and_deny_conflicts(client):
    uid, h = _user(client)
    aid = _mint_approval(uid)
    csrf = _csrf(client)
    r1 = client.post(f"/api/approvals/{aid}/approve", headers={**h, **csrf})
    assert r1.status_code == 200 and r1.json()["state"] == "approved" and r1.json()["idempotent"] is False
    r2 = client.post(f"/api/approvals/{aid}/approve", headers={**h, **csrf})
    assert r2.status_code == 200 and r2.json()["idempotent"] is True   # duplicate identical decision
    r3 = client.post(f"/api/approvals/{aid}/deny", headers={**h, **csrf})
    assert r3.status_code == 409                                       # conflicting decision


def test_deny_then_deny_idempotent_then_approve_conflicts(client):
    uid, h = _user(client)
    aid = _mint_approval(uid)
    csrf = _csrf(client)
    assert client.post(f"/api/approvals/{aid}/deny", headers={**h, **csrf}).json()["state"] == "denied"
    assert client.post(f"/api/approvals/{aid}/deny", headers={**h, **csrf}).json()["idempotent"] is True
    assert client.post(f"/api/approvals/{aid}/approve", headers={**h, **csrf}).status_code == 409


def test_decide_does_not_execute_and_no_tool_output_field(client):
    uid, h = _user(client)
    aid = _mint_approval(uid)
    r = client.post(f"/api/approvals/{aid}/approve", headers={**h, **_csrf(client)})
    body = r.json()
    assert set(body) == {"id", "state", "idempotent"}     # no execution / output fields
    # no tool.execution.* event was written by the API
    db = SessionLocal()
    try:
        types = {e.event_type for e in db.query(ActionLogEntry).all()}
        assert not any(t.startswith("tool.execution.") for t in types)
        assert "approval.approved" in types
    finally:
        db.close()


def test_csrf_required_on_state_change(client):
    uid, h = _user(client)
    aid = _mint_approval(uid)
    # a browser request (Origin present) with NO X-CSRF-Token header -> 403
    bad = client.post(f"/api/approvals/{aid}/approve",
                      headers={**h, "Origin": "http://testserver"})
    assert bad.status_code == 403
    # with the CSRF token it succeeds
    ok = client.post(f"/api/approvals/{aid}/approve",
                     headers={**h, "Origin": "http://testserver", **_csrf(client)})
    assert ok.status_code == 200


def test_client_cannot_send_trusted_fields(client):
    uid, h = _user(client)
    aid = _mint_approval(uid)
    r = client.post(f"/api/approvals/{aid}/approve", headers={**h, **_csrf(client)},
                    json={"risk_level": "low", "action_hash": "0" * 64, "state": "consumed"})
    assert r.status_code == 422  # extra=forbid


# ── no public consume route ────────────────────────────────────────


def test_no_public_consume_route_exists(client):
    paths = {r.path for r in app.routes}
    assert not any("consume" in p.lower() for p in paths)
    uid, h = _user(client)
    aid = _mint_approval(uid)
    for verb in ("post", "get", "put"):
        resp = getattr(client, verb)(f"/api/approvals/{aid}/consume",
                                     headers={**h, **_csrf(client)})
        assert resp.status_code in (404, 405)


# ── list bounds / redaction ────────────────────────────────────────


def test_list_is_paginated_and_bounded(client):
    uid, h = _user(client)
    for i in range(5):
        _mint_approval(uid, args={"x": i})
    r = client.get("/api/approvals?limit=2&offset=1", headers=h)
    assert r.status_code == 200 and len(r.json()["items"]) == 2 and r.json()["total"] == 5
    assert client.get("/api/approvals?limit=9999", headers=h).status_code == 422  # capped


def test_display_summary_is_redacted_in_responses(client):
    uid, h = _user(client)
    # mint with an arg that looks like a secret in the (redacted) display summary
    aid = _mint_approval(uid, args={"api_key": "sk-abcdefghijklmnopqrstuvwx"})
    body = client.get(f"/api/approvals/{aid}", headers=h).json()
    import json as _j
    blob = _j.dumps(body)
    assert "sk-abcdefghijklmnopqrstuvwx" not in blob


# ── session grants: NO public creation (Decision 2) ───────────────


def test_no_public_grant_creation_route_exists(client):
    # the route is intentionally omitted in Phase 1B.2
    paths = {r.path for r in app.routes}
    assert not any(p.endswith("/grant") for p in paths)


def test_unauthenticated_cannot_create_a_grant(client):
    r = client.post("/api/approvals/anything/grant")
    assert r.status_code in (404, 405)          # never 200 / 401-then-created


def test_authenticated_grant_creation_is_unavailable_and_creates_no_row(client):
    uid, h = _user(client)
    aid = _mint_approval(uid)
    csrf = _csrf(client)
    client.post(f"/api/approvals/{aid}/approve", headers={**h, **csrf})
    # even approved + CSRF + owner: the route is gone
    for verb in ("post", "put", "patch"):
        resp = getattr(client, verb)(f"/api/approvals/{aid}/grant", headers={**h, **csrf})
        assert resp.status_code in (404, 405)
    db = SessionLocal()
    try:
        assert db.query(SessionGrant).count() == 0   # no disabled path made a row
    finally:
        db.close()


def test_approve_does_not_create_a_grant(client):
    uid, h = _user(client)
    aid = _mint_approval(uid)
    client.post(f"/api/approvals/{aid}/approve", headers={**h, **_csrf(client)})
    db = SessionLocal()
    try:
        assert db.query(SessionGrant).count() == 0
        assert db.get(PendingApproval, aid).state == "approved"   # one-time only
    finally:
        db.close()


def test_client_cannot_submit_a_grant_ttl(client):
    uid, h = _user(client)
    aid = _mint_approval(uid)
    csrf = _csrf(client)
    client.post(f"/api/approvals/{aid}/approve", headers={**h, **csrf})
    # there is no route to POST a TTL to — a client-selected grant lifetime is
    # impossible to submit
    r = client.post(f"/api/approvals/{aid}/grant", headers={**h, **csrf},
                    json={"ttl_seconds": 999999})
    assert r.status_code in (404, 405)
    db = SessionLocal()
    try:
        assert db.query(SessionGrant).count() == 0
    finally:
        db.close()


def test_grant_list_and_revoke_remain_owner_scoped(client):
    uid, h = _user(client)
    _aid, gid = _mint_grant(uid)
    lst = client.get("/api/session-grants", headers=h).json()
    assert lst["total"] == 1 and lst["items"][0]["id"] == gid
    csrf = _csrf(client)
    r1 = client.post(f"/api/session-grants/{gid}/revoke", headers={**h, **csrf})
    assert r1.status_code == 200 and r1.json()["revoked"] is True and r1.json()["idempotent"] is False
    r2 = client.post(f"/api/session-grants/{gid}/revoke", headers={**h, **csrf})
    assert r2.json()["idempotent"] is True
    # other user cannot see or revoke it
    _uidb, hb = _user(client, "appr_b")
    assert client.post(f"/api/session-grants/{gid}/revoke", headers={**hb, **_csrf(client)}).status_code == 404
    assert client.get("/api/session-grants", headers=hb).json()["total"] == 0


def test_session_grant_list_page_size_is_bounded(client):
    uid, h = _user(client)
    assert client.get("/api/session-grants?limit=1", headers=h).status_code == 200
    assert client.get("/api/session-grants?limit=9999", headers=h).status_code == 422
