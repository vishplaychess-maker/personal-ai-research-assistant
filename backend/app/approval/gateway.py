"""Centralized approval gateway / dispatcher boundary (Phase 1B.2).

``dispatch(ctx, mcp_name, args, execute_mcp)`` is the ONE trusted path a
registered MCP tool call goes through. It:

  1. checks gateway availability (fail closed);
  2. uses the server-owned identity in ``ctx`` (never client/LLM values);
  3. resolves the tool through the trusted registry (unknown / locked /
     disabled / schema- or fingerprint-changed / registry error -> blocked);
  4. enforces bounded active-proposal ceilings (per turn / session / user);
  5. builds & hashes the ActionEnvelope server-side;
  6. creates or reuses a PendingApproval via ``proposal_dedupe_key``;
  7. if an exact one-time approval is ``approved`` -> reconstructs & re-hashes
     the envelope from the stored ``action_nonce`` and atomically consumes it;
     else if a matching live SessionGrant exists -> uses it (writing exactly one
     ``grant.authorization_used`` at the handoff);
     else -> returns ``approval_required`` WITHOUT executing;
  8. enforces at-most-once LOCAL dispatch per persisted ``tool_call_id``
     (``dispatch_records`` UNIQUE + CAS);
  9. writes ``tool.execution.started`` then ``…succeeded`` / ``…failed`` —
     sanitized, with correlation fields, NEVER raw secrets or unrestricted
     tool output;
 10. invokes only the caller-supplied trusted ``execute_mcp`` callback.

**Every pre-invocation refusal emits a sanitized ``tool.execution.blocked``
event** (Phase 1B.2 correction — Decision 3): a stable machine-readable
``reason_code`` (see ``BLOCK_REASONS``), ``execution_attempted = false``, and
only trusted server-established correlation fields — never raw args, credentials,
headers, launch config, or unrestricted error text. A blocked event never
implies execution started; ``execute_mcp`` is never called on a blocked path.
If the mandatory blocked-audit write itself fails, execution stays blocked and
we do NOT recurse through the same failed ActionLog path
(``reason_code = action_log_failure`` is returned without a further event).

Any registry / gateway / DB / authorization / ActionLog error blocks execution.
There is NO fallback to legacy marker auto-execution.

External exactly-once is NOT claimed: ``dispatch_records`` is a local guarantee.
A crash after dispatch begins leaves ``outcome = 'started'`` (unknown); a later
attempt on the same ``tool_call_id`` is blocked, not auto-retried.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.approval import action_log as _log
from app.approval import registry as _registry
from app.approval import service as _service
from app.approval import state_machine as _sm
from app.approval.clock import Clock, SystemClock
from app.approval.dedupe import PROPOSAL_DEDUPE_VERSION, proposal_dedupe_key
from app.approval.errors import (
    ActiveProposalLimitError,
    ApprovalAlreadyConsumed,
    ApprovalExpired,
    ApprovalError,
    ChainIntegrityError,
    EnvelopeMismatch,
    GrantScopeError,
    InvalidTransition,
)
from app.approval.hashing import args_hash as _args_hash
from app.approval.hashing import sha256_hex
from app.approval.redaction import redact as _redact
from app.approval.service import GRANT_AUTHORIZATION_USED_EVENT
from app.models.models import DispatchRecord, PendingApproval

_ERR_MAX = 500

TOOL_EXECUTION_BLOCKED_EVENT = "tool.execution.blocked"
ACTIVE_PROPOSAL_LIMIT_EVENT = "approval.limit_exceeded"

# The stable, closed vocabulary of pre-invocation block reasons. The gateway
# NEVER emits a reason string outside this set — an attacker-influenced value
# (tool name, error text, marker content) can never become a reason code.
BLOCK_REASONS = frozenset({
    "gateway_unavailable",
    "registry_unavailable",
    "unknown_tool",
    "tool_disabled",
    "tool_locked",
    "schema_mismatch",
    "fingerprint_mismatch",
    "tenant_mismatch",
    "user_mismatch",
    "session_mismatch",
    "approval_denied",
    "approval_expired",
    "approval_consumed",
    "envelope_mismatch",
    "active_proposal_limit",
    "duplicate_dispatch",
    "dispatch_incomplete",
    "action_log_failure",
    "database_failure",
})

_ACTIVE_STATES = (_sm.REQUESTED, _sm.APPROVED)


@dataclass(frozen=True)
class GatewayContext:
    """Server-established caller context. Every field is server-owned."""
    db: Session
    user_id: int
    tenant_id: str | None
    session_id: int | None
    run_id: str | None
    execution_turn_id: str
    tool_call_id: str
    policy_version: str = _service.DEFAULT_POLICY_VERSION


@dataclass
class GatewayResult:
    status: str            # executed | approval_required | blocked
    reason_code: str       # ok | needs_approval | tool_errored | <BLOCK_REASONS member>
    detail: str = ""
    approval_id: str | None = None
    grant_id: str | None = None
    tool_output: str | None = None
    is_error: bool = False
    correlation: dict = field(default_factory=dict)


def _tenant_eq(col, tenant_id: str | None):
    return col.is_(None) if tenant_id is None else col == tenant_id


def _sanitize_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return str(_redact(text))[:_ERR_MAX]


def _safe_correlation(
    ctx: GatewayContext,
    *,
    reason_code: str | None = None,
    trusted=None,
    action_hash: str | None = None,
    approval_id: str | None = None,
    grant_id: str | None = None,
) -> dict:
    """Correlation payload built ONLY from trusted, server-established values.
    Never contains tool arguments, credentials, headers, launch config, or
    error text."""
    corr: dict = {
        "approval_id": approval_id,
        "grant_id": grant_id,
        "action_hash": action_hash,
        "run_id": ctx.run_id,
        "execution_turn_id": ctx.execution_turn_id,
        "tool_call_id": ctx.tool_call_id,
        "tenant_id": ctx.tenant_id,
        "user_id": ctx.user_id,
        "session_id": ctx.session_id,
        "policy_version": ctx.policy_version,
    }
    if trusted is not None:
        corr.update(
            tool_id=trusted.tool_id,
            mcp_server_id=trusted.mcp_server_id,
            server_fingerprint=trusted.server_fingerprint,
            tool_schema_hash=trusted.tool_schema_hash,
            policy_version=trusted.policy_version,
        )
    if reason_code is not None:
        corr["reason_code"] = reason_code
        corr["execution_attempted"] = False
    return corr


# backwards-compatible alias used by the execution-lifecycle events
def _correlation(ctx: GatewayContext, *, trusted, action_hash: str,
                 approval_id: str | None, grant_id: str | None) -> dict:
    corr = _safe_correlation(ctx, trusted=trusted, action_hash=action_hash,
                             approval_id=approval_id, grant_id=grant_id)
    corr.pop("reason_code", None)
    corr.pop("execution_attempted", None)
    return corr


def _block(
    ctx: GatewayContext,
    reason_code: str,
    detail: str,
    *,
    trusted=None,
    approval_id: str | None = None,
    grant_id: str | None = None,
    action_hash: str | None = None,
    emit_event: bool = True,
    clock: Clock | None = None,
) -> GatewayResult:
    """Return a blocked result and (best-effort) write ONE sanitized
    ``tool.execution.blocked`` audit event. ``reason_code`` MUST be a
    ``BLOCK_REASONS`` member. ``emit_event=False`` is used only when the block
    is itself caused by an ActionLog write failure (no recursion)."""
    if reason_code not in BLOCK_REASONS:  # never emit an attacker-influenced code
        reason_code = "database_failure"
    corr = _safe_correlation(ctx, reason_code=reason_code, trusted=trusted,
                             action_hash=action_hash, approval_id=approval_id,
                             grant_id=grant_id)
    if emit_event:
        try:
            _log.append_event(
                ctx.db, tenant_id=ctx.tenant_id, user_id=ctx.user_id,
                event_type=TOOL_EXECUTION_BLOCKED_EVENT, approval_id=approval_id,
                grant_id=grant_id, action_hash=action_hash, event_metadata=corr,
                clock=clock or SystemClock(),
            )
        except Exception:
            # The block stands regardless. Do NOT retry through the same path.
            try:
                ctx.db.rollback()
            except Exception:
                pass
    return GatewayResult(status="blocked", reason_code=reason_code, detail=detail,
                         approval_id=approval_id, grant_id=grant_id, correlation=corr)


def _enforce_active_proposal_limits(
    ctx: GatewayContext, trusted, *, clock: Clock,
) -> GatewayResult | None:
    """Fail closed if creating a NEW active proposal would exceed a bounded
    ceiling for this attempt's turn / session / user. A retry of an already
    pending logical call (same ``tool_call_id``) is never blocked here. Returns
    a blocked ``GatewayResult`` on limit exhaustion, else ``None``.

    Concurrency: the count + the subsequent ``request_approval`` insert run
    under the per-scope ActionLog chain lock (SQLite ``BEGIN IMMEDIATE`` /
    PostgreSQL ``SELECT … FOR UPDATE`` on the scope head row). Shared
    multi-worker / PostgreSQL enforcement remains subject to the hosted
    PostgreSQL verification gate.
    """
    db = ctx.db
    # close any open read transaction so the chain lock's BEGIN IMMEDIATE takes
    # effect on SQLite (nothing uncommitted exists at this point in dispatch()).
    try:
        db.rollback()
    except Exception:
        pass
    try:
        _log._ChainLock(db).acquire(
            _log.chain_scope_id(tenant_id=ctx.tenant_id, user_id=ctx.user_id)
        )
    except Exception:
        pass  # best-effort serialization; the COUNT below still runs

    not_this_call = or_(
        PendingApproval.tool_call_id.is_(None),
        PendingApproval.tool_call_id != ctx.tool_call_id,
    )

    def _count(*conds) -> int:
        q = db.query(PendingApproval).filter(
            PendingApproval.state.in_(_ACTIVE_STATES), not_this_call, *conds,
        )
        return q.count()

    checks: list[tuple[str, int, int]] = []
    turn_n = _count(PendingApproval.execution_turn_id == ctx.execution_turn_id)
    checks.append(("execution_turn", int(settings.max_active_proposals_per_turn), turn_n))
    if ctx.session_id is not None:
        sess_n = _count(
            PendingApproval.user_id == ctx.user_id,
            PendingApproval.session_id == ctx.session_id,
            _tenant_eq(PendingApproval.tenant_id, ctx.tenant_id),
        )
        checks.append(("session", int(settings.max_active_proposals_per_session), sess_n))
    user_n = _count(
        PendingApproval.user_id == ctx.user_id,
        _tenant_eq(PendingApproval.tenant_id, ctx.tenant_id),
    )
    checks.append(("user", int(settings.max_active_proposals_per_user), user_n))

    for scope, limit, active in checks:
        if active >= limit:
            exc = ActiveProposalLimitError(scope, limit, active)
            # bounded metric/audit signal — scope + counts only, never args
            try:
                _log.append_event(
                    db, tenant_id=ctx.tenant_id, user_id=ctx.user_id,
                    event_type=ACTIVE_PROPOSAL_LIMIT_EVENT,
                    action_hash=None,
                    event_metadata={
                        "scope": scope, "limit": limit, "active_count": active,
                        "tool_id": trusted.tool_id if trusted is not None else None,
                        "execution_turn_id": ctx.execution_turn_id,
                        "session_id": ctx.session_id, "user_id": ctx.user_id,
                        "tenant_id": ctx.tenant_id,
                    },
                    clock=clock,
                )
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            return _block(ctx, "active_proposal_limit", str(exc), trusted=trusted,
                          clock=clock)
    return None


def dispatch(
    ctx: GatewayContext,
    *,
    mcp_name: str,
    args: dict,
    execute_mcp: Callable[[], tuple[str, bool]],
    clock: Clock | None = None,
) -> GatewayResult:
    clock = clock or SystemClock()
    db = ctx.db

    # 1. gateway availability (fail closed)
    if not settings.approval_gateway_available:
        return _block(ctx, "gateway_unavailable", "approval gateway is unavailable",
                      clock=clock)

    # 3-4. trusted registry resolution (precise fail-closed reason)
    try:
        trusted, why = _registry.resolve_reason(
            db, owner_user_id=ctx.user_id, tool_id=mcp_name, tenant_id=ctx.tenant_id,
        )
    except Exception:  # DB / registry failure -> block
        return _block(ctx, "registry_unavailable", "trusted registry is unavailable",
                      clock=clock)
    if trusted is None:
        detail = {
            "unknown_tool": "tool is not registered",
            "tool_disabled": "tool is disabled",
            "tool_locked": "tool is locked pending operator verification",
            "schema_mismatch": "tool schema changed; prior authorizations are invalid",
            "fingerprint_mismatch": "server fingerprint changed; prior authorizations are invalid",
            "tenant_mismatch": "tool belongs to a different tenant",
        }.get(why, "tool is not authorized")
        reason = why if why in BLOCK_REASONS else "unknown_tool"
        return _block(ctx, reason, detail, clock=clock)

    # A prior DENIED decision for this exact persisted logical call sticks — do
    # not silently re-prompt the user for the same tool_call_id (nag-attack).
    denied = (db.query(PendingApproval)
              .filter(PendingApproval.tool_call_id == ctx.tool_call_id,
                      PendingApproval.state == _sm.DENIED).first())
    if denied is not None:
        return _block(ctx, "approval_denied",
                      "this logical tool call was denied by the user",
                      trusted=trusted, approval_id=denied.id, clock=clock)

    # 8 (early). at-most-once LOCAL dispatch: if this persisted logical
    # tool_call_id already has a dispatch record, do NOT re-run — regardless of
    # approval state. A fresh proposal for the same call must not re-execute it.
    prior = (db.query(DispatchRecord)
             .filter(DispatchRecord.tool_call_id == ctx.tool_call_id).one_or_none())
    if prior is not None:
        if prior.outcome in ("succeeded", "failed"):
            return _block(ctx, "duplicate_dispatch",
                          f"this logical tool_call_id already dispatched ({prior.outcome}); "
                          "a side-effecting tool is not auto-retried",
                          trusted=trusted, clock=clock)
        return _block(ctx, "dispatch_incomplete",
                      "a prior dispatch of this tool_call_id is in progress or its "
                      "outcome is unknown; not auto-retried",
                      trusted=trusted, clock=clock)

    # 4. bounded active-proposal ceilings (per turn / session / user)
    limited = _enforce_active_proposal_limits(ctx, trusted, clock=clock)
    if limited is not None:
        return limited

    # 5-7. build the trusted envelope + create/reuse the PendingApproval
    a_hash_args = _args_hash(args)
    dedupe = proposal_dedupe_key({
        "proposal_dedupe_version": PROPOSAL_DEDUPE_VERSION,
        "tenant_id": ctx.tenant_id, "user_id": ctx.user_id,
        "session_id": ctx.session_id, "run_id": ctx.run_id,
        "execution_turn_id": ctx.execution_turn_id, "tool_call_id": ctx.tool_call_id,
        "tool_id": trusted.tool_id, "tool_version": trusted.tool_version,
        "source_type": "mcp",
        "mcp_server_id": str(trusted.mcp_server_id) if trusted.mcp_server_id else None,
        "server_fingerprint": trusted.server_fingerprint,
        "tool_schema_hash": trusted.tool_schema_hash,
        "args_hash": a_hash_args,
        "affected_resources": [],
        "network_scope": None, "filesystem_scope": None, "credential_scope": None,
        "risk_level": "medium", "risk_scope_flags": [],
        "policy_version": trusted.policy_version,
    })
    try:
        env, approval = _service.request_approval(
            db, tool_id=trusted.tool_id, source_type="mcp", args=args,
            server_fingerprint=trusted.server_fingerprint,
            tool_schema_hash=trusted.tool_schema_hash, user_id=ctx.user_id,
            tenant_id=ctx.tenant_id, session_id=ctx.session_id, run_id=ctx.run_id,
            tool_version=trusted.tool_version or None,
            mcp_server_id=str(trusted.mcp_server_id) if trusted.mcp_server_id else None,
            affected_resources=(), execution_turn_id=ctx.execution_turn_id,
            tool_call_id=ctx.tool_call_id, proposal_dedupe_key=dedupe,
            policy_version=trusted.policy_version, clock=clock,
        )
    except EnvelopeMismatch as exc:
        return _block(ctx, "envelope_mismatch", _sanitize_error(exc),
                      trusted=trusted, clock=clock)
    except ChainIntegrityError as exc:
        return _block(ctx, "database_failure", _sanitize_error(exc),
                      trusted=trusted, clock=clock)
    except ApprovalError as exc:
        return _block(ctx, "database_failure", _sanitize_error(exc),
                      trusted=trusted, clock=clock)
    except Exception as exc:
        return _block(ctx, "database_failure", _sanitize_error(exc),
                      trusted=trusted, clock=clock)

    a_hash = env.action_hash()

    # 7. authorization: exact one-time approval, else a matching SessionGrant
    if approval.state == _sm.APPROVED:
        try:
            _service.consume_approval(
                db, approval.id, resubmit_envelope=env, user_id=ctx.user_id,
                tenant_id=ctx.tenant_id, session_id=ctx.session_id,
                run_id=ctx.run_id, clock=clock,
            )
        except ApprovalExpired as exc:
            return _block(ctx, "approval_expired", _sanitize_error(exc),
                          trusted=trusted, approval_id=approval.id,
                          action_hash=a_hash, clock=clock)
        except ApprovalAlreadyConsumed as exc:
            return _block(ctx, "approval_consumed", _sanitize_error(exc),
                          trusted=trusted, approval_id=approval.id,
                          action_hash=a_hash, clock=clock)
        except InvalidTransition as exc:
            state = getattr(db.get(PendingApproval, approval.id), "state", None)
            reason = {"denied": "approval_denied", "expired": "approval_expired",
                      "consumed": "approval_consumed"}.get(state, "approval_consumed")
            return _block(ctx, reason, _sanitize_error(exc), trusted=trusted,
                          approval_id=approval.id, action_hash=a_hash, clock=clock)
        except EnvelopeMismatch as exc:
            return _block(ctx, "envelope_mismatch", _sanitize_error(exc),
                          trusted=trusted, approval_id=approval.id,
                          action_hash=a_hash, clock=clock)
        except GrantScopeError as exc:
            msg = str(exc).lower()
            reason = ("user_mismatch" if "user" in msg else
                      "tenant_mismatch" if "tenant" in msg else
                      "session_mismatch")  # session / run both -> session_mismatch
            return _block(ctx, reason, _sanitize_error(exc), trusted=trusted,
                          approval_id=approval.id, action_hash=a_hash, clock=clock)
        return _do_dispatch(ctx, env, trusted=trusted, execute_mcp=execute_mcp,
                            approval_id=approval.id, clock=clock)

    grant = _service.resolve_session_grant(
        db, envelope=env, user_id=ctx.user_id, session_id=ctx.session_id or 0,
        tenant_id=ctx.tenant_id, clock=clock,
    )
    if grant is not None:
        return _do_dispatch(ctx, env, trusted=trusted, execute_mcp=execute_mcp,
                            grant_id=grant.id, clock=clock)

    # no authorization -> approval required, do NOT execute (this is the normal
    # first pass, NOT a block — no tool.execution.blocked event)
    return GatewayResult(
        status="approval_required", reason_code="needs_approval",
        detail="a user approval or a matching session grant is required",
        approval_id=approval.id,
        correlation=_correlation(ctx, trusted=trusted, action_hash=a_hash,
                                 approval_id=approval.id, grant_id=None),
    )


def _do_dispatch(
    ctx: GatewayContext,
    env,
    *,
    trusted,
    execute_mcp: Callable[[], tuple[str, bool]],
    approval_id: str | None = None,
    grant_id: str | None = None,
    clock: Clock | None = None,
) -> GatewayResult:
    clock = clock or SystemClock()
    db = ctx.db
    a_hash = env.action_hash()
    corr = _correlation(ctx, trusted=trusted, action_hash=a_hash,
                        approval_id=approval_id, grant_id=grant_id)

    # 8. at-most-once LOCAL dispatch per persisted tool_call_id
    rec = DispatchRecord(
        id=str(uuid.uuid4()), tool_call_id=ctx.tool_call_id, tenant_id=ctx.tenant_id,
        user_id=ctx.user_id, approval_id=approval_id, grant_id=grant_id,
        action_hash=a_hash, outcome="started", started_at=clock.now(),
    )
    db.add(rec)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = (db.query(DispatchRecord)
                 .filter(DispatchRecord.tool_call_id == ctx.tool_call_id).one_or_none())
        if prior is not None and prior.outcome == "succeeded":
            return _block(ctx, "duplicate_dispatch",
                          "this logical tool_call_id already dispatched (succeeded)",
                          trusted=trusted, approval_id=approval_id, grant_id=grant_id,
                          action_hash=a_hash, clock=clock)
        return _block(ctx, "dispatch_incomplete",
                      "a prior dispatch of this tool_call_id is in progress or "
                      "its outcome is unknown; not auto-retried",
                      trusted=trusted, approval_id=approval_id, grant_id=grant_id,
                      action_hash=a_hash, clock=clock)

    # authorization handoff event (grant path only — the one-time path's
    # approval.consumed was already written by consume_approval)
    if grant_id is not None:
        try:
            _log.append_event(
                db, tenant_id=ctx.tenant_id, user_id=ctx.user_id,
                event_type=GRANT_AUTHORIZATION_USED_EVENT, grant_id=grant_id,
                action_hash=a_hash,
                event_metadata={**corr,
                                "meaning": "session-grant authorization exercised for "
                                           "this attempt; NOT an execution-result event"},
                clock=clock,
            )
        except Exception as exc:
            rec.outcome = "failed"; rec.finished_at = clock.now(); db.commit()
            # do NOT recurse through the same failed ActionLog path
            return _block(ctx, "action_log_failure",
                          f"authorization audit failed: {_sanitize_error(exc)}",
                          trusted=trusted, approval_id=approval_id, grant_id=grant_id,
                          action_hash=a_hash, emit_event=False, clock=clock)

    try:
        _log.append_event(db, tenant_id=ctx.tenant_id, user_id=ctx.user_id,
                          event_type="tool.execution.started",
                          approval_id=approval_id, grant_id=grant_id, action_hash=a_hash,
                          event_metadata=corr, clock=clock)
    except Exception as exc:
        rec.outcome = "failed"; rec.finished_at = clock.now(); db.commit()
        return _block(ctx, "action_log_failure",
                      f"started-event write failed: {_sanitize_error(exc)}",
                      trusted=trusted, approval_id=approval_id, grant_id=grant_id,
                      action_hash=a_hash, emit_event=False, clock=clock)

    # 10. invoke the trusted registered dispatcher
    try:
        out_text, is_error = execute_mcp()
    except Exception as exc:  # ordinary tool error -> failed, return a result
        rec.outcome = "failed"
        rec.finished_at = clock.now()
        db.commit()
        _safe_event(db, ctx, "tool.execution.failed", approval_id, grant_id, a_hash,
                    {**corr, "error": _sanitize_error(exc)}, clock)
        return GatewayResult(status="executed", reason_code="tool_errored",
                             detail="tool raised", tool_output=_sanitize_error(exc),
                             is_error=True, approval_id=approval_id, grant_id=grant_id,
                             correlation=corr)
    except BaseException:  # KeyboardInterrupt / SystemExit — honest unknown, propagate
        try:
            rec.outcome = "incomplete"
            rec.finished_at = clock.now()
            db.commit()
            _safe_event(db, ctx, "tool.execution.failed", approval_id, grant_id, a_hash,
                        {**corr, "error": "process interrupted; outcome unknown"}, clock)
        except Exception:
            db.rollback()
        raise

    rec.outcome = "failed" if is_error else "succeeded"
    rec.finished_at = clock.now()
    db.commit()
    ev = "tool.execution.failed" if is_error else "tool.execution.succeeded"
    # NEVER put raw tool output in the audit payload — only a hash + size.
    out_meta = {**corr, "output_sha256": sha256_hex((out_text or "").encode("utf-8")),
                "output_len": len(out_text or "")}
    if is_error:
        out_meta["error"] = str(_redact(out_text or ""))[:_ERR_MAX]
    _safe_event(db, ctx, ev, approval_id, grant_id, a_hash, out_meta, clock)

    return GatewayResult(
        status="executed", reason_code="tool_errored" if is_error else "ok",
        detail=ev, tool_output=out_text, is_error=is_error,
        approval_id=approval_id, grant_id=grant_id, correlation=corr,
    )


def _safe_event(db, ctx, event_type, approval_id, grant_id, action_hash, meta, clock):
    try:
        _log.append_event(db, tenant_id=ctx.tenant_id, user_id=ctx.user_id,
                          event_type=event_type, approval_id=approval_id,
                          grant_id=grant_id, action_hash=action_hash,
                          event_metadata=meta, clock=clock)
    except Exception:
        pass  # the dispatch outcome is already recorded on the DispatchRecord
