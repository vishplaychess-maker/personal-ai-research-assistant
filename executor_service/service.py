"""MP-4C - the executor service orchestration boundary (SIGNED-GRANT API).

Order is fixed and fail-closed:

  1. verify the backend credential, the bound identity and the EXACT code bytes;
  2. refuse while the service is not ready (startup gate);
  3. cheap availability pre-flight (daemon + pinned image) - BEFORE quota;
  4. consume the grant's jti ATOMICALLY - BEFORE quota or any container;
  5. reserve quota (1 active/user, 2 globally);
  6. start exactly one container, and release the slot on every path.

The static token map is NOT used here: one token per user cannot represent
arbitrary authenticated users. Identity arrives inside the signed grant, which
only a holder of the shared secret can produce.

Logging policy: bounded non-secret scalars only. The grant, the key, the code and
the child's raw output are never logged, stored or returned.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from executor_service.contract import (CONTAINER_PREFIX, DEFAULT_OWNER,
                                       MAX_CODE_BYTES, ExecutionPlan, build_plan)
from executor_service.nonce_store import NonceStoreError
from executor_service.quota import QuotaBroker
from executor_service.reservations import ReservationBook
from executor_service.runner import (ContainerRunner, RunOutcome, STATUS_OK,
                                     STATUS_OUTCOME_UNKNOWN)
from mp4c_contract import grant as grant_contract

logger = logging.getLogger("executor_service")

STATUS_NOT_READY = "executor_not_ready"
STATUS_REPLAY = "executor_replay_rejected"
STATUS_NONCE_UNAVAILABLE = "executor_nonce_store_unavailable"
STATUS_BUSY = "executor_quota_exceeded"
STATUS_REQUEST_REJECTED = "executor_request_rejected"
STATUS_CODE_REJECTED = "executor_code_rejected"
#: A reservation is a CAPACITY claim that must exist BEFORE gateway dispatch.
STATUS_RESERVATION_MISSING = "executor_reservation_missing"
STATUS_RESERVED = "executor_reserved"
STATUS_RESERVATION_EXISTS = "executor_reservation_exists"
STATUS_RELEASED = "executor_released"

#: A caller-supplied run id must be a bounded, restricted-charset token so it can
#: never smuggle arbitrary text (let alone source) into a log line.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

_LOG_FIELDS = ("event", "run_id", "user_id", "status", "code_bytes", "duration_ms",
               "container", "reason")


@dataclass(frozen=True)
class ExecutionOutcome:
    ok: bool
    status: str
    output: str = ""
    truncated: bool = False


class ExecutionService:
    """Single-instance executor orchestration over signed grants."""

    def __init__(self, *, key: bytes, nonce_store,
                 runner: Optional[ContainerRunner] = None,
                 broker: Optional[QuotaBroker] = None,
                 owner: str = DEFAULT_OWNER,
                 max_code_bytes: int = MAX_CODE_BYTES,
                 ready: Optional[Callable[[], bool]] = None,
                 preflight: Optional[Callable[[], tuple]] = None,
                 reservations: Optional[ReservationBook] = None) -> None:
        self._key = key
        self.nonce_store = nonce_store
        self.runner = runner or ContainerRunner()
        self.broker = broker or QuotaBroker()
        # Shares the SAME broker, so a reservation and an execution can never
        # double-count a slot.
        self.reservations = (reservations if reservations is not None
                             else ReservationBook(broker=self.broker))
        self.owner = owner
        self.max_code_bytes = int(max_code_bytes)
        self._ready = ready or (lambda: True)
        self._preflight = preflight or (lambda: (True, None))

    def _log(self, **kw) -> None:
        safe = {k: v for k, v in kw.items() if k in _LOG_FIELDS}
        logger.info("executor_request %s",
                    " ".join("%s=%s" % (k, safe[k]) for k in sorted(safe)))

    def _code_ok(self, code: object) -> bool:
        return (isinstance(code, str) and bool(code.strip())
                and len(code.encode("utf-8")) <= self.max_code_bytes)

    def reserve(self, *, grant: object) -> ExecutionOutcome:
        """PRE-DISPATCH capacity reservation for a bound action.

        Reserving NEVER runs code: there is no argv, no container and the nonce
        store is untouched. A refusal here is what lets the backend avoid
        committing DispatchRecord(outcome='started') for work it cannot run.
        """
        try:
            claims = grant_contract.parse_and_verify(
                grant, self._key, purpose=grant_contract.PURPOSE_RESERVE)
        except grant_contract.GrantError as exc:
            self._log(event="rejected", status=exc.code, reason="reserve_grant")
            return ExecutionOutcome(False, exc.code)
        if not self._ready():
            return ExecutionOutcome(False, STATUS_NOT_READY)
        ok, reason = self._preflight()
        if not ok:
            self._log(event="rejected", status=reason, reason="reserve_preflight")
            return ExecutionOutcome(False, reason or STATUS_NOT_READY)
        reserved, status = self.reservations.reserve(
            key=grant_contract.bound_action_key(claims), user_id=claims.user_id,
            expires_at=claims.exp, now=int(time.time()))
        self._log(event="reserved" if reserved else "rejected",
                  status=status, user_id=claims.user_id)
        return ExecutionOutcome(reserved, status)

    def release(self, *, grant: object) -> ExecutionOutcome:
        """Abandon a reservation (approval denied or cancelled). Frees the slot."""
        try:
            claims = grant_contract.parse_and_verify(
                grant, self._key, purpose=grant_contract.PURPOSE_RESERVE)
        except grant_contract.GrantError as exc:
            return ExecutionOutcome(False, exc.code)
        freed = self.reservations.release(grant_contract.bound_action_key(claims),
                                          int(time.time()))
        self._log(event="released" if freed else "rejected", status=STATUS_RELEASED,
                  user_id=claims.user_id)
        return ExecutionOutcome(bool(freed), STATUS_RELEASED)

    def execute(self, *, grant: object, code: object,
                run_id: object) -> ExecutionOutcome:
        # 1. credential + identity + EXACT code binding, before anything else.
        try:
            claims = grant_contract.parse_and_verify(grant, self._key, code=code)
        except grant_contract.GrantError as exc:
            self._log(event="rejected", status=exc.code, reason="grant")
            return ExecutionOutcome(False, exc.code)

        if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id or ""):
            self._log(event="rejected", status=STATUS_REQUEST_REJECTED,
                      user_id=claims.user_id, reason="run_id_shape")
            return ExecutionOutcome(False, STATUS_REQUEST_REJECTED)

        if not self._code_ok(code) or claims.code_len > self.max_code_bytes:
            size = len(code.encode("utf-8")) if isinstance(code, str) else -1
            self._log(event="rejected", status=STATUS_CODE_REJECTED,
                      user_id=claims.user_id, run_id=run_id, code_bytes=size)
            return ExecutionOutcome(False, STATUS_CODE_REJECTED)

        # 2. startup readiness gate.
        if not self._ready():
            self._log(event="rejected", status=STATUS_NOT_READY,
                      user_id=claims.user_id, run_id=run_id, reason="service_unready")
            return ExecutionOutcome(False, STATUS_NOT_READY)

        # 3. cheap availability pre-flight, BEFORE quota and before the nonce:
        #    a refused request must not burn the grant.
        ok, reason = self._preflight()
        if not ok:
            self._log(event="rejected", status=reason,
                      user_id=claims.user_id, run_id=run_id, reason="preflight")
            return ExecutionOutcome(False, reason or STATUS_NOT_READY)

        # 4. the reservation for this bound action must exist and still be live.
        #    PEEK first (non-destructive) so a capacity miss does NOT burn the grant:
        #    the backend can retry the same action with a fresh reservation.
        bound_key = grant_contract.bound_action_key(claims)
        if self.reservations.peek(bound_key, int(time.time())) is None:
            # TWO different reasons can leave no live reservation, and they must
            # not share one status. A duplicate whose reservation has ALREADY been
            # finished (the winner's `finally`) is a REPLAY - its jti was consumed
            # by the first request - and reporting it as a capacity miss both
            # misleads the backend and breaks the replay contract. The check is
            # NON-DESTRUCTIVE: an unseen jti stays a capacity miss whose grant is
            # NOT burned, so the same action remains retryable with a fresh
            # reservation (and no container is ever started on either path).
            try:
                already_consumed = self.nonce_store.seen(claims.jti)
            except NonceStoreError:
                self._log(event="rejected", status=STATUS_NONCE_UNAVAILABLE,
                          user_id=claims.user_id, run_id=run_id,
                          reason="nonce_store")
                return ExecutionOutcome(False, STATUS_NONCE_UNAVAILABLE)
            if already_consumed:
                self._log(event="rejected", status=STATUS_REPLAY,
                          user_id=claims.user_id, run_id=run_id, reason="replay")
                return ExecutionOutcome(False, STATUS_REPLAY)
            self._log(event="rejected", status=STATUS_RESERVATION_MISSING,
                      user_id=claims.user_id, run_id=run_id,
                      reason="no_matching_reservation")
            return ExecutionOutcome(False, STATUS_RESERVATION_MISSING)

        # 5. consume the jti ATOMICALLY, before quota or any container start.
        try:
            consumed = self.nonce_store.consume(claims.jti, expires_at=claims.exp,
                                                now=int(time.time()))
        except NonceStoreError:
            self._log(event="rejected", status=STATUS_NONCE_UNAVAILABLE,
                      user_id=claims.user_id, run_id=run_id, reason="nonce_store")
            return ExecutionOutcome(False, STATUS_NONCE_UNAVAILABLE)
        if not consumed:
            self._log(event="rejected", status=STATUS_REPLAY,
                      user_id=claims.user_id, run_id=run_id, reason="replay")
            return ExecutionOutcome(False, STATUS_REPLAY)

        # 6. atomically CONSUME that reservation. The slot was acquired BEFORE the
        #    gateway committed DispatchRecord.started, so a busy broker can no longer
        #    leave a started row behind. A lost race fails closed with zero
        #    containers.
        if self.reservations.consume(bound_key, int(time.time())) is None:
            self._log(event="rejected", status=STATUS_RESERVATION_MISSING,
                      user_id=claims.user_id, run_id=run_id,
                      reason="no_matching_reservation")
            return ExecutionOutcome(False, STATUS_RESERVATION_MISSING)

        name = CONTAINER_PREFIX + uuid.uuid4().hex[:10]
        started = time.monotonic()
        try:
            plan: ExecutionPlan = build_plan(code, name=name, owner=self.owner)
            try:
                outcome: RunOutcome = self.runner.run(plan)
            except Exception as exc:  # noqa: BLE001
                # The jti is ALREADY consumed, so this is never retried. Whether
                # the container ran is unknown, and that is what we report.
                self._log(event="unknown", run_id=run_id, user_id=claims.user_id,
                          status=STATUS_OUTCOME_UNKNOWN, container=name,
                          reason=type(exc).__name__)
                return ExecutionOutcome(False, STATUS_OUTCOME_UNKNOWN)
            self._log(event="finished", run_id=run_id, user_id=claims.user_id,
                      status=outcome.status, code_bytes=len(plan.stdin_code),
                      duration_ms=int((time.monotonic() - started) * 1000),
                      container=name)
            return ExecutionOutcome(ok=outcome.status == STATUS_OK,
                                    status=outcome.status,
                                    output=outcome.output,
                                    truncated=outcome.truncated)
        finally:
            # The reservation owns the slot for the whole run; finishing it both
            # drops the record and returns the capacity.
            self.reservations.finish(bound_key)
