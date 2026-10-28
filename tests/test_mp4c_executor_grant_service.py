"""MP-4C - executor service with a signed grant: tests FIRST (RED before wiring).

Hermetic: no container is launched and no docker call is made (the pre-flight and
the runner are injected). Decision assertions pinned here:
  * verification happens BEFORE quota reservation or container creation;
  * the jti is consumed atomically BEFORE the container starts;
  * same signed request -> ONE start; concurrent replay -> ONE start;
  * a broker restart on the SAME persistent volume -> ZERO additional starts;
  * an unavailable nonce store -> ZERO starts (fail closed);
  * a crash after consumption -> no retry, outcome unknown;
  * daemon / image lost after startup -> bounded refusal before any start.
"""
from __future__ import annotations

import threading
import time

import pytest

from executor_service import runner as runner_mod
from executor_service.nonce_store import NonceStore, NonceStoreError
from executor_service.service import (STATUS_REPLAY,
                                       STATUS_RESERVATION_MISSING,
                                       ExecutionService)
from mp4c_contract import grant as g

KEY = b"service-grant-key-0123456789"
CODE = "print('hello')"
CANARY = "MP4C_SVC_GRANT_CANARY_" + "s" * 12


class _Recorder:
    def __init__(self, outcome=None, blocker=None):
        self.plans = []
        self.outcome = outcome or runner_mod.RunOutcome(status=runner_mod.STATUS_OK,
                                                        output="OK")
        self.blocker = blocker

    def run(self, plan):
        self.plans.append(plan)
        if self.blocker is not None:
            self.blocker()
        return self.outcome


class _BoomRunner:
    def __init__(self):
        self.plans = []

    def run(self, plan):
        self.plans.append(plan)
        raise RuntimeError("simulated crash after nonce consumption")


def _claims(**over):
    now = int(time.time())
    sha, ln = g.code_sha256(CODE)
    base_claims = {
        "v": g.GRANT_VERSION, "jti": "jti-" + "z" * 16, "iat": now, "exp": now + 120,
        "user_id": 7, "tenant_id": None, "run_id": "run-1", "step_id": "step-1",
        "tool_call_id": "tc-1", "approval_id": "appr-1", "action_hash": "a" * 64,
        "code_sha256": sha, "code_len": ln,
    }
    base_claims.update(over)
    return base_claims


def _token(**over):
    return g.issue(g.GrantClaims.from_claims(_claims(**over)), KEY)


def _svc(tmp_path, *, runner=None, store=None, preflight=None, ready=None,
         key=KEY):
    return ExecutionService(
        key=key,
        nonce_store=store if store is not None else NonceStore(str(tmp_path / "n.db")),
        runner=runner or _Recorder(),
        preflight=preflight if preflight is not None else (lambda: (True, None)),
        ready=ready if ready is not None else (lambda: True))


def _call(svc, token=None, code=CODE, run_id="run-1", *, reserve=True):
    """Reserve the token's BOUND ACTION first, exactly as the backend now does BEFORE
    gateway dispatch, then execute. Tests that specifically exercise the
    missing/unmatched reservation pass reserve=False."""
    if reserve and isinstance(token, str):
        try:
            claims = g.parse_and_verify(token, KEY, code=code)
        except g.GrantError:
            claims = None
        if claims is not None:
            svc.reserve(grant=g.issue(claims, KEY,
                                      purpose=g.PURPOSE_RESERVE))
    return svc.execute(grant=token, code=code, run_id=run_id)


# -- the happy path --------------------------------------------------


def test_a_valid_grant_starts_exactly_one_container(tmp_path):
    rec = _Recorder()
    out = _call(_svc(tmp_path, runner=rec), _token())
    assert out.ok is True and out.status == runner_mod.STATUS_OK
    assert len(rec.plans) == 1
    assert rec.plans[0].stdin_code == CODE.encode("utf-8")


def test_two_distinct_users_both_execute(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    a = _call(svc, _token(user_id=7, jti="jti-" + "a" * 16))
    b = _call(svc, _token(user_id=8, jti="jti-" + "b" * 16), run_id="run-2")
    assert (a.ok, b.ok) == (True, True)
    assert len(rec.plans) == 2


# -- replay ----------------------------------------------------------


def test_the_same_signed_request_twice_starts_once(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    token = _token()
    first = _call(svc, token)
    second = _call(svc, token)
    assert first.ok is True
    assert second.ok is False and second.status == "executor_replay_rejected"
    assert len(rec.plans) == 1


#: Hang guards, NOT performance allowances. The barrier previously had NO timeout
#: (an unbounded wait) and the joins discarded their return value, so a starved or
#: deadlocked thread silently turned into a SHORT `results` list and the failure
#: surfaced as a confusing count mismatch. These bounds make the same conditions
#: fail loudly and self-describing instead.
_BARRIER_HANG_GUARD_S = 30
_JOIN_HANG_GUARD_S = 10


def test_a_duplicate_after_its_reservation_finished_is_a_replay_not_a_capacity_miss(
        tmp_path):
    """DETERMINISTIC schedule for the intermittent concurrency failure.

    ONE identical grant completes IN FULL - its reservation is FINISHED and gone -
    BEFORE the seven late duplicates reach the reservation lookup. The contract
    (see test_the_same_signed_request_twice_starts_once and the service docstring)
    is that a duplicate is a REPLAY: its jti is already consumed. It must never be
    reported as a capacity miss merely because the reservation has been finished.

    `reserve=False` is the point: no fresh reservation is created, so the only
    thing left to observe is the classification of an already-consumed grant.
    """
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    token = _token()

    first = _call(svc, token)                       # reserve + execute + finish
    assert first.ok is True and first.status == runner_mod.STATUS_OK
    assert len(rec.plans) == 1
    assert svc.nonce_store.count() == 1, "the jti is consumed exactly once"

    statuses = [_call(svc, token, reserve=False).status for _ in range(7)]

    # Captured evidence for the classification contract.
    assert statuses == [STATUS_REPLAY] * 7, statuses
    assert len(rec.plans) == 1, "zero ADDITIONAL starts"
    assert svc.nonce_store.count() == 1, "still exactly one nonce row"


def test_a_missing_reservation_never_consumes_a_fresh_grant(tmp_path):
    """The rule the classification fix MUST preserve: an unseen jti with no live
    reservation is a capacity miss, and the grant is left UNBURNED so the same
    action can be retried with a fresh reservation."""
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    token = _token()

    miss = _call(svc, token, reserve=False)
    assert miss.ok is False and miss.status == STATUS_RESERVATION_MISSING, miss.status
    assert len(rec.plans) == 0
    assert svc.nonce_store.count() == 0, "a capacity miss must NOT burn the grant"

    # ...and the SAME token still works once a reservation exists.
    ok = _call(svc, token)
    assert ok.ok is True and ok.status == runner_mod.STATUS_OK
    assert len(rec.plans) == 1
    assert svc.nonce_store.count() == 1


def test_concurrent_identical_grants_start_once(tmp_path):
    release = threading.Event()
    started = threading.Event()

    def blocker():
        started.set()
        release.wait(10)

    rec = _Recorder(blocker=blocker)
    svc = _svc(tmp_path, runner=rec)
    token = _token()
    results = []
    errors = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        # A raised exception in a thread used to vanish: the thread simply ended
        # without appending, so a REAL defect looked like a short list. Capture it.
        try:
            barrier.wait(timeout=_BARRIER_HANG_GUARD_S)
            out = _call(svc, token)
        except BaseException as exc:  # noqa: BLE001 - re-raised by the assertions
            with lock:
                errors.append("%s: %s" % (type(exc).__name__, exc))
            return
        with lock:
            results.append(out.status)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    started.wait(10)
    release.set()
    for t in threads:
        t.join(_JOIN_HANG_GUARD_S)

    # Diagnostics first: an incomplete run must say WHY it was incomplete, so a
    # scheduling/resource hiccup is never confused with a concurrency defect (and
    # a genuine defect is never masked). Counts below are UNCHANGED.
    stuck = [i for i, t in enumerate(threads) if t.is_alive()]
    assert not errors, "thread errors: %r" % (errors,)
    assert not stuck, "threads still running after join: %r" % (stuck,)
    assert len(results) == 8, "incomplete outcome list: %r" % (results,)
    assert len(rec.plans) == 1, rec.plans
    assert results.count(runner_mod.STATUS_OK) == 1, results
    assert results.count("executor_replay_rejected") == 7, results


def test_replay_is_rejected_after_a_broker_restart_on_the_same_volume(tmp_path):
    path = str(tmp_path / "shared.db")
    token = _token()
    first = _Recorder()
    svc1 = _svc(tmp_path, runner=first, store=NonceStore(path))
    assert _call(svc1, token).ok is True
    assert len(first.plans) == 1
    svc1.nonce_store.close()

    second = _Recorder()
    svc2 = _svc(tmp_path, runner=second, store=NonceStore(path))   # RESTART
    out = _call(svc2, token)
    assert out.ok is False and out.status == "executor_replay_rejected"
    assert len(second.plans) == 0, "zero ADDITIONAL starts after a restart"
    svc2.nonce_store.close()


def test_a_different_volume_cannot_reject_the_replay(tmp_path):
    """Documents the boundary honestly: the volume IS the durability."""
    token = _token()
    svc1 = _svc(tmp_path, store=NonceStore(str(tmp_path / "v1.db")))
    assert _call(svc1, token).ok is True
    other = _Recorder()
    svc2 = _svc(tmp_path, runner=other, store=NonceStore(str(tmp_path / "v2.db")))
    assert _call(svc2, token).ok is True
    assert len(other.plans) == 1


# -- fail closed on the store ---------------------------------------


def test_an_unavailable_nonce_store_yields_zero_starts(tmp_path):
    class Broken:
        def consume(self, jti, *, expires_at, now):
            raise NonceStoreError("boom")

    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec, store=Broken())
    out = _call(svc, _token())
    assert out.ok is False and out.status == "executor_nonce_store_unavailable"
    assert rec.plans == []


def test_a_corrupt_nonce_store_yields_zero_starts(tmp_path):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a database" * 10)
    store = NonceStore.__new__(NonceStore)
    store.__init__ = lambda *a, **k: None      # never used; consume will fail
    with pytest.raises(NonceStoreError):
        NonceStore(str(path))


# -- authentication / binding refused before anything happens --------


def test_missing_grant_is_refused_before_any_start(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    for bad in (None, "", "   ", "not-a-grant"):
        out = _call(svc, bad)
        assert out.ok is False and out.status == g.MALFORMED
    assert rec.plans == []


def test_wrong_key_grant_is_refused(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    forged = g.issue(g.GrantClaims.from_claims(_claims()), b"a-totally-different-key")
    out = _call(svc, forged)
    assert out.ok is False and out.status == g.BAD_SIGNATURE
    assert rec.plans == []


def test_expired_grant_is_refused(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    now = int(time.time())
    out = _call(svc, _token(iat=now - 300, exp=now - 200))
    assert out.ok is False and out.status == g.EXPIRED
    assert rec.plans == []


def test_altered_code_is_refused_before_any_start(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    sha_b, _len_b = g.code_sha256("print('different')")
    out = _call(svc, _token(code_sha256=sha_b,
                            code_len=g.code_sha256("print('different')")[1]))
    assert out.ok is False and out.status == g.CODE_BINDING_MISMATCH
    assert rec.plans == []


def test_cross_user_substitution_is_refused(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    signed_for_7 = _token(user_id=7)
    substituted = g.GrantClaims.from_claims(_claims(user_id=8))
    with pytest.raises(g.GrantError) as exc:
        g.verify_claims(signed_for_7, substituted, KEY, code=CODE)
    assert exc.value.code == g.BAD_SIGNATURE
    out = _call(svc, g.issue(substituted, b"wrong-key-wrong-key-1"))
    assert out.ok is False and out.status == g.BAD_SIGNATURE
    assert rec.plans == []


def test_tenant_substitution_is_refused(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    signed = _token(tenant_id="t-1")
    substituted = g.GrantClaims.from_claims(_claims(tenant_id="t-2"))
    with pytest.raises(g.GrantError) as exc:
        g.verify_claims(signed, substituted, KEY, code=CODE)
    assert exc.value.code == g.BAD_SIGNATURE
    assert rec.plans == []


# -- availability after startup --------------------------------------


def test_daemon_unavailable_after_startup_refuses_before_any_start(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec,
               preflight=lambda: (False, "executor_daemon_unavailable"))
    out = _call(svc, _token())
    assert out.ok is False and out.status == "executor_daemon_unavailable"
    assert rec.plans == []
    assert svc.broker.active_total() == 0


def test_image_missing_after_startup_refuses_before_any_start(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec,
               preflight=lambda: (False, "executor_image_not_provisioned"))
    out = _call(svc, _token())
    assert out.ok is False and out.status == "executor_image_not_provisioned"
    assert rec.plans == []


def test_not_ready_refuses_before_any_start(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec, ready=lambda: False)
    out = _call(svc, _token())
    assert out.ok is False and out.status == "executor_not_ready"
    assert rec.plans == []


def test_preflight_runs_before_the_nonce_is_consumed(tmp_path):
    """A refused request must not burn the grant (the store stays empty)."""
    rec = _Recorder()
    store = NonceStore(str(tmp_path / "n.db"))
    svc = _svc(tmp_path, runner=rec, store=store,
               preflight=lambda: (False, "executor_daemon_unavailable"))
    assert _call(svc, _token()).ok is False
    assert store.count() == 0
    store.close()


# -- crash after consumption -----------------------------------------


def test_a_crash_after_consumption_is_not_retried_and_reports_unknown(tmp_path):
    rec = _BoomRunner()
    svc = _svc(tmp_path, runner=rec)
    token = _token()
    out = _call(svc, token)
    assert out.ok is False
    assert out.status == runner_mod.STATUS_OUTCOME_UNKNOWN
    assert len(rec.plans) == 1
    # The crashed run returns its slot immediately (the reservation is finished).
    assert svc.broker.active_total() == 0
    retry = _call(svc, token)          # a FRESH reservation for the same action
    assert retry.ok is False and retry.status == "executor_replay_rejected"
    assert len(rec.plans) == 1, "a crash must never be retried"
    # The retry's own reservation stays live for the backend to release or for it
    # to expire; a refusal never silently consumes committed capacity.
    assert svc.broker.active_total() == 1


# -- the store never holds code --------------------------------------


def test_the_nonce_db_never_contains_the_code(tmp_path):
    store = NonceStore(str(tmp_path / "n.db"))
    svc = _svc(tmp_path, store=store)
    code = "print('" + CANARY + "')"
    sha, ln = g.code_sha256(code)
    assert _call(svc, _token(code_sha256=sha, code_len=ln), code=code).ok is True
    store.close()
    blob = open(str(tmp_path / "n.db"), "rb").read()
    assert CANARY.encode() not in blob
    assert b"print(" not in blob
