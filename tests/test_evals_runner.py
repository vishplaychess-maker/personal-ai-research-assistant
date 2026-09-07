"""Unit + integrity tests for the Phase 1A eval harness (evals/).

Hermetic: no network, no running backend, no import of backend/app.
(The in-process adapter DOES import backend/app; those checks live in
tests/test_evals_inprocess.py and only run when the backend env is present.)
"""

import json as _json
from pathlib import Path

import pytest

from evals.schema import Task, Grader, Assert, load_dataset, EvalDatasetError
from evals.asserts import run_assert, resolve_path
from evals.grader import grade
from evals.provenance import Provenance, redact, EVIDENCE_LEVELS, VERIFIED_PRODUCT_LEVELS
from evals.runner import (
    evaluate_task,
    TaskResult,
    SAFETY_CATEGORIES,
    AdapterUnavailable,
    run,
    render_markdown,
    write_report_json,
    build_report_doc,
    _source_payload,
)

REPO = Path(__file__).resolve().parent.parent


# ── helpers ───────────────────────────────────────────────────────────────


def _write(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "d.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _a(**kw):
    return Assert.model_validate(kw)


def _task(**kw) -> Task:
    base = dict(id="T", category="research", title="t", input="q")
    base.update(kw)
    return Task.model_validate(base)


def _prov_dict(**over) -> dict:
    p = Provenance(
        fixture_kind="synthetic_regression", evidence_level="synthetic",
        source_commit_sha="deadbeef", captured_at="2026-01-01T00:00:00+00:00",
        capture_mode="offline", application_path="authored", runtime="authored",
        dataset_version="test", prompt_version="test",
    ).to_dict()
    p.update(over)
    return p


def _fixtures(tmp_path, mapping: dict) -> Path:
    d = tmp_path / "fx"
    d.mkdir()
    for tid, payload in mapping.items():
        payload.setdefault("provenance", _prov_dict())
        (d / f"{tid}.json").write_text(_json.dumps(payload), encoding="utf-8")
    return d


def _dataset(tmp_path, tasks: list[dict]) -> Path:
    p = tmp_path / "ds.jsonl"
    p.write_text("\n".join(_json.dumps(t) for t in tasks), encoding="utf-8")
    return p


# ── schema + loader ──────────────────────────────────────────────────────


def test_load_dataset_parses_tasks(tmp_path):
    p = _write(tmp_path, [
        '{"id": "T1", "category": "research", "title": "x", "input": "q", "evidence_level": "synthetic", "asserts": [{"kind": "contains", "value": "a"}]}',
        '# a comment line',
        '',
        '{"id": "T2", "category": "research", "title": "y", "input": "q", "evidence_level": "product_backend", "blocked_by": "4C"}',
    ])
    tasks = load_dataset(p)
    assert [t.id for t in tasks] == ["T1", "T2"]
    assert tasks[0].asserts[0].kind == "contains"
    assert tasks[1].blocked_by == "4C"


def test_load_dataset_rejects_malformed_line(tmp_path):
    p = _write(tmp_path, ['{"id": "T1"  BROKEN'])
    with pytest.raises(EvalDatasetError) as exc:
        load_dataset(p)
    assert "d.jsonl:1" in str(exc.value)


def test_load_dataset_rejects_duplicate_ids(tmp_path):
    p = _write(tmp_path, [
        '{"id": "T1", "category": "c", "title": "t", "input": "i"}',
        '{"id": "T1", "category": "c", "title": "t", "input": "i"}',
    ])
    with pytest.raises(EvalDatasetError) as exc:
        load_dataset(p)
    assert "duplicate" in str(exc.value).lower()


def test_load_dataset_rejects_bad_evidence_level(tmp_path):
    p = _write(tmp_path, [
        '{"id": "T1", "category": "c", "title": "t", "input": "i", "evidence_level": "totally_made_up"}',
    ])
    with pytest.raises(EvalDatasetError) as exc:
        load_dataset(p)
    assert "evidence_level" in str(exc.value)


# ── assert engine ───────────────────────────────────────────────────────

_PAYLOAD = {
    "response_text": "Answer. [Source: a.pdf, Page: 3] [1] see also [2]",
    "citations": [{"filename": "a.pdf", "page_number": 3}, {"filename": "b.pdf"}],
    "status_code": 200,
    "response_json": {"backend": "ok", "chromadb": "unavailable"},
}


@pytest.mark.parametrize("kw,expected", [
    (dict(kind="contains", value="answer"), True),
    (dict(kind="contains", value="missing"), False),
    (dict(kind="not_contains", value="missing"), True),
    (dict(kind="not_contains", value="answer"), False),
    (dict(kind="regex", pattern=r"page:\s*3", flags="i"), True),
    (dict(kind="regex", pattern=r"page:\s*9"), False),
    (dict(kind="json_path_exists", path="citations[0].page_number"), True),
    (dict(kind="json_path_exists", path="citations[5].nope"), False),
    (dict(kind="citation_present", min=2), True),
    (dict(kind="citation_present", min=5), False),
    (dict(kind="marker_absent", marker="[SAVE_MEMORY:"), True),
    (dict(kind="marker_absent", marker="[1]"), False),
    (dict(kind="status_code", equals=200), True),
    (dict(kind="status_code", equals=500), False),
])
def test_run_assert_each_kind(kw, expected):
    assert run_assert(_a(**kw), _PAYLOAD).ok is expected


def test_unknown_assert_kind_is_failure_not_crash():
    r = run_assert(_a(kind="banana"), _PAYLOAD)
    assert r.ok is False and "unknown" in r.detail.lower()


def test_resolve_path_dollar_json_and_nested():
    assert "chromadb" in resolve_path(_PAYLOAD, "$json")
    assert resolve_path(_PAYLOAD, "response_json.backend") == "ok"


def test_contains_targets_a_path():
    assert run_assert(_a(kind="contains", value="unavailable", **{"in": "$json"}), _PAYLOAD).ok


# ── grader bridge ───────────────────────────────────────────────────────


def test_grade_skipped_when_no_grader():
    r = grade(_task(), {"response_text": "x"}, offline=True)
    assert r.status == "skipped" and r.score is None


def test_grade_skipped_when_offline_even_with_grader():
    t = _task(grader=Grader(rubric="rubrics/x.md", threshold=70))
    r = grade(t, {"response_text": "x"}, offline=True)
    assert r.status == "skipped"


def test_grade_skipped_when_backend_import_fails(monkeypatch, tmp_path):
    rubric = tmp_path / "rubrics"
    rubric.mkdir()
    (rubric / "x.md").write_text("grade it", encoding="utf-8")
    monkeypatch.setattr("evals.grader._EVALS_DIR", tmp_path)

    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name.startswith("app.services.evaluation_service") or name == "app":
            raise ImportError("no backend here")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    t = _task(grader=Grader(rubric="rubrics/x.md", threshold=70))
    r = grade(t, {"response_text": "x"}, offline=False)
    assert r.status == "skipped" and "import" in r.detail.lower()


# ── evaluate_task ──────────────────────────────────────────────────────


def test_blocked_task_is_blocked(tmp_path):
    t = _task(id="T21", blocked_by="3A")
    r = evaluate_task(t, mode="offline", fixtures_dir=tmp_path, attempt_blocked=False)
    assert r.outcome == "blocked" and r.blocked_by == "3A"


def test_attempt_blocked_without_fixture_STAYS_blocked_not_error(tmp_path):
    t = _task(id="T21", blocked_by="3A", asserts=[_a(kind="contains", value="x")])
    r = evaluate_task(t, mode="offline", fixtures_dir=_fixtures(tmp_path, {}),
                      attempt_blocked=True)
    assert r.outcome == "blocked"  # NOT "error"
    assert r.failure_class == "no_evidence"


def test_missing_fixture_for_unblocked_task_is_infra_error(tmp_path):
    t = _task(id="T99", asserts=[_a(kind="contains", value="x")])
    r = evaluate_task(t, mode="offline", fixtures_dir=_fixtures(tmp_path, {}),
                      attempt_blocked=False)
    assert r.outcome == "error" and r.failure_class == "infrastructure"


def test_placeholder_fixture_is_unverified_never_pass(tmp_path):
    t = _task(id="T1", asserts=[_a(kind="contains", value="hello")])
    d = _fixtures(tmp_path, {"T1": {"response_text": "hello world",
                                    "provenance": _prov_dict(fixture_kind="authored_placeholder",
                                                             evidence_level="placeholder")}})
    r = evaluate_task(t, mode="offline", fixtures_dir=d, attempt_blocked=False)
    assert r.outcome == "unverified" and r.failure_class == "placeholder"


def test_synthetic_pass_is_not_a_verified_product_pass(tmp_path):
    t = _task(id="T1", asserts=[_a(kind="contains", value="hi")])
    d = _fixtures(tmp_path, {"T1": {"response_text": "hi"}})
    r = evaluate_task(t, mode="offline", fixtures_dir=d, attempt_blocked=False)
    assert r.outcome == "pass"
    assert r.evidence_level == "synthetic"
    assert r.is_verified_pass is False
    assert r.is_synthetic_pass is True


def test_failing_assert_is_assertion_failure(tmp_path):
    t = _task(id="T1", asserts=[_a(kind="contains", value="nope")])
    d = _fixtures(tmp_path, {"T1": {"response_text": "hello world"}})
    r = evaluate_task(t, mode="offline", fixtures_dir=d, attempt_blocked=False)
    assert r.outcome == "fail" and r.failure_class == "assertion"


def test_missing_provenance_is_reported(tmp_path):
    t = _task(id="T1", asserts=[_a(kind="contains", value="hi")])
    # payload with an almost-empty provenance
    d = tmp_path / "fx"; d.mkdir()
    (d / "T1.json").write_text(_json.dumps(
        {"response_text": "hi", "provenance": {"fixture_kind": "synthetic_regression"}}),
        encoding="utf-8")
    r = evaluate_task(t, mode="offline", fixtures_dir=d, attempt_blocked=False)
    assert "evidence_level" in r.provenance_missing
    assert "source_commit_sha" in r.provenance_missing


def test_adapter_unavailable_becomes_unverified_not_pass(tmp_path, monkeypatch):
    t = _task(id="Tx", evidence_level="product_inprocess",
              asserts=[_a(kind="contains", value="x")])

    def fake_source(task, *, mode, fixtures_dir):
        raise AdapterUnavailable("no free model")

    monkeypatch.setattr("evals.runner._source_payload", fake_source)
    r = evaluate_task(t, mode="inprocess", fixtures_dir=tmp_path, attempt_blocked=False)
    assert r.outcome == "unverified" and r.failure_class == "no_evidence"


def test_adapter_hard_failure_is_error_not_pass(tmp_path, monkeypatch):
    t = _task(id="Tx", evidence_level="product_inprocess",
              asserts=[_a(kind="contains", value="x")])

    def boom(task, *, mode, fixtures_dir):
        raise RuntimeError("adapter blew up")

    monkeypatch.setattr("evals.runner._source_payload", boom)
    r = evaluate_task(t, mode="inprocess", fixtures_dir=tmp_path, attempt_blocked=False)
    assert r.outcome == "error" and r.failure_class == "infrastructure"


def test_result_evidence_level_comes_from_payload_not_task(tmp_path):
    # task DESIGNED for product_backend, but the fixture only carries synthetic
    # evidence -> result must report synthetic, never the task's design goal.
    t = _task(id="T1", evidence_level="product_backend",
              asserts=[_a(kind="contains", value="hi")])
    d = _fixtures(tmp_path, {"T1": {"response_text": "hi"}})
    r = evaluate_task(t, mode="offline", fixtures_dir=d, attempt_blocked=False)
    assert r.designed_evidence_level == "product_backend"
    assert r.evidence_level == "synthetic"
    assert r.is_verified_pass is False


# ── aggregation / report / exit code ──────────────────────────────────


def test_offline_counts_are_evidence_separated(tmp_path):
    ds = _dataset(tmp_path, [
        {"id": "T1", "category": "research", "title": "t", "input": "q",
         "evidence_level": "synthetic", "asserts": [{"kind": "contains", "value": "ok"}]},
        {"id": "T2", "category": "research", "title": "t", "input": "q",
         "evidence_level": "graded_quality", "asserts": [{"kind": "contains", "value": "ok"}]},
        {"id": "T3", "category": "research", "title": "t", "input": "q",
         "evidence_level": "product_backend", "blocked_by": "4C"},
    ])
    fx = _fixtures(tmp_path, {
        "T1": {"response_text": "ok"},
        "T2": {"response_text": "ok", "provenance": _prov_dict(
            fixture_kind="authored_placeholder", evidence_level="placeholder")},
    })
    rep = run(ds, fx, mode="offline", attempt_blocked=False)
    assert rep.synthetic_pass_count == 1
    assert rep.verified_pass_count == 0
    assert rep.placeholder_count == 1
    assert rep.unverified_count == 1
    assert rep.counts["blocked"] == 1
    assert rep.by_blocked_by == {"4C": ["T3"]}
    assert rep.exit_code == 0


def test_offline_exit_nonzero_on_synthetic_safety_regression(tmp_path):
    ds = _dataset(tmp_path, [{"id": "T27", "category": "tool_approvals_safety",
                              "title": "t", "input": "q", "evidence_level": "product_inprocess",
                              "asserts": [{"kind": "contains", "value": "nope"}]}])
    fx = _fixtures(tmp_path, {"T27": {"response_text": "hello"}})
    rep = run(ds, fx, mode="offline", attempt_blocked=False)
    assert "T27" in rep.synthetic_safety_regressions and rep.exit_code == 1


def test_inprocess_mode_gates_on_missing_verified_safety_evidence(tmp_path, monkeypatch):
    # A safety task with no in-process target reaches NO verified evidence in
    # inprocess mode -> it must FAIL the safety gate (exit 1), not pass.
    ds = _dataset(tmp_path, [{"id": "TZ_unregistered", "category": "tool_approvals_safety",
                              "title": "t", "input": "q", "evidence_level": "product_inprocess",
                              "asserts": [{"kind": "contains", "value": "ok"}]}])

    def fake_source(task, *, mode, fixtures_dir):
        raise AdapterUnavailable("no in-process target for this id")

    monkeypatch.setattr("evals.runner._source_payload", fake_source)
    rep = run(ds, tmp_path, mode="inprocess", attempt_blocked=False)
    assert "TZ_unregistered" in rep.safety_verified_gap and rep.exit_code == 1


def test_markdown_and_json_report_show_the_same_counts(tmp_path):
    ds = _dataset(tmp_path, [
        {"id": "T1", "category": "research", "title": "t", "input": "q",
         "evidence_level": "synthetic", "asserts": [{"kind": "contains", "value": "ok"}]},
        {"id": "T2", "category": "research", "title": "t", "input": "q",
         "evidence_level": "product_backend", "blocked_by": "4C"},
    ])
    fx = _fixtures(tmp_path, {"T1": {"response_text": "ok"}})
    rep = run(ds, fx, mode="offline", attempt_blocked=False)
    md = render_markdown(rep)
    doc = build_report_doc(rep)
    assert f"synthetic-fixture passes (harness/regression only, NOT product): **{doc['counts']['synthetic_pass']}**" in md
    assert f"blocked (future phase): **{doc['counts']['blocked']}**" in md
    assert doc["counts"]["verified_pass"] == 0


def test_report_json_redacts_secrets(tmp_path):
    ds = _dataset(tmp_path, [{"id": "T1", "category": "research", "title": "t", "input": "q",
                              "evidence_level": "synthetic",
                              "asserts": [{"kind": "contains", "value": "hi"}]}])
    fx = tmp_path / "fx"; fx.mkdir()
    (fx / "T1.json").write_text(_json.dumps({
        "response_text": "hi bearer eyJabc.eyJdef.ghijklmn and gAAAAAxxxxxxxxxxxxxxxxxxxxxx",
        "response_json": {"Authorization": "Bearer secrettoken12345", "api_key": "sk-or-v1-plaintextkey"},
        "provenance": _prov_dict(),
    }), encoding="utf-8")
    rep = run(ds, fx, mode="offline", attempt_blocked=False)
    out = tmp_path / "r.json"
    write_report_json(rep, out)
    blob = out.read_text()
    assert "secrettoken12345" not in blob
    assert "sk-or-v1-plaintextkey" not in blob
    assert "eyJabc.eyJdef.ghijklmn" not in blob
    assert "gAAAAAxxxxxxxxxxxxxxxxxxxxxx" not in blob


def test_report_json_carries_dataset_and_prompt_version(tmp_path):
    ds = _dataset(tmp_path, [{"id": "T1", "category": "research", "title": "t", "input": "q",
                              "evidence_level": "synthetic"}])
    fx = _fixtures(tmp_path, {"T1": {"response_text": "x"}})
    rep = run(ds, fx, mode="offline", attempt_blocked=False)
    doc = build_report_doc(rep)
    assert doc["dataset_version"] and doc["prompt_version"]


def test_redact_keeps_already_masked_value():
    out = redact({"api_key": "sk-****1234", "authorization": "Bearer abcdefghijkl"})
    assert out["api_key"] == "sk-****1234"          # kept for the assertion
    assert out["authorization"] == "[REDACTED]"


# ── the real dataset ──────────────────────────────────────────────────


def test_real_dataset_loads_45_unique_tasks():
    tasks = load_dataset(REPO / "evals" / "dataset.jsonl")
    assert len(tasks) == 45
    assert [t.id for t in tasks] == [f"T{i}" for i in range(1, 46)]
    for t in tasks:
        assert t.evidence_level in EVIDENCE_LEVELS


def test_every_unblocked_task_has_a_fixture():
    tasks = load_dataset(REPO / "evals" / "dataset.jsonl")
    fx = REPO / "evals" / "fixtures"
    missing = [t.id for t in tasks if not t.blocked_by and not (fx / f"{t.id}.json").is_file()]
    assert missing == []


def test_every_fixture_has_full_provenance():
    fx = REPO / "evals" / "fixtures"
    bad = {}
    for fp in sorted(fx.glob("T*.json")):
        obj = _json.loads(fp.read_text(encoding="utf-8"))
        prov = Provenance.from_dict(obj.get("provenance"))
        miss = prov.missing_fields()
        if miss:
            bad[fp.name] = miss
    assert bad == {}, bad


def test_real_dataset_offline_baseline(tmp_path):
    rep = run(REPO / "evals" / "dataset.jsonl", REPO / "evals" / "fixtures",
              mode="offline", attempt_blocked=False)
    # placeholders (T1, T4) are unverified, NOT passes.
    assert rep.verified_pass_count == 0, "offline mode can never produce a verified product pass"
    assert rep.synthetic_pass_count == 15
    assert rep.placeholder_count == 2
    assert rep.counts["blocked"] == 28
    assert rep.counts["fail"] == 0 and rep.counts["error"] == 0
    assert rep.exit_code == 0
    assert rep.provenance_gaps == {}


def test_real_dataset_attempt_blocked_keeps_blocked_tasks_blocked(tmp_path):
    rep = run(REPO / "evals" / "dataset.jsonl", REPO / "evals" / "fixtures",
              mode="offline", attempt_blocked=True)
    # no fixtures for blocked tasks -> they STAY blocked, never become errors
    assert rep.counts["error"] == 0
    assert rep.counts["blocked"] == 28


def test_safety_category_tasks_are_named_as_expected():
    tasks = {t.id: t for t in load_dataset(REPO / "evals" / "dataset.jsonl")}
    for tid in ["T27", "T28", "T29", "T30", "T31", "T32"]:
        assert tasks[tid].category == "tool_approvals_safety"
    for tid in ["T41", "T42", "T43", "T44", "T45"]:
        assert tasks[tid].category == "approval_gateway"


# ── Part A close-out: redaction precision ───────────────────────────────
# (regression for the substring-match bug that redacted token_count etc.)


def test_redact_keeps_token_usage_metadata_recursively():
    doc = {
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
                  "token_count": 5, "max_tokens": 4096, "tokenizer": "cl100k"},
        "items": [{"prompt_tokens": 1, "completion_tokens": 2}],
        "nested": {"deep": {"total_tokens": 99, "model_token_limit": 8192}},
    }
    out = redact(doc)
    assert out["usage"] == doc["usage"]
    assert out["items"][0] == {"prompt_tokens": 1, "completion_tokens": 2}
    assert out["nested"]["deep"] == {"total_tokens": 99, "model_token_limit": 8192}


def test_redact_strips_sensitive_keys_recursively():
    doc = {
        "api_key": "sk-abcdefghijklmnopqrstuvwx",
        "headers": {"Authorization": "Bearer abc.def.ghi", "set-cookie": "s=1",
                    "X-CSRF-Token": "tok", "Proxy-Authorization": "x"},
        "creds": [{"password": "hunter2", "client_secret": "csx",
                   "openrouter_api_key": "sk-or-v1-zzzzzzzzzzzz",
                   "user_password": "p"}],
        "access_token": "a", "refresh_token": "r", "id_token": "i",
        "SECRET": "top", "db_secret_key": "k",
    }
    out = redact(doc)
    assert out["api_key"] == "[REDACTED]"
    assert out["headers"]["Authorization"] == "[REDACTED]"
    assert out["headers"]["set-cookie"] == "[REDACTED]"
    assert out["headers"]["X-CSRF-Token"] == "[REDACTED]"
    assert out["headers"]["Proxy-Authorization"] == "[REDACTED]"
    assert out["creds"][0]["password"] == "[REDACTED]"
    assert out["creds"][0]["client_secret"] == "[REDACTED]"
    assert out["creds"][0]["openrouter_api_key"] == "[REDACTED]"
    assert out["creds"][0]["user_password"] == "[REDACTED]"
    assert out["access_token"] == "[REDACTED]"
    assert out["refresh_token"] == "[REDACTED]"
    assert out["id_token"] == "[REDACTED]"
    assert out["SECRET"] == "[REDACTED]"          # case-insensitive exact
    assert out["db_secret_key"] == "[REDACTED]"   # anchored suffix


def test_redact_masked_value_passthrough_and_value_patterns():
    doc = {
        "api_key": "sk-****1234",
        "note": "see token eyJhdr00000.eyJbody00000.sigsig00000 in the log",
        "blob": "prefix gAAAAA" + "x" * 40 + " suffix",
    }
    out = redact(doc)
    assert out["api_key"] == "sk-****1234"                 # kept for masking asserts
    assert "eyJ" not in out["note"] and "[REDACTED_JWT]" in out["note"]
    assert "gAAAAA" not in out["blob"] and "[REDACTED_FERNET]" in out["blob"]


# ── Part A close-out: VCS-truthful provenance ───────────────────────────


def test_vcs_provenance_has_all_required_fields():
    from evals import provenance as _p

    v = _p.vcs_provenance()
    for k in ("source_commit_sha", "working_tree_dirty",
              "source_tree_or_diff_digest", "generated_at",
              "dataset_version", "evaluator_version"):
        assert k in v, k
    assert isinstance(v["working_tree_dirty"], bool)
    # a tree digest is present exactly when the tree is dirty
    assert (v["source_tree_or_diff_digest"] is not None) == v["working_tree_dirty"]
    assert v["evaluator_version"] == _p.EVALUATOR_VERSION


def test_report_doc_carries_truthful_vcs_provenance():
    from evals.runner import Report, build_report_doc

    doc = build_report_doc(Report([]))
    for k in ("provenance", "working_tree_dirty", "source_tree_or_diff_digest",
              "evaluator_version", "dataset_version", "source_commit_sha"):
        assert k in doc, k
    assert doc["provenance"]["source_commit_sha"] == doc["source_commit_sha"]
    assert doc["provenance"]["working_tree_dirty"] == doc["working_tree_dirty"]


# ── Part A close-out: backend adapter authorisation (hermetic) ──────────


import evals.adapters.backend_http as _bh  # noqa: E402
from evals.provenance import AdapterUnavailable  # noqa: E402

_BACKEND_ENV = (
    "EVAL_BACKEND_URL", "EVAL_ALLOW_REMOTE_BACKEND", "EVAL_ALLOW_LLM_CALLS",
    "PAID_ENABLED", "EVAL_BACKEND_ALLOW_HOSTS", "EVAL_BACKEND_USERNAME",
    "EVAL_BACKEND_PASSWORD",
)


@pytest.fixture
def clean_backend_env(monkeypatch):
    for k in _BACKEND_ENV:
        monkeypatch.delenv(k, raising=False)
    # never touch the network from Gate A
    monkeypatch.setattr(_bh, "_ssrf_check_remote", lambda url: None)
    return monkeypatch


def test_backend_rejects_unset_url(clean_backend_env):
    with pytest.raises(AdapterUnavailable):
        _bh._authorise_target()


def test_backend_rejects_unsupported_scheme(clean_backend_env):
    clean_backend_env.setenv("EVAL_BACKEND_URL", "ftp://example.com/path")
    clean_backend_env.setenv("EVAL_ALLOW_LLM_CALLS", "true")
    with pytest.raises(AdapterUnavailable) as e:
        _bh._authorise_target()
    assert "scheme" in str(e.value).lower()


def test_backend_rejects_remote_without_explicit_authorization(clean_backend_env):
    clean_backend_env.setattr(_bh, "_host_is_local", lambda h: False)
    clean_backend_env.setenv("EVAL_BACKEND_URL", "https://api.thunder-ai.example.com")
    clean_backend_env.setenv("EVAL_ALLOW_LLM_CALLS", "true")
    with pytest.raises(AdapterUnavailable) as e:
        _bh._authorise_target()
    assert "EVAL_ALLOW_REMOTE_BACKEND" in str(e.value)


def test_backend_rejects_live_model_call_without_flag(clean_backend_env):
    clean_backend_env.setenv("EVAL_BACKEND_URL", "http://127.0.0.1:8000")
    with pytest.raises(AdapterUnavailable) as e:
        _bh._authorise_target()
    assert "EVAL_ALLOW_LLM_CALLS" in str(e.value)


def test_backend_remote_still_blocked_by_paid_policy(clean_backend_env):
    clean_backend_env.setattr(_bh, "_host_is_local", lambda h: False)
    clean_backend_env.setenv("EVAL_BACKEND_URL", "https://api.thunder-ai.example.com")
    clean_backend_env.setenv("EVAL_ALLOW_LLM_CALLS", "true")
    clean_backend_env.setenv("EVAL_ALLOW_REMOTE_BACKEND", "true")
    with pytest.raises(AdapterUnavailable) as e:
        _bh._authorise_target()
    assert "PAID_ENABLED" in str(e.value)


def test_backend_allows_local_target_with_llm_flag(clean_backend_env):
    clean_backend_env.setenv("EVAL_BACKEND_URL", "http://127.0.0.1:8000/")
    clean_backend_env.setenv("EVAL_ALLOW_LLM_CALLS", "true")
    assert _bh._authorise_target() == "http://127.0.0.1:8000"


def test_backend_allows_remote_when_fully_authorised(clean_backend_env):
    clean_backend_env.setattr(_bh, "_host_is_local", lambda h: False)
    clean_backend_env.setenv("EVAL_BACKEND_URL", "https://api.thunder-ai.example.com/")
    clean_backend_env.setenv("EVAL_ALLOW_LLM_CALLS", "true")
    clean_backend_env.setenv("EVAL_ALLOW_REMOTE_BACKEND", "true")
    clean_backend_env.setenv("PAID_ENABLED", "true")
    assert _bh._authorise_target() == "https://api.thunder-ai.example.com"


def test_backend_run_is_blocked_for_blocked_task(clean_backend_env):
    t = Task.model_validate({"id": "T41", "category": "approval_gateway",
                             "title": "x", "input": "y", "blocked_by": "1B"})
    with pytest.raises(AdapterUnavailable):
        _bh.run(t)
