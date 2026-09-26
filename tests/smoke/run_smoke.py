"""D6B deterministic real-browser durable-chat smoke orchestrator.

TEST-ONLY. Starts the REAL backend (real lifespan, real supervisor, real worker)
and the REAL Vite dev server on loopback, seeds a real account through the real
registration route, then hands a bounded, credential-free manifest to the external
Chromium controller and waits for it to finish.

Usage:
    PYTHONPATH=backend python tests/smoke/run_smoke.py [--browser-timeout 300]

Exit code 0 only when every invariant holds.

Safety properties:

* One process owns every child; teardown runs in a finally block.
* The backend child runs with cwd set to the throwaway temp root, so the
  repository .env (and therefore the developer real provider keys) is NEVER
  loaded; configuration comes only from the environment this script builds.
* Nothing is written inside the repository.
* Diagnostics are bounded and redacted: no password, cookie, CSRF token, JWT or
  API key is ever printed or written to the manifest.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.smoke.fakes import PROMPT_PREFIX, SMOKE_RESPONSE  # noqa: E402
from tests.smoke.helpers import (  # noqa: E402
    SmokeError, SmokeTimeout, assert_loopback_url, atomic_write_json,
    free_loopback_port, read_json, terminate_process_tree, wait_for,
)
from tests.smoke.python_fakes import (  # noqa: E402
    CANARY as PYTHON_CANARY,
    PYTHON_CODE,
    SECOND_ANSWER,
    VISIBLE_PREFIX,
)

#: tests/smoke-scoped scenario names for the real-browser Python journeys.
PYTHON_SCENARIO = "python_approve"
PYTHON_DENY_SCENARIO = "python_deny"
#: The assistant text that journey must render (visible prefix + resumed answer).
PYTHON_EXPECTED_ASSISTANT = VISIBLE_PREFIX + SECOND_ANSWER

SMOKE_USERNAME = "d6b_smoke_user"
# Non-secret fixture. It exists only inside the throwaway database and is never
# written to the manifest, so the manifest stays credential-free.
SMOKE_PASSWORD = "d6b-smoke-password-1"
# A secret-shaped canary placed in the child environment. It must never appear in
# any durable public field, in the rendered UI, or in the backend log.
CANARY_SECRET = "CANARY-D6B-MUST-NEVER-LEAK-9f3c1a"

BACKEND_BOOT_TIMEOUT_S = 120.0
FRONTEND_BOOT_TIMEOUT_S = 120.0


class SmokeFailure(AssertionError):
    """One failed D6B invariant."""


# ── process startup ─────────────────────────────────────────────────


#: Environment names that could carry a real provider credential or steer the app.
#: They are removed from every child so the smoke can never reach a live provider,
#: regardless of what the developer shell or the repository .env holds.
SCRUBBED_ENV_KEYS = (
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
    "TOGETHER_API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY",
    "NEXA_API_KEY", "AGNES_API_KEY", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN",
    "OLLAMA_HOST", "CHROMADB_HOST", "PYTHONSTARTUP", "PYTHONPATH",
    "THUNDER_DEV_PROXY_TARGET", "THUNDER_SMOKE_ROOT", "PRODUCTION_MODE",
)


def _base_child_env() -> dict:
    """Inherit the ambient environment (the OS needs it: `Path.home()` and the
    chromadb import both depend on it) and then strip every credential-shaped or
    behaviour-steering name before the app-specific overrides are applied."""
    env = os.environ.copy()
    for name in SCRUBBED_ENV_KEYS:
        env.pop(name, None)
    return env


def _child_env(root: Path, backend_port: int, frontend_origin: str = "") -> dict:
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    env = _base_child_env()
    env.update({
        # backend/ for `app.*`; the repo root for `tests.smoke.*` (the child runs
        # with cwd set to the throwaway root, so the repo is not implicitly on path).
        "PYTHONPATH": str(REPO_ROOT / "backend") + os.pathsep + str(REPO_ROOT),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "DATABASE_URL": "sqlite:///" + (root / "smoke.db").as_posix(),
        "DATA_DIR": str(root / "data"),
        "UPLOAD_DIR": str(root / "uploads"),
        "THUNDER_SMOKE_ROOT": str(root),
        "THUNDER_SMOKE_BACKEND_PORT": str(backend_port),
        "PRODUCTION_MODE": "false",
        "JWT_SECRET": secrets.token_hex(32),
        "ENCRYPTION_KEY": key,
        "RUN_IDEMPOTENCY_HMAC_KEY": secrets.token_hex(32),
        "RUN_ENGINE_ENABLED": "true",
        "RUN_WORKER_ENABLED": "true",
        "RUN_PUBLIC_API_ENABLED": "true",
        "RUN_CHAT_STREAM_ENABLED": "true",
        "RUN_MESSAGES_STREAM_DURABLE_ENABLED": "true",
        "RUN_DEEP_RESEARCH_ENABLED": "false",  # D6B must not need these
        "RUN_MULTI_AGENT_ENABLED": "false",
        "RUN_WORKER_WEB_CONCURRENCY": "1",
        # D6B-R1: the browser reaches the backend directly via VITE_API_BASE_URL,
        # so the backend must allow exactly the smoke frontend origin (loopback).
        "CORS_ORIGINS": frontend_origin,
        # Canary: proves a secret-shaped value in the process environment never
        # reaches a durable public field, the UI, or the log.
        "OPENROUTER_API_KEY": CANARY_SECRET,
    })
    return env


def _start_backend(root: Path, port: int, log_path: Path,
                   max_invocations: int = 1, frontend_origin: str = "",
                   scenario: str = "") -> subprocess.Popen:
    env = _child_env(root, port, frontend_origin)
    # tests/smoke-scoped scenario switch (D6C-J2/J3); child-process env only.
    if scenario:
        env["THUNDER_SMOKE_SCENARIO"] = scenario
    # D6C-J6: Deep Research is enabled ONLY for the tests/smoke-scoped deep_research
    # scenario, in this child process only (run_smoke already sets engine / worker /
    # public-api / chat-stream true and deep-research false for every other run).
    # Multi-Agent stays disabled. Production Settings defaults are never touched.
    if scenario == "deep_research":
        env["RUN_DEEP_RESEARCH_ENABLED"] = "true"
        # D6C-J6 evidence-only handoff: the throwaway per-run key protects ONLY
        # disposable smoke data, and the launcher needs it to open the sealed
        # research_sources payload with the PRODUCTION read helper after the
        # journey. The file lives in the isolated temp root (outside the repo) and
        # is deleted with it at teardown. No production surface is created.
        (root / "encryption_key.txt").write_text(env["ENCRYPTION_KEY"], encoding="utf-8")
    # D6C-J7: Multi-Agent enabled for the tests/smoke-scoped multi_agent scenario only,
    # in this child process only. Deep Research stays disabled here. Production
    # Settings defaults are never touched.
    if scenario in ("multi_agent", "multi_agent_longtokens"):
        env["RUN_MULTI_AGENT_ENABLED"] = "true"
        # D6C-J8-R1: the long-unbroken-token layout variant is a tests/smoke-scoped
        # SCENARIO, not a new flag - it reuses this exact child-env mechanism and
        # leaves the normal multi_agent fixture byte-for-byte unchanged.
        if scenario == "multi_agent_longtokens":
            env["THUNDER_SMOKE_MA_LONG_TOKENS"] = "1"
        # D6C-J7 evidence-only handoff (same rationale as the deep_research case):
        # the launcher opens the sealed agent artifacts with the PRODUCTION
        # context-bound helper after the journey. Throwaway root, deleted at
        # teardown, no production surface.
        (root / "encryption_key.txt").write_text(env["ENCRYPTION_KEY"], encoding="utf-8")
    # MP-4C: `run_engine_sandbox` authorizes the PROPOSAL path only, and only
    # in this tests/smoke-scoped child process. Production Settings defaults are
    # never touched (verified separately in a fresh production Settings).
    if scenario in (PYTHON_SCENARIO, PYTHON_DENY_SCENARIO):
        env["RUN_ENGINE_SANDBOX"] = "true"
    # TEST-ONLY diagnostic override; the acceptance run never sets it.
    env["THUNDER_SMOKE_MAX_PROVIDER_INVOCATIONS"] = str(int(max_invocations))
    log = log_path.open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.smoke.durable_smoke_app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(root),                 # no repo .env is loadable from here
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    proc._d6b_log = log  # type: ignore[attr-defined]
    return proc


def _start_frontend(port: int, backend_port: int, log_path: Path) -> subprocess.Popen:
    env = _base_child_env()
    env["THUNDER_DEV_PROXY_TARGET"] = "http://127.0.0.1:" + str(backend_port)
    # D6B-R1: the browser talks to the backend THROUGH the existing, production-
    # supported `VITE_API_BASE_URL` seam instead of the Vite dev-server proxy.
    #
    # Proven by the D6B-R1 evidence matrix: the Vite dev proxy drops an in-flight
    # SSE upstream connection as soon as other requests are proxied concurrently
    # (`api/health` polling), so the stream ends without its terminal frame. That
    # is a DEVELOPMENT-SERVER artifact only -- production serves the frontend from
    # nginx / a same-origin rewrite and never uses this proxy. The proxy is not in
    # the acceptance path because it is not in the production path.
    env["VITE_API_BASE_URL"] = "http://127.0.0.1:" + str(backend_port)
    log = log_path.open("wb")
    npm = shutil.which("npm.cmd") or shutil.which("npm") or "npm.cmd"
    proc = subprocess.Popen(
        [npm, "run", "dev", "--", "--port", str(port), "--strictPort",
         "--host", "127.0.0.1"],
        cwd=str(REPO_ROOT / "frontend"), env=env,
        stdout=log, stderr=subprocess.STDOUT,
    )
    proc._d6b_log = log  # type: ignore[attr-defined]
    return proc


# ── bounded HTTP helpers ────────────────────────────────────────────


def _http(url: str, *, method: str = "GET", payload: dict | None = None,
          timeout_s: float = 5.0) -> tuple[int, str]:
    import urllib.error
    import urllib.request

    assert_loopback_url(url)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


def _json_or_none(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


# ── assertions ──────────────────────────────────────────────────────


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def check_database(db_path: Path, prompt: str, checks: list) -> dict:
    """Every direct-DB invariant from the D6B acceptance criteria."""
    conn = _connect(db_path)
    try:
        def scalar(sql: str, params=()):
            return conn.execute(sql, params).fetchone()

        def rows(sql: str, params=()):
            return conn.execute(sql, params).fetchall()

        users = rows("SELECT id, username FROM users WHERE username = ?", (SMOKE_USERNAME,))
        checks.append(("exactly_one_smoke_user", len(users) == 1, len(users)));
        user_id = users[0]["id"] if users else None

        user_msgs = rows("SELECT id, content FROM messages WHERE role = ?", ("user",))
        checks.append(("exactly_one_user_message", len(user_msgs) == 1, len(user_msgs)));
        checks.append(("user_message_matches_prompt",
                       len(user_msgs) == 1 and user_msgs[0]["content"] == prompt,
                       None if len(user_msgs) != 1 else len(user_msgs[0]["content"])))

        assistant = rows("SELECT id, content, citations FROM messages WHERE role = ?", ("assistant",))
        checks.append(("exactly_one_assistant_message", len(assistant) == 1, len(assistant)));

        runs = rows("SELECT id, kind, status, status_reason, auth_session_ref FROM runs")
        checks.append(("exactly_one_run", len(runs) == 1, len(runs)));
        run = runs[0] if runs else None
        checks.append(("run_kind_is_chat_stream", bool(run) and run["kind"] == "chat_stream",
                       None if run is None else run["kind"]))
        checks.append(("run_status_succeeded", bool(run) and run["status"] == "succeeded",
                       None if run is None else run["status"]))
        checks.append(("run_bound_to_real_session_family",
                       bool(run) and bool((run["auth_session_ref"] or "").strip()),
                       None if run is None else bool(run["auth_session_ref"])))

        steps = rows("SELECT id, seq, kind, status FROM run_steps WHERE run_id = ?",
                     (run["id"] if run else "",))
        checks.append(("exactly_one_run_step", len(steps) == 1, len(steps)));
        checks.append(("run_step_kind_load_context",
                       len(steps) == 1 and steps[0]["kind"] == "load_context",
                       None if len(steps) != 1 else steps[0]["kind"]))
        checks.append(("run_step_succeeded",
                       len(steps) == 1 and steps[0]["status"] == "succeeded",
                       None if len(steps) != 1 else steps[0]["status"]))

        for table, label in (("dispatch_records", "no_dispatch_records"),
                             ("pending_approvals", "no_pending_approvals"),
                             ("tool_calls", "no_tool_calls"),
                             ("execution_turns", "no_execution_turns")):
            n = scalar("SELECT COUNT(*) AS n FROM " + table)["n"]
            checks.append((label, n == 0, n))

        text_events = rows(
            "SELECT run_seq, data FROM run_events WHERE run_id = ? AND category = ? "
            "ORDER BY run_seq ASC", (run["id"] if run else "", "text"))
        reconstructed = ""
        for row in text_events:
            payload = _json_or_none(row["data"] or "") or {}
            reconstructed += payload.get("text", "")
        checks.append(("persisted_text_reconstructs_response",
                       reconstructed == SMOKE_RESPONSE,
                       {"events": len(text_events), "chars": len(reconstructed)}))
        checks.append(("final_message_equals_reconstructed_text",
                       len(assistant) == 1 and assistant[0]["content"] == reconstructed,
                       None))
        checks.append(("final_message_equals_expected_response",
                       len(assistant) == 1 and assistant[0]["content"] == SMOKE_RESPONSE,
                       None))

        # canary must never reach a durable public field
        leaked = []
        if run is not None and CANARY_SECRET in (run["status_reason"] or ""):
            leaked.append("runs.status_reason")
        for table, cols in (("run_events", ("data", "type")),
                            ("run_steps", ("input_ref", "output_ref", "error_code")),
                            ("messages", ("content", "citations"))):
            for row in rows("SELECT * FROM " + table):
                for col in cols:
                    try:
                        value = row[col]
                    except (IndexError, KeyError):
                        continue
                    if isinstance(value, str) and CANARY_SECRET in value:
                        leaked.append(table + "." + col)
        checks.append(("canary_absent_from_durable_fields", not leaked, sorted(set(leaked))))

        return {
            "run_id": run["id"] if run else None,
            "user_id": user_id,
            "text_event_count": len(text_events),
            "text_event_chars": len(reconstructed),
            "response_chars": len(SMOKE_RESPONSE),
        }
    finally:
        conn.close()


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"event": "unparsable"})
    return out


def _tail(path: Path, limit: int = 4000) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    return data[-limit:].decode("utf-8", "replace")
# ── main orchestration ──────────────────────────────────────────────


def run(args) -> int:
    checks: list = []
    root = Path(tempfile.mkdtemp(prefix="thunder_d6b_"))
    backend = None
    frontend = None
    result = {"status": "error", "stage": "init", "smoke_root": str(root)}

    backend_port = free_loopback_port()
    frontend_port = free_loopback_port()
    backend_url = "http://127.0.0.1:%d" % backend_port
    frontend_url = "http://127.0.0.1:%d" % frontend_port
    assert_loopback_url(backend_url)
    assert_loopback_url(frontend_url)

    prompt = "%s %s" % (PROMPT_PREFIX, secrets.token_hex(6))
    backend_log = root / "backend.log"
    frontend_log = root / "frontend.log"
    manifest_path = root / "manifest.json"
    done_path = root / "browser_done"
    abort_path = root / "browser_abort"
    browser_result_path = root / "browser_result.json"
    db_path = root / "smoke.db"

    try:
        # 1-5. backend on the real lifespan, bounded readiness polling
        result["stage"] = "backend_start"
        backend = _start_backend(root, backend_port, backend_log,
                                 max_invocations=args.max_provider_invocations,
                                 frontend_origin=frontend_url,
                                 scenario=getattr(args, "scenario", ""))
        wait_for(lambda: _backend_health_ok(backend_url, backend, backend_log),
                 timeout_s=BACKEND_BOOT_TIMEOUT_S, interval_s=0.5,
                 description="backend /api/health to answer 200")

        result["stage"] = "register"
        status, body = _http(backend_url + "/api/auth/register", method="POST",
                             payload={"username": SMOKE_USERNAME,
                                      "email": SMOKE_USERNAME + "@example.invalid",
                                      "password": SMOKE_PASSWORD})
        if status not in (201, 400):      # 400 = already registered (rerun)
            raise SmokeFailure("registration failed with HTTP %d" % status)

        # D6C-J2/J3: seed the real trusted_tools row via the production registry
        # helpers, once, before any submission can exist. Idempotent by design.
        if getattr(args, "scenario", "") in ("mcp_approve", "mcp_deny"):
            from sqlalchemy import create_engine as _ce
            from sqlalchemy.orm import sessionmaker as _sm
            from app.models.models import User
            from tests.smoke.mcp_fakes import seed_trusted_echo_tool

            _eng = _ce("sqlite:///" + db_path.as_posix())
            try:
                _db = _sm(bind=_eng)()
                _u = _db.query(User).filter(User.username == SMOKE_USERNAME).one()
                seed_trusted_echo_tool(_db, owner_user_id=_u.id)
                _db.close()
            finally:
                _eng.dispose()

        # 6. supervisor + worker must be alive BEFORE any browser navigation
        result["stage"] = "worker_ready"
        wait_for(lambda: _worker_alive(backend_url), timeout_s=60.0,
                 interval_s=0.5,
                 description="/api/health/worker 200 with a live worker")

        # 7-8. real Vite dev server pointed at this backend, bounded readiness
        result["stage"] = "frontend_start"
        frontend = _start_frontend(frontend_port, backend_port, frontend_log)
        wait_for(lambda: _frontend_ready(frontend_url, frontend, frontend_log),
                 timeout_s=FRONTEND_BOOT_TIMEOUT_S, interval_s=0.5,
                 description="Vite dev server to serve the app")

        # 9. credential-free manifest for the external Chromium controller
        result["stage"] = "await_browser"
        atomic_write_json(manifest_path, {
            "run_id": prompt.rsplit(" ", 1)[-1],
            "backend_url": backend_url,
            "frontend_url": frontend_url,
            "backend_pid": backend.pid,
            "frontend_pid": frontend.pid,
            "smoke_root": str(root),
            "done_path": str(done_path),
            "abort_path": str(abort_path),
            "browser_result_path": str(browser_result_path),
            "username": SMOKE_USERNAME,
            "password_source": "tests/smoke/README.md (non-secret fixture)",
            "prompt": prompt,
            "expected_response": SMOKE_RESPONSE,
            "expected_response_chars": len(SMOKE_RESPONSE),
            "python_scenario": (
                getattr(args, "scenario", "")
                if getattr(args, "scenario", "") in
                (PYTHON_SCENARIO, PYTHON_DENY_SCENARIO) else None),
            "python_expected_assistant": (
                PYTHON_EXPECTED_ASSISTANT
                if getattr(args, "scenario", "") == PYTHON_SCENARIO else None),
            "python_expect_denial": (
                getattr(args, "scenario", "") == PYTHON_DENY_SCENARIO),
        })
        print("D6B manifest: " + str(manifest_path), flush=True)

        # 10-12. wait for the controller, bounded, with failure diagnostics
        _wait_for_browser(root, done_path, abort_path, args.browser_timeout)

        result["stage"] = "assert"
        browser_result = None
        if browser_result_path.exists():
            browser_result = read_json(browser_result_path)

        _scenario = getattr(args, "scenario", "")
        if _scenario == PYTHON_SCENARIO:
            # MP-4C python_approve acceptance profile. The D6B profile
            # describes a chat-only run (one step, ZERO dispatch / approval /
            # tool-call / turn rows); this journey deliberately has all of
            # those plus a resumed generation, so it is NEVER applied here.
            summary = check_python_approval_database(db_path, prompt, checks)
            _check_python_executor(checks, root)
            _check_python_provider(checks, root)
            _check_tripwire(checks, root)
            _check_logs(checks, backend_log)
            _check_python_action_log(checks, db_path, summary.get("user_id"))
            _check_python_browser(checks, browser_result)
        elif _scenario == PYTHON_DENY_SCENARIO:
            # MP-4C python_deny acceptance profile: the denied counterpart.
            # It shares the D6B tripwire/log checks but has its OWN DB,
            # executor, provider, action-log and browser invariants (a denied
            # run has NO resumed generation, NO dispatch and NO assistant).
            summary = check_python_deny_database(db_path, prompt, checks)
            _check_python_deny_executor(checks, root)
            _check_python_deny_provider(checks, root)
            _check_tripwire(checks, root)
            _check_logs(checks, backend_log)
            _check_python_deny_action_log(checks, db_path, summary.get("user_id"))
            _check_python_deny_browser(checks, browser_result)
        else:
            check_database(db_path, prompt, checks)
            _check_provider(checks, root)
            _check_tripwire(checks, root)
            _check_logs(checks, backend_log)
            _check_browser(checks, browser_result)

        failed = [c for c in checks if not c[1]]
        result["checks"] = [{"name": n, "ok": bool(ok), "observed": obs}
                            for n, ok, obs in checks]
        result["failed"] = [n for n, ok, _ in failed]
        result["status"] = "pass" if not failed else "fail"
        result["stage"] = "done"
    except SmokeFailure as exc:
        result["status"] = "fail"
        result["error"] = str(exc)[:300]
    except SmokeTimeout as exc:
        result["status"] = "fail"
        result["error"] = "timeout: " + str(exc)[:280]
    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:280])
    finally:
        terminate_process_tree(frontend, grace_s=15.0)
        terminate_process_tree(backend, grace_s=20.0)
        for proc in (frontend, backend):
            log = getattr(proc, "_d6b_log", None)
            if log is not None:
                try:
                    log.close()
                except Exception:  # noqa: BLE001
                    pass
        result["teardown"] = {
            "backend_dead": backend is None or backend.poll() is not None,
            "frontend_dead": frontend is None or frontend.poll() is not None,
        }
        result["backend_log_tail"] = _tail(backend_log, 2500)
        result["frontend_log_tail"] = _tail(frontend_log, 1200)
        try:
            atomic_write_json(root / "result.json", result)
        except Exception:  # noqa: BLE001
            pass
        if not args.keep_root:
            shutil.rmtree(root, ignore_errors=True)
            result["smoke_root_removed"] = not root.exists()
        else:
            result["smoke_root_removed"] = False

    print(json.dumps(result, sort_keys=True, indent=2), flush=True)
    return 0 if result.get("status") == "pass" else 1


def _wait_for_browser(root: Path, done_path: Path, abort_path: Path,
                      timeout_s: float) -> None:
    wait_for(lambda: done_path.exists() or abort_path.exists(),
             timeout_s=timeout_s, interval_s=0.5,
             description="the external Chromium controller to finish")
    if abort_path.exists():
        raise SmokeFailure("the browser controller reported an abort")


def _backend_health_ok(base_url: str, proc, log: Path) -> bool:
    if proc.poll() is not None:
        raise SmokeFailure("backend exited early; log tail: " + _tail(log, 600))
    try:
        status, _ = _http(base_url + "/api/health", timeout_s=3.0)
    except Exception:  # noqa: BLE001
        return False
    return status == 200


def _worker_alive(base_url: str) -> bool:
    try:
        status, body = _http(base_url + "/api/health/worker", timeout_s=3.0)
    except Exception:  # noqa: BLE001
        return False
    if status != 200:
        return False
    payload = _json_or_none(body) or {}
    durable = payload.get("durable_run") or {}
    return bool(payload.get("durable_path_ready")) and bool(durable.get("alive"))


def _frontend_ready(base_url: str, proc, log: Path) -> bool:
    if proc.poll() is not None:
        raise SmokeFailure("frontend exited early; log tail: " + _tail(log, 600))
    try:
        status, body = _http(base_url + "/", timeout_s=3.0)
    except Exception:  # noqa: BLE001
        return False
    return status == 200 and "root" in body


def _check_provider(checks: list, root: Path) -> None:
    """The deterministic provider must be selected once and REALLY invoked once."""
    calls = _read_jsonl(root / "provider_calls.jsonl")
    selected = [c for c in calls if c.get("event") == "provider_selected"]
    invoked = [c for c in calls if c.get("event") == "provider_invoked"]
    checks.append(("provider_selected_exactly_once", len(selected) == 1, len(selected)))
    checks.append(("provider_invoked_exactly_once", len(invoked) == 1, len(invoked)))
    snapshot = selected[0] if selected else {}
    checks.append(("provider_snapshot_recorded",
                   bool(snapshot.get("provider")) and bool(snapshot.get("model")),
                   {"provider": snapshot.get("provider"),
                    "model": snapshot.get("model"),
                    "message_count": snapshot.get("message_count")}))


def _check_tripwire(checks: list, root: Path) -> None:
    """Any recorded attempt means an outbound call (or a call to Ollama/ChromaDB)
    was attempted. The tripwire blocks as well as records, so a non-empty log is a
    hard failure even if the run otherwise succeeded."""
    attempts = _read_jsonl(root / "tripwire.jsonl")
    reasons = sorted({a.get("reason") for a in attempts})
    checks.append(("no_outbound_network_attempt", not attempts,
                   {"attempts": len(attempts), "reasons": reasons}))


def _check_logs(checks: list, backend_log: Path) -> None:
    text = _tail(backend_log, 2_000_000)
    checks.append(("canary_absent_from_backend_log", CANARY_SECRET not in text, None))


def _check_browser(checks: list, browser_result) -> None:
    if browser_result is None:
        checks.append(("browser_result_present", False, None))
        return
    checks.append(("browser_result_present", True, None))
    rendered = browser_result.get("rendered_text") or ""
    console_errors = browser_result.get("console_errors") or []
    page_errors = browser_result.get("page_errors") or []
    checks.append(("browser_rendered_matches_expected", rendered == SMOKE_RESPONSE,
                   {"chars": len(rendered), "expected_chars": len(SMOKE_RESPONSE)}))
    checks.append(("browser_console_errors_zero", len(console_errors) == 0,
                   {"count": len(console_errors)}))
    checks.append(("browser_page_errors_zero", len(page_errors) == 0,
                   {"count": len(page_errors)}))
    checks.append(("browser_reached_completed_state",
                   bool(browser_result.get("completed")),
                   bool(browser_result.get("completed"))))
    checks.append(("browser_observed_progressive_render",
                   bool(browser_result.get("saw_partial_render")),
                   bool(browser_result.get("saw_partial_render"))))
    checks.append(("canary_absent_from_rendered_ui",
                   CANARY_SECRET not in rendered, None))


# ── python_approve acceptance profile (MP-4C) ───────────────────────
#
# The D6B profile (check_database / _check_provider / _check_browser) describes
# a chat-only durable run: exactly ONE load_context step, ZERO dispatch /
# approval / tool-call / turn rows, and one canned response. The python_approve
# journey deliberately HAS an approval, a dispatch and a resumed generation, so
# it gets its own profile. These checks are the whole verdict for that scenario.


def check_python_approval_database(db_path: Path, prompt: str, checks: list) -> dict:
    """Scenario-specific DB invariants for the Python approval journey.
    Same (name, ok, observed) contract as check_database."""
    conn = _connect(db_path)
    try:
        def rows(sql: str, params=()):
            return conn.execute(sql, params).fetchall()

        users = rows("SELECT id, username FROM users WHERE username = ?",
                     (SMOKE_USERNAME,))
        checks.append(("py_exactly_one_smoke_user", len(users) == 1, len(users)))
        user_id = users[0]["id"] if users else None

        runs = rows("SELECT id, kind, status, status_reason, auth_session_ref FROM runs")
        checks.append(("py_exactly_one_run", len(runs) == 1, len(runs)))
        run = runs[0] if runs else None
        run_id = run["id"] if run else None
        checks.append(("py_run_kind_is_chat_stream",
                       bool(run) and run["kind"] == "chat_stream",
                       None if run is None else run["kind"]))
        checks.append(("py_run_status_succeeded",
                       bool(run) and run["status"] == "succeeded",
                       None if run is None else run["status"]))
        checks.append(("py_run_bound_to_real_session_family",
                       bool(run) and bool((run["auth_session_ref"] or "").strip()),
                       None if run is None else bool(run["auth_session_ref"])))

        steps = rows("SELECT seq, kind, status, output_payload_id, input_ref "
                     "FROM run_steps WHERE run_id = ? ORDER BY seq ASC", (run_id,))
        kinds = [s["kind"] for s in steps]
        checks.append(("py_exactly_three_steps", len(steps) == 3, len(steps)))
        checks.append(("py_step_kinds_context_dispatch_generate",
                       kinds == ["load_context", "tool_dispatch", "llm_generate"],
                       kinds))
        checks.append(("py_all_steps_succeeded",
                       len(steps) == 3 and all(s["status"] == "succeeded"
                                               for s in steps),
                       [s["status"] for s in steps]))
        disp = next((s for s in steps if s["kind"] == "tool_dispatch"), None)
        checks.append(("py_dispatch_step_sealed_tool_output",
                       bool(disp) and bool(disp["output_payload_id"]),
                       None if disp is None else disp["output_payload_id"]))
        ref = _json_or_none((disp["input_ref"] if disp is not None else "") or "") or {}
        checks.append(("py_dispatch_origin_is_python_execution",
                       ref.get("purpose") == "python_execution", ref.get("purpose")))
        checks.append(("py_dispatch_input_ref_carries_no_raw_code",
                       bool(ref) and "code" not in ref
                       and {"args_hash", "args_payload_id", "code_bytes",
                            "purpose"} <= set(ref),
                       sorted(ref)))

        approvals = rows("SELECT id, state, tool_id, display_summary FROM pending_approvals")
        checks.append(("py_exactly_one_approval", len(approvals) == 1, len(approvals)))
        ap = approvals[0] if approvals else None
        checks.append(("py_approval_consumed",
                       bool(ap) and ap["state"] == "consumed",
                       None if ap is None else ap["state"]))
        checks.append(("py_approval_tool_is_python_sandbox",
                       bool(ap) and ap["tool_id"] == "python_sandbox",
                       None if ap is None else ap["tool_id"]))
        checks.append(("py_approval_summary_redacted",
                       bool(ap) and PYTHON_CODE not in (ap["display_summary"] or ""),
                       None))

        dispatch = rows("SELECT outcome FROM dispatch_records")
        checks.append(("py_exactly_one_dispatch", len(dispatch) == 1, len(dispatch)))
        checks.append(("py_dispatch_succeeded",
                       len(dispatch) == 1 and dispatch[0]["outcome"] == "succeeded",
                       [d["outcome"] for d in dispatch]))

        user_msgs = rows("SELECT content FROM messages WHERE role = 'user'")
        asst = rows("SELECT content FROM messages WHERE role = 'assistant'")
        checks.append(("py_exactly_one_user_message", len(user_msgs) == 1, len(user_msgs)))
        checks.append(("py_user_message_matches_prompt",
                       len(user_msgs) == 1 and user_msgs[0]["content"] == prompt, None))
        checks.append(("py_exactly_one_assistant_message", len(asst) == 1, len(asst)))
        checks.append(("py_assistant_message_matches_expected",
                       len(asst) == 1
                       and asst[0]["content"] == PYTHON_EXPECTED_ASSISTANT,
                       None if len(asst) != 1 else len(asst[0]["content"])))

        counts = {t: conn.execute("SELECT COUNT(*) AS n FROM " + t).fetchone()["n"]
                  for t in ("tool_calls", "execution_turns", "run_signals")}
        checks.append(("py_exactly_one_tool_call_turn_and_signal",
                       counts == {"tool_calls": 1, "execution_turns": 1,
                                  "run_signals": 1},
                       counts))

        payloads = {r["payload_type"]: r["n"] for r in rows(
            "SELECT payload_type, COUNT(*) AS n FROM run_payloads "
            "GROUP BY payload_type")}
        checks.append(("py_tool_args_and_tool_output_sealed_once",
                       payloads.get("tool_args") == 1
                       and payloads.get("tool_output") == 1,
                       payloads))

        leaked = []
        for table, cols in (("run_events", ("data", "type")),
                            ("run_steps", ("input_ref", "output_ref", "error_code")),
                            ("pending_approvals", ("display_summary", "purpose_redacted")),
                            ("messages", ("content", "citations"))):
            for row in rows("SELECT * FROM " + table):
                for col in cols:
                    try:
                        value = row[col]
                    except (IndexError, KeyError):
                        continue
                    if isinstance(value, str) and (
                            PYTHON_CODE in value or PYTHON_CANARY in value):
                        leaked.append(table + "." + col)
        checks.append(("py_raw_code_absent_from_public_fields",
                       not leaked, sorted(set(leaked))))

        return {"run_id": run_id, "user_id": user_id}
    finally:
        conn.close()


def _check_python_executor(checks: list, root: Path) -> None:
    """The counting fake executor must have been called EXACTLY once, must have
    seen a committed DispatchRecord(outcome='started') at call time, and must
    have received the code reopened from the sealed tool_args payload."""
    recs = _read_jsonl(root / "python_executor_calls.jsonl")
    checks.append(("py_executor_invoked_exactly_once", len(recs) == 1, len(recs)))
    rec = recs[0] if recs else {}
    checks.append(("py_executor_saw_committed_started_dispatch",
                   bool(rec) and int(rec.get("started_dispatch_at_call") or 0) >= 1,
                   rec.get("started_dispatch_at_call")))
    checks.append(("py_executor_received_the_sealed_proposal",
                   bool(rec) and rec.get("code_matches_proposal") is True,
                   rec.get("code_matches_proposal")))


def _check_python_provider(checks: list, root: Path) -> None:
    """Exactly one PROPOSAL invocation and exactly one RESUME invocation."""
    recs = _read_jsonl(root / "python_provider_calls.jsonl")
    invoked = [r for r in recs if r.get("event") == "provider_invoked"]
    checks.append(("py_provider_invoked_twice", len(invoked) == 2, len(invoked)))
    checks.append(("py_provider_ordinals_are_one_and_two",
                   [r.get("ordinal") for r in invoked] == [1, 2],
                   [r.get("ordinal") for r in invoked]))


def _check_python_action_log(checks: list, db_path: Path, user_id) -> None:
    """Tamper-evident chain: recompute every row_hash and the linkage with the
    PRODUCTION verifier, for the smoke user's own scope."""
    name = "py_action_log_chain_verified"
    if user_id is None:
        checks.append((name, False, "no smoke user id"))
        return
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.approval.action_log import verify_chain

        eng = create_engine("sqlite:///" + db_path.as_posix())
        try:
            db = sessionmaker(bind=eng)()
            try:
                rep = verify_chain(db, tenant_id=None, user_id=int(user_id))
            finally:
                db.close()
        finally:
            eng.dispose()
    except Exception as exc:  # noqa: BLE001
        checks.append((name, False, "error: %s" % str(exc)[:160]))
        return
    checks.append((name, bool(rep.ok),
                   {"ok": rep.ok, "scope": rep.scope, "count": rep.count,
                    "break_at": rep.break_at, "detail": rep.detail}))
    checks.append(("py_action_log_has_five_events", rep.count == 5, rep.count))


def _check_python_browser(checks: list, browser_result) -> None:
    """Browser-observable invariants. Console errors are CLASSIFIED: the
    pre-login `/api/auth/refresh` 401 is recorded separately and is never folded
    into an unqualified 'zero console errors' claim."""
    if browser_result is None:
        checks.append(("py_browser_result_present", False, None))
        return
    checks.append(("py_browser_result_present", True, None))
    rendered = browser_result.get("rendered_text") or ""
    checks.append(("py_browser_rendered_matches_expected",
                   rendered == PYTHON_EXPECTED_ASSISTANT,
                   {"chars": len(rendered),
                    "expected_chars": len(PYTHON_EXPECTED_ASSISTANT)}))
    checks.append(("py_browser_reached_completed_state",
                   bool(browser_result.get("completed")),
                   bool(browser_result.get("completed"))))
    checks.append(("py_browser_observed_approval_dialog",
                   bool(browser_result.get("approval_dialog_seen")),
                   bool(browser_result.get("approval_dialog_seen"))))
    checks.append(("py_browser_single_stream_post",
                   browser_result.get("stream_post_count") == 1,
                   browser_result.get("stream_post_count")))
    checks.append(("py_browser_single_idempotency_key",
                   browser_result.get("idempotency_key_count") == 1,
                   browser_result.get("idempotency_key_count")))

    errs = browser_result.get("console_errors") or []
    pre = browser_result.get("console_errors_pre_login") or []
    checks.append(("py_browser_console_errors_zero_after_login", len(errs) == 0,
                   {"count": len(errs), "samples": [str(e)[:120] for e in errs[:3]]}))
    checks.append(("py_browser_prelogin_errors_are_the_refresh_401",
                   all("401" in str(e) for e in pre),
                   {"count": len(pre),
                    "samples": [str(e)[:120] for e in pre[:3]]}))
    checks.append(("py_browser_page_errors_zero",
                   len(browser_result.get("page_errors") or []) == 0,
                   len(browser_result.get("page_errors") or [])))
    # MP-4C informed approval: the approver must have SEEN the exact code in
    # the dialog before Approve was enabled, and it must equal the sealed
    # proposal. This is the one place the code is legitimately rendered.
    checks.append(("py_browser_exact_code_preview_seen",
                   bool(browser_result.get("exact_code_preview_seen")),
                   bool(browser_result.get("exact_code_preview_seen"))))
    checks.append(("py_browser_exact_code_matches_the_proposal",
                   browser_result.get("exact_code_preview_code") == PYTHON_CODE,
                   {"matches": (browser_result.get("exact_code_preview_code")
                               == PYTHON_CODE)}))
    checks.append(("py_raw_code_absent_from_rendered_ui",
                   PYTHON_CODE not in rendered and PYTHON_CANARY not in rendered,
                   None))


# ── python_deny acceptance profile (MP-4C) ──────────────────────────
#
# The denied counterpart of the python_approve profile. A denied Run is
# terminal `denied` with NO status_reason, exactly two steps (load_context
# succeeded, tool_dispatch blocked/approval_denied), ZERO dispatch records,
# ZERO tool_output payloads, ZERO assistant messages and ZERO executor or MCP
# transport calls. The ActionLog chain carries the denial events only.


def check_python_deny_database(db_path: Path, prompt: str, checks: list) -> dict:
    conn = _connect(db_path)
    try:
        def rows(sql, params=()):
            return conn.execute(sql, params).fetchall()

        users = rows("SELECT id, username FROM users WHERE username = ?",
                     (SMOKE_USERNAME,))
        checks.append(("pyd_exactly_one_smoke_user", len(users) == 1, len(users)))
        user_id = users[0]["id"] if users else None

        runs = rows("SELECT id, kind, status, status_reason, auth_session_ref FROM runs")
        checks.append(("pyd_exactly_one_run", len(runs) == 1, len(runs)))
        run = runs[0] if runs else None
        run_id = run["id"] if run else None
        checks.append(("pyd_run_kind_is_chat_stream",
                       bool(run) and run["kind"] == "chat_stream",
                       None if run is None else run["kind"]))
        checks.append(("pyd_run_status_denied",
                       bool(run) and run["status"] == "denied",
                       None if run is None else run["status"]))
        checks.append(("pyd_run_status_reason_is_none",
                       bool(run) and run["status_reason"] is None,
                       None if run is None else run["status_reason"]))
        checks.append(("pyd_run_bound_to_real_session_family",
                       bool(run) and bool((run["auth_session_ref"] or "").strip()),
                       None if run is None else bool(run["auth_session_ref"])))

        steps = rows("SELECT seq, kind, status, error_code, output_payload_id, "
                     "input_ref FROM run_steps WHERE run_id = ? ORDER BY seq ASC",
                     (run_id,))
        kinds = [s["kind"] for s in steps]
        checks.append(("pyd_exactly_two_steps", len(steps) == 2, len(steps)))
        checks.append(("pyd_step_kinds_context_then_dispatch",
                       kinds == ["load_context", "tool_dispatch"], kinds))
        load = next((s for s in steps if s["kind"] == "load_context"), None)
        checks.append(("pyd_load_context_succeeded",
                       bool(load) and load["status"] == "succeeded",
                       None if load is None else load["status"]))
        disp = next((s for s in steps if s["kind"] == "tool_dispatch"), None)
        checks.append(("pyd_dispatch_blocked_approval_denied",
                       bool(disp) and disp["status"] == "blocked"
                       and disp["error_code"] == "approval_denied",
                       None if disp is None else [disp["status"],
                                                  disp["error_code"]]))
        checks.append(("pyd_dispatch_has_no_tool_output",
                       bool(disp) and not disp["output_payload_id"],
                       None if disp is None else disp["output_payload_id"]))
        ref = _json_or_none((disp["input_ref"] if disp is not None else "") or "") or {}
        checks.append(("pyd_dispatch_origin_is_python_execution",
                       ref.get("purpose") == "python_execution", ref.get("purpose")))
        checks.append(("pyd_dispatch_input_ref_carries_no_raw_code",
                       bool(ref) and "code" not in ref
                       and {"args_hash", "args_payload_id", "code_bytes",
                            "purpose"} <= set(ref),
                       sorted(ref)))

        approvals = rows("SELECT state, tool_id, display_summary FROM pending_approvals")
        checks.append(("pyd_exactly_one_approval", len(approvals) == 1, len(approvals)))
        ap = approvals[0] if approvals else None
        checks.append(("pyd_approval_state_denied",
                       bool(ap) and ap["state"] == "denied",
                       None if ap is None else ap["state"]))
        checks.append(("pyd_approval_tool_is_python_sandbox",
                       bool(ap) and ap["tool_id"] == "python_sandbox",
                       None if ap is None else ap["tool_id"]))
        checks.append(("pyd_approval_summary_redacted",
                       bool(ap) and PYTHON_CODE not in (ap["display_summary"] or ""),
                       None))

        n_dispatch = len(rows("SELECT 1 FROM dispatch_records"))
        checks.append(("pyd_zero_dispatch_records", n_dispatch == 0, n_dispatch))
        n_out = len(rows("SELECT 1 FROM run_payloads WHERE payload_type = 'tool_output'"))
        checks.append(("pyd_zero_tool_output_payloads", n_out == 0, n_out))
        n_args = len(rows("SELECT 1 FROM run_payloads WHERE payload_type = 'tool_args'"))
        checks.append(("pyd_exactly_one_tool_args_payload", n_args == 1, n_args))

        user_msgs = rows("SELECT content FROM messages WHERE role = 'user'")
        asst = rows("SELECT 1 FROM messages WHERE role = 'assistant'")
        checks.append(("pyd_exactly_one_user_message", len(user_msgs) == 1, len(user_msgs)))
        checks.append(("pyd_user_message_matches_prompt",
                       len(user_msgs) == 1 and user_msgs[0]["content"] == prompt, None))
        checks.append(("pyd_zero_assistant_messages", len(asst) == 0, len(asst)))

        counts = {t: conn.execute("SELECT COUNT(*) AS n FROM " + t).fetchone()["n"]
                  for t in ("tool_calls", "execution_turns", "run_signals")}
        checks.append(("pyd_one_tool_call_turn_and_signal",
                       counts == {"tool_calls": 1, "execution_turns": 1,
                                  "run_signals": 1},
                       counts))

        leaked = []
        for table, cols in (("run_events", ("data", "type")),
                            ("run_steps", ("input_ref", "output_ref", "error_code")),
                            ("pending_approvals", ("display_summary", "purpose_redacted")),
                            ("messages", ("content", "citations"))):
            for row in rows("SELECT * FROM " + table):
                for col in cols:
                    try:
                        value = row[col]
                    except (IndexError, KeyError):
                        continue
                    if isinstance(value, str) and (
                            PYTHON_CODE in value or PYTHON_CANARY in value):
                        leaked.append(table + "." + col)
        checks.append(("pyd_raw_code_absent_from_public_fields",
                       not leaked, sorted(set(leaked))))

        return {"run_id": run_id, "user_id": user_id}
    finally:
        conn.close()


def _check_python_deny_executor(checks: list, root: Path) -> None:
    """A DENIED run must never execute Python or touch the MCP transport."""
    recs = _read_jsonl(root / "python_executor_calls.jsonl")
    checks.append(("pyd_executor_never_called", len(recs) == 0, len(recs)))
    trecs = _read_jsonl(root / "python_mcp_transport_calls.jsonl")
    checks.append(("pyd_mcp_transport_never_called", len(trecs) == 0, len(trecs)))


def _check_python_deny_provider(checks: list, root: Path) -> None:
    """The provider is invoked ONLY for the marker segment (ordinal 1); there
    is no resume invocation."""
    recs = _read_jsonl(root / "python_provider_calls.jsonl")
    invoked = [r for r in recs if r.get("event") == "provider_invoked"]
    checks.append(("pyd_provider_invoked_once_for_the_marker_segment",
                   len(invoked) == 1, len(invoked)))
    checks.append(("pyd_provider_ordinal_is_one",
                   [r.get("ordinal") for r in invoked] == [1],
                   [r.get("ordinal") for r in invoked]))


def _check_python_deny_action_log(checks: list, db_path: Path, user_id) -> None:
    name = "pyd_action_log_chain_verified"
    if user_id is None:
        checks.append((name, False, "no smoke user id"))
        return
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.approval.action_log import ActionLogEntry, verify_chain

        eng = create_engine("sqlite:///" + db_path.as_posix())
        try:
            db = sessionmaker(bind=eng)()
            try:
                rep = verify_chain(db, tenant_id=None, user_id=int(user_id))
                types = [r.event_type for r in db.query(ActionLogEntry)
                         .filter(ActionLogEntry.chain_scope_id == rep.scope)
                         .order_by(ActionLogEntry.seq.asc()).all()]
            finally:
                db.close()
        finally:
            eng.dispose()
    except Exception as exc:  # noqa: BLE001
        checks.append((name, False, "error: %s" % str(exc)[:160]))
        return
    checks.append((name, bool(rep.ok),
                   {"ok": rep.ok, "scope": rep.scope, "count": rep.count,
                    "break_at": rep.break_at, "detail": rep.detail}))
    checks.append(("pyd_action_log_denial_events",
                   types == ["approval.requested", "approval.denied"], types))
    checks.append(("pyd_action_log_has_no_execution_events",
                   not any(t.startswith("tool.execution") for t in types)
                   and "approval.consumed" not in types, types))


def _check_python_deny_browser(checks: list, browser_result) -> None:
    if browser_result is None:
        checks.append(("pyd_browser_result_present", False, None))
        return
    checks.append(("pyd_browser_result_present", True, None))
    checks.append(("pyd_browser_denial_observed",
                   bool(browser_result.get("denial_observed")),
                   bool(browser_result.get("denial_observed"))))
    checks.append(("pyd_browser_terminal_error_code",
                   browser_result.get("terminal_error_code") == "approval_denied",
                   browser_result.get("terminal_error_code")))
    checks.append(("pyd_browser_no_assistant_answer",
                   not browser_result.get("assistant_answer_rendered"),
                   browser_result.get("assistant_answer_rendered")))
    state = browser_result.get("post_reload") or {}
    checks.append(("pyd_browser_reload_keeps_user_message",
                   bool(state.get("user_message")), state))
    checks.append(("pyd_browser_reload_shows_no_assistant_message",
                   state.get("assistant_message") is False, state))
    checks.append(("pyd_browser_single_stream_post",
                   browser_result.get("stream_post_count") == 1,
                   browser_result.get("stream_post_count")))
    checks.append(("pyd_browser_single_idempotency_key",
                   browser_result.get("idempotency_key_count") == 1,
                   browser_result.get("idempotency_key_count")))
    errs = browser_result.get("console_errors") or []
    pre = browser_result.get("console_errors_pre_login") or []
    checks.append(("pyd_browser_console_errors_zero_after_login", len(errs) == 0,
                   {"count": len(errs), "samples": [str(e)[:120] for e in errs[:3]]}))
    checks.append(("pyd_browser_prelogin_errors_are_the_refresh_401",
                   all("401" in str(e) for e in pre),
                   {"count": len(pre), "samples": [str(e)[:120] for e in pre[:3]]}))
    checks.append(("pyd_browser_page_errors_zero",
                   len(browser_result.get("page_errors") or []) == 0,
                   len(browser_result.get("page_errors") or [])))
    rendered = browser_result.get("rendered_text") or ""
    checks.append(("pyd_raw_code_absent_from_rendered_ui",
                   PYTHON_CODE not in rendered and PYTHON_CANARY not in rendered,
                   None))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="D6B durable-chat browser smoke")
    parser.add_argument("--browser-timeout", type=float, default=300.0,
                        help="seconds to wait for the external Chromium controller")
    parser.add_argument("--keep-root", action="store_true",
                        help="keep the temp root for inspection (default: remove)")
    parser.add_argument("--scenario", default="",
                        help="tests/smoke scenario switch, e.g. mcp_approve / mcp_deny")
    parser.add_argument("--max-provider-invocations", type=int, default=1,
                        help="TEST-ONLY diagnostic bound; the D6B acceptance run uses 1")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
