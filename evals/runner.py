"""Eval harness runner: evaluate tasks, aggregate, report, exit code, CLI.

Evidence model (Phase 1A closure)
---------------------------------
A result is never a bare "pass". It carries an *evidence level* saying where the
response came from:

  synthetic           - an authored regression fixture; NO product code ran
  provider_recording  - a provider API was called directly, no product path
  product_inprocess   - a real backend function ran in-process, side effects
                        mocked only at the external boundary
  product_backend     - a running backend HTTP endpoint served the response
  e2e_live            - full running product + live model
  graded_quality      - human/model graded (overlay)

Only `product_inprocess`, `product_backend`, `e2e_live` count as
"the real Thunder AI product path executed" (VERIFIED_PRODUCT_LEVELS).

Outcomes: pass | fail | blocked | error | unverified.
`unverified` = we could not obtain product evidence (placeholder fixture, or a
model-quality task with no model available). It is never a pass and never a fail.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from evals.asserts import run_assert
from evals.grader import grade
from evals.provenance import (
    EVALUATOR_VERSION,
    PROMPT_VERSION,
    AdapterUnavailable,
    Provenance,
    VERIFIED_PRODUCT_LEVELS,
    provenance_defaults,
    redact,
    rubric_version,
    runtime_fingerprint,
    vcs_provenance,
)

# Re-exported for callers that import it from here (kept for compatibility).
__all__ = ["AdapterUnavailable"]
from evals.schema import Task, load_dataset

SAFETY_CATEGORIES = {"tool_approvals_safety", "approval_gateway", "budget_policy"}
MODES = ("offline", "inprocess", "backend")


@dataclass
class TaskResult:
    id: str
    category: str
    outcome: str                 # pass | fail | blocked | error | unverified
    failure_class: str = "none"  # none | assertion | grader | infrastructure | placeholder | no_evidence
    blocked_by: str | None = None
    evidence_level: str = ""     # ACTUAL level the evidence reached (not the task's design goal)
    designed_evidence_level: str = ""
    execution_attempted: bool | None = None  # for boundary-mock tasks: did real code try the side effect?
    provenance: dict = field(default_factory=dict)
    provenance_missing: list[str] = field(default_factory=list)
    assert_details: list[str] = field(default_factory=list)
    grader_detail: str = ""
    message: str = ""

    @property
    def is_verified_pass(self) -> bool:
        return self.outcome == "pass" and self.evidence_level in VERIFIED_PRODUCT_LEVELS

    @property
    def is_synthetic_pass(self) -> bool:
        return self.outcome == "pass" and self.evidence_level == "synthetic"


# ── payload sourcing per mode ───────────────────────────────────────────


def _load_fixture_payload(fixtures_dir: Path, task_id: str) -> dict:
    fp = fixtures_dir / f"{task_id}.json"
    return json.loads(fp.read_text(encoding="utf-8"))


def _source_payload(task: Task, *, mode: str, fixtures_dir: Path):
    """Return (payload, provenance_dict). Raises FileNotFoundError / json errors
    for offline; AdapterUnavailable / Exception for adapters."""
    if mode == "offline":
        payload = _load_fixture_payload(fixtures_dir, task.id)
        prov = payload.get("provenance") or (
            # tolerate the older meta.fixture_kind shape
            {"fixture_kind": (payload.get("meta") or {}).get("fixture_kind", ""),
             "evidence_level": "synthetic"}
        )
        return payload, prov
    if mode == "inprocess":
        from evals.adapters import inprocess
        return inprocess.run(task)
    if mode == "backend":
        from evals.adapters import backend_http
        return backend_http.run(task)
    raise ValueError(f"unknown mode {mode!r}")


# ── evaluate one task ──────────────────────────────────────────────────


def evaluate_task(task: Task, *, mode: str, fixtures_dir: Path,
                  attempt_blocked: bool) -> TaskResult:
    designed = task.evidence_level

    if task.blocked_by and not attempt_blocked:
        return TaskResult(task.id, task.category, "blocked", "none",
                          blocked_by=task.blocked_by, designed_evidence_level=designed,
                          message=f"blocked by {task.blocked_by}")

    try:
        payload, prov_dict = _source_payload(task, mode=mode, fixtures_dir=fixtures_dir)
    except FileNotFoundError:
        if task.blocked_by:  # attempt_blocked but nothing to run -> STAY blocked, do not error
            return TaskResult(task.id, task.category, "blocked", "no_evidence",
                              blocked_by=task.blocked_by, designed_evidence_level=designed,
                              message=f"blocked by {task.blocked_by}; no fixture/adapter to attempt")
        return TaskResult(task.id, task.category, "error", "infrastructure",
                          designed_evidence_level=designed, message="fixture not found")
    except (OSError, json.JSONDecodeError) as exc:
        return TaskResult(task.id, task.category, "error", "infrastructure",
                          blocked_by=task.blocked_by, designed_evidence_level=designed,
                          message=f"bad fixture: {exc}")
    except AdapterUnavailable as exc:
        # e.g. model-quality task with no free model, or no reachable backend.
        oc = "blocked" if task.blocked_by else "unverified"
        return TaskResult(task.id, task.category, oc, "no_evidence",
                          blocked_by=task.blocked_by, designed_evidence_level=designed,
                          message=f"no product evidence available: {exc}")
    except Exception as exc:  # adapter itself failed -> real error, never a pass
        return TaskResult(task.id, task.category, "error", "infrastructure",
                          blocked_by=task.blocked_by, designed_evidence_level=designed,
                          message=f"adapter error: {type(exc).__name__}: {exc}")

    payload = redact(payload)
    prov = Provenance.from_dict(prov_dict)
    prov_missing = prov.missing_fields()
    actual_level = prov.evidence_level or "synthetic"
    exec_attempted = payload.get("execution_attempted")
    if exec_attempted is None:
        exec_attempted = (payload.get("meta") or {}).get("execution_attempted")

    # Placeholder fixtures never count as a product pass or a fail.
    if prov.fixture_kind == "authored_placeholder" or actual_level == "placeholder":
        return TaskResult(task.id, task.category, "unverified", "placeholder",
                          blocked_by=task.blocked_by, evidence_level="synthetic",
                          designed_evidence_level=designed, execution_attempted=exec_attempted,
                          provenance=prov.to_dict(), provenance_missing=prov_missing,
                          message="placeholder fixture: not product-verified")

    details: list[str] = []
    failed = False
    for a in task.asserts:
        res = run_assert(a, payload)
        details.append(f"[{'ok' if res.ok else 'X'}] {res.kind}: {res.detail}")
        failed = failed or not res.ok
    if failed:
        return TaskResult(task.id, task.category, "fail", "assertion",
                          blocked_by=task.blocked_by, evidence_level=actual_level,
                          designed_evidence_level=designed, execution_attempted=exec_attempted,
                          provenance=prov.to_dict(), provenance_missing=prov_missing,
                          assert_details=details, message="assertion failed")

    g = grade(task, payload, offline=(mode == "offline"))
    if g.status == "fail":
        return TaskResult(task.id, task.category, "fail", "grader",
                          blocked_by=task.blocked_by, evidence_level=actual_level,
                          designed_evidence_level=designed, execution_attempted=exec_attempted,
                          provenance=prov.to_dict(), provenance_missing=prov_missing,
                          assert_details=details, grader_detail=g.detail,
                          message="grader below threshold")

    return TaskResult(task.id, task.category, "pass", "none",
                      blocked_by=task.blocked_by, evidence_level=actual_level,
                      designed_evidence_level=designed, execution_attempted=exec_attempted,
                      provenance=prov.to_dict(), provenance_missing=prov_missing,
                      assert_details=details, grader_detail=g.detail)


# ── aggregate ─────────────────────────────────────────────────────────


@dataclass
class Report:
    results: list[TaskResult]
    mode: str = "offline"

    @property
    def counts(self) -> dict[str, int]:
        out = {"pass": 0, "fail": 0, "blocked": 0, "error": 0, "unverified": 0}
        for r in self.results:
            out[r.outcome] = out.get(r.outcome, 0) + 1
        return out

    @property
    def evidence_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            if r.outcome in ("blocked",):
                continue
            lvl = r.evidence_level or "unknown"
            out[lvl] = out.get(lvl, 0) + 1
        return out

    @property
    def verified_pass_count(self) -> int:
        return sum(1 for r in self.results if r.is_verified_pass)

    @property
    def synthetic_pass_count(self) -> int:
        return sum(1 for r in self.results if r.is_synthetic_pass)

    @property
    def placeholder_count(self) -> int:
        return sum(1 for r in self.results if r.failure_class == "placeholder")

    @property
    def unverified_count(self) -> int:
        return self.counts["unverified"]

    @property
    def by_blocked_by(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in self.results:
            if r.outcome == "blocked" and r.blocked_by:
                out.setdefault(r.blocked_by, []).append(r.id)
        return out

    @property
    def synthetic_safety_regressions(self) -> list[str]:
        """Safety-category tasks that DO have a synthetic fixture but no longer
        pass it. Offline (Gate A) regression gate."""
        return [r.id for r in self.results
                if r.category in SAFETY_CATEGORIES
                and r.outcome in ("fail", "error")]

    @property
    def safety_verified_gap(self) -> list[str]:
        """Unblocked safety-category tasks WITHOUT a verified product pass.
        Gate for inprocess / backend modes only."""
        return [r.id for r in self.results
                if r.category in SAFETY_CATEGORIES
                and r.outcome != "blocked"
                and not r.is_verified_pass]

    @property
    def provenance_gaps(self) -> dict[str, list[str]]:
        return {r.id: r.provenance_missing for r in self.results if r.provenance_missing}

    @property
    def exit_code(self) -> int:
        if self.counts["fail"] or self.counts["error"]:
            return 1
        if self.mode == "offline":
            if self.synthetic_safety_regressions:
                return 1
        else:  # inprocess / backend: require verified evidence for safety tasks
            if self.safety_verified_gap:
                return 1
        return 0


def run(dataset_path, fixtures_dir, *, mode: str = "offline",
        attempt_blocked: bool = False) -> Report:
    tasks = load_dataset(dataset_path)
    fixtures_dir = Path(fixtures_dir)
    results = [evaluate_task(t, mode=mode, fixtures_dir=fixtures_dir,
                             attempt_blocked=attempt_blocked) for t in tasks]
    return Report(results, mode=mode)


# ── render ────────────────────────────────────────────────────────────


def _summary_counts(report: Report) -> dict:
    c = report.counts
    return {
        "outcomes": c,
        "verified_pass": report.verified_pass_count,
        "synthetic_pass": report.synthetic_pass_count,
        "placeholder": report.placeholder_count,
        "unverified": report.unverified_count,
        "blocked": c["blocked"],
        "failed": c["fail"],
        "errored": c["error"],
        "by_evidence_level": report.evidence_counts,
    }


def render_markdown(report: Report) -> str:
    s = _summary_counts(report)
    prov = vcs_provenance()
    if prov["working_tree_dirty"]:
        commit_line = (
            f"- (!) generated from a DIRTY working tree -- NOT a faithful "
            f"representation of commit `{prov['source_commit_sha'][:12]}`. "
            f"tree digest: `{prov['source_tree_or_diff_digest']}`"
        )
    else:
        commit_line = f"- source commit: `{prov['source_commit_sha'][:12]}` (clean tree)"
    lines = [
        f"# Thunder AI eval report ({report.mode} mode)",
        "",
        commit_line,
        f"- generated_at: {prov['generated_at']} | dataset_version: "
        f"{prov['dataset_version']} | evaluator_version: {EVALUATOR_VERSION}",
        "",
        "## Counts (evidence-separated)",
        f"- **verified product passes** (product_inprocess/product_backend/e2e_live): **{s['verified_pass']}**",
        f"- synthetic-fixture passes (harness/regression only, NOT product): **{s['synthetic_pass']}**",
        f"- placeholder (unverified, excluded from all pass counts): **{s['placeholder']}**",
        f"- unverified (no product evidence available): **{s['unverified']}**",
        f"- blocked (future phase): **{s['blocked']}**",
        f"- failed: **{s['failed']}**   errored: **{s['errored']}**",
        f"- by evidence level: {s['by_evidence_level'] or '(none)'}",
    ]
    if report.provenance_gaps:
        lines.append(f"- provenance gaps: {report.provenance_gaps}")
    lines += ["", "## blocked_by breakdown"]
    if report.by_blocked_by:
        for reason, ids in sorted(report.by_blocked_by.items()):
            lines.append(f"- **{reason}**: {', '.join(ids)}")
    else:
        lines.append("- (none)")
    if report.mode == "offline":
        lines += ["", "## safety regression (synthetic fixtures must still pass)"]
        lines.append("- " + (", ".join(report.synthetic_safety_regressions) or "(none - no regressions)"))
        lines += ["",
                  "> Offline mode proves the HARNESS and guards against synthetic-fixture "
                  "regressions. It does NOT verify product behaviour. Run `--inprocess` / "
                  "`--backend` for verified product evidence."]
    else:
        lines += ["", "## safety tasks WITHOUT verified product evidence"]
        lines.append("- " + (", ".join(report.safety_verified_gap) or "(none - all verified)"))
    lines += ["", "## failures / errors"]
    bad = [r for r in report.results if r.outcome in ("fail", "error")]
    if not bad:
        lines.append("- (none)")
    for r in bad:
        lines.append(f"- **{r.id}** ({r.category}) — {r.outcome}/{r.failure_class} "
                     f"@{r.evidence_level or 'n/a'}: {r.message}")
        lines += [f"    {d}" for d in r.assert_details if d.startswith("[X]")]
    return "\n".join(lines) + "\n"


def build_report_doc(report: Report) -> dict:
    s = _summary_counts(report)
    prov = vcs_provenance()
    return {
        # VCS-truthful provenance: source_commit_sha describes the evaluated
        # code ONLY when working_tree_dirty is False. When dirty,
        # source_tree_or_diff_digest identifies the actual tree state and this
        # report must NOT be described as representing the commit.
        "provenance": prov,
        "generated_at": prov["generated_at"],
        "mode": report.mode,
        "dataset_version": prov["dataset_version"],
        "evaluator_version": EVALUATOR_VERSION,
        "prompt_version": PROMPT_VERSION,
        "source_commit_sha": prov["source_commit_sha"],
        "working_tree_dirty": prov["working_tree_dirty"],
        "source_tree_or_diff_digest": prov["source_tree_or_diff_digest"],
        "runtime": runtime_fingerprint(),
        "counts": s,
        "by_blocked_by": report.by_blocked_by,
        "synthetic_safety_regressions": report.synthetic_safety_regressions,
        "safety_verified_gap": report.safety_verified_gap,
        "provenance_gaps": report.provenance_gaps,
        "exit_code": report.exit_code,
        "results": [
            {"id": r.id, "category": r.category, "outcome": r.outcome,
             "failure_class": r.failure_class, "blocked_by": r.blocked_by,
             "evidence_level": r.evidence_level,
             "designed_evidence_level": r.designed_evidence_level,
             "execution_attempted": r.execution_attempted,
             "provenance": r.provenance, "provenance_missing": r.provenance_missing,
             "message": r.message}
            for r in report.results
        ],
    }


def write_report_json(report: Report, path) -> None:
    Path(path).write_text(json.dumps(redact(build_report_doc(report)), indent=2),
                          encoding="utf-8")


# ── record mode (FREE local model only) ──────────────────────────────


def _record_payload(task: Task) -> dict:
    """Call a FREE local model (GLM local router / Ollama). Never paid."""
    import httpx

    for base, model in (("http://127.0.0.1:8320/v1", "glm-5.3-flash"),
                        ("http://localhost:11434/v1", "llama3.2:3b")):
        try:
            r = httpx.post(f"{base}/chat/completions", json={
                "model": model,
                "messages": [{"role": "user", "content": task.input}],
                "max_tokens": 512, "temperature": 0.2,
            }, timeout=60.0)
            if r.status_code == 200:
                txt = r.json()["choices"][0]["message"]["content"]
                prov = Provenance(
                    fixture_kind="provider_capture", evidence_level="provider_recording",
                    captured_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                    capture_mode="record", provider="local", model=model,
                    application_path="direct provider call (NOT the product path)",
                    grader_version=rubric_version(task.grader.rubric if task.grader else None),
                    from_product_code=False,
                    **provenance_defaults(),
                )
                return {"task_id": task.id, "response_text": txt, "citations": [],
                        "status_code": 200, "response_json": None,
                        "provenance": prov.to_dict()}
        except Exception:
            continue
    raise RuntimeError("record mode: no free local model reachable (GLM router / Ollama)")


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evals.runner")
    ap.add_argument("--offline", action="store_true", help="score authored fixtures (default; Gate A)")
    ap.add_argument("--inprocess", action="store_true", help="run real backend functions in-process (Gate B)")
    ap.add_argument("--backend", metavar="BASE_URL", help="run against a running backend (Gate B/C)")
    ap.add_argument("--record", action="store_true", help="refresh provider recordings via a FREE local model (Gate C)")
    ap.add_argument("--attempt-blocked", action="store_true",
                    help="attempt blocked tasks; those with no fixture/adapter STAY blocked (not error)")
    ap.add_argument("--include-blocked", action="store_true",
                    help="[deprecated] alias for --attempt-blocked")
    ap.add_argument("--dataset", default="evals/dataset.jsonl")
    ap.add_argument("--fixtures", default="evals/fixtures")
    ap.add_argument("--report", default="evals/report.json")
    ns = ap.parse_args(argv)

    if ns.include_blocked and not ns.attempt_blocked:
        print("warning: --include-blocked is deprecated; use --attempt-blocked "
              "(blocked tasks with no evidence stay blocked, they do not become errors)")
        ns.attempt_blocked = True

    if ns.backend:
        mode = "backend"
        import os
        os.environ.setdefault("EVAL_BACKEND_URL", ns.backend)
    elif ns.inprocess:
        mode = "inprocess"
    else:
        mode = "offline"

    if ns.record:
        tasks = load_dataset(ns.dataset)
        rec_dir = Path("evals/recordings")
        rec_dir.mkdir(parents=True, exist_ok=True)
        for t in tasks:
            if t.blocked_by:
                continue
            try:
                payload = _record_payload(t)
                (rec_dir / f"{t.id}.json").write_text(
                    json.dumps(redact(payload), indent=2), encoding="utf-8")
            except RuntimeError as exc:
                print(f"skip {t.id}: {exc}")

    report = run(ns.dataset, ns.fixtures, mode=mode, attempt_blocked=ns.attempt_blocked)
    print(render_markdown(report))
    write_report_json(report, ns.report)
    print(f"mode={mode}  verified_product_passes={report.verified_pass_count}  "
          f"synthetic_passes={report.synthetic_pass_count}  "
          f"unverified={report.unverified_count}  blocked={report.counts['blocked']}  "
          f"failed={report.counts['fail']}  errored={report.counts['error']}  "
          f"-> wrote {ns.report}")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
