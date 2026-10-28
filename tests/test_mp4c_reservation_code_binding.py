"""MP-4C - cross-grant CODE binding between a reservation and an execution grant.

A reservation is held for the BOUND ACTION, whose key includes the code digest and
byte length (mp4c_contract.grant.bound_action_key). These tests isolate the
cross-grant binding: every other identity (tenant, user, Run, step, ToolCall,
approval) is IDENTICAL, and ONLY the code differs. The execution grant is validly
signed and matches its OWN presented bytes, so a refusal can only come from the
cross-grant comparison.
"""
from __future__ import annotations

from executor_service.service import STATUS_RESERVATION_MISSING
from mp4c_contract import grant as g
from tests.test_mp4c_predispatch_reservation import (CODE, KEY, _Recorder, _exec_token,
                                                     _reserve_token, _svc)

CODE_A = "print('A')"
CODE_B = "print('B')"


def _pair(code, **over):
    sha, ln = g.code_sha256(code)
    over.setdefault("code_sha256", sha)
    over.setdefault("code_len", ln)
    return _reserve_token(**over), _exec_token(**over)


def test_the_two_grant_types_share_an_identical_claim_set_pair():
    """Isolation precondition: the reserve and execute grants differ ONLY in their
    signing purpose, so any refusal below is about the CODE, not the claim set."""
    ra, ea = _pair(CODE_A)
    ca = g.parse_and_verify(ra, KEY, purpose=g.PURPOSE_RESERVE)
    ea_claims = g.parse_and_verify(ea, KEY, code=CODE_A)
    assert ca.to_claims() | {"jti": None, "iat": None, "exp": None} == \
        ea_claims.to_claims() | {"jti": None, "iat": None, "exp": None}


def test_a_reservation_for_code_a_does_not_authorize_code_b(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    reserve_a, _exec_a = _pair(CODE_A)
    assert svc.reserve(grant=reserve_a).ok is True
    assert svc.broker.active_total() == 1

    # A SEPARATELY and VALIDLY signed execution grant for code B, matching its own
    # presented bytes and signed hash.
    _reserve_b, exec_b = _pair(CODE_B)
    claims_b = g.parse_and_verify(exec_b, KEY, code=CODE_B)
    assert claims_b.code_sha256 == g.code_sha256(CODE_B)[0]
    assert claims_b.code_len == g.code_sha256(CODE_B)[1]

    out = svc.execute(grant=exec_b, code=CODE_B, run_id="run-1")

    assert out.ok is False and out.status == STATUS_RESERVATION_MISSING
    assert rec.plans == [], "zero container/runner calls"

    # The exact mechanism: the bound-action key carries the code digest + length,
    # so a different code cannot match the held reservation.
    claims_a = g.parse_and_verify(reserve_a, KEY, purpose=g.PURPOSE_RESERVE)
    assert g.bound_action_key(claims_a) != g.bound_action_key(claims_b)

    # The refusal is non-destructive: A's reservation is still live and A still runs.
    assert svc.broker.active_total() == 1
    _reserve_a2, exec_a2 = _pair(CODE_A)
    assert svc.execute(grant=exec_a2, code=CODE_A, run_id="run-1").ok is True
    assert len(rec.plans) == 1


def test_the_reverse_mismatch_is_also_refused(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    reserve_b, _exec_b = _pair(CODE_B)
    assert svc.reserve(grant=reserve_b).ok is True

    _reserve_a, exec_a = _pair(CODE_A)
    out = svc.execute(grant=exec_a, code=CODE_A, run_id="run-1")
    assert out.ok is False and out.status == STATUS_RESERVATION_MISSING
    assert rec.plans == []


def test_the_same_code_control_is_accepted_and_runs_once(tmp_path):
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    reserve_a, exec_a = _pair(CODE_A)
    assert svc.reserve(grant=reserve_a).ok is True
    out = svc.execute(grant=exec_a, code=CODE_A, run_id="run-1")
    assert out.ok is True and len(rec.plans) == 1
    assert rec.plans[0].stdin_code == CODE_A.encode("utf-8")


def test_the_default_code_control_still_works(tmp_path):
    """Control on the shared fixture code, so the pair helpers are not special."""
    rec = _Recorder()
    svc = _svc(tmp_path, runner=rec)
    reserve, exec_token = _pair(CODE)
    assert svc.reserve(grant=reserve).ok is True
    assert svc.execute(grant=exec_token, code=CODE, run_id="run-1").ok is True
    assert len(rec.plans) == 1
