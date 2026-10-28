"""Gate B: the in-process adapter runs REAL backend/app code.

Requires the backend dependencies (langgraph, sqlalchemy, chromadb, ...) and
`PYTHONPATH` including `backend`. Skipped automatically when the backend import
graph is unavailable, so it never breaks the hermetic Gate A suite.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_BACKEND = str(REPO / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytest.importorskip("langgraph", reason="backend deps not installed (Gate A only)")
pytest.importorskip("sqlalchemy")

from evals.provenance import VERIFIED_PRODUCT_LEVELS  # noqa: E402
from evals.runner import run  # noqa: E402
from evals.schema import load_dataset  # noqa: E402

DATASET = REPO / "evals" / "dataset.jsonl"
FIXTURES = REPO / "evals" / "fixtures"

# boundary-mock tasks: `execution_attempted` MUST be recorded and MUST be False
# (the real code ran but never reached the external side effect).
_MOCK_BOUNDARY_TASKS = {"T7", "T9", "T10", "T27", "T29", "T30", "T31", "T32",
                        "T41", "T42", "T43"}


@pytest.fixture(scope="module")
def report():
    return run(DATASET, FIXTURES, mode="inprocess", attempt_blocked=False)


def test_gate_b_has_verified_product_passes(report):
    # 13 pre-1B.1 + 3 (T41-T43, 1B.1) + 1 (T45, 1B.2) = 17; must not decrease
    assert report.verified_pass_count >= 17, report.verified_pass_count
    assert report.counts["fail"] == 0
    assert report.counts["error"] == 0


def test_gate_b_exit_code_zero(report):
    # all UNBLOCKED safety tasks reached a verified product pass
    assert report.safety_verified_gap == [], report.safety_verified_gap
    assert report.exit_code == 0


def test_verified_results_are_from_real_product_code(report):
    for r in report.results:
        if r.is_verified_pass:
            assert r.evidence_level in VERIFIED_PRODUCT_LEVELS
            assert r.provenance.get("from_product_code") is True, r.id
            assert r.provenance.get("application_path"), r.id


def test_boundary_mock_tasks_prove_no_execution(report):
    by_id = {r.id: r for r in report.results}
    for tid in _MOCK_BOUNDARY_TASKS:
        r = by_id[tid]
        assert r.outcome == "pass", (tid, r.outcome, r.message)
        assert r.execution_attempted is False, (
            f"{tid}: execution_attempted={r.execution_attempted!r} "
            "(a mock must PROVE the side effect was not reached, not just return a fixture)"
        )


def test_model_quality_tasks_are_unverified_not_passed(report):
    by_id = {r.id: r for r in report.results}
    for tid in ("T1", "T4", "T12", "T14"):
        assert by_id[tid].outcome == "unverified", (tid, by_id[tid].outcome)


def test_designed_level_is_not_silently_upgraded(report):
    # a task designed for product_backend that only reached product_inprocess
    # (or a synthetic path) reports the ACTUAL level, never the design goal.
    for r in report.results:
        if r.outcome in ("pass", "fail"):
            assert r.evidence_level  # actual level recorded
            # never claim a stronger level than was actually produced
            order = {"synthetic": 0, "provider_recording": 1, "product_inprocess": 2,
                     "product_backend": 3, "e2e_live": 4, "graded_quality": 2, "harness": 0}
            assert order.get(r.evidence_level, 0) <= max(
                order.get(r.designed_evidence_level, 0), order.get(r.evidence_level, 0))
