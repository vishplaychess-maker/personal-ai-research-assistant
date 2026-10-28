"""MP-4C - tests for the python_approve acceptance profile (test-only harness).

The profile is the WHOLE verdict for the python_approve scenario in
tests/smoke/run_smoke.py. These tests pin it both ways against a synthetic
SQLite database and synthetic controller artifacts: a CONFORMING fixture must
produce zero failed checks, and each deliberately-broken variant must fail a
NAMED check. They never start a server and never run a subprocess.

Run: PYTHONPATH=backend python -m pytest tests/smoke/test_python_approval_profile.py -q
"""
from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.smoke import run_smoke as RUN

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "tests" / "smoke" / "run_smoke.py"

PROMPT = "D6B deterministic durable chat smoke deadbeef"

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
CREATE TABLE runs (id TEXT, kind TEXT, status TEXT, status_reason TEXT, auth_session_ref TEXT);
CREATE TABLE run_steps (id TEXT, run_id TEXT, seq INTEGER, kind TEXT, status TEXT,
                        output_payload_id TEXT, input_ref TEXT, output_ref TEXT, error_code TEXT);
CREATE TABLE pending_approvals (id TEXT, state TEXT, tool_id TEXT, display_summary TEXT, purpose_redacted TEXT);
CREATE TABLE dispatch_records (id TEXT, outcome TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, citations TEXT);
CREATE TABLE tool_calls (id TEXT);
CREATE TABLE execution_turns (id TEXT);
CREATE TABLE run_signals (id TEXT);
CREATE TABLE run_payloads (id TEXT, payload_type TEXT);
CREATE TABLE run_events (run_seq INTEGER, category TEXT, type TEXT, data TEXT);
CREATE TABLE action_log_heads (chain_scope_id TEXT PRIMARY KEY, seq INTEGER, row_hash TEXT, updated_at TEXT);
"""

EXEC_REC = {"event": "executor_invoked", "ordinal": 1,
            "started_dispatch_at_call": 1, "code_matches_proposal": True,
            "code_len": len(RUN.PYTHON_CODE)}
PROV_RECS = [{"event": "provider_selected", "ordinal": 1},
             {"event": "provider_invoked", "ordinal": 1, "message_count": 1},
             {"event": "provider_selected", "ordinal": 2},
             {"event": "provider_invoked", "ordinal": 2, "message_count": 3}]
PRE_LOGIN_401 = ("[ERROR] Failed to load resource: the server responded "
                 "with a status of 401 (Unauthorized) @ /api/auth/refresh:0")


def _build_db(tmp_path: Path, *, break_kind: str = "") -> Path:
    db_path = tmp_path / "smoke.db"
    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA)
    con.execute("INSERT INTO users(id, username) VALUES (2, ?)", (RUN.SMOKE_USERNAME,))
    con.execute("INSERT INTO runs(id, kind, status, status_reason, auth_session_ref) "
                "VALUES ('run-1','chat_stream','succeeded',NULL,'fam-1')")
    ref = json.dumps({"tool_id": "python_sandbox", "args_hash": "h",
                      "args_payload_id": "p-args", "cycle": 2,
                      "purpose": "python_execution",
                      "code_bytes": len(RUN.PYTHON_CODE)}, sort_keys=True)
    con.executemany(
        "INSERT INTO run_steps(id, run_id, seq, kind, status, output_payload_id, "
        "input_ref, output_ref, error_code) VALUES (?,?,?,?,?,?,?,?,?)",
        [("s1", "run-1", 1, "load_context", "succeeded", "p-load",
          json.dumps({"mode": "chat"}), None, None),
         ("s2", "run-1", 2, "tool_dispatch", "succeeded", "p-out", ref, None, None),
         ("s3", "run-1", 3, "llm_generate", "succeeded", None, None, None, None)])
    summary = json.dumps({"tool_id": "python_sandbox", "source_type": "mcp"})
    con.execute("INSERT INTO pending_approvals(id, state, tool_id, display_summary, "
                "purpose_redacted) VALUES ('ap-1','consumed','python_sandbox',?,'')",
                (summary,))
    con.execute("INSERT INTO dispatch_records(id, outcome) VALUES ('d-1','succeeded')")
    con.execute("INSERT INTO messages(id, role, content, citations) VALUES (1,'user',?,NULL)",
                (PROMPT,))
    con.execute("INSERT INTO messages(id, role, content, citations) VALUES (2,'assistant',?,'[]')",
                (RUN.PYTHON_EXPECTED_ASSISTANT,))
    con.execute("INSERT INTO tool_calls(id) VALUES ('tc-1')")
    con.execute("INSERT INTO execution_turns(id) VALUES ('t-1')")
    con.execute("INSERT INTO run_signals(id) VALUES ('sig-1')")
    con.executemany("INSERT INTO run_payloads(id, payload_type) VALUES (?,?)",
                    [("pl1", "tool_args"), ("pl2", "tool_output"),
                     ("pl3", "other"), ("pl4", "other")])
    con.execute("INSERT INTO run_events(run_seq, category, type, data) VALUES (1,'control','run.created','{}')")
    con.execute("INSERT INTO run_events(run_seq, category, type, data) VALUES (4,'text','text',?)",
                (json.dumps({"text": RUN.VISIBLE_PREFIX}),))
    if break_kind == "extra_dispatch":
        con.execute("INSERT INTO dispatch_records(id, outcome) VALUES ('d-2','started')")
    if break_kind == "code_leak":
        con.execute("INSERT INTO run_events(run_seq, category, type, data) VALUES (9,'text','text',?)",
                    (json.dumps({"text": RUN.PYTHON_CODE}),))
    if break_kind == "extra_assistant":
        con.execute("INSERT INTO messages(id, role, content, citations) VALUES (3,'assistant','second','[]')")
    if break_kind == "missing_tool_output":
        con.execute("DELETE FROM run_payloads WHERE payload_type = 'tool_output'")
    con.commit()
    con.close()

    # real, tamper-evident chain via the PRODUCTION append helper
    from app.approval.action_log import ActionLogEntry, append_event
    eng = create_engine("sqlite:///" + db_path.as_posix())
    try:
        ActionLogEntry.__table__.create(eng)
        db = sessionmaker(bind=eng)()
        try:
            for et in ("approval.requested", "approval.approved",
                       "approval.consumed", "tool.execution.started",
                       "tool.execution.succeeded"):
                append_event(db, tenant_id=None, user_id=2, event_type=et)
        finally:
            db.close()
    finally:
        eng.dispose()
    return db_path


def _write_artifacts(root: Path, *, break_kind: str = "") -> None:
    root.mkdir(parents=True, exist_ok=True)
    ex = [] if break_kind == "executor_absent" else [EXEC_REC]
    (root / "python_executor_calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in ex), encoding="utf-8")
    (root / "python_provider_calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in PROV_RECS), encoding="utf-8")


def _browser_result(*, break_kind: str = "") -> dict:
    errs = []
    if break_kind == "post_login_console_error":
        errs = ["[ERROR] Uncaught TypeError: boom"]
    return {
        "rendered_text": RUN.PYTHON_EXPECTED_ASSISTANT,
        "completed": True,
        "approval_dialog_seen": True,
        "stream_post_count": 1,
        "idempotency_key_count": 1,
        "console_errors": errs,
        "console_errors_pre_login": [PRE_LOGIN_401],
        "page_errors": [],
        "exact_code_preview_seen": break_kind != "no_preview",
        "exact_code_preview_code": (
            None if break_kind == "no_preview" else RUN.PYTHON_CODE),
    }


def _profile(tmp_path: Path, *, break_kind: str = "") -> dict:
    db_path = _build_db(tmp_path, break_kind=break_kind)
    root = tmp_path / "root"
    _write_artifacts(root, break_kind=break_kind)
    checks: list = []
    summary = RUN.check_python_approval_database(db_path, PROMPT, checks)
    RUN._check_python_executor(checks, root)
    RUN._check_python_provider(checks, root)
    RUN._check_python_action_log(checks, db_path, summary.get("user_id"))
    RUN._check_python_browser(checks, _browser_result(break_kind=break_kind))
    return {name: bool(ok) for name, ok, _ in checks}


# -- positive -------------------------------------------------------


def test_conforming_fixture_passes_every_profile_check(tmp_path):
    result = _profile(tmp_path)
    failed = sorted(n for n, ok in result.items() if not ok)
    assert failed == [], failed
    assert len(result) >= 20, sorted(result)
    assert result["py_action_log_chain_verified"] is True
    assert result["py_executor_saw_committed_started_dispatch"] is True


# -- negatives ------------------------------------------------------


def test_extra_started_dispatch_fails_the_dispatch_checks(tmp_path):
    result = _profile(tmp_path, break_kind="extra_dispatch")
    assert result["py_exactly_one_dispatch"] is False
    assert result["py_dispatch_succeeded"] is False


def test_raw_code_in_a_public_event_fails_the_leak_check(tmp_path):
    result = _profile(tmp_path, break_kind="code_leak")
    assert result["py_raw_code_absent_from_public_fields"] is False


def test_second_assistant_message_fails_the_exactly_one_check(tmp_path):
    result = _profile(tmp_path, break_kind="extra_assistant")
    assert result["py_exactly_one_assistant_message"] is False


def test_missing_sealed_tool_output_fails_the_seal_check(tmp_path):
    result = _profile(tmp_path, break_kind="missing_tool_output")
    assert result["py_tool_args_and_tool_output_sealed_once"] is False


def test_absent_executor_log_fails_the_exactly_once_check(tmp_path):
    result = _profile(tmp_path, break_kind="executor_absent")
    assert result["py_executor_invoked_exactly_once"] is False


def test_post_login_console_error_fails_while_the_prelogin_401_stays_classified(tmp_path):
    result = _profile(tmp_path, break_kind="post_login_console_error")
    assert result["py_browser_console_errors_zero_after_login"] is False
    assert result["py_browser_prelogin_errors_are_the_refresh_401"] is True


def test_missing_exact_code_preview_fails_the_preview_checks(tmp_path):
    result = _profile(tmp_path, break_kind="no_preview")
    assert result["py_browser_exact_code_preview_seen"] is False
    assert result["py_browser_exact_code_matches_the_proposal"] is False


# -- the runner actually dispatches this profile --------------------


def test_runner_dispatches_the_python_profile_and_keeps_d6b_in_the_else():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    run_fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "run")
    branches = [n for n in ast.walk(run_fn) if isinstance(n, ast.If)
                and "PYTHON_SCENARIO" in ast.unparse(n.test)]
    assert len(branches) == 1, "expected exactly one PYTHON_SCENARIO branch"
    arm, orelse = branches[0].body, branches[0].orelse
    def called(stmts):
        return sorted(n.func.id for s in stmts for n in ast.walk(s)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name))
    assert "check_python_approval_database" in called(arm), called(arm)
    assert "check_database" in called(orelse), called(orelse)
    assert "check_database" not in called(arm)
    assert "check_python_approval_database" not in called(orelse)
