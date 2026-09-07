"""Dataset schema + JSONL loader for the Thunder AI eval harness."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from evals.provenance import EVIDENCE_LEVELS


class EvalDatasetError(Exception):
    """Raised when evals/dataset.jsonl (or a test dataset) is malformed."""


class Assert(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: str


class Grader(BaseModel):
    rubric: str          # path relative to the evals/ directory
    threshold: int = Field(ge=0, le=100)


class Task(BaseModel):
    id: str
    category: str
    title: str
    input: str
    asserts: list[Assert] = []
    grader: Grader | None = None
    blocked_by: str | None = None
    # The strongest evidence level this task is DESIGNED to reach once its
    # dependencies exist (one of evals.provenance.EVIDENCE_LEVELS). A result
    # may land at a weaker level; that is reported, never silently upgraded.
    evidence_level: str = "synthetic"
    # Dotted reference to the real product function / route / parser this task
    # claims to evaluate, e.g. "app.services.browser_agent.assert_url_allowed"
    # or "POST /api/terminal/execute". Free text; used by the in-process /
    # backend adapters and shown in the report.
    product_target: str | None = None


def load_dataset(path: str | Path) -> list[Task]:
    """Parse a JSONL dataset. Blank lines and lines starting with '#' are ignored."""
    path = Path(path)
    tasks: list[Task] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalDatasetError(f"{path}:{lineno}: invalid JSON: {exc.msg}") from exc
        try:
            task = Task.model_validate(obj)
        except Exception as exc:  # pydantic ValidationError -> our error type
            raise EvalDatasetError(f"{path}:{lineno}: {exc}") from exc
        if task.id in seen:
            raise EvalDatasetError(f"{path}:{lineno}: duplicate task id {task.id!r}")
        if task.evidence_level not in EVIDENCE_LEVELS:
            raise EvalDatasetError(
                f"{path}:{lineno}: {task.id} has evidence_level "
                f"{task.evidence_level!r} not in {EVIDENCE_LEVELS}"
            )
        seen.add(task.id)
        tasks.append(task)
    return tasks
