"""MP-4C - pre-dispatch capacity reservation + the started-before-capacity contradiction.

Two things are proven here:

1. THE CONTRADICTION (current application path). gateway.dispatch commits
   DispatchRecord(outcome='started') BEFORE it invokes the executor callback, so a
   busy broker can only refuse AFTER a started row already exists.
2. THE RESERVATION CONTRACT (executor side). Capacity is reserved BEFORE dispatch,
   so a busy refusal creates ZERO started rows, and reservation alone never runs code.

Boundary stated, not hidden: the application DB and the broker SQLite are separate
stores. There is NO atomic transaction across them; the crash intervals are
enumerated in tests below rather than papered over.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

from app.models.models import DispatchRecord
from executor_service.nonce_store import NonceStore, NonceStoreError
from executor_service.quota import QuotaBroker
from executor_service.reservations import (ReservationBook, STATUS_BUSY,
                                           STATUS_RESERVATION_EXISTS, STATUS_RESERVED)
from executor_service import runner as runner_mod
from executor_service.runner import RunOutcome
from executor_service.service import (STATUS_RESERVATION_MISSING, ExecutionService)
from mp4c_contract import grant as g
from tests.run_engine_helpers import mk_engine, mk_session

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = b"reservation-key-0123456789"
CODE = "print('reserved')"
CANARY = "MP4C_RES_CANARY_" + "r" * 12


@pytest.fixture
def db():
    eng = mk_engine()
    session = mk_session(eng)
    yield session
    session.close()


# ── 1. THE CONTRADICTION ────────────────────────────────────────────


def test_gateway_commits_started_before_invoking_the_executor_callback():
    """Structural proof from the gateway source: the started row is committed
    BEFORE the callback that would discover a busy broker."""
    src = open(os.path.join(REPO_ROOT, "backend", "app", "approval", "gateway.py"),
               encoding="utf-8").read()
    declare = src.index('rec = DispatchRecord(')
    commit = src.index("db.commit()", declare)
    callback = src.index("execute_mcp()", commit)
    assert declare < commit < callback, (declare, commit, callback)
    assert 'outcome="started"' in src[declare:commit]


def test_a_busy_broker_can_only_refuse_after_started(db):
    """CURRENT order: commit started, THEN ask the broker -> a started row survives
    for work that never ran."""
    now = int(time.time())
    book = ReservationBook()
    assert book.reserve(key="occupier", user_id=7, expires_at=now + 120,
                        now=now)[0] is True          # the user is already saturated

    db.add(DispatchRecord(id=str(uuid.uuid4()), tool_call_id="tc-late",
                          tenant_id=None, user_id=7, action_hash="a" * 64,
                          outcome="started"))
    db.commit()                                       # gateway.dispatch does this FIRST

    ok, status = book.reserve(key="bound-late", user_id=7, expires_at=now + 120,
                              now=now)                # only now is capacity discovered
    assert ok is False and status == STATUS_BUSY
    row = db.query(DispatchRecord).filter_by(tool_call_id="tc-late").one()
    assert row.outcome == "started", "the contradiction: started for work that cannot run"


def test_predispatch_reservation_creates_zero_started_rows(db):
    """CORRECTED order: reserve FIRST, so a busy refusal never reaches dispatch."""
    now = int(time.time())
    book = ReservationBook()
    assert book.reserve(key="occupier", user_id=7, expires_at=now + 120,
                        now=now)[0] is True
    ok, status = book.reserve(key="bound", user_id=7, expires_at=now + 120, now=now)
    assert ok is False and status == STATUS_BUSY
    assert db.query(DispatchRecord).count() == 0, "no started row may ever exist"


# ── 2. THE RESERVATION BOOK ─────────────────────────────────────────


def test_reserve_then_consume_is_exactly_once():
    now = int(time.time())
    book = ReservationBook()
    assert book.reserve(key="k", user_id=7, expires_at=now + 120, now=now) == (
        True, STATUS_RESERVED)
    assert book.consume("k", now) is not None
    assert book.consume("k", now) is None, "a reservation is consumed once"
    book.finish("k")
    assert book.broker.active_total() == 0


def test_a_duplicate_reservation_for_the_same_bound_action_is_refused():
    now = int(time.time())
    book = ReservationBook()
    assert book.reserve(key="k", user_id=7, expires_at=now + 120, now=now)[0] is True
    ok, status = book.reserve(key="k", user_id=7, expires_at=now + 120, now=now)
    assert ok is False and status == STATUS_RESERVATION_EXISTS


def test_reservations_honour_one_per_user_and_two_globally():
    now = int(time.time())
    book = ReservationBook()
    assert book.reserve(key="a", user_id=1, expires_at=now + 120, now=now)[0] is True
    assert book.reserve(key="b", user_id=2, expires_at=now + 120, now=now)[0] is True
    assert book.reserve(key="c", user_id=3, expires_at=now + 120,
                        now=now)[1] == STATUS_BUSY
    assert book.reserve(key="a2", user_id=1, expires_at=now + 120,
                        now=now)[0] is False


def test_concurrent_reservation_of_the_same_bound_action_yields_one_winner():
    now = int(time.time())
    book = ReservationBook()
    wins = []
    lock = threading.Lock()
    start = threading.Barrier(16)

    def worker():
        start.wait()
        ok, _status = book.reserve(key="same", user_id=7, expires_at=now + 120,
                                   now=now)
        if ok:
            with lock:
                wins.append(1)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1, wins
    book.finish("same")


def test_release_frees_the_slot_and_drops_the_reservation():
    now = int(time.time())
    book = ReservationBook()
    assert book.reserve(key="k", user_id=7, expires_at=now + 120, now=now)[0] is True
    assert book.release("k", now) is True
    assert book.broker.active_total() == 0
    assert book.peek("k", now) is None
    assert book.release("k", now) is False, "releasing twice is a no-op, not a crash"


def test_an_expired_reservation_is_dropped_and_the_slot_returned():
    now = int(time.time())
    book = ReservationBook()
    assert book.reserve(key="k", user_id=7, expires_at=now + 10, now=now)[0] is True
    assert book.peek("k", now + 11) is None
    assert book.broker.active_total() == 0
    assert book.prune(now + 11) == 0, "already dropped by the peek"


def test_a_broker_restart_loses_reservations_and_fails_closed():
    """In-process by design: after a restart there is no reservation, so the
    execution grant fails CLOSED with zero containers - never a duplicate run."""
    now = int(time.time())
    before = ReservationBook()
    assert before.reserve(key="k", user_id=7, expires_at=now + 120, now=now)[0] is True
    after_restart = ReservationBook()          # a NEW process
    assert after_restart.peek("k", now) is None
    assert after_restart.live_count() == 0
    assert after_restart.broker.active_total() == 0


# ── 3. THE SERVICE CONTRACT ─────────────────────────────────────────


class _Recorder:
    def __init__(self):
        self.plans = []

    def run(self, plan):
        self.plans.append(plan)
        return RunOutcome(status=runner_mod.STATUS_OK, output="OK")


def _claims(purpose_ok=True, **over):
    now = int(time.time())
    sha, ln = g.code_sha256(CODE)
    raw = {"v": g.GRANT_VERSION, "jti": uuid.uuid4().hex, "iat": now,
           "exp": now + 120, "user_id": 7, "tenant_id": None, "run_id": "run-1",
           "step_id": "step-1", "tool_call_id": "tc-1", "approval_id": "ap-1",
           "action_hash": "a" * 64, "code_sha256": sha, "code_len": ln}
    raw.update(over)
    return g.GrantClaims.from_claims(raw)


def _svc(tmp_path, *, runner=None, book=None, store=None):
    return ExecutionService(
        key=KEY, nonce_store=store or NonceStore(str(tmp_path / "n.db")),
        runner=runner or _Recorder(), preflight=lambda: (True, None),
        reservations=book)


def _reserve_token(**over):
    return g.issue(_claims(**over), KEY, purpose=g.PURPOSE_RESERVE)


def _exec_token(**over):
    return g.issue(_claims(**over), KEY, purpose=g.PURPOSE_EXECUTE)


def test_the_reserve_and_release_routes_build(tmp_path):
    """REGRESSION (real service defect, observed live): a request-body model defined
    INSIDE create_app cannot be resolved from FastAPI's module-scope annotation
    namespace, so route registration raised NameError: name 'ReservationBody' is not
    defined. The app never built, the container crash-looped (restarting, 4
    restarts) and every request was refused. Readiness itself had passed."""
    from executor_service.app import create_app
    app = create_app(service=_svc(tmp_path), availability_fn=lambda: (True, None))
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert {"/healthz", "/v1/execute", "/v1/reserve", "/v1/release"} <= paths, paths


def test_reserve_then_execute_launches_exactly_one_container(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    assert svc.reserve(grant=_reserve_token()).ok is True
    out = svc.execute(grant=_exec_token(), code=CODE, run_id="run-1")
    assert out.ok is True and len(rec.plans) == 1
    assert svc.broker.active_total() == 0, "the slot is returned after the run"


def test_reservation_alone_never_runs_code(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    assert svc.reserve(grant=_reserve_token()).ok is True
    assert rec.plans == [], "reserving must never launch a container"
    assert svc.nonce_store.count() == 0, "reserving must not touch the nonce store"


def test_a_reserve_credential_cannot_be_used_as_an_execution_credential(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    out = svc.execute(grant=_reserve_token(), code=CODE, run_id="run-1")
    assert out.ok is False and out.status == g.BAD_SIGNATURE
    assert rec.plans == []


def test_execute_without_a_reservation_fails_closed(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    out = svc.execute(grant=_exec_token(), code=CODE, run_id="run-1")
    assert out.ok is False and out.status == STATUS_RESERVATION_MISSING
    assert rec.plans == []


def test_a_reservation_for_a_different_bound_action_does_not_match(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    # The bound key is pinned by the server-owned tool_call_id (action_hash is
    # deliberately excluded: it differs between the proposal and the gateway).
    assert svc.reserve(grant=_reserve_token(tool_call_id="tc-other")).ok is True
    out = svc.execute(grant=_exec_token(), code=CODE, run_id="run-1")
    assert out.ok is False and out.status == STATUS_RESERVATION_MISSING
    assert rec.plans == []


def test_a_different_code_does_not_match_the_reservation(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    other = "print('other')"
    sha, ln = g.code_sha256(other)
    assert svc.reserve(grant=_reserve_token(code_sha256=sha, code_len=ln)).ok is True
    out = svc.execute(grant=_exec_token(), code=CODE, run_id="run-1")
    assert out.ok is False and out.status == STATUS_RESERVATION_MISSING
    assert rec.plans == []


def test_a_denied_reservation_is_released_and_execution_then_fails_closed(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    token = _reserve_token()
    assert svc.reserve(grant=token).ok is True
    assert svc.broker.active_total() == 1
    released = svc.release(grant=token)
    assert released.ok is True and svc.broker.active_total() == 0
    out = svc.execute(grant=_exec_token(), code=CODE, run_id="run-1")
    assert out.ok is False and out.status == STATUS_RESERVATION_MISSING
    assert rec.plans == []


def test_a_busy_reservation_is_refused_with_zero_starts(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    assert svc.reserve(grant=_reserve_token(user_id=7,
                                            tool_call_id="tc-1")).ok is True
    busy = svc.reserve(grant=_reserve_token(user_id=7,
                                            tool_call_id="tc-2"))
    assert busy.ok is False and busy.status == STATUS_BUSY
    assert rec.plans == [], "a busy refusal must create zero containers"


def test_execute_after_the_reservation_expires_fails_closed(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    now = int(time.time())
    # The CREDENTIAL still verifies (exp is inside the +-30 s skew) while the
    # RESERVATION itself is already stale. Deterministic, unlike a sleep, because
    # expiry is compared with integer seconds.
    token = _reserve_token(iat=now - 10, exp=now - 1)
    assert svc.reserve(grant=token).ok is True
    out = svc.execute(grant=_exec_token(), code=CODE, run_id="run-1")
    assert out.ok is False and out.status == STATUS_RESERVATION_MISSING
    assert rec.plans == []
    assert svc.broker.active_total() == 0, "the stale slot is reclaimed on the way"


def test_a_nonce_store_failure_still_fails_closed_with_zero_starts(tmp_path):
    class Broken:
        def consume(self, jti, *, expires_at, now):
            raise NonceStoreError("boom")

    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec, store=Broken())
    assert svc.reserve(grant=_reserve_token()).ok is True
    out = svc.execute(grant=_exec_token(), code=CODE, run_id="run-1")
    assert out.ok is False
    assert out.status == "executor_nonce_store_unavailable"
    assert rec.plans == []
    # The reservation is deliberately NOT released here: the backend can retry the
    # same bound action with a FRESH execution grant (new jti) and reuse the slot.
    # It is reclaimed at expiry. Failing closed never means leaking a duplicate run.
    assert svc.broker.active_total() == 1
    svc.nonce_store = NonceStore(str(tmp_path / "ok.db"))
    retry = svc.execute(grant=g.issue(_claims(jti="fresh-" + "f" * 8), KEY),
                        code=CODE, run_id="run-1")
    assert retry.ok is True and len(rec.plans) == 1
    assert svc.broker.active_total() == 0


def test_the_same_execution_grant_twice_consumes_the_reservation_once(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    assert svc.reserve(grant=_reserve_token()).ok is True
    token = _exec_token()
    assert svc.execute(grant=token, code=CODE, run_id="run-1").ok is True
    second = svc.execute(grant=token, code=CODE, run_id="run-1")
    assert second.ok is False
    assert second.status in ("executor_replay_rejected", STATUS_RESERVATION_MISSING)
    assert len(rec.plans) == 1, "never a duplicate launch"


# ── 4. CRASH INTERVALS (stated, not claimed atomic) ─────────────────


def test_crash_after_reserve_before_dispatch_leaves_a_slot_held_until_expiry():
    """App DB crash between reserve and dispatch: the reservation and its slot
    survive until expiry, then are reclaimed. Zero containers, zero started rows."""
    now = int(time.time())
    book = ReservationBook()
    assert book.reserve(key="k", user_id=7, expires_at=now + 5, now=now)[0] is True
    assert book.broker.active_total() == 1
    assert book.prune(now + 6) == 1
    assert book.broker.active_total() == 0


def test_crash_after_dispatch_started_before_execute_fails_closed():
    """DispatchRecord.started committed, then the process dies: the grant is still
    valid, so the executor runs it ONCE. If the executor also dies, the nonce may
    or may not be consumed - and that is exactly why it is never auto-retried."""
    rec = _Recorder()
    book = ReservationBook()
    svc = ExecutionService(key=KEY,
                           nonce_store=NonceStore.__new__(NonceStore), runner=rec,
                           preflight=lambda: (True, None), reservations=book)
    svc.nonce_store.consume = lambda *a, **k: True       # stands in for a fresh store
    assert book.reserve(key=g.bound_action_key(_claims()), user_id=7,
                        expires_at=int(time.time()) + 120, now=int(time.time()))[0]
    out = svc.execute(grant=_exec_token(), code=CODE, run_id="run-1")
    assert out.ok is True and len(rec.plans) == 1


def test_no_atomic_transaction_is_claimed_between_the_two_stores():
    """The application DB and the broker SQLite are separate stores. The code must
    not pretend otherwise: no two-phase commit, no distributed transaction."""
    for rel in ("executor_service/reservations.py", "executor_service/service.py"):
        src = open(os.path.join(REPO_ROOT, rel), encoding="utf-8").read()
        for forbidden in ("two_phase", "2pc", "prepare_commit", "xa_"):
            assert forbidden not in src, (rel, forbidden)
