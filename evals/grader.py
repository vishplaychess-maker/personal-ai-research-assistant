"""Optional LLM-rubric grader. Never raises; degrades to 'skipped'."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from evals.schema import Task

_EVALS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _EVALS_DIR.parent


@dataclass
class GradeResult:
    status: str          # "pass" | "fail" | "skipped"
    score: int | None
    detail: str = ""


def grade(task: Task, payload: dict, *, offline: bool) -> GradeResult:
    if task.grader is None:
        return GradeResult("skipped", None, "no grader on task")
    if offline:
        return GradeResult("skipped", None, "offline mode: no LLM available")
    rubric_path = _EVALS_DIR / task.grader.rubric
    if not rubric_path.is_file():
        return GradeResult("skipped", None, f"rubric not found: {task.grader.rubric}")
    rubric = rubric_path.read_text(encoding="utf-8")
    try:
        backend = str(_REPO_ROOT / "backend")
        if backend not in sys.path:
            sys.path.insert(0, backend)
        os.environ.setdefault("PYTHONPATH", backend)
        from app.services.evaluation_service import evaluate_response  # guarded
    except Exception as exc:  # ImportError, config errors, anything
        return GradeResult("skipped", None, f"grader import failed: {exc!r}")
    try:
        result = evaluate_response(
            response=payload.get("response_text", ""),
            query=task.input,
            system_prompt=rubric,
        )
        score = result.get("confidence")
    except Exception as exc:
        return GradeResult("skipped", None, f"grader call failed: {exc!r}")
    if not isinstance(score, int):
        return GradeResult("skipped", None, "grader returned no numeric score")
    ok = score >= task.grader.threshold
    return GradeResult("pass" if ok else "fail", score, f"{score} vs threshold {task.grader.threshold}")
