"""MP-4C - tests for the python_deny acceptance profile (test-only harness).

Pins the DENIED profile both ways against a synthetic SQLite database and
synthetic controller artifacts: a CONFORMING deny fixture must produce zero
failed checks, and each deliberately-broken variant must fail a NAMED check.
Never starts a server and never runs a subprocess.

Run: PYTHONPATH=backend python -m pytest tests/smoke/test_python_deny_profile.py -q
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

PROV_RECS = [{"event": "provider_selected", "ordinal": 1},
             {"event": "provider_invoked", "ordinal": 1, "message_count": 1}]
PRE_LOGIN_401 = ("[ERROR] Failed to load resource: the server responded "
                 "with a status of 401 (Unauthorized) @ /api/auth/refresh:0")


def _build_db(tmp_path: Path, *, break_kind: str = "") -> Path:
    db_path = tmp_path / "smoke.db"
    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA)
    con.execute("INSERT INTO users(id, username) VALUES (2, ?)", (RUN.SMOKE_USERNAME,))
    con.execute("INSERT INTO runs(id, kind, status, status_reason, auth_session_ref) "
                "VALUES ('run-1','chat_stream','denied',NULL,'fam-1')")
    ref = json.dumps({"tool_id": "python_sandbox", "args_hash": "h",
                      "args_payload_id": "p-args", "cycle": 2,
                      "purpose": "python_execution",
                      "code_bytes": len(RUN.PYTHON_CODE)}, sort_keys=True)
    con.executemany(
        "INSERT INTO run_steps(id, run_id, seq, kind, status, output_payload_id, "
        "input_ref, output_ref, error_code) VALUES (?,?,?,?,?,?,?,?,?)",
        [("s1", "run-1", 1, "load_context", "succeeded", "p-load",
          json.dumps({"mode": "chat"}), None, None),
         ("s2", "run-1", 2, "tool_dispatch", "blocked", None, ref, None,
          "approval_denied")])
    summary = json.dumps({"tool_id": "python_sandbox", "source_type": "mcp"})
    con.execute("INSERT INTO pending_approvals(id, state, tool_id, display_summary, "
                "purpose_redacted) VALUES ('ap-1','denied','python_sandbox',?,'')",
                (summary,))
    con.execute("INSERT INTO messages(id, role, content, citations) VALUES (1,'user',?,NULL)",
                (PROMPT,))
    con.execute("INSERT INTO tool_calls(id) VALUES ('tc-1')")
    con.execute("INSERT INTO execution_turns(id) VALUES ('t-1')")
    con.execute("INSERT INTO run_signals(id) VALUES ('sig-1')")
    con.executemany("INSERT INTO run_payloads(id, payload_type) VALUES (?,?)",
                    [("pl1", "tool_args"), ("pl2", "other")])
    con.execute("INSERT INTO run_events(run_seq, category, type, data) VALUES (1,'control','run.created','{}')")
    con.execute("INSERT INTO run_events(run_seq, category, type, data) VALUES (4,'text','text',?)",
                (json.dumps({"text": RUN.VISIBLE_PREFIX}),))
    if break_kind == "started_dispatch":
        con.execute("INSERT INTO dispatch_records(id, outcome) VALUES ('d-1','started')")
    if break_kind == "assistant_message":
        con.execute("INSERT INTO messages(id, role, content, citations) VALUES (2,'assistant','answer','[]')")
    if break_kind == "approval_requested":
        con.execute("UPDATE pending_approvals SET state = 'requested'")
    con.commit()
    con.close()

    from app.approval.action_log import ActionLogEntry, append_event
    eng = create_engine("sqlite:///" + db_path.as_posix())
    try:
        ActionLogEntry.__table__.create(eng)
        db = sessionmaker(bind=eng)()
        try:
            for et in ("approval.requested", "approval.denied"):
                append_event(db, tenant_id=None, user_id=2, event_type=et)
            if break_kind == "broken_chain":
                db.execute(ActionLogEntry.__table__.update().values(
                    event_metadata=json.dumps({"tampered": True})).where(
                    ActionLogEntry.seq == 1))
                db.commit()
        finally:
            db.close()
    finally:
        eng.dispose()
    return db_path


def _write_artifacts(root: Path, *, break_kind: str = "") -> None:
    root.mkdir(parents=True, exist_ok=True)
    ex = [{"event": "executor_invoked", "ordinal": 1}] if break_kind == "executor_called" else []
    (root / "python_executor_calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in ex), encoding="utf-8")
    (root / "python_mcp_transport_calls.jsonl").write_text("", encoding="utf-8")
    (root / "python_provider_calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in PROV_RECS), encoding="utf-8")


def _browser_result(*, break_kind: str = "") -> dict:
    errs = []
    if break_kind == "post_login_console_error":
        errs = ["[ERROR] Uncaught TypeError: boom"]
    return {
        "denial_observed": break_kind != "no_denial_observed",
        "terminal_error_code": "approval_denied",
        "assistant_answer_rendered": False,
        "post_reload": {"user_message": True, "assistant_message": False},
        "stream_post_count": 1,
        "idempotency_key_count": 1,
        "console_errors": errs,
        "console_errors_pre_login": [PRE_LOGIN_401],
        "page_errors": [],
        "rendered_text": RUN.VISIBLE_PREFIX,
    }


def _profile(tmp_path: Path, *, break_kind: str = "") -> dict:
    db_path = _build_db(tmp_path, break_kind=break_kind)
    root = tmp_path / "root"
    _write_artifacts(root, break_kind=break_kind)
    checks: list = []
    summary = RUN.check_python_deny_database(db_path, PROMPT, checks)
    RUN._check_python_deny_executor(checks, root)
    RUN._check_python_deny_provider(checks, root)
    RUN._check_python_deny_action_log(checks, db_path, summary.get("user_id"))
    RUN._check_python_deny_browser(checks, _browser_result(break_kind=break_kind))
    return {name: bool(ok) for name, ok, _ in checks}


# -- positive -------------------------------------------------------


def test_conforming_deny_fixture_passes_every_profile_check(tmp_path):
    result = _profile(tmp_path)
    failed = sorted(n for n, ok in result.items() if not ok)
    assert failed == [], failed
    assert len(result) >= 20, sorted(result)
    assert result["pyd_run_status_denied"] is True
    assert result["pyd_run_status_reason_is_none"] is True
    assert result["pyd_action_log_chain_verified"] is True


# -- negatives ------------------------------------------------------


def test_any_executor_call_fails_the_never_called_check(tmp_path):
    result = _profile(tmp_path, break_kind="executor_called")
    assert result["pyd_executor_never_called"] is False


def test_a_started_dispatch_fails_the_zero_dispatch_check(tmp_path):
    result = _profile(tmp_path, break_kind="started_dispatch")
    assert result["pyd_zero_dispatch_records"] is False


def test_an_assistant_message_fails_the_zero_assistant_check(tmp_path):
    result = _profile(tmp_path, break_kind="assistant_message")
    assert result["pyd_zero_assistant_messages"] is False


def test_an_undecided_approval_fails_the_state_check(tmp_path):
    result = _profile(tmp_path, break_kind="approval_requested")
    assert result["pyd_approval_state_denied"] is False


def test_missing_browser_denial_observation_fails_the_browser_check(tmp_path):
    result = _profile(tmp_path, break_kind="no_denial_observed")
    assert result["pyd_browser_denial_observed"] is False


def test_a_broken_action_log_chain_fails_the_chain_check(tmp_path):
    result = _profile(tmp_path, break_kind="broken_chain")
    assert result["pyd_action_log_chain_verified"] is False


def test_post_login_console_error_fails_while_the_prelogin_401_stays_classified(tmp_path):
    result = _profile(tmp_path, break_kind="post_login_console_error")
    assert result["pyd_browser_console_errors_zero_after_login"] is False
    assert result["pyd_browser_prelogin_errors_are_the_refresh_401"] is True


# -- the runner dispatches this profile, D6B stays in the final else --


def test_runner_dispatches_the_deny_profile_without_touching_the_others():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    run_fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "run")
    ifs = [n for n in ast.walk(run_fn) if isinstance(n, ast.If)]
    deny = [n for n in ifs if "PYTHON_DENY_SCENARIO" in ast.unparse(n.test)]
    assert len(deny) == 1, "expected exactly one PYTHON_DENY_SCENARIO branch"
    arm = deny[0].body
    def called(stmts):
        return sorted(n.func.id for s in stmts for n in ast.walk(s)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name))
    assert "check_python_deny_database" in called(arm), called(arm)
    assert "check_python_approval_database" not in called(arm)
