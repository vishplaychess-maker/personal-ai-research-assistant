"""Approval APIs (Phase 1B.2) — authenticated, owner-scoped.

    GET  /api/approvals                    list own pending approvals (paginated)
    GET  /api/approvals/{id}               inspect one owned approval (redacted)
    GET  /api/approvals/{id}/action        exact python_sandbox code (requested
                                           + owner only; 409 otherwise)
    POST /api/approvals/{id}/approve       requested -> approved  (idempotent; 409 on conflict)
    POST /api/approvals/{id}/deny          requested -> denied    (idempotent; 409 on conflict)
    GET  /api/session-grants               list own session grants (paginated)
    POST /api/session-grants/{id}/revoke   revoke an owned grant  (idempotent)

There is NO /consume endpoint. Approve/deny never execute a tool. A one-time
approval never becomes a reusable grant.

**Public session-grant CREATION is intentionally not exposed in Phase 1B.2**
(correction — Decision 2). The repository has no trustworthy authenticated
authenticated-session expiry / revocation binding, so there is no honest value
for a reusable grant's lifetime and no client-selected TTL is accepted. Grants
are created only by the trusted internal domain function
(``app.approval.service.create_session_grant``) with a server-controlled
``session_expires_at``; that path and its tests stay. This route set only lists
and revokes already-existing owned grants. Public grant creation will be enabled
only after a real session-expiry + revocation binding is implemented and tested.

Clients cannot send tool / risk / hash / envelope / identity / policy / registry
/ execution / TTL fields — the request bodies carry none. Cross-user /
cross-tenant rows return 404 (non-disclosing). Responses redact sensitive
fields and are bounded.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.approval import state_machine as _sm
from app.approval.clock import SystemClock, ensure_utc
from app.approval.redaction import redact
from app.approval.service import record_decision, revoke_session_grant
from app.database import get_db
from app.models.models import PendingApproval, SessionGrant, User
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf

router = APIRouter(tags=["approvals"])

_PAGE_MAX = 100

_ACTIVE = (_sm.REQUESTED, _sm.APPROVED)


def _owned_approval(db: Session, approval_id: str, user: User) -> PendingApproval:
    row = db.get(PendingApproval, approval_id)
    if row is None or int(row.user_id) != int(user.id):
        raise HTTPException(status_code=404, detail="approval not found")
    return row


def _approval_view(row: PendingApproval) -> dict:
    try:
        summary = json.loads(row.display_summary or "{}")
    except (ValueError, TypeError):
        summary = {}
    return {
        "id": row.id,
        "state": row.state,
        "tool_id": row.tool_id,
        "source_type": row.source_type,
        "risk_level": row.risk_level,
        "action_hash": row.action_hash,
        "policy_version": row.policy_version,
        "session_id": row.session_id,
        "run_id": row.run_id,
        "execution_turn_id": row.execution_turn_id,
        "tool_call_id": row.tool_call_id,
        "display_summary": redact(summary),
        "purpose_redacted": row.purpose_redacted,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


def _grant_view(g: SessionGrant) -> dict:
    return {
        "id": g.id,
        "tool_id": g.tool_id,
        "session_id": g.session_id,
        "grant_scope_hash": g.grant_scope_hash,
        "policy_version": g.policy_version,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "expires_at": g.expires_at.isoformat() if g.expires_at else None,
        "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
    }


# ── list / inspect ────────────────────────────────────────────────────


@router.get("/api/approvals")
def list_approvals(
    state: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=_PAGE_MAX),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(PendingApproval).filter(PendingApproval.user_id == user.id)
    if state:
        if state not in _sm.STATES:
            raise HTTPException(status_code=422, detail="unknown state")
        q = q.filter(PendingApproval.state == state)
    total = q.count()
    rows = (q.order_by(PendingApproval.created_at.desc())
            .offset(offset).limit(limit).all())
    return {"total": total, "limit": limit, "offset": offset,
            "items": [_approval_view(r) for r in rows]}


@router.get("/api/approvals/{approval_id}")
def get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _approval_view(_owned_approval(db, approval_id, user))


@router.get("/api/approvals/{approval_id}/action")
def get_approval_action(
    approval_id: str,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """MP-4C informed approval - the EXACT python_sandbox code bound to this
    REQUESTED approval, for the owning user only. Read-only, no-store, never
    logged or persisted. Every non-owner, non-python, non-requested,
    mismatched or unopenable case fails closed WITHOUT code."""
    from app.approval.action_preview import (
        ActionPreviewError,
        build_python_action_preview,
        build_save_action_preview,
    )
    from app.run_engine.save_approval_identity import SAVE_TOOL_IDS

    row = _owned_approval(db, approval_id, user)      # 404 for a non-owner
    try:
        # MP-4C decision 2: the same ONE owner-only preview endpoint serves the
        # approved-write surfaces. Anything that is neither python_sandbox nor a
        # save tool fails closed inside the chosen builder.
        if row.tool_id in SAVE_TOOL_IDS:
            payload = build_save_action_preview(db, row, actor_user_id=user.id)
        else:
            payload = build_python_action_preview(db, row, actor_user_id=user.id)
    except ActionPreviewError as exc:
        status = 404 if exc.reason == "not_found" else 409
        raise HTTPException(status_code=status,
                            detail="action preview unavailable") from None
    response.headers["Cache-Control"] = "no-store"
    return payload


# ── decide ───────────────────────────────────────────────────────────


class _EmptyBody(BaseModel):
    """No client-controlled fields are accepted on a decision."""
    model_config = {"extra": "forbid"}


def _decide(db: Session, row: PendingApproval, *, approve: bool) -> dict:
    target = _sm.APPROVED if approve else _sm.DENIED
    other = _sm.DENIED if approve else _sm.APPROVED
    # server-decided expiry first
    if row.state == _sm.REQUESTED and ensure_utc(row.expires_at) <= SystemClock().now():
        from app.approval.state_machine import expire_if_due
        expire_if_due(db, row.id)
        db.refresh(row)
    if row.state == target:
        return {"id": row.id, "state": row.state, "idempotent": True}
    if row.state == other:
        raise HTTPException(status_code=409, detail=f"already {row.state}")
    if row.state in (_sm.EXPIRED, _sm.CONSUMED, _sm.REVOKED):
        raise HTTPException(status_code=409, detail=f"approval is {row.state}")
    if row.state != _sm.REQUESTED:
        raise HTTPException(status_code=409, detail=f"cannot decide from {row.state}")
    # Phase 1B.2R MP-2: the decision, its audit event, and a durable-run
    # RunSignal(approval_decided) commit together — or roll back together. The
    # signal is flushed first (no commit); record_decision(commit=False) then
    # CAS-transitions the approval and, only on success, the tamper-evident
    # ActionLog append issues the single commit that persists the whole unit.
    # This endpoint still never invokes MCP / the worker / an executor — it only
    # appends one idempotent allowlisted signal linked to the run.
    from app.run_engine.mcp_flow import mirror_decision_signal
    try:
        mirror_decision_signal(db, row, decision=(_sm.APPROVED if approve else _sm.DENIED))
        record_decision(db, row.id, approve=approve, commit=False)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(status_code=409, detail="decision could not be recorded")
    db.refresh(row)
    return {"id": row.id, "state": row.state, "idempotent": False}


@router.post("/api/approvals/{approval_id}/approve")
def approve_approval(
    approval_id: str,
    _body: _EmptyBody | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    return _decide(db, _owned_approval(db, approval_id, user), approve=True)


@router.post("/api/approvals/{approval_id}/deny")
def deny_approval(
    approval_id: str,
    _body: _EmptyBody | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    return _decide(db, _owned_approval(db, approval_id, user), approve=False)


# ── session grants (list / revoke owned only — NO public creation) ────


@router.get("/api/session-grants")
def list_session_grants(
    active_only: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=_PAGE_MAX),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(SessionGrant).filter(SessionGrant.user_id == user.id)
    if active_only:
        q = q.filter(SessionGrant.revoked_at.is_(None))
    total = q.count()
    rows = q.order_by(SessionGrant.created_at.desc()).offset(offset).limit(limit).all()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [_grant_view(g) for g in rows]}


@router.post("/api/session-grants/{grant_id}/revoke")
def revoke_grant(
    grant_id: str,
    _body: _EmptyBody | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    g = db.get(SessionGrant, grant_id)
    if g is None or int(g.user_id) != int(user.id):
        raise HTTPException(status_code=404, detail="session grant not found")
    did = revoke_session_grant(db, grant_id)
    db.refresh(g)
    return {"id": g.id, "revoked": g.revoked_at is not None, "idempotent": not did}
