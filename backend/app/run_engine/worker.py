"""RunWorker — durable-run claim / lease / cancellation / recovery core (Phase 1B.2R — MP-1B).

This is the **mechanism only**. It is NOT started from the app lifespan; a later
flow cutover (MP-3) wires it in with one ``await RunWorker(session_factory).run_forever()``.
``run_forever`` refuses to run unless BOTH ``settings.run_engine_enabled`` and
``settings.run_worker_enabled`` are true, so the wiring stays fail-closed.

What it does
------------
* **Atomic claim.** One ``queued`` run is taken with a conditional ``UPDATE``
  guarded by ``runs.version`` (CAS) and a lease-expiry predicate. Two racing
  claimers yield exactly one lease owner; the loser re-polls.
* **Server-minted lease.** ``lease_owner = "<worker_id>/<uuid4-hex>"`` is an
  identity *and* an unforgeable per-claim token. Every worker mutation carries
  ``WHERE lease_owner = :mine AND version = :v``; a stale token or a lost race
  matches 0 rows → :class:`RunLeaseLost`, and the worker abandons the run for
  this sweep (the next sweep re-evaluates it). ``renew_lease`` extends only the
  current holder's lease.
* **Fresh session-family revalidation.** The run's own refresh-token family is
  re-checked against the LIVE database via
  :func:`app.run_engine.session_binding.validate_session_binding`, using an
  :class:`AuthContext` built from the run row — never client input. Checked at
  claim time AND again immediately before a handler runs. A revoked / expired
  family → the run fails closed (``status_reason = "session_revoked"``); the
  handler never runs. The cached ``AuthContext`` validity flag is never trusted.
* **Cancel wins, consumed exactly once.** A live ``cancel_requested`` signal is
  consumed with ``UPDATE ... SET consumed_at = :now WHERE id = :id AND
  consumed_at IS NULL`` (rowcount == 1 is the once-guarantee); the consume, the
  ``run.status`` events, and ``running -> cancelling -> cancelled`` commit
  together. Checked before handler invocation (blocks it) and again after a
  side-effect-free handler returns.
* **Retry-safe recovery.** An expired-lease ``running`` run whose ``run.kind``
  has a registered **retry_safe** handler, still inside its wall-clock deadline,
  is reclaimed (new lease token, version bump) and run again. Anything else — an
  unknown handler, a non-retry-safe handler — becomes ``outcome_unknown`` and is
  **never** auto-retried.

Out of MP-1B scope (asserted): no MCP / LLM / browser / terminal / desktop /
Python execution; no approval resume; no public Run API, ``/messages/stream``,
SSE, frontend, or scheduler. The handler registry ships EMPTY — an unknown
``run.kind`` fails closed.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Callable, Optional
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.models import (
    DispatchRecord,
    Message,
    MessageRole,
    Run,
    RunEvent,
    RunSignal,
    RunStep,
    _utcnow_aware,
)
from app.run_engine.context import AuthContext
from app.run_engine.errors import (
    ControlEventContention,
    RunLeaseLost,
    RunNotFound,
    SessionBindingMismatch,
    SessionBindingRequired,
)
from app.run_engine.events import validate_event_data
from app.run_engine.payloads import seal_payload
from app.run_engine.session_binding import validate_session_binding
from app.run_engine.signals import parse_signal_payload
from app.run_engine.state_machine import (
    APPROVED,
    CANCELLED,
    CANCELLING,
    DENIED,
    FAILED,
    OUTCOME_UNKNOWN,
    QUEUED,
    RESUMING,
    RUNNING,
    RUN_TERMINAL,
    SUCCEEDED,
    TIMED_OUT,
    WAITING_FOR_APPROVAL,
    assert_run_transition,
)

logger = logging.getLogger(__name__)

_MAX_WORKER_ID = 24
_SYSTEM_PRINCIPAL = object()  # AuthContext.user placeholder — never dereferenced


def _aware(dt: Optional[_dt.datetime]) -> Optional[_dt.datetime]:
    """Coerce a datetime read back from the DB to tz-aware UTC. SQLite drops
    ``tzinfo`` on a ``DateTime(timezone=True)`` round-trip (PostgreSQL keeps it);
    normalize before any Python-side comparison against ``now``."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_dt.timezone.utc)

# Bounded, machine-readable reasons the worker may write to ``runs.status_reason``.
# NEVER a raw exception message / prompt / arg / token. (The closed control-event
# schema registry in ``events.py`` is deliberately not perturbed by MP-1B.)
_WORKER_STATUS_REASONS = frozenset({
    "session_revoked",     # family revoked / expired at claim or pre-invocation
    "unknown_handler",     # no registered handler for run.kind — fail closed
    "handler_error",       # handler raised, or returned ok=False without a code
    "handler_rejected",    # handler returned ok=False with a bounded reject code
    "handler_timeout",     # handler wall-clock exceeded run_worker_handler_timeout_s
    "cancelled",           # driven by a consumed cancel_requested signal
    "deadline_exceeded",   # recovery gave up — past runs.run_deadline_at
    "outcome_unknown",     # expired lease, no retry-safe handler — unprovable
    "provider_rate_limited",  # upstream 429 - bounded slug, never upstream text
})
# Codes a handler may itself return via HandlerResult.error_code (anything else
# collapses to "handler_error").
_HANDLER_ERROR_CODES = frozenset({"handler_error", "handler_rejected", "handler_timeout"})


def _provider_reason(exc) -> str:
    """Bounded reason for a provider stream failure.

    A provider classification is honoured ONLY when it is an allowlisted bounded
    slug; anything else degrades to `provider_error`. The reason never contains
    upstream text, a status line, a header or a credential.
    """
    code = getattr(exc, "code", None)
    return code if code in _WORKER_STATUS_REASONS else "provider_error"

# MP-2: the MCP driver also writes bounded gateway BLOCK_REASONS / durable-flow
# terminals to ``status_reason``. Imported here (no cycle: mcp_flow never imports
# worker) so ``_apply_transition`` accepts them instead of masking them.
from app.run_engine.mcp_flow import (  # noqa: E402
    MCP_RUN_KIND as _MCP_RUN_KIND,
    MCP_STATUS_REASONS,
)
# MP-3B: the chat driver writes bounded provider/marker terminals to
# ``status_reason`` (no cycle — chat_stream never imports worker).
from app.run_engine.chat_stream import (  # noqa: E402
    CHAT_RUN_KIND as _CHAT_RUN_KIND,
    CHAT_STATUS_REASONS,
)
# MP-3C2: durable chat -> MCP approval -> same-run resume. Bounded marker /
# cycle terminals (no cycle — chat_tools never imports worker).
from app.run_engine.chat_tools import CHAT_TOOL_STATUS_REASONS  # noqa: E402
# MP-3C3D4C: durable deep research. Reuses CHAT_STATUS_REASONS for its
# synthesis step (the SAME generation bridge, so the SAME failure modes) —
# only 3 genuinely new bounded reasons are added below (no cycle —
# submission.py never imports worker).
from app.run_engine.submission import _DEEP_RESEARCH_RUN_KIND  # noqa: E402
# MP-3C3D5B: durable multi-agent is FOUNDATION-ONLY — no advance_*/handler
# exists yet — but the generic sweep below must still never claim (or
# "recover") a queued/running multi_agent run: an unregistered handler would
# otherwise immediately fail it closed as unknown_handler before D5C's own
# dedicated sweep can ever see it (no cycle — multi_agent_orchestration.py
# never imports worker).
from app.run_engine.multi_agent_orchestration import (  # noqa: E402
    MULTI_AGENT_RUN_KIND as _MULTI_AGENT_RUN_KIND,
)

_DEEP_RESEARCH_STATUS_REASONS = frozenset({
    "research_no_sources",          # acquisition completed with zero usable sources
    "citation_validation_failed",   # final answer cites a marker not in the sealed map
    "research_citations_missing",   # research content produced but zero valid citations
})

_ALL_STATUS_REASONS = (
    _WORKER_STATUS_REASONS | MCP_STATUS_REASONS | CHAT_STATUS_REASONS
    | CHAT_TOOL_STATUS_REASONS | _DEEP_RESEARCH_STATUS_REASONS
)
# run states the MP-2 MCP driver may lease and advance
_MCP_ADVANCEABLE = frozenset({
    QUEUED, RUNNING, WAITING_FOR_APPROVAL, APPROVED, RESUMING,
})
# run states the MP-3C2 chat driver may lease and advance (adds the durable
# chat -> tool suspend/resume states to MP-3B's QUEUED/RUNNING).
_CHAT_ADVANCEABLE = frozenset({
    QUEUED, RUNNING, WAITING_FOR_APPROVAL, APPROVED, RESUMING,
})
# MP-3C3D4C: deep research has no MCP-approval sub-state-machine (unless a
# future phase explicitly approves an MCP continuation inside it — D4A/D4C
# do not) — only the two generic states.
_DEEP_RESEARCH_ADVANCEABLE = frozenset({QUEUED, RUNNING})
# MP-3C3D5C: multi-agent has no MCP-approval sub-state-machine either — only
# the two generic states, same shape as deep research immediately above.
_MULTI_AGENT_ADVANCEABLE = frozenset({QUEUED, RUNNING})


def _multi_agent_citations_for_message(ctx_snap: dict, citation_markers: list) -> list:
    """The final, server-authoritative local-citation dict list actually
    referenced by the Reviewer's validated final text — sourced ONLY from
    the sealed D2 context snapshot's own already-validated Citation objects
    (never a model-authored citation object), in *citation_markers*'
    already first-reference-ordered, deduplicated order (K — no
    re-ordering/re-deduping performed here). A marker with no matching
    sealed citation (unreachable by construction — every marker in
    *citation_markers* was already checked against this SAME snapshot
    before the artifact was sealed) is defensively skipped, never guessed."""
    by_marker = {
        c["marker"]: c for c in (ctx_snap.get("citations") or [])
        if isinstance(c, dict) and isinstance(c.get("marker"), str)
    }
    return [by_marker[m] for m in citation_markers if m in by_marker]


# ── handler registry ─────────────────────────────────────────────────


@dataclass(frozen=True)
class HandlerContext:
    """Everything a handler is given — deliberately no DB session, no auth
    token, no prompt / args. MP-1B handlers are pure and side-effect-free."""

    run_id: str
    kind: str
    user_id: int
    tenant_id: Optional[str]
    attempt: int  # 0 on first claim; 1 on a recovery reclaim


@dataclass(frozen=True)
class HandlerResult:
    """A handler's outcome. ``error_code`` (when ``ok`` is False) must be a
    bounded slug — free text is dropped in favour of ``"handler_error"``."""

    ok: bool
    error_code: Optional[str] = None


HandlerFn = Callable[[HandlerContext], HandlerResult]


@dataclass(frozen=True)
class HandlerSpec:
    fn: HandlerFn
    retry_safe: bool  # only True is auto-recovered after a lease expiry


_HANDLERS: dict[str, HandlerSpec] = {}


def register_handler(kind: str, fn: HandlerFn, *, retry_safe: bool) -> None:
    """Register the executor for ``run.kind``. ``retry_safe`` MUST be True only
    for a handler with no external / uncertain side effect — it alone controls
    whether an expired-lease run of this kind is re-run or declared
    ``outcome_unknown``."""
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("handler kind must be a non-empty string")
    if not callable(fn):
        raise ValueError("handler fn must be callable")
    _HANDLERS[kind] = HandlerSpec(fn=fn, retry_safe=bool(retry_safe))


def unregister_handler(kind: str) -> None:
    _HANDLERS.pop(kind, None)


def clear_handlers() -> None:
    """Drop every registration (test aid — the registry ships empty)."""
    _HANDLERS.clear()


def resolve_handler(kind: str) -> Optional[HandlerSpec]:
    """The spec for ``kind``, or ``None`` — an unknown kind fails the run closed."""
    return _HANDLERS.get(kind)


# ── worker value objects ─────────────────────────────────────────────


@dataclass(frozen=True)
class ClaimedRun:
    run_id: str
    kind: str
    user_id: int
    tenant_id: Optional[str]
    auth_session_ref: Optional[str]
    lease_owner: str
    version: int
    attempt: int = 0


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: str            # a terminal run state, "abandoned", or "none"
    detail: Optional[str] = None


class _ChatHeartbeat:
    """Bounded background lease renewal for an in-flight MP-3B chat generation
    (a provider may pause longer than one chunk interval — "renew per chunk" is
    not enough). Renews ONLY as the current lease owner/token (``renew_lease``
    is CAS'd on ``lease_owner`` and refuses terminal runs). Stops on completion,
    cancellation, failure, or lease loss; a lost renewal sets ``lease_lost`` so
    the producer aborts — a stale worker can neither keep producing nor create
    the assistant Message, and no second worker reclaims a healthy generation."""

    def __init__(self, worker: "RunWorker", run_id: str, lease_owner: str,
                 *, interval_s: int) -> None:
        self._w = worker
        self._run_id = run_id
        self._owner = lease_owner
        self._interval = max(1, int(interval_s))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.lease_lost = False
        self.beats = 0

    def beat(self) -> bool:
        """One renewal attempt (also the unit-test seam). False → lease lost."""
        ok = bool(self._w.renew_lease(self._run_id, self._owner))
        self.beats += 1
        if not ok:
            self.lease_lost = True
        return ok

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            if not self.beat():
                return

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="chat-lease-hb",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)


# ── the worker ───────────────────────────────────────────────────────


class RunWorker:
    """One worker. Owns its own DB sessions (via ``session_factory``) and lease
    identity. Safe to run several concurrently — correctness rests on the
    conditional-UPDATE CAS, not on there being a single worker."""

    def __init__(
        self,
        session_factory: Callable[[], "object"],
        *,
        worker_id: Optional[str] = None,
        now_fn: Optional[Callable[[], _dt.datetime]] = None,
        mcp_transport: Optional[Callable[..., tuple[str, bool]]] = None,
        python_executor: Optional[Callable[[str], "object"]] = None,
        python_broker: Optional["object"] = None,
        broker_ready: Optional[Callable[[], bool]] = None,
        chat_provider: Optional[Callable[[Optional[str]], "object"]] = None,
        research_search_adapter: Optional["object"] = None,
        research_fetch_adapter: Optional["object"] = None,
    ) -> None:
        self._sf = session_factory
        raw = (worker_id or settings.run_worker_id or "").strip()
        self.worker_id = (raw[:_MAX_WORKER_ID] or f"w-{uuid4().hex[:8]}")
        self._now = now_fn or _utcnow_aware
        self._stopped = False
        # (db, user_id, tool_id, args) -> (text, is_error). Injected in tests;
        # the default calls the real MCP service. Used ONLY by the MP-2 MCP
        # driver, and ONLY through app.approval.gateway.dispatch's execute_mcp.
        self._mcp_transport = mcp_transport
        # MP-4C-R2B-2C-2A - optional Python executor seam (TEST ONLY). Code ->
        # object with .text/.is_error (SandboxOutcome). None means the reserved
        # route has NO in-process executor: real execution then requires the
        # isolated executor BROKER, never a host subprocess (that launcher is
        # refused unconditionally in app.tools.python_sandbox).
        self._python_executor = python_executor
        # MP-4C policy — the fake bypass is a python_executor supplied AT
        # CONSTRUCTION. Production (worker_supervisor) never supplies one, so the
        # "no host-isolated runtime" refusal always applies there. A later
        # assignment to `_python_executor` does NOT flip this flag.
        self._python_executor_injected = python_executor is not None
        # MP-4C — the RESERVE/EXECUTE broker client (executor service). This is a
        # DIFFERENT seam from `python_executor`:
        #   * injecting it is NOT the fake-executor bypass above and must never be
        #     consulted as the security authority for real execution;
        #   * it is NOT the operator gate either (`real_python_execution_available()`
        #     stays the separate production authority);
        #   * broker READINESS (`broker_ready`) is its own explicit requirement.
        self._python_broker = python_broker
        self._python_broker_injected = python_broker is not None
        self._broker_ready = broker_ready
        # model -> LLMProvider. Injected in tests (fake streaming provider); the
        # default is the reused get_provider seam. MP-3B chat driver only.
        self._chat_provider = chat_provider
        # MP-3C3D4C: injectable search/fetch adapters (fakes in tests; the
        # default is the real free DuckDuckGo/guarded-httpx adapters). Deep
        # research driver only.
        self._research_search_adapter = research_search_adapter
        self._research_fetch_adapter = research_fetch_adapter
        # MP-3C3A cooperative shutdown hook. When set and it returns True the
        # worker starts NO new sweeps of new work and an in-flight chat
        # generation aborts at its next guard / idle tick WITHOUT finalising —
        # the run stays `running`, its lease expires, and the next boot's
        # recovery declares it `outcome_unknown` (never a false success).
        self._should_stop: Optional[Callable[[], bool]] = None

    def attach_stop(self, fn: Callable[[], bool]) -> None:
        """Register the supervisor's cooperative stop predicate (idempotent)."""
        self._should_stop = fn

    def _stopping(self) -> bool:
        try:
            return bool(self._should_stop is not None and self._should_stop())
        except Exception:  # noqa: BLE001 — a broken predicate must not wedge the worker
            return False

    #: Bounded reason when a wired broker reports itself not ready.
    _BROKER_NOT_READY = "broker_not_ready"

    def _python_route_refusal(self):
        """None when the real Python route may proceed; else a bounded reason.

        `_python_executor_injected` is the CONSTRUCTION-time test seam only
        (worker_supervisor never supplies it). On every production path real
        execution needs BOTH:
          * a wired broker client, which is the ONLY permitted real executor,
            reporting itself LIVE-ready right now; AND
          * the single policy authority `real_python_execution_available()`
            (explicit operator opt-in + a live, verified executor).

        Injecting a broker is therefore never the authorization, and the
        authority alone never falls back to the in-process host subprocess:
        that launcher is refused unconditionally in app.tools.python_sandbox.
        """
        from app.run_engine import sandbox_policy
        if self._python_broker is None:
            # The construction-time TEST seam is the ONLY exception; on every
            # production path a real execution needs a wired broker.
            return None if self._python_executor_injected \
                else sandbox_policy.REAL_EXECUTION_UNAVAILABLE_REASON
        # A wired broker that is not LIVE-ready is its own bounded refusal, and
        # it is checked even for the test seam (a broker is never assumed up).
        if not (callable(self._broker_ready) and self._broker_ready()):
            return self._BROKER_NOT_READY
        if self._python_executor_injected:
            return None
        if not sandbox_policy.real_python_execution_available():
            return sandbox_policy.REAL_EXECUTION_UNAVAILABLE_REASON
        return None

    def _python_real_route_available(self) -> bool:
        return self._python_route_refusal() is None

    # ---- infra -----------------------------------------------------

    @contextmanager
    def _session(self):
        db = self._sf()
        try:
            yield db
        finally:
            db.close()

    def _mint_lease(self) -> str:
        return f"{self.worker_id}/{uuid4().hex}"

    def _lease_ttl(self) -> _dt.timedelta:
        return _dt.timedelta(seconds=int(settings.run_worker_lease_ttl_s))

    def _recovery_deadline(self, now: _dt.datetime) -> _dt.datetime:
        # Wall-clock bound on total recovery effort: about `max_attempts`
        # handler timeouts. MP-1B has no per-run attempt column (no migration);
        # this is the "bounded attempts" mechanism.
        secs = int(settings.run_worker_handler_timeout_s) * int(settings.run_worker_max_attempts)
        return now + _dt.timedelta(seconds=secs)

    # ---- event allocation (non-committing) ------------------------

    def _alloc_event(self, db, *, run_id, tenant_id, user_id, event_type, data) -> bool:
        """Allocate the next per-run ``run_seq`` and insert one control row, in
        the caller's open transaction. Uses a Core scalar select for the
        high-water mark (an ORM re-query would be stale when called twice in one
        uncommitted transaction). Returns False on a seq race."""
        cur = int(db.execute(select(Run.last_event_seq).where(Run.id == run_id)).scalar() or 0)
        nxt = cur + 1
        rc = db.execute(
            update(Run)
            .where(Run.id == run_id, Run.last_event_seq == cur)
            .values(last_event_seq=nxt, updated_at=self._now())
        ).rowcount
        if rc != 1:
            return False
        db.add(RunEvent(
            id=str(uuid4()), run_id=run_id, run_seq=nxt, tenant_id=tenant_id,
            user_id=user_id, category="control", type=event_type, data=data,
        ))
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return False
        return True

    def _emit_status(self, db, run_id, tenant_id, user_id, frm, to) -> bool:
        return self._alloc_event(
            db, run_id=run_id, tenant_id=tenant_id, user_id=user_id,
            event_type="run.status",
            data=validate_event_data("run.status", {"from_status": frm, "to_status": to}),
        )

    # ---- transitions under lease + version CAS (non-committing) ----

    def _apply_transition(self, db, *, claimed: ClaimedRun, frm: str, to: str,
                          status_reason: Optional[str] = None) -> int:
        """Move ``claimed.run_id`` ``frm -> to`` iff the row still carries this
        worker's ``lease_owner`` and ``version``. Emits one ``run.status``
        event. Returns the new version. Raises :class:`RunLeaseLost` (stale
        token / lost race / advanced run) or :class:`ControlEventContention`
        (seq race). NEVER commits."""
        assert_run_transition(frm, to)
        if status_reason is not None and status_reason not in _ALL_STATUS_REASONS:
            status_reason = "handler_error"  # never store an unbounded reason
        now = self._now()
        vals = {"status": to, "version": Run.version + 1, "updated_at": now}
        if status_reason is not None:
            vals["status_reason"] = status_reason
        if to in RUN_TERMINAL:
            vals["finished_at"] = now
        # a terminal run, or one parked awaiting a human, holds no worker lease
        if to in RUN_TERMINAL or to == WAITING_FOR_APPROVAL:
            vals["lease_owner"] = None
            vals["lease_expires_at"] = None
        rc = db.execute(
            update(Run).where(
                Run.id == claimed.run_id,
                Run.version == claimed.version,
                Run.status == frm,
                Run.lease_owner == claimed.lease_owner,
            ).values(**vals)
        ).rowcount
        if rc != 1:
            raise RunLeaseLost(claimed.run_id)
        if not self._emit_status(db, claimed.run_id, claimed.tenant_id,
                                 claimed.user_id, frm, to):
            raise ControlEventContention()
        return claimed.version + 1

    def _finalize_terminal(self, db, claimed: ClaimedRun, to: str,
                           status_reason: Optional[str], *,
                           frm: str = RUNNING) -> RunOutcome:
        try:
            self._apply_transition(db, claimed=claimed, frm=frm, to=to,
                                   status_reason=status_reason)
        except (RunLeaseLost, ControlEventContention) as exc:
            db.rollback()
            return RunOutcome(claimed.run_id, "abandoned", detail=type(exc).__name__)
        db.commit()
        return RunOutcome(claimed.run_id, to, detail=status_reason)

    # ---- pre-invocation gate ------------------------------------

    def _precheck(self, db, claimed: ClaimedRun) -> None:
        """Stale-worker rejection + fresh session-family revalidation, together.

        Re-reads the run (``expire_all`` first) and raises :class:`RunLeaseLost`
        unless it is still ``running`` under exactly this worker's
        ``lease_owner`` and ``version``. Then re-checks the run's OWN
        refresh-token family against the LIVE database with an
        :class:`AuthContext` built from the run row (never a request) — raising
        SessionBinding* / :class:`RunNotFound` on a revoked / expired / mismatched
        family. Called when handling begins AND again immediately before the
        handler is invoked; the cached validity flag is never trusted."""
        db.expire_all()
        run = db.query(Run).filter(Run.id == claimed.run_id).first()
        if (
            run is None
            or run.status != RUNNING
            or run.lease_owner != claimed.lease_owner
            or run.version != claimed.version
        ):
            raise RunLeaseLost(claimed.run_id)
        wctx = AuthContext(
            user=_SYSTEM_PRINCIPAL,
            user_id=claimed.user_id,
            tenant_id=claimed.tenant_id,
            auth_session_ref=claimed.auth_session_ref,
            durable_session_binding_valid=False,  # never trusted; DB is authority
        )
        validate_session_binding(db, wctx, run=run)

    # ---- cancellation -------------------------------------------

    def _live_cancel_id(self, db, run_id: str):
        return db.execute(
            select(RunSignal.id).where(
                RunSignal.run_id == run_id,
                RunSignal.kind == "cancel_requested",
                RunSignal.consumed_at.is_(None),
            ).limit(1)
        ).scalar()

    def _consume_cancel(self, db, claimed: ClaimedRun, *, frm: str = RUNNING) -> RunOutcome:
        """Consume the live cancel signal EXACTLY once and drive
        ``frm -> cancelling -> cancelled`` — all in one transaction. ``frm`` is
        ``running`` for the MP-1B generic path; the MP-2 MCP driver passes
        ``waiting_for_approval`` when a cancel lands while parked."""
        now = self._now()
        sig_id = self._live_cancel_id(db, claimed.run_id)
        if sig_id is None:
            db.rollback()
            return RunOutcome(claimed.run_id, "abandoned", detail="cancel_gone")
        rc = db.execute(
            update(RunSignal)
            .where(RunSignal.id == sig_id, RunSignal.consumed_at.is_(None))
            .values(consumed_at=now)
        ).rowcount
        if rc != 1:  # someone else consumed it — let them finish the cancel
            db.rollback()
            return RunOutcome(claimed.run_id, "abandoned", detail="cancel_raced")
        try:
            v = self._apply_transition(db, claimed=claimed, frm=frm, to=CANCELLING)
            self._apply_transition(db, claimed=replace(claimed, version=v),
                                   frm=CANCELLING, to=CANCELLED, status_reason="cancelled")
        except (RunLeaseLost, ControlEventContention) as exc:
            db.rollback()
            return RunOutcome(claimed.run_id, "abandoned", detail=type(exc).__name__)
        db.commit()
        return RunOutcome(claimed.run_id, CANCELLED, detail="cancelled")

    # ---- claim ------------------------------------------------

    def claim_next_queued(self) -> Optional[ClaimedRun]:
        """Atomically take the oldest claimable ``queued`` run. Returns a
        :class:`ClaimedRun` (run now ``running`` under this worker's lease) or
        ``None`` when there is nothing to claim / the race was lost."""
        if self._stopping():
            return None                       # MP-3C3A — accept no new work while stopping
        now = self._now()
        with self._session() as db:
            row = db.execute(
                select(Run.id, Run.version, Run.kind, Run.user_id, Run.tenant_id,
                       Run.auth_session_ref)
                .where(Run.status == QUEUED)
                .where(Run.kind != _MCP_RUN_KIND)    # MP-2: MCP runs use advance_mcp_runs()
                .where(Run.kind != _CHAT_RUN_KIND)   # MP-3C1: chat runs are ALWAYS worker-reserved
                .where(Run.kind != _DEEP_RESEARCH_RUN_KIND)  # MP-3C3D4C: reserved for advance_deep_research_runs()
                .where(Run.kind != _MULTI_AGENT_RUN_KIND)  # MP-3C3D5B: reserved — no sweep exists yet
                .where(or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now))
                .order_by(Run.created_at.asc())
                .limit(1)
            ).first()
            if row is None:
                return None
            assert_run_transition(QUEUED, RUNNING)  # pure guard
            lease = self._mint_lease()
            rc = db.execute(
                update(Run).where(
                    Run.id == row.id,
                    Run.version == row.version,
                    Run.status == QUEUED,
                    or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now),
                ).values(
                    status=RUNNING,
                    version=Run.version + 1,
                    lease_owner=lease,
                    lease_expires_at=now + self._lease_ttl(),
                    run_deadline_at=func.coalesce(Run.run_deadline_at,
                                                  self._recovery_deadline(now)),
                    started_at=func.coalesce(Run.started_at, now),
                    updated_at=now,
                )
            ).rowcount
            if rc != 1:  # lost the race to another claimer
                db.rollback()
                return None
            if not self._emit_status(db, row.id, row.tenant_id, row.user_id,
                                     QUEUED, RUNNING):
                db.rollback()
                return None
            db.commit()
            fresh = db.execute(
                select(Run.version, Run.lease_owner).where(Run.id == row.id)
            ).first()
            return ClaimedRun(
                run_id=row.id, kind=row.kind, user_id=row.user_id,
                tenant_id=row.tenant_id, auth_session_ref=row.auth_session_ref,
                lease_owner=fresh.lease_owner, version=fresh.version, attempt=0,
            )

    def renew_lease(self, run_id: str, lease_owner: str) -> bool:
        """Extend a non-terminal run's lease — only for the current holder.
        Returns True on success, False for a stale / wrong ``lease_owner`` or a
        terminal run."""
        now = self._now()
        with self._session() as db:
            rc = db.execute(
                update(Run).where(
                    Run.id == run_id,
                    Run.lease_owner == lease_owner,
                    Run.status.notin_(tuple(RUN_TERMINAL)),
                ).values(lease_expires_at=now + self._lease_ttl(), updated_at=now)
            ).rowcount
            if rc == 1:
                db.commit()
                return True
            db.rollback()
            return False

    # ---- execute --------------------------------------------

    def execute(self, claimed: ClaimedRun) -> RunOutcome:
        """Advance a claimed (``running``) run to a terminal state: session
        revalidation → cancel check → handler resolve → session revalidation
        again → invoke → terminal."""
        with self._session() as db:
            # A — stale-worker + fresh family check when handling begins
            try:
                self._precheck(db, claimed)
            except RunLeaseLost:
                return RunOutcome(claimed.run_id, "abandoned", detail="RunLeaseLost")
            except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                return self._finalize_terminal(db, claimed, FAILED, "session_revoked")

            # B — cancel already requested: consume once, cancel wins
            if self._live_cancel_id(db, claimed.run_id) is not None:
                return self._consume_cancel(db, claimed)

            # C — resolve the executor; unknown kind fails closed
            spec = resolve_handler(claimed.kind)
            if spec is None:
                return self._finalize_terminal(db, claimed, FAILED, "unknown_handler")

            # D — stale-worker + fresh family check AGAIN, immediately pre-invocation
            try:
                self._precheck(db, claimed)
            except RunLeaseLost:
                return RunOutcome(claimed.run_id, "abandoned", detail="RunLeaseLost")
            except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                return self._finalize_terminal(db, claimed, FAILED, "session_revoked")

            # E — invoke (no db, no token, no prompt handed to the handler)
            start = self._now()
            try:
                result = spec.fn(HandlerContext(
                    run_id=claimed.run_id, kind=claimed.kind, user_id=claimed.user_id,
                    tenant_id=claimed.tenant_id, attempt=claimed.attempt,
                ))
            except Exception:  # noqa: BLE001 — bounded code only, never the text
                return self._finalize_terminal(db, claimed, FAILED, "handler_error")

            elapsed = (self._now() - start).total_seconds()
            if elapsed > int(settings.run_worker_handler_timeout_s):
                return self._finalize_terminal(db, claimed, TIMED_OUT, "handler_timeout")

            if not isinstance(result, HandlerResult) or not result.ok:
                code = getattr(result, "error_code", None)
                code = code if code in _HANDLER_ERROR_CODES else "handler_error"
                return self._finalize_terminal(db, claimed, FAILED, code)

            # F — a cancel that landed during a side-effect-free handler still wins
            if self._live_cancel_id(db, claimed.run_id) is not None:
                return self._consume_cancel(db, claimed)

            return self._finalize_terminal(db, claimed, SUCCEEDED, None)

    # ---- recovery ------------------------------------------

    def recover_expired(self) -> list[RunOutcome]:
        """Sweep expired-lease ``running`` runs. Retry-safe & within deadline →
        reclaim + re-run; everything else → ``outcome_unknown``. A fresh
        ``RunWorker`` calling this IS a restart: only retry-safe kinds resume."""
        now = self._now()
        with self._session() as db:
            rows = db.execute(
                select(Run.id, Run.version, Run.kind, Run.user_id, Run.tenant_id,
                       Run.auth_session_ref, Run.run_deadline_at)
                .where(Run.status == RUNNING)
                .where(Run.kind != _MCP_RUN_KIND)    # MP-2 MCP runs recover via advance_mcp_runs()
                .where(Run.kind != _CHAT_RUN_KIND)   # MP-3C1 chat runs recover via advance_chat_runs()
                .where(Run.kind != _DEEP_RESEARCH_RUN_KIND)  # MP-3C3D4C: recovers via advance_deep_research_runs()
                .where(Run.kind != _MULTI_AGENT_RUN_KIND)  # MP-3C3D5B: reserved — no sweep exists yet
                .where(Run.lease_expires_at.isnot(None))
                .where(Run.lease_expires_at < now)
                .order_by(Run.lease_expires_at.asc())
                .limit(int(settings.run_worker_recover_batch))
            ).all()
        return [self._recover_one(r) for r in rows]

    def _recover_one(self, r) -> RunOutcome:
        spec = resolve_handler(r.kind)
        now = self._now()
        if spec is None or not spec.retry_safe:
            return self._force_terminal_expired(r, OUTCOME_UNKNOWN, "outcome_unknown")
        deadline = _aware(r.run_deadline_at)
        if deadline is not None and now >= deadline:
            return self._force_terminal_expired(r, FAILED, "deadline_exceeded")

        with self._session() as db:
            lease = self._mint_lease()
            rc = db.execute(
                update(Run).where(
                    Run.id == r.id,
                    Run.version == r.version,
                    Run.status == RUNNING,
                    Run.lease_expires_at.isnot(None),
                    Run.lease_expires_at < now,
                ).values(lease_owner=lease, lease_expires_at=now + self._lease_ttl(),
                         version=Run.version + 1, updated_at=now)
            ).rowcount
            if rc != 1:
                db.rollback()
                return RunOutcome(r.id, "abandoned", detail="reclaim_raced")
            db.commit()
            fresh = db.execute(
                select(Run.version, Run.lease_owner).where(Run.id == r.id)
            ).first()

        return self.execute(ClaimedRun(
            run_id=r.id, kind=r.kind, user_id=r.user_id, tenant_id=r.tenant_id,
            auth_session_ref=r.auth_session_ref, lease_owner=fresh.lease_owner,
            version=fresh.version, attempt=1,
        ))

    def _force_terminal_expired(self, r, to: str, status_reason: str) -> RunOutcome:
        """Terminal move for a run whose lease we do NOT hold (it expired). CAS
        is ``version`` + ``status == running`` + still-expired, not lease_owner."""
        now = self._now()
        assert_run_transition(RUNNING, to)
        with self._session() as db:
            rc = db.execute(
                update(Run).where(
                    Run.id == r.id,
                    Run.version == r.version,
                    Run.status == RUNNING,
                    Run.lease_expires_at.isnot(None),
                    Run.lease_expires_at < now,
                ).values(status=to, status_reason=status_reason,
                         version=Run.version + 1, finished_at=now,
                         lease_owner=None, lease_expires_at=None, updated_at=now)
            ).rowcount
            if rc != 1:
                db.rollback()
                return RunOutcome(r.id, "abandoned", detail="recover_raced")
            if not self._emit_status(db, r.id, r.tenant_id, r.user_id, RUNNING, to):
                db.rollback()
                return RunOutcome(r.id, "abandoned", detail="seq_contention")
            db.commit()
        return RunOutcome(r.id, to, detail=status_reason)

    # ---- MP-2: durable one-time MCP approval resume ----------------

    def _mcp_ctx(self, claimed: ClaimedRun) -> AuthContext:
        return AuthContext(
            user=_SYSTEM_PRINCIPAL, user_id=claimed.user_id,
            tenant_id=claimed.tenant_id, auth_session_ref=claimed.auth_session_ref,
            durable_session_binding_valid=False,
        )

    def _mcp_session_check(self, db, claimed: ClaimedRun) -> None:
        """Fresh DB refresh-family validation for the run's OWN binding (context
        from the run row, never a request). Raises SessionBinding* / RunNotFound.
        Strict origin-session binding is enforced by validate_session_binding."""
        db.expire_all()
        run = db.query(Run).filter(Run.id == claimed.run_id).first()
        validate_session_binding(db, self._mcp_ctx(claimed), run=run)

    def advance_mcp_runs(self) -> list[RunOutcome]:
        """Drive every advanceable durable ``mcp_call`` run one step: first
        gateway pass (create the proposal, park at ``waiting_for_approval``); on
        a live ``approval_decided`` signal, one-time consume + at-most-once
        dispatch; or a crash-safe recovery. Bounded per sweep by
        ``run_worker_recover_batch``."""
        if self._stopping():
            return []                         # MP-3C3A — no new proposal / dispatch while stopping
        now = self._now()
        with self._session() as db:
            ids = db.execute(
                select(Run.id)
                .where(Run.kind == _MCP_RUN_KIND)
                .where(Run.status.in_(tuple(_MCP_ADVANCEABLE)))
                .where(or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now))
                .order_by(Run.created_at.asc())
                .limit(int(settings.run_worker_recover_batch))
            ).scalars().all()
        return [self._advance_mcp_run(rid) for rid in ids]

    def _advance_mcp_run(self, run_id: str) -> RunOutcome:
        from app.approval import gateway
        from app.run_engine import mcp_flow

        now = self._now()
        with self._session() as db:
            row = db.execute(
                select(Run.id, Run.version, Run.status, Run.kind, Run.user_id,
                       Run.tenant_id, Run.session_id, Run.auth_session_ref)
                .where(Run.id == run_id)
            ).first()
            if row is None or row.kind != _MCP_RUN_KIND:
                return RunOutcome(run_id, "none", "not_mcp")
            if row.status in RUN_TERMINAL:
                return RunOutcome(run_id, row.status, "already_terminal")
            if row.status not in _MCP_ADVANCEABLE:
                return RunOutcome(run_id, "none", row.status)

            lease = self._mint_lease()
            rc = db.execute(
                update(Run).where(
                    Run.id == run_id, Run.version == row.version,
                    Run.status == row.status,
                    or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now),
                ).values(lease_owner=lease, lease_expires_at=now + self._lease_ttl(),
                         version=Run.version + 1,
                         started_at=func.coalesce(Run.started_at, now), updated_at=now)
            ).rowcount
            if rc != 1:
                db.rollback()
                return RunOutcome(run_id, "abandoned", "lease_race")
            db.commit()

            claimed = ClaimedRun(
                run_id=run_id, kind=row.kind, user_id=row.user_id,
                tenant_id=row.tenant_id, auth_session_ref=row.auth_session_ref,
                lease_owner=lease, version=row.version + 1, attempt=0,
            )
            status, session_id = row.status, row.session_id
            step = (
                db.query(RunStep)
                .filter(RunStep.run_id == run_id, RunStep.kind == "tool_dispatch")
                .order_by(RunStep.seq.asc()).first()
            )
            if step is None:
                return self._finalize_terminal(db, claimed, FAILED, "no_step", frm=status)

            if status == QUEUED or (status == RUNNING and not step.approval_id):
                return self._mcp_first_pass(db, gateway, mcp_flow, claimed, step,
                                            session_id, frm=status)
            if status == WAITING_FOR_APPROVAL:
                return self._mcp_on_decision(db, gateway, mcp_flow, claimed, step,
                                             session_id)
            # APPROVED / RESUMING / RUNNING-with-approval -> normalize -> dispatch
            if status in (APPROVED, RESUMING):
                try:
                    self._mcp_session_check(db, claimed)
                except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                    return self._finalize_terminal(db, claimed, FAILED,
                                                   "session_revoked", frm=status)
                try:
                    v = claimed.version
                    if status == APPROVED:
                        v = self._apply_transition(db, claimed=replace(claimed, version=v),
                                                   frm=APPROVED, to=RESUMING)
                        status = RESUMING
                    v = self._apply_transition(db, claimed=replace(claimed, version=v),
                                               frm=RESUMING, to=RUNNING)
                except (RunLeaseLost, ControlEventContention) as exc:
                    db.rollback()
                    return RunOutcome(run_id, "abandoned", type(exc).__name__)
                db.commit()
                claimed = replace(claimed, version=v)
            return self._mcp_dispatch(db, gateway, mcp_flow, claimed, step, session_id)

    def _mcp_first_pass(self, db, gateway, mcp_flow, claimed, step, session_id,
                        *, frm) -> RunOutcome:
        try:
            self._mcp_session_check(db, claimed)
        except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=frm)
        if self._live_cancel_id(db, claimed.run_id) is not None:
            return self._consume_cancel(db, claimed, frm=frm)

        v = claimed.version
        if frm == QUEUED:
            try:
                v = self._apply_transition(db, claimed=replace(claimed, version=v),
                                           frm=QUEUED, to=RUNNING)
            except (RunLeaseLost, ControlEventContention) as exc:
                db.rollback()
                return RunOutcome(claimed.run_id, "abandoned", type(exc).__name__)
        claimed = replace(claimed, version=v)
        db.commit()

        run_row = db.query(Run).filter(Run.id == claimed.run_id).first()
        try:
            args = mcp_flow.open_mcp_args(db, run_row, step)
        except Exception:  # noqa: BLE001 — sealed-args unreadable; bounded code only
            return self._finalize_terminal(db, claimed, FAILED, "gateway_error", frm=RUNNING)
        tool_id = mcp_flow.step_tool_id(step) or ""
        gctx = gateway.GatewayContext(
            db=db, user_id=claimed.user_id, tenant_id=claimed.tenant_id,
            session_id=session_id, run_id=claimed.run_id,
            execution_turn_id=step.execution_turn_id, tool_call_id=step.tool_call_id,
        )

        def _forbidden():  # first pass MUST NOT execute the tool
            raise AssertionError("gateway invoked execute_mcp on the proposal pass")

        try:
            res = gateway.dispatch(gctx, mcp_name=tool_id, args=args,
                                   execute_mcp=_forbidden, clock=None)
        except AssertionError:
            raise
        except Exception:  # noqa: BLE001
            return self._finalize_terminal(db, claimed, FAILED, "gateway_error", frm=RUNNING)

        if res.status == "approval_required":
            # Bind the run to this approval ONCE. The pointer is the run BOUND
            # APPROVAL IDENTITY and is intentionally retained after the decision
            # (see models.Run.pending_approval_id): guard on run.status / the
            # approval state, never on "pending_approval_id is not None".
            db.execute(update(Run).where(Run.id == claimed.run_id)
                       .values(pending_approval_id=res.approval_id))
            db.execute(update(RunStep).where(RunStep.id == step.id)
                       .values(approval_id=res.approval_id, status="waiting"))
            try:
                self._apply_transition(db, claimed=claimed, frm=RUNNING,
                                       to=WAITING_FOR_APPROVAL)
            except (RunLeaseLost, ControlEventContention) as exc:
                db.rollback()
                return RunOutcome(claimed.run_id, "abandoned", type(exc).__name__)
            db.commit()
            return RunOutcome(claimed.run_id, WAITING_FOR_APPROVAL, res.approval_id)

        if res.status == "blocked":
            code = res.reason_code if res.reason_code in MCP_STATUS_REASONS else "gateway_error"
            db.execute(update(RunStep).where(RunStep.id == step.id)
                       .values(status="blocked", error_code=(res.reason_code or "gateway_error")[:48]))
            return self._finalize_terminal(db, claimed, FAILED, code, frm=RUNNING)

        if res.status == "executed":  # a matching session grant (not expected in MP-2)
            return self._mcp_finalize(db, mcp_flow, claimed, step, res)
        return self._finalize_terminal(db, claimed, FAILED, "gateway_error", frm=RUNNING)

    def _mcp_on_decision(self, db, gateway, mcp_flow, claimed, step, session_id) -> RunOutcome:
        sig = db.execute(
            select(RunSignal.id, RunSignal.payload).where(
                RunSignal.run_id == claimed.run_id,
                RunSignal.kind == "approval_decided",
                RunSignal.consumed_at.is_(None),
            ).limit(1)
        ).first()
        if sig is None:  # nothing to act on — drop the lease, stay parked
            # MP-4C — the session and cancellation guards stay effective on this
            # path too, and an expired waiting approval is reconciled here (this
            # is the only place the sweep visits a waiting Run with no decision).
            try:
                self._mcp_session_check(db, claimed)
            except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                return self._finalize_terminal(db, claimed, FAILED, "session_revoked",
                                               frm=WAITING_FOR_APPROVAL)
            if self._live_cancel_id(db, claimed.run_id) is not None:
                return self._consume_cancel(db, claimed, frm=WAITING_FOR_APPROVAL)
            # MP-4C — shared expired-approval reconciliation (MCP and Python
            # alike). Cancellation has ALREADY won above; the dispatch ledger is
            # consulted BEFORE terminalization so an at-most-once outcome is
            # never overwritten. ONE existing expiry authority; no new Run status.
            try:
                approval_id = db.execute(
                    select(Run.pending_approval_id).where(Run.id == claimed.run_id)
                ).scalar()
            except Exception:  # noqa: BLE001
                approval_id = None
            if approval_id:
                from app.approval.state_machine import EXPIRED as _APPR_EXPIRED
                from app.approval.state_machine import expire_if_due as _expire_if_due
                from app.models.models import PendingApproval as _PendingApproval

                rec = (db.query(DispatchRecord)
                       .filter(DispatchRecord.tool_call_id == step.tool_call_id)
                       .one_or_none())
                if rec is None:
                    try:
                        _expire_if_due(db, approval_id)
                        appr = (db.query(_PendingApproval)
                                .filter(_PendingApproval.id == approval_id).one_or_none())
                    except Exception:  # noqa: BLE001
                        appr = None
                    if appr is not None and appr.state == _APPR_EXPIRED:
                        db.execute(update(RunStep).where(RunStep.id == step.id).values(
                            status="blocked", error_code="approval_expired",
                            finished_at=self._now()))
                        return self._finalize_terminal(db, claimed, FAILED,
                                                       "approval_expired",
                                                       frm=WAITING_FOR_APPROVAL)
            db.execute(update(Run).where(Run.id == claimed.run_id,
                                         Run.lease_owner == claimed.lease_owner)
                       .values(lease_owner=None, lease_expires_at=None,
                               updated_at=self._now()))
            db.commit()
            return RunOutcome(claimed.run_id, WAITING_FOR_APPROVAL, "no_signal")

        try:  # invariant 5 — fresh family check BEFORE consuming the signal
            self._mcp_session_check(db, claimed)
        except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked",
                                           frm=WAITING_FOR_APPROVAL)
        if self._live_cancel_id(db, claimed.run_id) is not None:  # invariant 6
            return self._consume_cancel(db, claimed, frm=WAITING_FOR_APPROVAL)

        payload = None
        try:
            payload = parse_signal_payload("approval_decided", sig.payload)
            decision = payload["decision"]
        except Exception:  # noqa: BLE001 — unparseable -> fail closed
            decision = "denied"

        # MP-4C-R2B-2B-guard: the delivered decision must name the SAME approval
        # bound to this Run AND to its tool_dispatch step. Without this check the
        # signal's approval_id is decorative — R2B-2A-T3 demonstrated that a
        # signal carrying a DIFFERENT (genuine) approval id still authorized
        # dispatch, and that changing only RunStep.approval_id still executed.
        # A mismatch is handled EXACTLY like the existing unparseable-payload
        # case: it routes down the established bounded denied terminal, so the
        # signal is consumed once (no retry loop), the step is blocked, the run
        # is terminal, and there are zero callback calls and zero started
        # DispatchRecords. No new state or reason slug is introduced.
        if payload is not None and decision != "denied":
            run_pending = db.execute(
                select(Run.pending_approval_id).where(Run.id == claimed.run_id)
            ).scalar()
            sig_approval = payload.get("approval_id")
            if (not sig_approval or sig_approval != step.approval_id
                    or run_pending != step.approval_id):
                logger.warning("approval identity mismatch; failing closed")
                decision = "denied"

        now = self._now()
        rc = db.execute(update(RunSignal).where(RunSignal.id == sig.id,
                                                RunSignal.consumed_at.is_(None))
                        .values(consumed_at=now)).rowcount
        if rc != 1:
            db.rollback()
            return RunOutcome(claimed.run_id, "abandoned", "signal_raced")

        if decision == "denied":
            db.execute(update(RunStep).where(RunStep.id == step.id)
                       .values(status="blocked", error_code="approval_denied", finished_at=now))
            try:
                self._apply_transition(db, claimed=claimed, frm=WAITING_FOR_APPROVAL, to=DENIED)
            except (RunLeaseLost, ControlEventContention) as exc:
                db.rollback()
                return RunOutcome(claimed.run_id, "abandoned", type(exc).__name__)
            db.commit()
            return RunOutcome(claimed.run_id, DENIED, "approval_denied")

        try:  # invariant 5 — fresh family check BEFORE transitioning to resuming/running
            self._mcp_session_check(db, claimed)
        except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
            db.rollback()  # un-consume the signal; the decision stays valid
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked",
                                           frm=WAITING_FOR_APPROVAL)
        try:  # signal-consume + these 3 transitions commit together (atomic)
            v = self._apply_transition(db, claimed=claimed, frm=WAITING_FOR_APPROVAL, to=APPROVED)
            v = self._apply_transition(db, claimed=replace(claimed, version=v),
                                       frm=APPROVED, to=RESUMING)
            v = self._apply_transition(db, claimed=replace(claimed, version=v),
                                       frm=RESUMING, to=RUNNING)
        except (RunLeaseLost, ControlEventContention) as exc:
            db.rollback()
            return RunOutcome(claimed.run_id, "abandoned", type(exc).__name__)
        db.commit()
        return self._mcp_dispatch(db, gateway, mcp_flow, replace(claimed, version=v),
                                  step, session_id)

    def _mcp_dispatch(self, db, gateway, mcp_flow, claimed, step, session_id) -> RunOutcome:
        """Run is ``running`` under our lease. At-most-once external dispatch via
        the gateway, gated by the ``dispatch_records`` crash ledger."""
        db.expire_all()
        rec = (db.query(DispatchRecord)
               .filter(DispatchRecord.tool_call_id == step.tool_call_id).one_or_none())
        if rec is not None:  # restart safety — the ledger is the at-most-once authority
            if rec.outcome == "succeeded":
                db.execute(update(RunStep).where(RunStep.id == step.id).values(status="succeeded"))
                return self._finalize_terminal(db, claimed, SUCCEEDED, None, frm=RUNNING)
            if rec.outcome == "failed":
                db.execute(update(RunStep).where(RunStep.id == step.id)
                           .values(status="failed", error_code="tool_errored"))
                return self._finalize_terminal(db, claimed, FAILED, "tool_errored", frm=RUNNING)
            db.execute(update(RunStep).where(RunStep.id == step.id)
                       .values(status="incomplete", error_code="dispatch_incomplete"))
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN, "outcome_unknown",
                                           frm=RUNNING)

        try:  # invariant 5 — immediately before the external invocation
            self._mcp_session_check(db, claimed)
        except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
        if self._live_cancel_id(db, claimed.run_id) is not None:  # invariant 6 — cancel wins
            return self._consume_cancel(db, claimed, frm=RUNNING)

        run_row = db.query(Run).filter(Run.id == claimed.run_id).first()
        try:
            args = mcp_flow.open_mcp_args(db, run_row, step)
        except Exception:  # noqa: BLE001
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN, "gateway_error",
                                           frm=RUNNING)
        tool_id = mcp_flow.step_tool_id(step) or ""
        gctx = gateway.GatewayContext(
            db=db, user_id=claimed.user_id, tenant_id=claimed.tenant_id,
            session_id=session_id, run_id=claimed.run_id,
            execution_turn_id=step.execution_turn_id, tool_call_id=step.tool_call_id,
        )
        transport = self._mcp_transport or mcp_flow.default_mcp_transport
        invoked = {"n": 0}
        # The PRE-DISPATCH capacity credential, when a broker client is wired.
        reservation_token = None

        # MP-4C-R2B-2C-2A — the reserved canonical Python identity NEVER uses the
        # MCP transport. It routes to the Python executor only when the server
        # setting is enabled AND the durable step carries the verified
        # server-owned Python origin; ANY mismatch fails closed HERE, before the
        # gateway is asked to dispatch (so no DispatchRecord.started is written).
        _ref = {}
        try:
            _ref = json.loads(step.input_ref or "{}")
        except Exception:  # noqa: BLE001
            _ref = {}
        if tool_id == "python_sandbox":
            from app.approval.hashing import args_hash as _args_hash
            from app.models.models import ToolCall as _ToolCall
            from app.tools import python_sandbox as _ps

            def _python_rejected():
                db.execute(update(RunStep).where(RunStep.id == step.id).values(
                    status="blocked", error_code="gateway_error",
                    finished_at=self._now()))
                return self._finalize_terminal(db, claimed, FAILED, "gateway_error",
                                               frm=RUNNING)

            # MP-4C policy: REAL execution requires the isolated executor broker.
            # Refuse HERE, before gateway.dispatch, so no DispatchRecord.started
            # is ever written and an already-waiting approval can never execute.
            from app.run_engine import sandbox_policy  # noqa: F401 (canonical reason)
            _refusal = self._python_route_refusal()
            if _refusal is not None:
                db.execute(update(RunStep).where(RunStep.id == step.id).values(
                    status="blocked", error_code=_refusal,
                    finished_at=self._now()))
                return self._finalize_terminal(db, claimed, FAILED, _refusal,
                                               frm=RUNNING)

            if not (getattr(settings, "run_engine_sandbox", False)
                    and _ref.get("purpose") == "python_execution"):
                return _python_rejected()          # wrong origin / flag off
            if (not isinstance(args, dict) or set(args) != {"code"}
                    or not isinstance(args.get("code"), str)
                    or not args["code"].strip()):
                return _python_rejected()          # not exactly {"code": str}
            if len(args["code"].encode("utf-8")) > _ps.max_code_bytes():
                return _python_rejected()          # byte bound
            _a = _args_hash(args)
            _tc = db.query(_ToolCall).filter(_ToolCall.id == step.tool_call_id).first()
            if _a != _ref.get("args_hash") or _tc is None or _a != _tc.args_hash:
                return _python_rejected()          # canonical hash binding

            if self._python_broker is not None:
                # `_python_route_refusal()` above ALREADY verified that this wired
                # broker reports itself live-ready (and produced the bounded
                # `broker_not_ready` refusal otherwise), so there is deliberately
                # no second readiness re-check here: one explicit round-trip plus
                # the single authority's own live probe. A broker client is still
                # NOT the authorization - it is only the transport.
                #
                # RESERVE BEFORE the gateway: a busy broker must produce a bounded
                # refusal with ZERO DispatchRecord.started rows and ZERO calls.
                from app.run_engine.executor_grant import derive_reservation_grant
                try:
                    reservation_token = derive_reservation_grant(
                        db, run_id=run_row.id, step_id=step.id,
                        tool_call_id=step.tool_call_id, code=args["code"],
                        tenant_id=claimed.tenant_id)
                except Exception:  # noqa: BLE001 — no credential => no reservation
                    return _python_rejected()
                try:
                    reserved = self._python_broker.reserve(grant=reservation_token)
                except Exception:  # noqa: BLE001
                    return _python_rejected()
                if not getattr(reserved, "ok", False):
                    code = str(getattr(reserved, "status", "reserve_refused"))[:48]
                    db.execute(update(RunStep).where(RunStep.id == step.id).values(
                        status="blocked", error_code=code,
                        finished_at=self._now()))
                    # Report the bounded RESERVATION reason when it is an allowed
                    # status reason (e.g. executor_quota_exceeded). This changes
                    # ONLY status_reason: no new Run state, and a slug outside the
                    # allowlist still degrades to gateway_error.
                    reason = code if code in _ALL_STATUS_REASONS else "gateway_error"
                    return self._finalize_terminal(db, claimed, FAILED, reason,
                                                   frm=RUNNING)

            def _execute():
                invoked["n"] += 1
                if self._python_broker is not None:
                    # Runs ONLY after the gateway committed DispatchRecord.started,
                    # so the execution grant is derived from the committed rows and
                    # the broker consumes the SAME reservation before one launch.
                    from app.run_engine.executor_grant import derive_executor_grant
                    db.expire_all()
                    try:
                        gtoken = derive_executor_grant(
                            db, run_id=run_row.id, step_id=step.id,
                            tool_call_id=step.tool_call_id, code=args["code"],
                            tenant_id=claimed.tenant_id)
                    except Exception:  # noqa: BLE001
                        return ("python execution grant unavailable", True)
                    result = self._python_broker.execute(
                        grant=gtoken, code=args["code"], run_id=run_row.id)
                    return (getattr(result, "output", "") or "",
                            not getattr(result, "ok", False))
                # Defence in depth: the in-process host subprocess is NEVER a
                # fallback. With no broker only the construction-time test seam
                # may supply an executor; production refuses here.
                if self._python_executor is None:
                    return ("[Python Sandbox] Unavailable: real execution "
                            "requires the isolated executor route. (%s)"
                            % _ps.RESULT_EXECUTION_UNAVAILABLE, True)
                outcome = self._python_executor(args["code"])
                return (outcome.text, bool(outcome.is_error))
        else:
            def _execute():
                invoked["n"] += 1
                return transport(db, claimed.user_id, tool_id, args)

        try:
            res = gateway.dispatch(gctx, mcp_name=tool_id, args=args,
                                   execute_mcp=_execute, clock=None)
        except Exception:  # noqa: BLE001 — gateway failure; ledger already committed 'started'?
            self._release_reservation(reservation_token)
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN, "gateway_error",
                                           frm=RUNNING)
        if reservation_token is not None and res.status != "executed":
            # Authorization refused, duplicate dispatch or an incomplete ledger row:
            # this run will not use the capacity, so return it (cancel included).
            self._release_reservation(reservation_token)
        return self._mcp_finalize(db, mcp_flow, claimed, step, res)

    def _release_reservation(self, token) -> None:
        """Return capacity this run will not use. Never raises; never re-executes."""
        if token is None or self._python_broker is None:
            return
        try:
            self._python_broker.release(grant=token)
        except Exception:  # noqa: BLE001 — a failed release is expired, not fatal
            pass

    def _mcp_finalize(self, db, mcp_flow, claimed, step, res) -> RunOutcome:
        db.expire_all()
        if res.status == "blocked":
            code = res.reason_code
            if code == "dispatch_incomplete":
                db.execute(update(RunStep).where(RunStep.id == step.id)
                           .values(status="incomplete", error_code=code))
                return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                               "outcome_unknown", frm=RUNNING)
            if code == "duplicate_dispatch":
                rec = (db.query(DispatchRecord)
                       .filter(DispatchRecord.tool_call_id == step.tool_call_id).one_or_none())
                if rec is not None and rec.outcome == "succeeded" and step.output_payload_id:
                    db.execute(update(RunStep).where(RunStep.id == step.id).values(status="succeeded"))
                    if claimed.kind == _CHAT_RUN_KIND:
                        # the MCP call already ran once and its result is sealed —
                        # do NOT re-execute; resume generation from the durable result.
                        db.commit()
                        return self._chat_continue_after_tool(db, claimed, step)
                    return self._finalize_terminal(db, claimed, SUCCEEDED, None, frm=RUNNING)
                db.execute(update(RunStep).where(RunStep.id == step.id)
                           .values(status="incomplete", error_code=code))
                return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                               "outcome_unknown", frm=RUNNING)
            reason = code if code in MCP_STATUS_REASONS else "gateway_error"
            db.execute(update(RunStep).where(RunStep.id == step.id)
                       .values(status="blocked", error_code=(code or "gateway_error")[:48]))
            return self._finalize_terminal(db, claimed, FAILED, reason, frm=RUNNING)

        if res.status == "executed":
            out_text = res.tool_output or ""
            is_error = bool(res.is_error)
            run_row = db.query(Run).filter(Run.id == claimed.run_id).first()
            try:  # seal the result BEFORE the run's terminal transition
                opid = seal_payload(
                    db, tenant_id=run_row.tenant_id, user_id=run_row.user_id,
                    run_id=run_row.id, step_id=step.id, payload_type="tool_output",
                    plaintext=out_text, commit=False,
                ).id
            except Exception:  # noqa: BLE001 — persistence failed; NEVER report success
                db.rollback()
                return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                               "output_persist_failed", frm=RUNNING)
            db.execute(update(RunStep).where(RunStep.id == step.id).values(
                output_payload_id=opid,
                status=("failed" if is_error else "succeeded"),
                error_code=("tool_errored" if is_error else None),
                finished_at=self._now(),
            ))
            if claimed.kind == _CHAT_RUN_KIND:
                # MP-3C2: the MCP call executed at-most-once and the result is
                # sealed. Do NOT terminate — resume the SAME durable chat run's
                # generation with the sealed tool result (a *known* tool error
                # is fed back too, as a delimited data envelope).
                db.commit()
                return self._chat_continue_after_tool(db, claimed, step)
            to = FAILED if is_error else SUCCEEDED
            return self._finalize_terminal(db, claimed, to,
                                           ("tool_errored" if is_error else None), frm=RUNNING)

        return self._finalize_terminal(db, claimed, FAILED, "gateway_error", frm=RUNNING)

    # ---- MP-3B / MP-3C2: worker-backed durable chat generation + tool resume --

    def advance_chat_runs(self) -> list["RunOutcome"]:
        """Drive claimable durable ``chat_stream`` runs one step.

        A fresh ``queued`` run is claimed and generated in this sweep; a
        generation segment that emits a COMPLETE ``[MCP_CALL]`` marker seals its
        clean text, opens a ``tool_dispatch`` step, and parks the SAME run at
        ``waiting_for_approval`` (lease dropped). A later sweep consumes the
        durable ``approval_decided`` signal, dispatches the MCP call at-most-once
        through the MP-2 gateway, seals the result, and resumes generation
        (up to ``run_chat_max_tool_cycles`` cycles). An expired-lease run is
        reclaimed ONLY where durable state proves provider invocation / dispatch
        had not begun — otherwise ``outcome_unknown`` with NO second side effect.
        No-op unless BOTH ``run_engine_enabled`` and ``run_chat_stream_enabled``."""
        if not (settings.run_engine_enabled and settings.run_chat_stream_enabled):
            return []
        if self._stopping():
            return []                         # MP-3C3A — start / resume no generation while stopping
        now = self._now()
        with self._session() as db:
            ids = db.execute(
                select(Run.id)
                .where(Run.kind == _CHAT_RUN_KIND)
                .where(Run.status.in_(tuple(_CHAT_ADVANCEABLE)))
                .where(or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now))
                .order_by(Run.created_at.asc())
                .limit(int(settings.run_worker_recover_batch))
            ).scalars().all()
        return [self._advance_chat_run(rid) for rid in ids]

    def _advance_chat_run(self, run_id: str) -> "RunOutcome":
        from app.run_engine import chat_stream
        from app.run_engine import chat_tools

        now = self._now()
        with self._session() as db:
            row = db.execute(
                select(Run.id, Run.version, Run.status, Run.kind, Run.user_id,
                       Run.tenant_id, Run.session_id, Run.auth_session_ref,
                       Run.lease_expires_at)
                .where(Run.id == run_id)
            ).first()
            if row is None or row.kind != _CHAT_RUN_KIND:
                return RunOutcome(run_id, "none", "not_chat")
            if row.status in RUN_TERMINAL:
                return RunOutcome(run_id, row.status, "already_terminal")
            if row.status not in _CHAT_ADVANCEABLE:
                return RunOutcome(run_id, "none", row.status)

            # a parked / decided / resuming chat run is a durable tool cycle
            if row.status in (WAITING_FOR_APPROVAL, APPROVED, RESUMING):
                return self._advance_chat_tool_run(run_id)

            steps = (db.query(RunStep).filter(RunStep.run_id == run_id)
                     .order_by(RunStep.seq.asc()).all())
            latest = steps[-1] if steps else None

            # a RUNNING chat run whose newest step is a tool_dispatch = a crash
            # window inside the tool cycle (before/after proposal, or mid-dispatch)
            if row.status == RUNNING and latest is not None \
                    and latest.kind == chat_tools.CHAT_TOOL_STEP_KIND:
                return self._advance_chat_tool_run(run_id)

            expired = (
                row.lease_expires_at is not None
                and _aware(row.lease_expires_at) < now
            )
            # `latest` is now a generation segment (load_context or a
            # continuation llm_generate). Its status is the durable proof of
            # whether the provider was invoked for THIS segment.
            if row.status == RUNNING and not expired:
                return RunOutcome(run_id, "running", "in_progress")  # a live worker owns it
            if row.status == RUNNING and expired:
                if latest is None or latest.status == "running":
                    return self._force_terminal_expired(row, OUTCOME_UNKNOWN, "outcome_unknown")
                if latest.status != "pending":
                    # segment already terminal but the run never advanced — the
                    # (atomic) suspend / finalize transaction did not land; the
                    # marker/decision is unreproducible -> unprovable
                    return self._force_terminal_expired(row, OUTCOME_UNKNOWN, "outcome_unknown")
                # latest.status == "pending" -> provider never ran -> reclaimable

            seg = latest
            claim_from = row.status
            lease = self._mint_lease()
            rc = db.execute(
                update(Run).where(
                    Run.id == run_id, Run.version == row.version,
                    Run.status == row.status,
                    or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now),
                ).values(
                    lease_owner=lease, lease_expires_at=now + self._lease_ttl(),
                    version=Run.version + 1,
                    run_deadline_at=func.coalesce(Run.run_deadline_at,
                                                  self._recovery_deadline(now)),
                    started_at=func.coalesce(Run.started_at, now), updated_at=now,
                )
            ).rowcount
            if rc != 1:
                db.rollback()
                return RunOutcome(run_id, "abandoned", "lease_race")
            db.commit()
            claimed = ClaimedRun(
                run_id=run_id, kind=row.kind, user_id=row.user_id,
                tenant_id=row.tenant_id, auth_session_ref=row.auth_session_ref,
                lease_owner=lease, version=row.version + 1, attempt=0,
            )

            if claim_from == QUEUED:
                try:
                    v = self._apply_transition(db, claimed=claimed, frm=QUEUED, to=RUNNING)
                    claimed = replace(claimed, version=v)
                except (RunLeaseLost, ControlEventContention) as exc:
                    db.rollback()
                    return RunOutcome(run_id, "abandoned", type(exc).__name__)
                db.commit()

            if seg is None:  # no generation step at all — cannot generate, fail closed
                return self._finalize_terminal(db, claimed, FAILED, "chat_input_error",
                                               frm=RUNNING)

            # ── pre-invocation gates ──
            try:
                self._precheck(db, claimed)
            except RunLeaseLost:
                return RunOutcome(run_id, "abandoned", "RunLeaseLost")
            except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                return self._finalize_terminal(db, claimed, FAILED, "session_revoked",
                                               frm=RUNNING)
            if self._live_cancel_id(db, claimed.run_id) is not None:
                return self._consume_cancel(db, claimed, frm=RUNNING)  # provider NEVER called

            run_row = db.query(Run).filter(Run.id == run_id).first()
            try:
                if seg.kind in chat_tools.CHAT_SEGMENT_KINDS and seg.seq == 1:
                    chat_input = chat_stream.build_chat_input(db, run_row, seg)
                else:  # a continuation segment — rebuild from durable state
                    chat_input = self._build_continuation_input(db, run_row, seg)
            except Exception:  # noqa: BLE001 — sealed submission unreadable / malformed
                return self._finalize_terminal(db, claimed, FAILED, "chat_input_error",
                                               frm=RUNNING)

            # DURABLE attempt-1 record: step pending -> running, COMMIT. After
            # this a crash / lease loss => outcome_unknown (never a 2nd attempt).
            db.execute(
                update(RunStep).where(RunStep.id == seg.id, RunStep.status == "pending")
                .values(status="running", started_at=now)
            )
            db.commit()

            return self._run_chat_generation(db, claimed, seg, chat_input,
                                             session_id=row.session_id)

    # ---- MP-3C2: durable chat -> tool suspend / decision / resume ----

    def _advance_chat_tool_run(self, run_id: str) -> "RunOutcome":
        """Lease + advance one step of a chat run's in-flight MCP tool cycle.
        Reuses the MP-2 gateway drivers unchanged — a chat run's
        ``tool_dispatch`` step is shaped exactly like an ``mcp_call`` run's."""
        from app.approval import gateway
        from app.run_engine import mcp_flow

        now = self._now()
        with self._session() as db:
            r = db.execute(
                select(Run.id, Run.version, Run.status, Run.kind, Run.user_id,
                       Run.tenant_id, Run.session_id, Run.auth_session_ref)
                .where(Run.id == run_id)
            ).first()
            if r is None or r.kind != _CHAT_RUN_KIND:
                return RunOutcome(run_id, "none", "not_chat")
            if r.status in RUN_TERMINAL:
                return RunOutcome(run_id, r.status, "already_terminal")
            if r.status not in _CHAT_ADVANCEABLE:
                return RunOutcome(run_id, "none", r.status)

            lease = self._mint_lease()
            rc = db.execute(
                update(Run).where(
                    Run.id == run_id, Run.version == r.version, Run.status == r.status,
                    or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now),
                ).values(lease_owner=lease, lease_expires_at=now + self._lease_ttl(),
                         version=Run.version + 1,
                         started_at=func.coalesce(Run.started_at, now), updated_at=now)
            ).rowcount
            if rc != 1:
                db.rollback()
                return RunOutcome(run_id, "abandoned", "lease_race")
            db.commit()

            claimed = ClaimedRun(
                run_id=run_id, kind=r.kind, user_id=r.user_id, tenant_id=r.tenant_id,
                auth_session_ref=r.auth_session_ref, lease_owner=lease,
                version=r.version + 1, attempt=0,
            )
            status, session_id = r.status, r.session_id
            step = (db.query(RunStep)
                    .filter(RunStep.run_id == run_id, RunStep.kind == "tool_dispatch")
                    .order_by(RunStep.seq.desc()).first())
            if step is None:
                return self._finalize_terminal(db, claimed, FAILED, "chat_input_error",
                                               frm=status)

            if status == WAITING_FOR_APPROVAL:
                return self._mcp_on_decision(db, gateway, mcp_flow, claimed, step, session_id)
            if status == RUNNING:
                if not step.approval_id or step.status in ("pending", "waiting"):
                    # crash before / after proposal, before park — dedupe-safe replay
                    return self._mcp_first_pass(db, gateway, mcp_flow, claimed, step,
                                                session_id, frm=RUNNING)
                return self._mcp_dispatch(db, gateway, mcp_flow, claimed, step, session_id)
            # APPROVED / RESUMING -> normalize to RUNNING -> dispatch
            try:
                self._mcp_session_check(db, claimed)
            except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                return self._finalize_terminal(db, claimed, FAILED, "session_revoked",
                                               frm=status)
            try:
                v = claimed.version
                if status == APPROVED:
                    v = self._apply_transition(db, claimed=replace(claimed, version=v),
                                               frm=APPROVED, to=RESUMING)
                    status = RESUMING
                v = self._apply_transition(db, claimed=replace(claimed, version=v),
                                           frm=RESUMING, to=RUNNING)
            except (RunLeaseLost, ControlEventContention) as exc:
                db.rollback()
                return RunOutcome(run_id, "abandoned", type(exc).__name__)
            db.commit()
            return self._mcp_dispatch(db, gateway, mcp_flow, replace(claimed, version=v),
                                      step, session_id)

    def _chat_suspend_for_tool(self, db, claimed: "ClaimedRun", gen_step,
                               captured_marker: str, session_id,
                               *, seg_text: str = "") -> "RunOutcome":
        """A generation segment produced a COMPLETE ``[MCP_CALL]`` marker. Seal
        the clean segment text, open server-owned turn / call identity, seal the
        normalized args, create a ``tool_dispatch`` step (ONE atomic commit), then
        run the MP-2 gateway first pass and park the SAME run for approval."""
        from app.approval import gateway
        from app.approval.hashing import args_hash
        from app.approval.identity import open_tool_call, open_turn
        from app.run_engine import chat_tools, mcp_flow

        db.expire_all()
        # 1 — parse with the ONE canonical grammar; exactly one call required
        try:
            tool_id, args = chat_tools.parse_one_mcp_call(captured_marker)
        except chat_tools.ChatToolMarkerError as exc:
            db.execute(update(RunStep).where(RunStep.id == gen_step.id).values(
                status="incomplete", error_code=exc.reason, finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, exc.reason, frm=RUNNING)

        # 2 — durable per-run cycle ceiling (no in-memory counter)
        cycles = (db.query(RunStep)
                  .filter(RunStep.run_id == claimed.run_id,
                          RunStep.kind == chat_tools.CHAT_TOOL_STEP_KIND).count())
        active = (db.query(RunStep)
                  .filter(RunStep.run_id == claimed.run_id,
                          RunStep.kind == chat_tools.CHAT_TOOL_STEP_KIND,
                          RunStep.status.in_(("pending", "running", "waiting"))).count())
        if cycles >= chat_tools.max_tool_cycles() or active > 0:
            db.execute(update(RunStep).where(RunStep.id == gen_step.id).values(
                status="incomplete", error_code="tool_cycle_limit", finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, "tool_cycle_limit", frm=RUNNING)

        # 3 — cancel / session revoked before ANY proposal persistence -> fail closed
        if self._live_cancel_id(db, claimed.run_id) is not None:
            return self._consume_cancel(db, claimed, frm=RUNNING)
        try:
            self._mcp_session_check(db, claimed)
        except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)

        # 4 — server-owned execution / tool identity first (these commit on their
        #     own; a crash here only orphans harmless identity rows — no proposal,
        #     no dispatch record, gen step still `running` -> the run recovers to
        #     `outcome_unknown`, never a re-execution).
        run_row = db.query(Run).filter(Run.id == claimed.run_id).first()
        try:
            turn = open_turn(db, user_id=claimed.user_id, tenant_id=claimed.tenant_id,
                             session_id=session_id, run_id=claimed.run_id, kind="reference")
            a_hash = args_hash(args)
            call = open_tool_call(db, turn=turn, call_index=0, tool_id=tool_id,
                                  args_hash=a_hash)
        except Exception:  # noqa: BLE001
            db.rollback()
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                           "output_persist_failed", frm=RUNNING)

        # 5 — ONE atomic commit: gen segment sealed + gen step `succeeded`
        #     + sealed normalized args + tool_dispatch step `pending`. After this
        #     the marker request is fully durable; a crash before the gateway
        #     first pass replays it dedupe-safe (never re-executes).
        try:
            seg_pid = None
            if seg_text:
                seg_pid = seal_payload(
                    db, tenant_id=run_row.tenant_id, user_id=run_row.user_id,
                    run_id=run_row.id, step_id=gen_step.id,
                    payload_type=chat_tools.SEGMENT_PAYLOAD_TYPE,
                    plaintext=seg_text, commit=False,
                ).id
            db.execute(update(RunStep).where(RunStep.id == gen_step.id).values(
                status="succeeded", output_payload_id=seg_pid, finished_at=self._now(),
                output_ref=json.dumps({"segment_payload_id": seg_pid,
                                       "output_len": len(seg_text)}, sort_keys=True)))
            tool_step_id = str(uuid4())
            pay = seal_payload(
                db, tenant_id=claimed.tenant_id, user_id=claimed.user_id,
                run_id=claimed.run_id, step_id=tool_step_id,
                payload_type=chat_tools.TOOL_ARGS_PAYLOAD_TYPE,
                plaintext=chat_tools.normalized_args_json(args), commit=False,
            )
            next_seq = int(db.execute(
                select(func.coalesce(func.max(RunStep.seq), 0))
                .where(RunStep.run_id == claimed.run_id)).scalar() or 0) + 1
            db.add(RunStep(
                id=tool_step_id, run_id=claimed.run_id, tenant_id=claimed.tenant_id,
                user_id=claimed.user_id, session_id=session_id, seq=next_seq,
                kind=chat_tools.CHAT_TOOL_STEP_KIND, engine="native", status="pending",
                execution_turn_id=turn.id, tool_call_id=call.id, attempt=0,
                input_payload_id=pay.id,
                input_ref=json.dumps({"tool_id": tool_id, "args_hash": a_hash,
                                      "args_payload_id": pay.id, "cycle": next_seq},
                                     sort_keys=True),
            ))
            db.flush()
            db.commit()
        except Exception:  # noqa: BLE001 — nothing durable landed; run stays running
            db.rollback()
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                           "output_persist_failed", frm=RUNNING)

        tool_step = db.query(RunStep).filter(RunStep.id == tool_step_id).first()
        # 5 — MP-2 gateway first pass (execute_mcp is _forbidden here) -> park
        return self._mcp_first_pass(db, gateway, mcp_flow, claimed, tool_step,
                                    session_id, frm=RUNNING)

    def _chat_suspend_for_python(self, db, claimed: "ClaimedRun", gen_step,
                                 captured_marker: str, session_id,
                                 *, seg_text: str = "") -> "RunOutcome":
        """MP-4C-R2A-3 — a generation segment produced a COMPLETE [PYTHON_CODE]
        marker and `run_engine_sandbox` is ON.

        Mirrors `_chat_suspend_for_tool`'s proven ordering, but the ONLY thing
        that differs is the parser and the server-owned tool identity: it seals
        the clean segment text, opens server-owned turn / call identity, seals
        the normalized Python args, creates ONE `tool_dispatch` step (one atomic
        commit), then runs the SAME `_mcp_first_pass` gateway proposal pass used
        by MCP and parks the SAME run at `waiting_for_approval`.

        ZERO Python is executed: the proposal pass's executor callback is
        forbidden, and approval consumption is not implemented in this phase.
        """
        from app.approval import gateway
        from app.approval.hashing import args_hash
        from app.approval.identity import open_tool_call, open_turn
        from app.run_engine import chat_tools, mcp_flow
        from app.run_engine import python_approval_identity as pai
        from app.run_engine import sandbox_policy

        db.expire_all()
        # 1 — strict parse with the ONE canonical grammar (no second regex).
        code, reason = sandbox_policy.strict_extract_python_code(captured_marker)
        if code is None:
            code_reason = reason or "sandbox_marker_malformed"
            db.execute(update(RunStep).where(RunStep.id == gen_step.id).values(
                status="incomplete", error_code=code_reason, finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, code_reason, frm=RUNNING)

        # 2 — UTF-8 byte ceiling from Settings (never a JSON-Schema char check).
        code_bytes = len(code.encode("utf-8"))
        if code_bytes > sandbox_policy.max_code_bytes():
            db.execute(update(RunStep).where(RunStep.id == gen_step.id).values(
                status="incomplete", error_code="sandbox_marker_oversized",
                finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED,
                                           "sandbox_marker_oversized", frm=RUNNING)

        # 3 — durable per-run cycle ceiling (no in-memory counter), identical to MCP.
        cycles = (db.query(RunStep)
                  .filter(RunStep.run_id == claimed.run_id,
                          RunStep.kind == chat_tools.CHAT_TOOL_STEP_KIND).count())
        active = (db.query(RunStep)
                  .filter(RunStep.run_id == claimed.run_id,
                          RunStep.kind == chat_tools.CHAT_TOOL_STEP_KIND,
                          RunStep.status.in_(("pending", "running", "waiting"))).count())
        if cycles >= chat_tools.max_tool_cycles() or active > 0:
            db.execute(update(RunStep).where(RunStep.id == gen_step.id).values(
                status="incomplete", error_code="tool_cycle_limit", finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, "tool_cycle_limit", frm=RUNNING)

        # 4 — cancel / session revoked BEFORE any proposal persistence -> fail closed.
        if self._live_cancel_id(db, claimed.run_id) is not None:
            return self._consume_cancel(db, claimed, frm=RUNNING)
        try:
            self._mcp_session_check(db, claimed)
        except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)

        # 4b - MP-4C policy: without the isolated executor broker, refuse BEFORE
        #      creating any PendingApproval - an approval must never falsely
        #      promise executable Python. Cancellation/session checks above win.
        _refusal = self._python_route_refusal()
        if _refusal is not None:
            db.execute(update(RunStep).where(RunStep.id == gen_step.id).values(
                status="incomplete", error_code=_refusal,
                finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, _refusal,
                                           frm=RUNNING)

        # 5 — the server-owned approval identity. A locked / drifted identity
        #     leaves the row locked and the gateway then BLOCKS (never executes).
        try:
            pai.ensure_python_approval_identity(db, claimed.user_id)
        except Exception:  # noqa: BLE001
            db.rollback()
            return self._finalize_terminal(db, claimed, FAILED, "gateway_error", frm=RUNNING)

        tool_id = pai.PYTHON_TOOL_ID
        args = {"code": code}

        # 6 — server-owned execution / tool identity.
        run_row = db.query(Run).filter(Run.id == claimed.run_id).first()
        try:
            turn = open_turn(db, user_id=claimed.user_id, tenant_id=claimed.tenant_id,
                             session_id=session_id, run_id=claimed.run_id, kind="reference")
            a_hash = args_hash(args)
            call = open_tool_call(db, turn=turn, call_index=0, tool_id=tool_id,
                                  args_hash=a_hash)
        except Exception:  # noqa: BLE001
            db.rollback()
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                           "output_persist_failed", frm=RUNNING)

        # 7 — ONE atomic commit: segment sealed + gen step succeeded + sealed
        #     Python args + tool_dispatch step pending. Raw code lives ONLY in
        #     the encrypted payload; every plaintext field carries bounded
        #     references / hashes only.
        try:
            seg_pid = None
            if seg_text:
                seg_pid = seal_payload(
                    db, tenant_id=run_row.tenant_id, user_id=run_row.user_id,
                    run_id=run_row.id, step_id=gen_step.id,
                    payload_type=chat_tools.SEGMENT_PAYLOAD_TYPE,
                    plaintext=seg_text, commit=False,
                ).id
            db.execute(update(RunStep).where(RunStep.id == gen_step.id).values(
                status="succeeded", output_payload_id=seg_pid, finished_at=self._now(),
                output_ref=json.dumps({"segment_payload_id": seg_pid,
                                       "output_len": len(seg_text)}, sort_keys=True)))
            tool_step_id = str(uuid4())
            pay = seal_payload(
                db, tenant_id=claimed.tenant_id, user_id=claimed.user_id,
                run_id=claimed.run_id, step_id=tool_step_id,
                payload_type=chat_tools.TOOL_ARGS_PAYLOAD_TYPE,
                plaintext=chat_tools.normalized_args_json(args), commit=False,
            )
            next_seq = int(db.execute(
                select(func.coalesce(func.max(RunStep.seq), 0))
                .where(RunStep.run_id == claimed.run_id)).scalar() or 0) + 1
            db.add(RunStep(
                id=tool_step_id, run_id=claimed.run_id, tenant_id=claimed.tenant_id,
                user_id=claimed.user_id, session_id=session_id, seq=next_seq,
                kind=chat_tools.CHAT_TOOL_STEP_KIND, engine="native", status="pending",
                execution_turn_id=turn.id, tool_call_id=call.id, attempt=0,
                input_payload_id=pay.id,
                input_ref=json.dumps({"tool_id": tool_id, "args_hash": a_hash,
                                      "args_payload_id": pay.id, "cycle": next_seq,
                                      "purpose": "python_execution",
                                      "code_bytes": code_bytes}, sort_keys=True),
            ))
            db.flush()
            db.commit()
        except Exception:  # noqa: BLE001 — nothing durable landed; run stays running
            db.rollback()
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                           "output_persist_failed", frm=RUNNING)

        tool_step = db.query(RunStep).filter(RunStep.id == tool_step_id).first()
        # 8 — the SAME gateway proposal pass MCP uses (execute callback forbidden).
        return self._mcp_first_pass(db, gateway, mcp_flow, claimed, tool_step,
                                    session_id, frm=RUNNING)

    def _chat_continue_after_tool(self, db, claimed: "ClaimedRun", tool_step) -> "RunOutcome":
        """The MCP call executed at-most-once and its result is sealed. Start a
        NEW durable continuation ``llm_generate`` step and resume generation with
        the sealed tool result as an untrusted, delimited data message."""
        from app.run_engine import chat_tools

        db.expire_all()
        try:
            self._precheck(db, claimed)
        except RunLeaseLost:
            return RunOutcome(claimed.run_id, "abandoned", "RunLeaseLost")
        except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
            # session revoked after tool success but before continuation -> no provider call
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
        if self._live_cancel_id(db, claimed.run_id) is not None:
            return self._consume_cancel(db, claimed, frm=RUNNING)

        run_row = db.query(Run).filter(Run.id == claimed.run_id).first()
        try:
            # built BEFORE the continuation step exists -> nothing to exclude
            chat_input = self._build_continuation_input(db, run_row)
        except Exception:  # noqa: BLE001
            return self._finalize_terminal(db, claimed, FAILED, "tool_continuation_failed",
                                           frm=RUNNING)

        now = self._now()
        cont_id = str(uuid4())
        next_seq = int(db.execute(
            select(func.coalesce(func.max(RunStep.seq), 0))
            .where(RunStep.run_id == claimed.run_id)).scalar() or 0) + 1
        db.add(RunStep(
            id=cont_id, run_id=claimed.run_id, tenant_id=claimed.tenant_id,
            user_id=claimed.user_id, session_id=run_row.session_id, seq=next_seq,
            kind=chat_tools.CHAT_CONTINUATION_KIND, engine="native", status="pending",
            attempt=0,
            input_ref=json.dumps({"continuation_after": tool_step.id, "seq": next_seq},
                                 sort_keys=True),
        ))
        db.commit()  # continuation step PENDING before provider invocation
        db.execute(update(RunStep).where(RunStep.id == cont_id, RunStep.status == "pending")
                   .values(status="running", started_at=now))
        db.commit()  # attempt-1 marker: a crash after this + expiry => outcome_unknown

        cont_step = db.query(RunStep).filter(RunStep.id == cont_id).first()
        return self._run_chat_generation(db, claimed, cont_step, chat_input,
                                         session_id=run_row.session_id)

    def _build_continuation_input(self, db, run_row, upto_step=None):
        """Rebuild the provider input for a continuation segment from durable
        state only: the MP-3A submission (snapshot provider / model / mode /
        system prompt / options — PRESERVED) + every prior sealed generation
        segment (assistant) + every prior sealed tool result (a delimited,
        size-bounded, non-instruction user message). ``upto_step`` (an
        already-created, in-flight continuation step) is excluded."""
        from app.run_engine import chat_stream, chat_tools

        seq1 = (db.query(RunStep).filter(RunStep.run_id == run_row.id)
                .order_by(RunStep.seq.asc()).first())
        base = chat_stream.build_chat_input(db, run_row, seq1)
        msgs = list(base.messages)
        for s in (db.query(RunStep).filter(RunStep.run_id == run_row.id)
                  .order_by(RunStep.seq.asc()).all()):
            if upto_step is not None and s.id == upto_step.id:
                continue
            if s.kind in chat_tools.CHAT_SEGMENT_KINDS and s.output_payload_id:
                t = chat_tools.open_segment_text(db, run_row, s)
                if t:
                    msgs.append({"role": "assistant", "content": t})
            elif s.kind == chat_tools.CHAT_TOOL_STEP_KIND and s.output_payload_id:
                out, is_err = chat_tools.open_tool_output(db, run_row, s)
                msgs.append({"role": "user", "content": chat_tools.tool_result_envelope(
                    chat_tools.tool_step_tool_id(s) or "mcp", out, is_err)})
        return chat_stream.ChatInput(
            messages=msgs, system_prompt=base.system_prompt, model=base.model,
            provider=base.provider, mode=base.mode, options=base.options)

    def _run_chat_generation(self, db, claimed: "ClaimedRun", step,
                             chat_input, *, session_id=None) -> "RunOutcome":
        from app.run_engine import chat_stream, chat_tools
        from app.run_engine.text_events import TextEventBuffer

        buf = TextEventBuffer(self._sf, claimed.run_id, tenant_id=claimed.tenant_id,
                              user_id=claimed.user_id, now_fn=self._now)
        clean_parts: list[str] = []
        streamed = [False]
        scanner = chat_stream.MarkerScanner(
            chat_stream.ALL_WATCHED_MARKERS,  # MP-3C3D2: + save_memory/save_directive/
            capture=True, max_capture_bytes=chat_tools.marker_max_bytes(),  # use_memory/plan/lesson
            # MP-4C-R2A-3: Python capture is enabled ONLY by this server-owned
            # setting — no request, header or body can influence it. With the
            # flag off the scanner behaves exactly as before (Python ->
            # `unsupported` -> `tool_resume_required`). Shell and browser stay
            # unsupported in every configuration.
            capture_python=bool(getattr(settings, "run_engine_sandbox", False)))
        state = {"abort": None, "marker": False, "tool": None, "tool_kind": None,
                 "reason": "tool_resume_required"}
        tick = max(1, int(settings.run_worker_lease_ttl_s) // 3)
        hb = _ChatHeartbeat(self, claimed.run_id, claimed.lease_owner, interval_s=tick)
        deadline = self._now() + _dt.timedelta(
            seconds=int(settings.run_worker_handler_timeout_s))
        seen = [0]

        def _guard() -> None:
            if self._stopping():                  # MP-3C3A cooperative shutdown
                state["abort"] = "shutdown"
                raise chat_stream.StreamAborted()
            if hb.lease_lost:
                state["abort"] = "lease"
                raise chat_stream.StreamAborted()
            gdb = self._sf()
            try:
                grun = gdb.query(Run).filter(Run.id == claimed.run_id).first()
                if (grun is None or grun.status != RUNNING
                        or grun.lease_owner != claimed.lease_owner
                        or grun.version != claimed.version):
                    state["abort"] = "lease"
                    raise chat_stream.StreamAborted()
                if self._live_cancel_id(gdb, claimed.run_id) is not None:
                    state["abort"] = "cancel"
                    raise chat_stream.StreamAborted()
                try:
                    validate_session_binding(gdb, self._mcp_ctx(claimed), run=grun)
                except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                    state["abort"] = "session"
                    raise chat_stream.StreamAborted()
            finally:
                gdb.close()

        def _on_idle() -> None:
            if self._now() >= deadline:
                raise chat_stream.ProviderStreamTimeout()
            _guard()

        def on_token(tok: str) -> None:
            if not tok:
                return
            streamed[0] = True
            safe = scanner.feed(tok)             # never returns full/partial marker text
            if safe:
                clean_parts.append(safe)
                buf.publish(safe)
                buf.tick()
            if scanner.tripped:
                if scanner.capture_status == "capturing":
                    # keep consuming to confidentially complete the [MCP_CALL] line
                    seen[0] += 1
                    if seen[0] >= chat_stream._GUARD_EVERY_TOKENS:
                        seen[0] = 0
                        _guard()
                    return
                self._route_tripped_scanner(scanner, state)
                raise chat_stream.StreamAborted()
            seen[0] += 1
            if seen[0] >= chat_stream._GUARD_EVERY_TOKENS:
                seen[0] = 0
                _guard()

        hb.start()
        provider = (self._chat_provider or chat_stream.default_chat_provider)(
            chat_input, db=db, user_id=claimed.user_id)
        full_text: Optional[str] = None
        try:
            full_text = chat_stream.run_stream(
                provider, chat_input, on_token=on_token, on_idle=_on_idle,
                deadline=deadline, now_fn=self._now, poll_interval=tick,
                # D6C-J4: flush buffered text on its own flush_ms bound while the
                # provider is idle, WITHOUT changing the guard cadence above.
                on_tick=buf.tick, tick_interval=buf.flush_interval_s)
        except chat_stream.StreamAborted:
            pass
        except chat_stream.ProviderStreamTimeout:
            hb.stop()
            buf.close()
            return self._finalize_terminal(db, claimed, FAILED, "provider_timeout", frm=RUNNING)
        except chat_stream.ProviderStreamError as exc:
            hb.stop()
            buf.close()
            return self._finalize_terminal(db, claimed, FAILED, _provider_reason(exc),
                                           frm=RUNNING)
        finally:
            hb.stop()

        # a non-streaming provider returned everything in `done.response`
        if not streamed[0] and full_text and state["abort"] is None \
                and not state["marker"] and state["tool"] is None:
            safe = scanner.feed(full_text)
            if safe:
                clean_parts.append(safe)
                buf.publish(safe)
            if scanner.tripped and scanner.capture_status != "capturing":
                self._route_tripped_scanner(scanner, state)

        if state["abort"] is None and not state["marker"] and state["tool"] is None:
            scanner.finish()                 # never emits the held tail
            if scanner.truncated:
                state["marker"] = True
                state["reason"] = "truncated_tool_marker"
            elif scanner.capture_status != "none":
                self._route_tripped_scanner(scanner, state)
        buf.close()  # final partial-buffer flush — clean text only, never a marker

        if state["abort"] == "lease":
            return RunOutcome(claimed.run_id, "abandoned", "RunLeaseLost")
        if state["abort"] == "shutdown":
            # MP-3C3A — forced shutdown mid-generation. Do NOT finalise: no
            # Message, no terminal transition. The run stays `running` with its
            # lease; the next boot's recovery (running + expired + invocation
            # began) declares it `outcome_unknown`. Never a false success.
            return RunOutcome(claimed.run_id, "abandoned", "worker_shutdown")
        if state["abort"] == "session":
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
        if state["abort"] == "cancel":
            return self._consume_cancel(db, claimed, frm=RUNNING)  # NO assistant Message

        if state["tool"] is not None:
            _sid = (session_id if session_id is not None
                    else getattr(step, "session_id", None))
            if state.get("tool_kind") == "python":
                return self._chat_suspend_for_python(
                    db, claimed, step, state["tool"], _sid,
                    seg_text="".join(clean_parts))
            return self._chat_suspend_for_tool(
                db, claimed, step, state["tool"], _sid,
                seg_text="".join(clean_parts))

        if state["marker"]:
            reason = state["reason"]
            db.execute(update(RunStep).where(RunStep.id == step.id).values(
                status="incomplete", error_code=reason, finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, reason, frm=RUNNING)

        this_text = "".join(clean_parts) or (full_text or "")
        return self._finalize_chat_success(db, claimed, step, this_text)

    @staticmethod
    def _route_tripped_scanner(scanner, state) -> None:
        """Map a tripped :class:`MarkerScanner` to a bounded outcome for the
        chat driver. Never carries marker / argument text."""
        st = scanner.capture_status
        if st == "complete" and scanner.captured:
            state["tool"] = scanner.captured
            # MP-4C-R2A-3: which opener this capture came from. "mcp" routes to
            # the proven MCP suspension path; "python" to the R2A-3 path.
            state["tool_kind"] = getattr(scanner, "captured_kind", None)
            return
        state["marker"] = True
        if st == "overflow":
            state["reason"] = "tool_marker_oversized"
        elif st == "incomplete":
            state["reason"] = "tool_marker_incomplete"
        elif st == "unsupported":
            state["reason"] = "tool_resume_required"      # python/shell/browser stay blocked
        elif st == "deferred":
            # MP-3C3D2: save_memory/save_directive/use_memory/plan/lesson — a
            # distinct bounded reason from tool_resume_required so operators
            # can tell "tried to save memory" from "tried to run Python".
            state["reason"] = "nonexec_marker_deferred"
        else:
            state["reason"] = "tool_resume_required"

    def _finalize_chat_success(self, db, claimed: "ClaimedRun", step,
                               last_segment_text: str) -> "RunOutcome":
        """The FINAL generation segment finished with no marker. Assemble the one
        assistant ``Message`` from every sealed prior segment + this last
        segment's clean text, then step success + run success — ONE transaction.
        The lease + version CAS in ``_apply_transition`` is the sole "who
        finalizes" authority; a stale worker can create neither a second Message
        nor a second success."""
        from app.run_engine import chat_stream, chat_tools

        db.expire_all()
        try:
            self._precheck(db, claimed)
        except RunLeaseLost:
            return RunOutcome(claimed.run_id, "abandoned", "RunLeaseLost")
        except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
        if self._live_cancel_id(db, claimed.run_id) is not None:
            return self._consume_cancel(db, claimed, frm=RUNNING)

        fresh_step = db.query(RunStep).filter(RunStep.id == step.id).first()
        if fresh_step is not None and fresh_step.output_ref:
            try:
                if json.loads(fresh_step.output_ref).get("message_id"):
                    return self._finalize_terminal(db, claimed, SUCCEEDED, None, frm=RUNNING)
            except (ValueError, TypeError):
                pass

        run_row = db.query(Run).filter(Run.id == claimed.run_id).first()
        parts: list[str] = []
        for s in (db.query(RunStep).filter(RunStep.run_id == claimed.run_id)
                  .order_by(RunStep.seq.asc()).all()):
            if s.id == step.id:
                continue
            if s.kind in chat_tools.CHAT_SEGMENT_KINDS and s.output_payload_id:
                t = chat_tools.open_segment_text(db, run_row, s)
                if t:
                    parts.append(t)
        final_text = "".join(parts) + (last_segment_text or "")
        if not final_text:
            db.execute(update(RunStep).where(RunStep.id == step.id).values(
                status="failed", error_code="empty_generation", finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, "empty_generation", frm=RUNNING)

        # MP-3C3D2 — truthful citations/sources_used/memories_used: read the
        # VERSIONED context-enrichment snapshot sealed at submission time
        # (seq=1, the same durable state build_chat_input already reads for
        # the prompt) — never re-derived, never re-selected, so finalization
        # matches EXACTLY what the provider actually received.
        # read_context_snapshot() distinguishes three outcomes: the field is
        # genuinely absent (missing key / null — a pre-MP-3C3D2 run, or
        # nothing was retrievable) → honest empty/false, no error; the field
        # is PRESENT but malformed or an unknown context_snapshot_version →
        # raises ContextSnapshotError; or an unrelated decrypt failure on the
        # SAME payload build_chat_input already opened successfully earlier
        # THIS run to even reach generation. The latter two are genuine
        # anomalies (corrupt/unexpected encrypted state), never "no context":
        # fail closed rather than silently succeed with fabricated-empty
        # citations. No Message is created on this path.
        seq1 = (db.query(RunStep).filter(RunStep.run_id == claimed.run_id)
                .order_by(RunStep.seq.asc()).first())
        if seq1 is None:
            db.execute(update(RunStep).where(RunStep.id == step.id).values(
                status="failed", error_code="context_snapshot_unreadable",
                finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED,
                                           "context_snapshot_unreadable", frm=RUNNING)
        try:
            ctx_snap = chat_stream.read_context_snapshot(db, run_row, seq1)
        except Exception:  # noqa: BLE001 — ContextSnapshotError or a decrypt failure; fail closed
            db.execute(update(RunStep).where(RunStep.id == step.id).values(
                status="failed", error_code="context_snapshot_unreadable",
                finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED,
                                           "context_snapshot_unreadable", frm=RUNNING)
        citations = chat_stream.validate_citations(ctx_snap.get("citations"))
        # honest by construction: true only when the SAME snapshot's text
        # block (what build_chat_input actually spliced into the provider's
        # messages) is non-empty — never the upstream-computed flag alone.
        sources_used = bool(citations) and bool(ctx_snap.get("rag_context_text"))
        memories_used = bool(ctx_snap.get("memory_context_text"))

        msg = Message(session_id=fresh_step.session_id, role=MessageRole.assistant,
                      content=final_text, citations=json.dumps(citations, sort_keys=True),
                      created_at=_utcnow_aware())
        db.add(msg)
        db.flush()                              # -> msg.id, NOT committed
        db.execute(update(RunStep).where(RunStep.id == step.id).values(
            status="succeeded", finished_at=self._now(),
            output_ref=json.dumps({"message_id": msg.id,
                                   "sources_used": sources_used,
                                   "memories_used": memories_used,
                                   "output_len": len(last_segment_text or ""),
                                   "final_len": len(final_text)}, sort_keys=True),
        ))
        try:
            self._apply_transition(db, claimed=claimed, frm=RUNNING, to=SUCCEEDED)
        except (RunLeaseLost, ControlEventContention) as exc:
            db.rollback()                       # -> NO Message persisted
            return RunOutcome(claimed.run_id, "abandoned", type(exc).__name__)
        try:
            db.commit()                         # Message + step + run + event, atomically
        except Exception:  # noqa: BLE001
            db.rollback()
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                           "output_persist_failed", frm=RUNNING)
        return RunOutcome(claimed.run_id, SUCCEEDED, None)

    # ---- MP-3C3D4C: durable deep research (acquisition -> synthesis) -----

    _DR_ACQUISITION_KIND = "research_fetch_extract"  # sealed by D4B's seal_research_snapshot
    _DR_SYNTHESIS_KIND = "deep_research_synthesis"

    def advance_deep_research_runs(self) -> list["RunOutcome"]:
        """Drive claimable durable ``deep_research`` runs one step. No-op
        unless the ONE authoritative readiness predicate is true — reused
        unchanged, never a second/duplicated gate."""
        from app.run_engine.deep_research_readiness import deep_research_executable

        if not deep_research_executable():
            return []
        if self._stopping():
            return []
        now = self._now()
        with self._session() as db:
            ids = db.execute(
                select(Run.id)
                .where(Run.kind == _DEEP_RESEARCH_RUN_KIND)
                .where(Run.status.in_(tuple(_DEEP_RESEARCH_ADVANCEABLE)))
                .where(or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now))
                .order_by(Run.created_at.asc())
                .limit(int(settings.run_worker_recover_batch))
            ).scalars().all()
        return [self._advance_deep_research_run(rid) for rid in ids]

    def _advance_deep_research_run(self, run_id: str) -> "RunOutcome":
        now = self._now()
        with self._session() as db:
            row = db.execute(
                select(Run.id, Run.version, Run.status, Run.kind, Run.user_id,
                       Run.tenant_id, Run.session_id, Run.auth_session_ref,
                       Run.lease_expires_at)
                .where(Run.id == run_id)
            ).first()
            if row is None or row.kind != _DEEP_RESEARCH_RUN_KIND:
                return RunOutcome(run_id, "none", "not_deep_research")
            if row.status in RUN_TERMINAL:
                return RunOutcome(run_id, row.status, "already_terminal")
            if row.status not in _DEEP_RESEARCH_ADVANCEABLE:
                return RunOutcome(run_id, "none", row.status)

            steps = (db.query(RunStep).filter(RunStep.run_id == run_id)
                     .order_by(RunStep.seq.asc()).all())
            latest = steps[-1] if steps else None

            expired = (
                row.lease_expires_at is not None
                and _aware(row.lease_expires_at) < now
            )
            if row.status == RUNNING and not expired:
                return RunOutcome(run_id, "running", "in_progress")  # a live worker owns it
            if row.status == RUNNING and expired:
                # Acquisition is retry-safe pre-seal (search/fetch are idempotent
                # reads) — by construction, D4B's seal_research_snapshot only
                # ever inserts a `research_fetch_extract` step ALREADY
                # `succeeded` (one atomic commit, no pending/running
                # intermediate state possible) — so its mere ABSENCE means
                # acquisition never got far enough to seal anything and is
                # always safe to retry from scratch. Only the synthesis
                # step's OWN "running" (invocation-start proof already
                # committed, provider maybe still running) is unprovable.
                if latest is not None and latest.kind == self._DR_SYNTHESIS_KIND \
                        and latest.status == "running":
                    return self._force_terminal_expired(row, OUTCOME_UNKNOWN, "outcome_unknown")
                # else: no synthesis step, or synthesis step still "pending"
                # (never invoked) -> reclaimable.

            claim_from = row.status
            lease = self._mint_lease()
            rc = db.execute(
                update(Run).where(
                    Run.id == run_id, Run.version == row.version,
                    Run.status == row.status,
                    or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now),
                ).values(
                    lease_owner=lease, lease_expires_at=now + self._lease_ttl(),
                    version=Run.version + 1,
                    run_deadline_at=func.coalesce(Run.run_deadline_at,
                                                  self._recovery_deadline(now)),
                    started_at=func.coalesce(Run.started_at, now), updated_at=now,
                )
            ).rowcount
            if rc != 1:
                db.rollback()
                return RunOutcome(run_id, "abandoned", "lease_race")
            db.commit()
            claimed = ClaimedRun(
                run_id=run_id, kind=row.kind, user_id=row.user_id,
                tenant_id=row.tenant_id, auth_session_ref=row.auth_session_ref,
                lease_owner=lease, version=row.version + 1, attempt=0,
            )

            if claim_from == QUEUED:
                try:
                    v = self._apply_transition(db, claimed=claimed, frm=QUEUED, to=RUNNING)
                    claimed = replace(claimed, version=v)
                except (RunLeaseLost, ControlEventContention) as exc:
                    db.rollback()
                    return RunOutcome(run_id, "abandoned", type(exc).__name__)
                db.commit()

            try:
                self._precheck(db, claimed)
            except RunLeaseLost:
                return RunOutcome(run_id, "abandoned", "RunLeaseLost")
            except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
            if self._live_cancel_id(db, claimed.run_id) is not None:
                return self._consume_cancel(db, claimed, frm=RUNNING)  # zero external calls

            submission_step = steps[0] if steps else None
            if submission_step is None:
                return self._finalize_terminal(db, claimed, FAILED, "chat_input_error", frm=RUNNING)

            if latest is None or latest.kind == submission_step.kind:
                return self._run_deep_research_acquisition(
                    db, claimed, submission_step, session_id=row.session_id)
            if latest.kind == self._DR_ACQUISITION_KIND:
                return self._run_deep_research_synthesis(
                    db, claimed, submission_step, latest, session_id=row.session_id)
            # latest.kind == self._DR_SYNTHESIS_KIND, status == "pending" (reclaimed)
            research_step = next(
                (s for s in steps if s.kind == self._DR_ACQUISITION_KIND), None)
            if research_step is None:  # unreachable by construction; fail closed
                return self._finalize_terminal(db, claimed, FAILED, "chat_input_error", frm=RUNNING)
            return self._run_deep_research_synthesis(
                db, claimed, submission_step, research_step, session_id=row.session_id,
                existing_synthesis_step=latest)

    def _run_deep_research_acquisition(self, db, claimed: "ClaimedRun", submission_step,
                                       *, session_id) -> "RunOutcome":
        from app.run_engine.research_acquisition import (
            AcquisitionSeams, ResearchAcquisitionCancelled, run_research_acquisition,
        )
        from app.run_engine.research_snapshot import seal_research_snapshot
        from app.tools.research_fetch import GuardedHttpFetchAdapter
        from app.tools.research_search import DuckDuckGoSearchAdapter

        run_row = db.query(Run).filter(Run.id == claimed.run_id).first()
        try:
            from app.run_engine.chat_stream import _open_submission_body

            body = _open_submission_body(db, run_row, submission_step)
        except Exception:  # noqa: BLE001
            return self._finalize_terminal(db, claimed, FAILED, "chat_input_error", frm=RUNNING)
        query = body.get("message") or ""

        tick = max(1, int(settings.run_worker_lease_ttl_s) // 3)
        hb = _ChatHeartbeat(self, claimed.run_id, claimed.lease_owner, interval_s=tick)
        deadline = self._now() + _dt.timedelta(seconds=int(settings.research_total_deadline_s))
        abort = {"reason": None}

        def _check(kind: str) -> None:
            if self._stopping():
                abort["reason"] = "shutdown"
                raise ResearchAcquisitionCancelled("shutdown")
            if hb.lease_lost:
                abort["reason"] = "lease"
                raise ResearchAcquisitionCancelled("lease_lost")
            gdb = self._sf()
            try:
                grun = gdb.query(Run).filter(Run.id == claimed.run_id).first()
                if (grun is None or grun.status != RUNNING
                        or grun.lease_owner != claimed.lease_owner
                        or grun.version != claimed.version):
                    abort["reason"] = "lease"
                    raise ResearchAcquisitionCancelled("lease_lost")
                if kind == "cancel" and self._live_cancel_id(gdb, claimed.run_id) is not None:
                    abort["reason"] = "cancel"
                    raise ResearchAcquisitionCancelled("cancel")
                if kind == "session":
                    try:
                        validate_session_binding(gdb, self._mcp_ctx(claimed), run=grun)
                    except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                        abort["reason"] = "session"
                        raise ResearchAcquisitionCancelled("session")
                if kind == "deadline" and self._now() >= deadline:
                    abort["reason"] = "deadline"
                    raise ResearchAcquisitionCancelled("deadline")
            finally:
                gdb.close()

        def _progress(code: str, data: dict) -> None:
            pdb = self._sf()
            try:
                if self._alloc_event(
                    pdb, run_id=claimed.run_id, tenant_id=claimed.tenant_id,
                    user_id=claimed.user_id, event_type="research.progress",
                    data=validate_event_data("research.progress", {"code": code}),
                ):
                    pdb.commit()
                else:
                    pdb.rollback()
            finally:
                pdb.close()

        seams = AcquisitionSeams(
            check_cancelled=lambda: _check("cancel"),
            check_session_binding=lambda: _check("session"),
            check_deadline=lambda: _check("deadline"),
            heartbeat=hb.beat,
            progress=_progress,
            now=self._now,
        )

        hb.start()
        try:
            result = run_research_acquisition(
                query, seams=seams,
                search_adapter=(self._research_search_adapter or DuckDuckGoSearchAdapter()),
                fetch_adapter=(self._research_fetch_adapter or GuardedHttpFetchAdapter()),
            )
        except ResearchAcquisitionCancelled:
            hb.stop()
            reason = abort["reason"]
            if reason == "lease":
                return RunOutcome(claimed.run_id, "abandoned", "RunLeaseLost")
            if reason == "shutdown":
                return RunOutcome(claimed.run_id, "abandoned", "worker_shutdown")
            if reason == "session":
                return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
            if reason == "cancel":
                return self._consume_cancel(db, claimed, frm=RUNNING)
            return self._finalize_terminal(db, claimed, FAILED, "deadline_exceeded", frm=RUNNING)
        except Exception:  # noqa: BLE001 — acquisition must never crash the worker
            hb.stop()
            return self._finalize_terminal(db, claimed, FAILED, "chat_input_error", frm=RUNNING)
        finally:
            hb.stop()

        snapshot = result.snapshot
        if not snapshot.get("sources"):
            return self._finalize_terminal(db, claimed, FAILED, "research_no_sources", frm=RUNNING)

        try:
            self._precheck(db, claimed)
        except RunLeaseLost:
            return RunOutcome(claimed.run_id, "abandoned", "RunLeaseLost")
        except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
        if self._live_cancel_id(db, claimed.run_id) is not None:
            return self._consume_cancel(db, claimed, frm=RUNNING)

        next_seq = int(db.execute(
            select(func.coalesce(func.max(RunStep.seq), 0))
            .where(RunStep.run_id == claimed.run_id)).scalar() or 0) + 1
        step_id = str(uuid4())
        if not self._alloc_event(
            db, run_id=claimed.run_id, tenant_id=claimed.tenant_id,
            user_id=claimed.user_id, event_type="research.progress",
            data=validate_event_data("research.progress", {"code": "research_sources_ready"}),
        ):
            db.rollback()
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                           "output_persist_failed", frm=RUNNING)
        try:
            research_step = seal_research_snapshot(
                db, tenant_id=claimed.tenant_id, user_id=claimed.user_id,
                session_id=session_id, run_id=claimed.run_id, step_id=step_id,
                seq=next_seq, snapshot=snapshot, commit=False,
            )
            db.commit()  # progress event + sealed payload + step, atomically
        except Exception:  # noqa: BLE001 — includes a concurrent-seal IntegrityError
            db.rollback()
            return RunOutcome(claimed.run_id, "abandoned", "seal_contention")

        # Continue straight into synthesis IN THIS SAME call — the run's
        # lease was just minted (fresh, not expired), so a LATER separate
        # `advance_deep_research_runs()` sweep would never re-select it
        # until that lease actually expires (mirrors `advance_chat_runs`'s
        # own single-call-drives-the-whole-generation pattern; there is no
        # "waiting on a human" reason to split acquisition and synthesis
        # across two separate claims the way MCP approval legitimately
        # does).
        return self._run_deep_research_synthesis(
            db, claimed, submission_step, research_step, session_id=session_id)

    def _run_deep_research_synthesis(self, db, claimed: "ClaimedRun", submission_step,
                                     research_step, *, session_id,
                                     existing_synthesis_step=None) -> "RunOutcome":
        from app.run_engine import chat_stream, chat_tools
        from app.run_engine.deep_research_citations import (
            build_web_citations, has_hallucinated_web_marker,
        )
        from app.run_engine.deep_research_synthesis import build_deep_research_input
        from app.run_engine.research_snapshot import open_research_snapshot
        from app.run_engine.text_events import TextEventBuffer

        run_row = db.query(Run).filter(Run.id == claimed.run_id).first()
        try:
            snapshot = open_research_snapshot(
                db, research_step, expected_tenant_id=claimed.tenant_id,
                expected_user_id=claimed.user_id, expected_run_id=claimed.run_id,
            )
            chat_input, marker_map = build_deep_research_input(
                db, run_row, submission_step, snapshot)
        except Exception:  # noqa: BLE001 — unreadable sealed state -> fail closed
            return self._finalize_terminal(db, claimed, FAILED,
                                           "context_snapshot_unreadable", frm=RUNNING)

        if existing_synthesis_step is not None:
            step = existing_synthesis_step
        else:
            next_seq = int(db.execute(
                select(func.coalesce(func.max(RunStep.seq), 0))
                .where(RunStep.run_id == claimed.run_id)).scalar() or 0) + 1
            step_id = str(uuid4())
            step = RunStep(
                id=step_id, run_id=claimed.run_id, tenant_id=claimed.tenant_id,
                user_id=claimed.user_id, session_id=session_id, seq=next_seq,
                kind=self._DR_SYNTHESIS_KIND, engine="native", status="pending",
            )
            db.add(step)
            db.commit()

        # DURABLE attempt-1 record: pending -> running, COMMIT. After this a
        # crash / lease loss => outcome_unknown (never a 2nd provider call).
        # "research_synthesizing" is allocated atomically in the SAME
        # commit — a reconnecting client must see it exactly when synthesis
        # actually starts, never before (this step was still "pending") and
        # never skipped (this code path only runs once per step, since a
        # resumed "running" step is already routed to outcome_unknown by
        # ``_advance_deep_research_run`` before ever reaching here).
        db.execute(update(RunStep).where(RunStep.id == step.id, RunStep.status == "pending")
                   .values(status="running", started_at=self._now()))
        if not self._alloc_event(
            db, run_id=claimed.run_id, tenant_id=claimed.tenant_id,
            user_id=claimed.user_id, event_type="research.progress",
            data=validate_event_data("research.progress", {"code": "research_synthesizing"}),
        ):
            db.rollback()
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                           "output_persist_failed", frm=RUNNING)
        db.commit()

        buf = TextEventBuffer(self._sf, claimed.run_id, tenant_id=claimed.tenant_id,
                              user_id=claimed.user_id, now_fn=self._now)
        clean_parts: list[str] = []
        streamed = [False]
        scanner = chat_stream.MarkerScanner(chat_stream.ALL_WATCHED_MARKERS, capture=False)
        state = {"abort": None, "marker": False, "reason": "tool_resume_required"}
        tick = max(1, int(settings.run_worker_lease_ttl_s) // 3)
        hb = _ChatHeartbeat(self, claimed.run_id, claimed.lease_owner, interval_s=tick)
        deadline = self._now() + _dt.timedelta(seconds=int(settings.run_worker_handler_timeout_s))
        seen = [0]

        def _guard() -> None:
            if self._stopping():
                state["abort"] = "shutdown"
                raise chat_stream.StreamAborted()
            if hb.lease_lost:
                state["abort"] = "lease"
                raise chat_stream.StreamAborted()
            gdb = self._sf()
            try:
                grun = gdb.query(Run).filter(Run.id == claimed.run_id).first()
                if (grun is None or grun.status != RUNNING
                        or grun.lease_owner != claimed.lease_owner
                        or grun.version != claimed.version):
                    state["abort"] = "lease"
                    raise chat_stream.StreamAborted()
                if self._live_cancel_id(gdb, claimed.run_id) is not None:
                    state["abort"] = "cancel"
                    raise chat_stream.StreamAborted()
                try:
                    validate_session_binding(gdb, self._mcp_ctx(claimed), run=grun)
                except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                    state["abort"] = "session"
                    raise chat_stream.StreamAborted()
            finally:
                gdb.close()

        def _on_idle() -> None:
            if self._now() >= deadline:
                raise chat_stream.ProviderStreamTimeout()
            _guard()

        def on_token(tok: str) -> None:
            if not tok:
                return
            streamed[0] = True
            safe = scanner.feed(tok)
            if safe:
                clean_parts.append(safe)
                buf.publish(safe)
                buf.tick()
            if scanner.tripped:
                # MP-3C3D4C: D4A did NOT approve an MCP/tool continuation
                # inside deep research — ANY marker in synthesis output
                # fails closed, exactly like an unsupported marker in plain
                # chat (never auto-executed here either way).
                state["marker"] = True
                state["reason"] = "tool_resume_required"
                raise chat_stream.StreamAborted()
            seen[0] += 1
            if seen[0] >= chat_stream._GUARD_EVERY_TOKENS:
                seen[0] = 0
                _guard()

        hb.start()
        provider = (self._chat_provider or chat_stream.default_chat_provider)(
            chat_input, db=db, user_id=claimed.user_id)
        full_text: Optional[str] = None
        try:
            full_text = chat_stream.run_stream(
                provider, chat_input, on_token=on_token, on_idle=_on_idle,
                deadline=deadline, now_fn=self._now, poll_interval=tick,
                # D6C-J4: flush buffered text on its own flush_ms bound while the
                # provider is idle, WITHOUT changing the guard cadence above.
                on_tick=buf.tick, tick_interval=buf.flush_interval_s)
        except chat_stream.StreamAborted:
            pass
        except chat_stream.ProviderStreamTimeout:
            hb.stop()
            buf.close()
            return self._finalize_terminal(db, claimed, FAILED, "provider_timeout", frm=RUNNING)
        except chat_stream.ProviderStreamError as exc:
            hb.stop()
            buf.close()
            return self._finalize_terminal(db, claimed, FAILED, _provider_reason(exc),
                                           frm=RUNNING)
        finally:
            hb.stop()

        if not streamed[0] and full_text and state["abort"] is None and not state["marker"]:
            safe = scanner.feed(full_text)
            if safe:
                clean_parts.append(safe)
                buf.publish(safe)
            if scanner.tripped:
                state["marker"] = True
                state["reason"] = "tool_resume_required"
        buf.close()

        if state["abort"] == "lease":
            return RunOutcome(claimed.run_id, "abandoned", "RunLeaseLost")
        if state["abort"] == "shutdown":
            return RunOutcome(claimed.run_id, "abandoned", "worker_shutdown")
        if state["abort"] == "session":
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
        if state["abort"] == "cancel":
            return self._consume_cancel(db, claimed, frm=RUNNING)  # NO assistant Message
        if state["marker"]:
            reason = state["reason"]
            db.execute(update(RunStep).where(RunStep.id == step.id).values(
                status="incomplete", error_code=reason, finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, reason, frm=RUNNING)

        final_text = "".join(clean_parts) or (full_text or "")
        if not final_text:
            db.execute(update(RunStep).where(RunStep.id == step.id).values(
                status="failed", error_code="empty_generation", finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, "empty_generation", frm=RUNNING)

        if has_hallucinated_web_marker(final_text, marker_map):
            db.execute(update(RunStep).where(RunStep.id == step.id).values(
                status="failed", error_code="citation_validation_failed", finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED,
                                           "citation_validation_failed", frm=RUNNING)

        web_citations = build_web_citations(final_text, marker_map)
        # local/document citations (unchanged [1]/[2] scheme) from the SAME
        # sealed context snapshot every chat run already reads. Mirrors the
        # chat path's own read_context_snapshot call exactly (see above,
        # ~line 1764): its docstring is explicit that a genuinely-absent
        # context is already handled internally (returns the canonical empty
        # snapshot, no exception) — anything that DOES raise here is a
        # present-but-malformed/corrupt sealed state, which must fail closed
        # rather than silently answer with a fabricated-empty citation set.
        try:
            ctx_snap = chat_stream.read_context_snapshot(db, run_row, submission_step)
        except Exception:  # noqa: BLE001 — ContextSnapshotError or a decrypt failure; fail closed
            db.execute(update(RunStep).where(RunStep.id == step.id).values(
                status="failed", error_code="context_snapshot_unreadable", finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED,
                                           "context_snapshot_unreadable", frm=RUNNING)
        local_citations = chat_stream.validate_citations(ctx_snap.get("citations"))
        all_citations = local_citations + web_citations

        if not web_citations:
            db.execute(update(RunStep).where(RunStep.id == step.id).values(
                status="failed", error_code="research_citations_missing", finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED,
                                           "research_citations_missing", frm=RUNNING)

        return self._finalize_deep_research_success(
            db, claimed, step, final_text, all_citations,
            sources_used=bool(web_citations), memories_used=bool(ctx_snap.get("memories_used")),
        )

    def _finalize_deep_research_success(self, db, claimed: "ClaimedRun", step,
                                        final_text: str, citations: list, *,
                                        sources_used: bool, memories_used: bool) -> "RunOutcome":
        """Mirrors ``_finalize_chat_success``'s atomicity exactly: lease +
        version CAS is the sole "who finalizes" authority — a stale worker
        can create neither a second Message nor a second success."""
        db.expire_all()
        try:
            self._precheck(db, claimed)
        except RunLeaseLost:
            return RunOutcome(claimed.run_id, "abandoned", "RunLeaseLost")
        except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
        if self._live_cancel_id(db, claimed.run_id) is not None:
            return self._consume_cancel(db, claimed, frm=RUNNING)

        fresh_step = db.query(RunStep).filter(RunStep.id == step.id).first()
        if fresh_step is not None and fresh_step.output_ref:
            try:
                if json.loads(fresh_step.output_ref).get("message_id"):
                    return self._finalize_terminal(db, claimed, SUCCEEDED, None, frm=RUNNING)
            except (ValueError, TypeError):
                pass

        msg = Message(session_id=fresh_step.session_id, role=MessageRole.assistant,
                      content=final_text, citations=json.dumps(citations, sort_keys=True),
                      created_at=_utcnow_aware())
        db.add(msg)
        db.flush()
        db.execute(update(RunStep).where(RunStep.id == step.id).values(
            status="succeeded", finished_at=self._now(),
            output_ref=json.dumps({"message_id": msg.id, "sources_used": sources_used,
                                   "memories_used": memories_used,
                                   "output_len": len(final_text)}, sort_keys=True),
        ))
        try:
            self._apply_transition(db, claimed=claimed, frm=RUNNING, to=SUCCEEDED)
        except (RunLeaseLost, ControlEventContention) as exc:
            db.rollback()
            return RunOutcome(claimed.run_id, "abandoned", type(exc).__name__)
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                           "output_persist_failed", frm=RUNNING)
        return RunOutcome(claimed.run_id, SUCCEEDED, None)

    # ---- MP-3C3D5C: durable multi-agent (researcher -> solution -> reviewer) ----

    def advance_multi_agent_runs(self) -> list["RunOutcome"]:
        """Drive claimable durable ``multi_agent`` runs one step. No-op
        unless the ONE authoritative readiness predicate is true — reused
        unchanged, never a second/duplicated gate."""
        from app.run_engine.multi_agent_readiness import multi_agent_executable

        if not multi_agent_executable():
            return []
        if self._stopping():
            return []
        now = self._now()
        with self._session() as db:
            ids = db.execute(
                select(Run.id)
                .where(Run.kind == _MULTI_AGENT_RUN_KIND)
                .where(Run.status.in_(tuple(_MULTI_AGENT_ADVANCEABLE)))
                .where(or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now))
                .order_by(Run.created_at.asc())
                .limit(int(settings.run_worker_recover_batch))
            ).scalars().all()
        return [self._advance_multi_agent_run(rid) for rid in ids]

    def _advance_multi_agent_run(self, run_id: str) -> "RunOutcome":
        from app.run_engine.multi_agent_orchestration import STEP_KIND_FOR_ROLE

        role_step_kinds = frozenset(STEP_KIND_FOR_ROLE.values())
        now = self._now()
        with self._session() as db:
            row = db.execute(
                select(Run.id, Run.version, Run.status, Run.kind, Run.user_id,
                       Run.tenant_id, Run.session_id, Run.auth_session_ref,
                       Run.lease_expires_at, Run.run_deadline_at)
                .where(Run.id == run_id)
            ).first()
            if row is None or row.kind != _MULTI_AGENT_RUN_KIND:
                return RunOutcome(run_id, "none", "not_multi_agent")
            if row.status in RUN_TERMINAL:
                return RunOutcome(run_id, row.status, "already_terminal")
            if row.status not in _MULTI_AGENT_ADVANCEABLE:
                return RunOutcome(run_id, "none", row.status)

            steps = (db.query(RunStep).filter(RunStep.run_id == run_id)
                     .order_by(RunStep.seq.asc()).all())
            latest = steps[-1] if steps else None

            expired = (
                row.lease_expires_at is not None
                and _aware(row.lease_expires_at) < now
            )
            if row.status == RUNNING and not expired:
                return RunOutcome(run_id, "running", "in_progress")  # a live worker owns it
            if row.status == RUNNING and expired:
                # A role's OWN step status of "running" is unprovable — the
                # committed pending->running proof means a provider call may
                # or may not have completed; NEVER re-invoked (F). Anything
                # else (no role step yet, or the latest role step already
                # "succeeded" with its artifact sealed, or "pending" —
                # crashed before the running-commit, never invoked) is safe
                # to reclaim and continue from.
                if (latest is not None and latest.kind in role_step_kinds
                        and latest.status == "running"):
                    return self._force_terminal_expired(row, OUTCOME_UNKNOWN, "outcome_unknown")

            claim_from = row.status
            lease = self._mint_lease()
            rc = db.execute(
                update(Run).where(
                    Run.id == run_id, Run.version == row.version,
                    Run.status == row.status,
                    or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now),
                ).values(
                    lease_owner=lease, lease_expires_at=now + self._lease_ttl(),
                    version=Run.version + 1,
                    run_deadline_at=func.coalesce(
                        Run.run_deadline_at,
                        now + _dt.timedelta(seconds=int(settings.multi_agent_total_deadline_s)),
                    ),
                    started_at=func.coalesce(Run.started_at, now), updated_at=now,
                )
            ).rowcount
            if rc != 1:
                db.rollback()
                return RunOutcome(run_id, "abandoned", "lease_race")
            db.commit()
            claimed = ClaimedRun(
                run_id=run_id, kind=row.kind, user_id=row.user_id,
                tenant_id=row.tenant_id, auth_session_ref=row.auth_session_ref,
                lease_owner=lease, version=row.version + 1, attempt=0,
            )

            if claim_from == QUEUED:
                try:
                    v = self._apply_transition(db, claimed=claimed, frm=QUEUED, to=RUNNING)
                    claimed = replace(claimed, version=v)
                except (RunLeaseLost, ControlEventContention) as exc:
                    db.rollback()
                    return RunOutcome(run_id, "abandoned", type(exc).__name__)
                db.commit()

            try:
                self._precheck(db, claimed)
            except RunLeaseLost:
                return RunOutcome(run_id, "abandoned", "RunLeaseLost")
            except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
            if self._live_cancel_id(db, claimed.run_id) is not None:
                return self._consume_cancel(db, claimed, frm=RUNNING)

            submission_step = steps[0] if steps else None
            if submission_step is None:
                return self._finalize_terminal(db, claimed, FAILED, "chat_input_error", frm=RUNNING)

            fresh_run = db.query(Run).filter(Run.id == run_id).first()
            total_deadline = _aware(fresh_run.run_deadline_at) or (
                now + _dt.timedelta(seconds=int(settings.multi_agent_total_deadline_s))
            )
            role_steps = {s.kind: s for s in steps if s.kind in role_step_kinds}
            return self._continue_multi_agent(
                db, claimed, run_row=fresh_run, submission_step=submission_step,
                role_steps=role_steps, total_deadline=total_deadline,
            )

    def _continue_multi_agent(self, db, claimed: ClaimedRun, *, run_row, submission_step,
                              role_steps: dict, total_deadline: _dt.datetime) -> "RunOutcome":
        """Advance the run to the next incomplete role, chaining straight
        through every already-ready role in THIS SAME call (mirrors
        ``_run_deep_research_acquisition`` chaining into synthesis) — the
        run's lease was just (re)minted, so no other sweep can steal an
        in-progress role mid-chain. Reaching the end with every role already
        ``succeeded`` is a pure, idempotent finalization resume — no
        provider is ever called again once the Reviewer's artifact exists."""
        from app.run_engine.multi_agent_artifacts import (
            AgentArtifactError, open_agent_artifact,
        )
        from app.run_engine.multi_agent_orchestration import (
            ROLE_ORDER, ROLE_REVIEWER, ROLE_RESEARCHER, ROLE_SOLUTION_SPECIALIST,
            STEP_KIND_FOR_ROLE,
        )

        prior_content: dict = {}
        for role in ROLE_ORDER:
            kind = STEP_KIND_FOR_ROLE[role]
            step = role_steps.get(kind)
            if step is not None and step.status == "succeeded":
                try:
                    artifact = open_agent_artifact(
                        db, step, expected_tenant_id=claimed.tenant_id,
                        expected_user_id=claimed.user_id, expected_run_id=claimed.run_id,
                        expected_role=role,
                    )
                except AgentArtifactError:
                    return self._finalize_terminal(db, claimed, FAILED,
                                                   "context_snapshot_unreadable", frm=RUNNING)
                prior_content[role] = artifact["content"]
                continue
            if step is not None and step.status == "running":
                # Defensive — `_advance_multi_agent_run` already routes an
                # expired-lease "running" latest step to outcome_unknown
                # before this is ever reached; a live (non-expired) lease
                # never reaches here either (claimed above under a fresh
                # lease). Unreachable in practice, fail closed if ever hit.
                return self._force_terminal_expired(run_row, OUTCOME_UNKNOWN, "outcome_unknown")
            # step is None (never created) or "pending" (crashed before the
            # running-commit, never invoked) — safe to (re)run this role.
            return self._run_multi_agent_role(
                db, claimed, role=role, run_row=run_row, submission_step=submission_step,
                existing_step=step, prior_content=prior_content, total_deadline=total_deadline,
            )

        reviewer_step = role_steps.get(STEP_KIND_FOR_ROLE[ROLE_REVIEWER])
        if reviewer_step is None:  # unreachable by construction; fail closed
            return self._finalize_terminal(db, claimed, FAILED, "chat_input_error", frm=RUNNING)
        return self._finalize_multi_agent_success(db, claimed, reviewer_step)

    def _run_multi_agent_role(self, db, claimed: ClaimedRun, *, role: str, run_row,
                              submission_step, existing_step, prior_content: dict,
                              total_deadline: _dt.datetime) -> "RunOutcome":
        """Run *role*'s ONE provider call end to end: pending -> running
        (durable invocation-start proof, attempt-once) -> private
        accumulation (NEVER streamed — L) -> marker fail-closed (J) ->
        citation fail-closed (K) -> sealed artifact + step succeeded in one
        commit (E) -> chain into the next role/finalization in THIS SAME
        call."""
        from app.run_engine import chat_stream
        from app.run_engine.multi_agent_artifacts import (
            AgentArtifactError, agent_artifact_output_ref, build_agent_artifact,
            extract_cited_local_markers, has_unknown_local_citation_marker,
            validate_and_seal_agent_artifact,
        )
        from app.run_engine.multi_agent_orchestration import (
            ROLE_REVIEWER, ROLE_RESEARCHER, ROLE_SOLUTION_SPECIALIST,
            STEP_KIND_FOR_ROLE, expected_parent_role, sequence_for_role,
        )
        from app.run_engine.multi_agent_prompts import build_role_input
        from app.run_engine.deep_research_citations import WEB_MARKER_PATTERN

        # Re-check BEFORE this role's own work begins — a cancel/lease-loss/
        # session-revocation that arrived between roles (this call may be
        # chained straight from a PRIOR role's own success, mirroring
        # `_run_deep_research_acquisition`'s identical re-check immediately
        # before it chains into synthesis) must be honored before a NEW
        # provider call starts, never only discovered mid-stream via the
        # in-stream `_guard()` below.
        try:
            self._precheck(db, claimed)
        except RunLeaseLost:
            return RunOutcome(claimed.run_id, "abandoned", "RunLeaseLost")
        except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
        if self._live_cancel_id(db, claimed.run_id) is not None:
            return self._consume_cancel(db, claimed, frm=RUNNING)

        step_kind = STEP_KIND_FOR_ROLE[role]
        step_seq = sequence_for_role(role) + 1
        progress_start = {
            ROLE_RESEARCHER: "agent_researching",
            ROLE_SOLUTION_SPECIALIST: "agent_solving",
            ROLE_REVIEWER: "agent_reviewing",
        }[role]
        progress_done = {
            ROLE_RESEARCHER: "agent_research_complete",
            ROLE_SOLUTION_SPECIALIST: "agent_solution_complete",
            ROLE_REVIEWER: None,  # Reviewer's completion IS finalization ("agent_finalizing")
        }[role]

        if existing_step is not None:
            step_id = existing_step.id
        else:
            step_id = str(uuid4())
            db.add(RunStep(
                id=step_id, run_id=claimed.run_id, tenant_id=claimed.tenant_id,
                user_id=claimed.user_id, session_id=run_row.session_id,
                seq=step_seq, kind=step_kind, engine="native", status="pending",
            ))
            db.commit()

        # DURABLE attempt-1 record: pending -> running, COMMIT. After this a
        # crash / lease loss => outcome_unknown for THIS role (never a 2nd
        # provider call for it) — F.
        db.execute(update(RunStep).where(RunStep.id == step_id, RunStep.status == "pending")
                   .values(status="running", started_at=self._now()))
        if not self._alloc_event(
            db, run_id=claimed.run_id, tenant_id=claimed.tenant_id,
            user_id=claimed.user_id, event_type="agent.progress",
            data=validate_event_data("agent.progress", {"code": progress_start}),
        ):
            db.rollback()
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                           "output_persist_failed", frm=RUNNING)
        db.commit()

        from app.run_engine.chat_stream import _open_submission_body

        try:
            body = _open_submission_body(db, run_row, submission_step)
        except Exception:  # noqa: BLE001
            return self._finalize_terminal(db, claimed, FAILED, "chat_input_error", frm=RUNNING)
        exec_snap = body.get("exec") if isinstance(body.get("exec"), dict) else {}
        original_request = body.get("message") or ""
        try:
            ctx_snap = chat_stream.read_context_snapshot(db, run_row, submission_step)
        except Exception:  # noqa: BLE001 — present-but-malformed sealed state; fail closed
            return self._finalize_terminal(db, claimed, FAILED,
                                           "context_snapshot_unreadable", frm=RUNNING)
        valid_local_markers = frozenset(
            c["marker"] for c in (ctx_snap.get("citations") or [])
            if isinstance(c, dict) and isinstance(c.get("marker"), str)
        )

        chat_input = build_role_input(
            role=role, provider=exec_snap.get("provider"), model=exec_snap.get("model"),
            original_request=original_request, context=ctx_snap,
            researcher_content=prior_content.get(ROLE_RESEARCHER),
            solution_content=prior_content.get(ROLE_SOLUTION_SPECIALIST),
        )

        clean_parts: list[str] = []
        scanner = chat_stream.MarkerScanner(chat_stream.ALL_WATCHED_MARKERS, capture=False)
        state = {"abort": None, "marker": False}
        tick = max(1, int(settings.run_worker_lease_ttl_s) // 3)
        hb = _ChatHeartbeat(self, claimed.run_id, claimed.lease_owner, interval_s=tick)
        role_deadline = min(
            total_deadline,
            self._now() + _dt.timedelta(seconds=int(settings.multi_agent_role_timeout_s)),
        )
        seen = [0]

        def _guard() -> None:
            if self._stopping():
                state["abort"] = "shutdown"
                raise chat_stream.StreamAborted()
            if hb.lease_lost:
                state["abort"] = "lease"
                raise chat_stream.StreamAborted()
            gdb = self._sf()
            try:
                grun = gdb.query(Run).filter(Run.id == claimed.run_id).first()
                if (grun is None or grun.status != RUNNING
                        or grun.lease_owner != claimed.lease_owner
                        or grun.version != claimed.version):
                    state["abort"] = "lease"
                    raise chat_stream.StreamAborted()
                if self._live_cancel_id(gdb, claimed.run_id) is not None:
                    state["abort"] = "cancel"
                    raise chat_stream.StreamAborted()
                try:
                    validate_session_binding(gdb, self._mcp_ctx(claimed), run=grun)
                except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                    state["abort"] = "session"
                    raise chat_stream.StreamAborted()
            finally:
                gdb.close()

        def _on_idle() -> None:
            if self._now() >= role_deadline:
                raise chat_stream.ProviderStreamTimeout()
            _guard()

        def on_token(tok: str) -> None:
            if not tok:
                return
            # NEVER published to TextEventBuffer here — L: intermediate (and
            # even Reviewer's own in-flight) output is private until this
            # role's full text passes marker + citation validation below.
            safe = scanner.feed(tok)
            if safe:
                clean_parts.append(safe)
            if scanner.tripped:
                state["marker"] = True
                raise chat_stream.StreamAborted()
            seen[0] += 1
            if seen[0] >= chat_stream._GUARD_EVERY_TOKENS:
                seen[0] = 0
                _guard()

        hb.start()
        provider = (self._chat_provider or chat_stream.default_chat_provider)(
            chat_input, db=db, user_id=claimed.user_id)
        full_text: Optional[str] = None
        try:
            full_text = chat_stream.run_stream(
                provider, chat_input, on_token=on_token, on_idle=_on_idle,
                deadline=role_deadline, now_fn=self._now, poll_interval=tick)
        except chat_stream.StreamAborted:
            pass
        except chat_stream.ProviderStreamTimeout:
            hb.stop()
            db.execute(update(RunStep).where(RunStep.id == step_id).values(
                status="incomplete", error_code="provider_timeout", finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, "provider_timeout", frm=RUNNING)
        except chat_stream.ProviderStreamError as exc:
            hb.stop()
            _preason = _provider_reason(exc)
            db.execute(update(RunStep).where(RunStep.id == step_id).values(
                status="incomplete", error_code=_preason, finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, _preason, frm=RUNNING)
        finally:
            hb.stop()

        if not clean_parts and full_text and state["abort"] is None and not state["marker"]:
            safe = scanner.feed(full_text)
            if safe:
                clean_parts.append(safe)
            if scanner.tripped:
                state["marker"] = True

        if state["abort"] == "lease":
            return RunOutcome(claimed.run_id, "abandoned", "RunLeaseLost")
        if state["abort"] == "shutdown":
            return RunOutcome(claimed.run_id, "abandoned", "worker_shutdown")
        if state["abort"] == "session":
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
        if state["abort"] == "cancel":
            return self._consume_cancel(db, claimed, frm=RUNNING)  # NO artifact, NO assistant Message
        scanner.finish()
        if state["marker"] or scanner.truncated:
            db.execute(update(RunStep).where(RunStep.id == step_id).values(
                status="incomplete", error_code="tool_resume_required", finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, "tool_resume_required", frm=RUNNING)

        final_text = "".join(clean_parts)
        if not final_text:
            db.execute(update(RunStep).where(RunStep.id == step_id).values(
                status="failed", error_code="empty_generation", finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED, "empty_generation", frm=RUNNING)

        # K — fail closed on ANY unknown local marker or ANY [W#] marker
        # BEFORE extracting/persisting anything (never silently drop a bad
        # citation and continue).
        if (WEB_MARKER_PATTERN.search(final_text) is not None
                or has_unknown_local_citation_marker(final_text, valid_local_markers)):
            db.execute(update(RunStep).where(RunStep.id == step_id).values(
                status="failed", error_code="citation_validation_failed", finished_at=self._now()))
            return self._finalize_terminal(db, claimed, FAILED,
                                           "citation_validation_failed", frm=RUNNING)
        citation_markers = extract_cited_local_markers(final_text, valid_local_markers)

        # Parent lineage id is the PRIOR role's REAL RunStep.id — re-derived
        # directly from the DB (the authoritative source), never threaded
        # through as caller state.
        parent_role = expected_parent_role(role)
        parent_step_id = None
        if parent_role is not None:
            parent_step_row = (
                db.query(RunStep)
                .filter(RunStep.run_id == claimed.run_id,
                        RunStep.kind == STEP_KIND_FOR_ROLE[parent_role])
                .first()
            )
            parent_step_id = parent_step_row.id if parent_step_row is not None else None

        try:
            artifact = build_agent_artifact(
                role=role, parent_artifact_id=parent_step_id,
                content=final_text, summary=final_text[:2000],
                claims=[], citation_markers=citation_markers,
                assumptions=[], uncertainties=[],
            )
            validated, payload = validate_and_seal_agent_artifact(
                db, tenant_id=claimed.tenant_id, user_id=claimed.user_id,
                run_id=claimed.run_id, step_id=step_id, artifact=artifact,
            )
        except AgentArtifactError:
            db.rollback()
            db.execute(update(RunStep).where(RunStep.id == step_id).values(
                status="failed", error_code="chat_input_error", finished_at=self._now()))
            db.commit()
            return self._finalize_terminal(db, claimed, FAILED, "chat_input_error", frm=RUNNING)

        db.execute(update(RunStep).where(RunStep.id == step_id).values(
            status="succeeded", finished_at=self._now(),
            output_payload_id=payload.id,
            output_ref=agent_artifact_output_ref(validated),
        ))
        if progress_done is not None:
            if not self._alloc_event(
                db, run_id=claimed.run_id, tenant_id=claimed.tenant_id,
                user_id=claimed.user_id, event_type="agent.progress",
                data=validate_event_data("agent.progress", {"code": progress_done}),
            ):
                db.rollback()
                return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                               "output_persist_failed", frm=RUNNING)
        try:
            db.commit()  # sealed payload + step succeeded (+ progress event), atomically
        except Exception:  # noqa: BLE001
            db.rollback()
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                           "output_persist_failed", frm=RUNNING)

        # Continue straight into the next role (or finalization) IN THIS
        # SAME call — the run's lease was just minted, so no other sweep can
        # steal it mid-chain (mirrors the deep-research acquisition ->
        # synthesis chain). Re-query fresh from the DB (the authoritative
        # source) rather than patching the caller's in-memory dict.
        role_step_kinds = frozenset(STEP_KIND_FOR_ROLE.values())
        next_role_steps = {
            s.kind: s
            for s in db.query(RunStep).filter(RunStep.run_id == claimed.run_id).all()
            if s.kind in role_step_kinds
        }
        return self._continue_multi_agent(
            db, claimed, run_row=run_row, submission_step=submission_step,
            role_steps=next_role_steps, total_deadline=total_deadline,
        )

    def _finalize_multi_agent_success(self, db, claimed: ClaimedRun, reviewer_step) -> "RunOutcome":
        """Turn an ALREADY-sealed, ALREADY-validated Reviewer artifact into
        the one final assistant Message. Mirrors
        ``_finalize_chat_success``/``_finalize_deep_research_success``'s
        atomicity exactly — lease + version CAS is the sole "who finalizes"
        authority, so a stale worker can create neither a second Message nor
        a second success. IDEMPOTENT (checks ``output_ref.message_id``
        first): safe to call again on ANY resumed run whose Reviewer step
        already succeeded — no provider is ever invoked here.

        N — no atomic join exists between ``TextEventBuffer``'s own
        short-lived per-flush commits and this function's Message/step/run
        commit (an existing, accepted primitive limitation — see
        ``text_events.TextEventBuffer``'s own session-per-flush design,
        unchanged here). Ordering instead: publish the validated text burst
        FIRST (own commit(s)), then the atomic Message + output_ref +
        run-succeeded commit. Documented crash window: a crash between the
        two lets a reconnecting client see the validated final text
        replayed via SSE while the run is still ``running`` — the NEXT
        resume calls this SAME idempotent function again (no new provider
        call, message_id check above still gates a duplicate Message).

        Because the burst spans SEVERAL independently committed chunks, that
        window is closed by exact-prefix continuation rather than by a
        boolean "already published?" flag: the persisted text is read back
        (``text_events.read_persisted_text``) and only the exact missing
        suffix is published, so a crash after ANY subset of the chunks
        loses no text and duplicates none. The final Message always carries
        the full validated ``final_text``, never the partial replay."""
        from app.run_engine import chat_stream
        from app.run_engine.multi_agent_orchestration import ROLE_REVIEWER
        from app.run_engine.multi_agent_artifacts import open_agent_artifact
        from app.run_engine.text_events import TextEventBuffer, read_persisted_text

        db.expire_all()
        try:
            self._precheck(db, claimed)
        except RunLeaseLost:
            return RunOutcome(claimed.run_id, "abandoned", "RunLeaseLost")
        except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
            return self._finalize_terminal(db, claimed, FAILED, "session_revoked", frm=RUNNING)
        if self._live_cancel_id(db, claimed.run_id) is not None:
            return self._consume_cancel(db, claimed, frm=RUNNING)

        fresh_step = db.query(RunStep).filter(RunStep.id == reviewer_step.id).first()
        if fresh_step is not None and fresh_step.output_ref:
            try:
                if json.loads(fresh_step.output_ref).get("message_id"):
                    return self._finalize_terminal(db, claimed, SUCCEEDED, None, frm=RUNNING)
            except (ValueError, TypeError):
                pass

        run_row = db.query(Run).filter(Run.id == claimed.run_id).first()
        submission_step = (
            db.query(RunStep)
            .filter(RunStep.run_id == claimed.run_id, RunStep.seq == 1)
            .first()
        )
        try:
            reviewer_artifact = open_agent_artifact(
                db, fresh_step, expected_tenant_id=claimed.tenant_id,
                expected_user_id=claimed.user_id, expected_run_id=claimed.run_id,
                expected_role=ROLE_REVIEWER,
            )
            ctx_snap = chat_stream.read_context_snapshot(db, run_row, submission_step)
        except Exception:  # noqa: BLE001 — unreadable sealed state; fail closed
            return self._finalize_terminal(db, claimed, FAILED,
                                           "context_snapshot_unreadable", frm=RUNNING)

        final_text = reviewer_artifact["content"]
        citations = _multi_agent_citations_for_message(ctx_snap, reviewer_artifact["citation_markers"])

        # Crash-safe publication — EXACT-PREFIX continuation, never a boolean
        # "any text event exists?" guard. TextEventBuffer commits EACH capped
        # chunk in its OWN session, so a crash mid-publish leaves a committed
        # PARTIAL prefix: skipping publication entirely would drop the suffix
        # forever, and re-publishing from zero would duplicate the prefix.
        # The persisted text is always an exact prefix of final_text — this
        # function is a multi_agent run's ONLY text producer (no role output
        # ever reaches a text event, J/L) and the validated Reviewer artifact
        # is immutable — so continuing at its exact length is deterministic
        # and stays correct across repeated crashes. Persisted text that is
        # NOT a prefix is corrupt/unexpected state: fail closed rather than
        # show text the Reviewer never produced.
        try:
            persisted_text = read_persisted_text(db, claimed.run_id)
        except ValueError:
            return self._finalize_terminal(db, claimed, FAILED,
                                           "text_recovery_unreadable", frm=RUNNING)
        if not final_text.startswith(persisted_text):
            return self._finalize_terminal(db, claimed, FAILED,
                                           "text_recovery_mismatch", frm=RUNNING)
        unpublished = final_text[len(persisted_text):]
        if unpublished:
            buf = TextEventBuffer(self._sf, claimed.run_id, tenant_id=claimed.tenant_id,
                                  user_id=claimed.user_id, now_fn=self._now)
            buf.publish(unpublished)
            buf.close()

        msg = Message(session_id=fresh_step.session_id, role=MessageRole.assistant,
                      content=final_text, citations=json.dumps(citations, sort_keys=True),
                      created_at=_utcnow_aware())
        db.add(msg)
        db.flush()
        if not self._alloc_event(
            db, run_id=claimed.run_id, tenant_id=claimed.tenant_id,
            user_id=claimed.user_id, event_type="agent.progress",
            data=validate_event_data("agent.progress", {"code": "agent_finalizing"}),
        ):
            db.rollback()
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                           "output_persist_failed", frm=RUNNING)
        db.execute(update(RunStep).where(RunStep.id == fresh_step.id).values(
            output_ref=json.dumps(
                {**json.loads(fresh_step.output_ref), "message_id": msg.id}, sort_keys=True),
        ))
        try:
            self._apply_transition(db, claimed=claimed, frm=RUNNING, to=SUCCEEDED)
        except (RunLeaseLost, ControlEventContention) as exc:
            db.rollback()
            return RunOutcome(claimed.run_id, "abandoned", type(exc).__name__)
        try:
            db.commit()  # Message + step output_ref + run succeeded, atomically
        except Exception:  # noqa: BLE001
            db.rollback()
            return self._finalize_terminal(db, claimed, OUTCOME_UNKNOWN,
                                           "output_persist_failed", frm=RUNNING)
        return RunOutcome(claimed.run_id, SUCCEEDED, None)

    def poll_once(self) -> list[RunOutcome]:
        """One sweep: recover expired-lease generic runs, advance every durable
        MCP run one step, then claim + execute one queued generic run."""
        outcomes = self.recover_expired()
        outcomes.extend(self.advance_mcp_runs())
        outcomes.extend(self.advance_chat_runs())   # MP-3B (no-op unless the flag is on)
        outcomes.extend(self.advance_deep_research_runs())  # MP-3C3D4C (no-op unless ready)
        outcomes.extend(self.advance_multi_agent_runs())  # MP-3C3D5C (no-op unless ready)
        claimed = self.claim_next_queued()
        if claimed is not None:
            outcomes.append(self.execute(claimed))
        return outcomes

    async def run_forever(self) -> None:
        """Bare poll loop — retained for standalone / MP-1B use. The FastAPI
        lifespan does NOT call this; it uses :class:`RunWorkerSupervisor`, which
        wraps the same :meth:`poll_once` with bounded backoff, a cooperative stop
        signal, fatal-error fail-closed, and a non-secret status object. Refuses
        to start unless BOTH the master flag and the worker flag are on."""
        if not (settings.run_engine_enabled and settings.run_worker_enabled):
            logger.info(
                "RunWorker not started (run_engine_enabled=%s run_worker_enabled=%s)",
                settings.run_engine_enabled, settings.run_worker_enabled,
            )
            return
        self._stopped = False
        logger.info("RunWorker %s started", self.worker_id)
        while not self._stopped and not self._stopping():
            try:
                await asyncio.to_thread(self.poll_once)
            except Exception as exc:  # noqa: BLE001 — a sweep must not kill the loop
                logger.error("RunWorker %s sweep failed: %s", self.worker_id,
                             type(exc).__name__)  # class only — no traceback / text
            await asyncio.sleep(int(settings.run_worker_poll_interval_s))
        logger.info("RunWorker %s stopped", self.worker_id)

    def stop(self) -> None:
        self._stopped = True


__all__ = [
    "RunWorker",
    "ClaimedRun",
    "RunOutcome",
    "HandlerContext",
    "HandlerResult",
    "HandlerSpec",
    "register_handler",
    "unregister_handler",
    "clear_handlers",
    "resolve_handler",
]
