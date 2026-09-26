"""Approval domain orchestration.

The API a (future, out-of-scope for 1B.1) call site and the eval targets use:

    env, approval = request_approval(db, tool_id=..., args=..., user_id=..., ...)
    record_decision(db, approval.id, approve=True)          # human said yes
    row = consume_approval(db, approval.id, resubmit_envelope=env2)  # at exec time

``consume_approval`` re-canonicalizes the **resubmitted** envelope and compares
its ``action_hash`` with the persisted one — changed args OR any changed
envelope field ⇒ ``EnvelopeMismatch`` and nothing is consumed.

Risk level and scope flags are set here by **trusted server policy**
(``_classify``) — never read from model/tool/browser/document/client text.
"""

from __future__ import annotations

import json
import secrets
import uuid
import datetime as _dt
from typing import Callable

from sqlalchemy.exc import IntegrityError as _IntegrityError
from sqlalchemy.orm import Session

from app.approval import action_log as _log
from app.approval import state_machine as _sm
from app.approval.clock import Clock, SystemClock, ensure_utc
from app.approval.envelope import ACTION_ENVELOPE_VERSION, ActionEnvelope, build_envelope
from app.approval.errors import ChainIntegrityError, EnvelopeMismatch, GrantScopeError
from app.approval.hashing import GRANT_SCOPE_VERSION
from app.approval.hashing import args_hash as _args_hash
from app.approval.redaction import redact
from app.models.models import PendingApproval, SessionGrant

DEFAULT_TTL_SECONDS = 300
DEFAULT_POLICY_VERSION = "1b1.1"
PURPOSE_MAX_CHARS = 280
_DEDUPE_RETRIES = 5

# ── trusted server-side policy (minimal; 1B.3 replaces this) ───────────

_DESTRUCTIVE_HINTS = ("delete", "rm ", "drop ", "destroy", "wipe", "revoke", "kill")
_CREDENTIAL_HINTS = ("password", "secret", "token", "api_key", "credential", "ssh", "key")
_PAYMENT_HINTS = ("pay", "payment", "charge", "invoice", "transfer", "purchase", "checkout")


def _classify(tool_id: str, source_type: str, affected_resources: tuple[str, ...]) -> dict:
    """Deterministic, conservative classification from TRUSTED inputs only
    (the server-known tool id, source type, and resolved resource ids).
    Returns risk_level + risk_scope_flags + coarse scope strings.
    """
    blob = " ".join([tool_id, source_type, *affected_resources]).lower()
    flags: set[str] = set()
    if any(h in blob for h in _DESTRUCTIVE_HINTS):
        flags.add("destructive")
    if any(h in blob for h in _CREDENTIAL_HINTS):
        flags.add("credential")
    if any(h in blob for h in _PAYMENT_HINTS):
        flags.add("payment")
    if source_type in ("terminal", "python"):
        flags.add("code_execution")
    if source_type == "browser":
        flags.add("browser")
    if source_type == "mcp":
        flags.add("external_tool")

    if flags & {"destructive", "credential", "payment"}:
        level = "high"
    elif flags & {"code_execution", "browser", "external_tool"}:
        level = "medium"
    else:
        level = "low"

    return {
        "risk_level": level,
        "risk_scope_flags": tuple(sorted(flags)),
        "network_scope": "browser" if source_type == "browser" else None,
        "filesystem_scope": "shell" if source_type in ("terminal", "python") else None,
        "credential_scope": "referenced" if "credential" in flags else None,
    }


# ── request / decide / consume ────────────────────────────────────────


def reconstruct_envelope(row: PendingApproval) -> ActionEnvelope | None:
    """Rebuild the EXACT approved ``ActionEnvelope`` from the persisted trusted
    fields (``trusted_envelope_json``) + the original ``action_nonce``. Used by
    the trusted dispatcher / API grant path so the client never resubmits an
    envelope. Returns None for a pre-1B.2 row that has no persisted envelope.
    """
    if not row.trusted_envelope_json:
        return None
    fields = json.loads(row.trusted_envelope_json)
    env = build_envelope(purpose="", **fields)
    if env.action_hash() != row.action_hash:
        raise EnvelopeMismatch(
            f"reconstructed envelope for {row.id!r} does not match its action_hash"
        )
    return env


def request_approval(
    db: Session,
    *,
    tool_id: str,
    source_type: str,
    args: object,
    server_fingerprint: str,
    tool_schema_hash: str,
    user_id: int,
    tenant_id: str | None = None,
    session_id: int | None = None,
    run_id: str | None = None,
    tool_version: str | None = None,
    mcp_server_id: str | None = None,
    affected_resources: tuple[str, ...] = (),
    purpose: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    policy_version: str = DEFAULT_POLICY_VERSION,
    execution_turn_id: str | None = None,
    tool_call_id: str | None = None,
    proposal_dedupe_key: str | None = None,
    clock: Clock | None = None,
) -> tuple[ActionEnvelope, PendingApproval]:
    """Build the trusted envelope, persist a ``requested`` PendingApproval,
    append a ``requested`` audit event. Returns (envelope, row).

    Idempotent re-proposal: when ``proposal_dedupe_key`` is given and an ACTIVE
    (``requested``/``approved``) PendingApproval already exists for that key
    (tenant-scoped), that row is **reused** — the envelope is reconstructed
    from the stored ``action_nonce`` (no new nonce, no new row, no duplicate
    audit ``requested`` event). ``proposal_dedupe_key`` is server-derived only;
    it is never accepted from the client.
    """
    clock = clock or SystemClock()
    now = clock.now()
    affected_resources = tuple(affected_resources)
    policy = _classify(tool_id, source_type, affected_resources)

    def _build(nonce: str) -> ActionEnvelope:
        return build_envelope(
            action_envelope_version=ACTION_ENVELOPE_VERSION,
            tool_id=tool_id, tool_version=tool_version, source_type=source_type,
            mcp_server_id=mcp_server_id, server_fingerprint=server_fingerprint,
            tool_schema_hash=tool_schema_hash, args_hash=_args_hash(args),
            affected_resources=affected_resources,
            network_scope=policy["network_scope"],
            filesystem_scope=policy["filesystem_scope"],
            credential_scope=policy["credential_scope"],
            risk_level=policy["risk_level"], risk_scope_flags=policy["risk_scope_flags"],
            tenant_id=tenant_id, user_id=user_id, session_id=session_id, run_id=run_id,
            action_nonce=nonce, policy_version=policy_version, purpose=purpose,
        )

    # ── dedupe hit: reuse the existing ACTIVE row + its original nonce ──
    if proposal_dedupe_key:
        existing = (
            db.query(PendingApproval)
            .filter(PendingApproval.proposal_dedupe_key == proposal_dedupe_key)
            .filter(PendingApproval.state.in_((_sm.REQUESTED, _sm.APPROVED)))
            .order_by(PendingApproval.created_at.asc())
            .first()
        )
        if existing is not None and (existing.tenant_id or None) == (tenant_id or None):
            env = reconstruct_envelope(existing) or _build(existing.action_nonce)
            if env.action_hash() != existing.action_hash:
                raise EnvelopeMismatch(
                    "proposal_dedupe_key matched a row whose trusted inputs "
                    "differ from this proposal — refusing to reuse it"
                )
            return env, existing

    nonce = secrets.token_hex(24)  # server CSPRNG
    env = _build(nonce)
    a_hash = env.action_hash()
    summary = redact(env.display_summary(max_chars=PURPOSE_MAX_CHARS))

    def _new_row() -> PendingApproval:
        return PendingApproval(
            id=str(uuid.uuid4()), action_hash=a_hash, args_hash=env.args_hash,
            action_envelope_version=env.action_envelope_version, tool_id=tool_id,
            server_fingerprint=server_fingerprint, tool_schema_hash=tool_schema_hash,
            source_type=source_type, tenant_id=tenant_id, user_id=user_id,
            session_id=session_id, run_id=run_id, action_nonce=nonce,
            policy_version=policy_version, risk_level=policy["risk_level"],
            state=_sm.REQUESTED,
            proposal_dedupe_key=proposal_dedupe_key,
            execution_turn_id=execution_turn_id, tool_call_id=tool_call_id,
            display_summary=json.dumps(summary, sort_keys=True, ensure_ascii=False),
            purpose_redacted=summary.get("purpose", ""),
            created_at=now, expires_at=now + _dt.timedelta(seconds=int(ttl_seconds)),
        )

    row = _new_row()
    row.trusted_envelope_json = json.dumps(env.hash_fields(), sort_keys=True, ensure_ascii=False)
    db.add(row)
    try:
        db.commit()
    except _IntegrityError:
        # concurrent request won the active-dedupe unique index — reuse the winner
        db.rollback()
        if not proposal_dedupe_key:
            raise
        for _ in range(_DEDUPE_RETRIES):
            won = (
                db.query(PendingApproval)
                .filter(PendingApproval.proposal_dedupe_key == proposal_dedupe_key)
                .filter(PendingApproval.state.in_((_sm.REQUESTED, _sm.APPROVED)))
                .order_by(PendingApproval.created_at.asc())
                .first()
            )
            if won is not None and (won.tenant_id or None) == (tenant_id or None):
                env2 = _build(won.action_nonce)
                if env2.action_hash() != won.action_hash:
                    raise EnvelopeMismatch("concurrent dedupe row has different trusted inputs")
                return env2, won
        raise ChainIntegrityError("dedupe collision could not be resolved")

    _log.append_event(
        db, tenant_id=tenant_id, user_id=user_id, event_type="approval.requested",
        approval_id=row.id, action_hash=a_hash,
        event_metadata={"tool_id": tool_id, "risk_level": policy["risk_level"],
                        "risk_scope_flags": list(policy["risk_scope_flags"])},
        clock=clock,
    )
    return env, row


def record_decision(db: Session, approval_id: str, *, approve: bool,
                    clock: Clock | None = None, commit: bool = True) -> None:
    """Human decision. ``requested -> approved`` or ``requested -> denied``
    via atomic CAS. Concurrent approve/deny → exactly one terminal decision.

    ``commit=False`` leaves the transaction open so the caller can commit the
    decision, its audit event, and a durable-run RunSignal together (the
    approvals route uses this for Phase 1B.2R MP-2). Existing callers keep the
    default and see no behaviour change.
    """
    clock = clock or SystemClock()
    row = db.get(PendingApproval, approval_id)
    tenant_id = row.tenant_id if row else None
    user_id = row.user_id if row else None
    if approve:
        _sm.approve(db, approval_id, clock=clock, commit=commit)
        etype = "approval.approved"
    else:
        _sm.deny(db, approval_id, clock=clock, commit=commit)
        etype = "approval.denied"
    _log.append_event(db, tenant_id=tenant_id, user_id=user_id, event_type=etype,
                      approval_id=approval_id, clock=clock)


def consume_approval(
    db: Session,
    approval_id: str,
    *,
    resubmit_envelope: ActionEnvelope,
    user_id: int,
    tenant_id: str | None = None,
    session_id: int | None = None,
    run_id: str | None = None,
    clock: Clock | None = None,
) -> PendingApproval:
    """Consume an approved approval for a resubmitted action.

    The resubmitted envelope is re-canonicalized and its ``action_hash``
    compared with the persisted one. The caller's identity (user/tenant/
    session/run) is also checked against the row — a valid approval id from a
    different user/tenant/session is refused. Exactly one caller can consume.
    """
    clock = clock or SystemClock()
    row = db.get(PendingApproval, approval_id)
    if row is None:
        raise _sm.InvalidTransition(f"approval {approval_id!r} not found")

    # identity binding — a stolen/replayed approval id must not cross a boundary
    if int(row.user_id) != int(user_id):
        _fail_audit(db, row, "identity.user_mismatch", clock)
        raise GrantScopeError("approval belongs to a different user")
    if (row.tenant_id or None) != (tenant_id or None):
        _fail_audit(db, row, "identity.tenant_mismatch", clock)
        raise GrantScopeError("approval belongs to a different tenant")
    if session_id is not None and row.session_id is not None and int(row.session_id) != int(session_id):
        _fail_audit(db, row, "identity.session_mismatch", clock)
        raise GrantScopeError("approval belongs to a different session")
    if run_id is not None and row.run_id is not None and str(row.run_id) != str(run_id):
        _fail_audit(db, row, "identity.run_mismatch", clock)
        raise GrantScopeError("approval belongs to a different run")

    resubmitted_hash = resubmit_envelope.action_hash()
    if resubmitted_hash != row.action_hash:
        _fail_audit(db, row, "envelope.mismatch", clock)
        raise EnvelopeMismatch(
            "resubmitted action envelope does not match the approved action_hash"
        )

    consumed = _sm.consume(db, approval_id, expected_action_hash=row.action_hash, clock=clock)
    _log.append_event(
        db, tenant_id=row.tenant_id, user_id=row.user_id, event_type="approval.consumed",
        approval_id=approval_id, action_hash=row.action_hash, clock=clock,
    )
    return consumed


def _fail_audit(db: Session, row: PendingApproval, reason: str, clock: Clock) -> None:
    try:
        _log.append_event(
            db, tenant_id=row.tenant_id, user_id=row.user_id,
            event_type="approval.consume_refused", approval_id=row.id,
            action_hash=row.action_hash, event_metadata={"reason": reason}, clock=clock,
        )
    except Exception:
        pass  # auditing a refusal must never mask the refusal itself


# ── session grants ───────────────────────────────────────────────────


def create_session_grant(
    db: Session,
    *,
    approval: PendingApproval,
    envelope: ActionEnvelope,
    session_expires_at: _dt.datetime,
    explicit_grant_decision: bool,
    clock: Clock | None = None,
) -> SessionGrant:
    """Create a narrow, reusable grant.

    ``explicit_grant_decision`` MUST be True — a one-time approval never
    silently becomes a session grant; the user must separately choose "allow
    this for the rest of my session". ``envelope`` must be the same verified
    envelope the approval was consumed against (its ``action_hash`` is checked
    against ``approval.action_hash``); the grant's reusable identity is
    ``envelope.grant_scope_hash()`` — no nonce, no run_id. Expires no later than
    the session; revocable; has its own CSPRNG nonce.
    """
    clock = clock or SystemClock()
    now = clock.now()
    if not explicit_grant_decision:
        raise GrantScopeError(
            "a session grant requires an explicit session-scoped user decision; "
            "approving a single action does not create a grant"
        )
    if approval.session_id is None:
        raise GrantScopeError("cannot create a session grant for an approval with no session")
    if envelope.action_hash() != approval.action_hash:
        raise GrantScopeError("envelope does not match the approval it is derived from")

    exp = min(ensure_utc(session_expires_at),
              now + _dt.timedelta(seconds=DEFAULT_TTL_SECONDS * 6))
    scope_hash = envelope.grant_scope_hash()
    grant = SessionGrant(
        id=str(uuid.uuid4()),
        grant_nonce=secrets.token_hex(24),
        grant_scope_version=GRANT_SCOPE_VERSION,
        tenant_id=approval.tenant_id,
        user_id=approval.user_id,
        session_id=approval.session_id,
        tool_id=approval.tool_id,
        server_fingerprint=approval.server_fingerprint,
        tool_schema_hash=approval.tool_schema_hash,
        grant_scope_hash=scope_hash,
        policy_version=approval.policy_version,
        creating_approval_id=approval.id,
        created_at=now,
        expires_at=exp,
    )
    db.add(grant)
    db.commit()
    _log.append_event(db, tenant_id=grant.tenant_id, user_id=grant.user_id,
                      event_type="grant.created", grant_id=grant.id,
                      action_hash=approval.action_hash,
                      event_metadata={"grant_scope_hash": scope_hash}, clock=clock)
    return grant


def resolve_session_grant(
    db: Session,
    *,
    envelope: ActionEnvelope,
    user_id: int,
    session_id: int,
    tenant_id: str | None = None,
    clock: Clock | None = None,
) -> SessionGrant | None:
    """Return a live grant whose ``grant_scope_hash`` exactly matches this
    action's stable scope, or None. Pure read — no side effects.

    Fails closed: any mismatch (tenant / user / session / tool / fingerprint /
    schema / args / resources / risk / policy, folded into grant_scope_hash),
    expiry, or revocation returns None. A changed ``action_nonce`` or ``run_id``
    does NOT prevent a match (that is the whole point of a reusable grant).
    """
    clock = clock or SystemClock()
    now = clock.now()
    scope_hash = envelope.grant_scope_hash()
    q = (
        db.query(SessionGrant)
        .filter(SessionGrant.user_id == user_id)
        .filter(SessionGrant.session_id == session_id)
        .filter(SessionGrant.tool_id == envelope.tool_id)
        .filter(SessionGrant.server_fingerprint == envelope.server_fingerprint)
        .filter(SessionGrant.tool_schema_hash == envelope.tool_schema_hash)
        .filter(SessionGrant.grant_scope_hash == scope_hash)
        .filter(SessionGrant.policy_version == envelope.policy_version)
        .filter(SessionGrant.grant_scope_version == GRANT_SCOPE_VERSION)
        .filter(SessionGrant.revoked_at.is_(None))
    )
    for g in q.all():
        if (g.tenant_id or None) != (tenant_id or None):
            continue
        if ensure_utc(g.expires_at) <= now:
            continue
        return g
    return None


# Event name for the authorization-to-dispatch handoff. It means "session-grant
# authorization was EXERCISED for this attempt" — NOT that the tool executed or
# succeeded. Phase 1B.2 adds the separate correlated dispatcher lifecycle events
# tool.execution.started / .succeeded / .failed / .blocked.
GRANT_AUTHORIZATION_USED_EVENT = "grant.authorization_used"


def dispatch_with_session_grant(
    db: Session,
    *,
    envelope: ActionEnvelope,
    user_id: int,
    session_id: int,
    execute: Callable[[], object],
    tenant_id: str | None = None,
    clock: Clock | None = None,
) -> object:
    """The server-side authorization-to-dispatch handoff for ONE execution
    attempt authorised by a reusable SessionGrant.

    1. Resolve a live grant for this (fresh-nonce) envelope.
    2. If none -> raise ``GrantScopeError``; ``execute`` is NEVER called and NO
       audit event is written (a failed check cannot reach the callback).
    3. If a grant matches -> write EXACTLY ONE ``grant.authorization_used``
       ActionLog event (authorization exercised for this attempt, referencing
       the grant id + this attempt's fresh ``action_hash`` / ``action_nonce``),
       written BEFORE ``execute`` so a later crash/provider failure cannot
       erase the evidence that authorization was exercised, and NOT claiming
       execution success. Then call ``execute()`` and return its result.

    A passive "does a grant exist?" check must use ``resolve_session_grant``,
    which writes nothing.
    """
    clock = clock or SystemClock()
    grant = resolve_session_grant(
        db, envelope=envelope, user_id=user_id, session_id=session_id,
        tenant_id=tenant_id, clock=clock,
    )
    if grant is None:
        raise GrantScopeError("no live session grant authorises this action")
    _log.append_event(
        db, tenant_id=grant.tenant_id, user_id=grant.user_id,
        event_type=GRANT_AUTHORIZATION_USED_EVENT, grant_id=grant.id,
        action_hash=envelope.action_hash(),
        event_metadata={
            "grant_scope_hash": grant.grant_scope_hash,
            "action_nonce": envelope.action_nonce,
            "meaning": "session-grant authorization exercised for this attempt; "
                       "NOT an execution-result event",
        },
        clock=clock,
    )
    return execute()


def revoke_session_grant(db: Session, grant_id: str, *, clock: Clock | None = None) -> bool:
    """Revoke a grant. Returns True if this call revoked it (idempotent)."""
    clock = clock or SystemClock()
    now = clock.now()
    from sqlalchemy import update as _update

    n = db.execute(
        _update(SessionGrant)
        .where(SessionGrant.id == grant_id)
        .where(SessionGrant.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if n:
        g = db.get(SessionGrant, grant_id)
        _log.append_event(db, tenant_id=g.tenant_id, user_id=g.user_id,
                          event_type="grant.revoked", grant_id=grant_id, clock=clock)
    return bool(n)
