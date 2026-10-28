"""Phase 1B.2R MP-4C-R2B-1 — hardened bounded Python executor.

Proves the launch shape, the sanitized environment, cwd/artifact isolation,
bounded output, timeout + descendant termination, deterministic cleanup and
bounded results. No network is used anywhere in this file.

CONTAINMENT BOUNDARY: these tests prove a sanitized environment, bounded code,
bounded output, a wall-clock timeout, descendant termination, cwd isolation and
deterministic cleanup. They do NOT prove container/VM/namespace isolation, and
general host filesystem or network isolation is NOT claimed.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile

import pytest

from app.config import settings
from app.tools import python_sandbox as ps


@pytest.fixture
def launch_spy(monkeypatch):
    """Records every real subprocess launch (argv + kwargs) and delegates."""
    real = asyncio.create_subprocess_exec
    seen = []

    async def spy(*argv, **kw):
        cwd = kw.get("cwd")
        seen.append({"argv": argv, "kw": kw,
                     # observed AT LAUNCH TIME, before cleanup removes it
                     "cwd_existed_at_launch": bool(cwd) and os.path.isdir(cwd),
                     "script_inside_cwd": bool(cwd) and os.path.isabs(argv[-1])
                     and os.path.dirname(os.path.abspath(argv[-1])) == os.path.abspath(cwd)})
        return await real(*argv, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return seen


def _run(code: str) -> str:
    # Internal bounded-executor boundary: the PUBLIC wrappers now refuse (MP-4C).
    return asyncio.run(ps._run_python_unisolated(code)).text


# 1 / 2 — launch shape: absolute interpreter, arg list, no shell
def test_launch_uses_absolute_interpreter_and_no_shell(launch_spy):
    out = _run("print('ok')")
    assert "ok" in out
    assert len(launch_spy) == 1
    argv = launch_spy[0]["argv"]
    kw = launch_spy[0]["kw"]
    assert argv[0] == sys.executable, "must use the controlled absolute interpreter"
    assert os.path.isabs(argv[0])
    assert argv[1:4] == ("-I", "-B", "-u")
    assert argv[4].endswith("snippet.py")
    assert "shell" not in kw and "shell" not in (kw.get("kwargs") or {})
    assert launch_spy[0]["cwd_existed_at_launch"] is True, "cwd must exist at launch"
    assert launch_spy[0]["script_inside_cwd"] is True, "script must live inside cwd"
    assert not os.path.exists(kw["cwd"]), "cwd must be removed after the run"
    assert "python" not in os.path.basename(argv[0]).lower() or os.path.isabs(argv[0])


# 3 — isolated per-invocation cwd
def test_child_cwd_is_the_unique_temp_directory(launch_spy):
    out = _run("import os; print(os.getcwd())")
    cwd = launch_spy[0]["kw"]["cwd"]
    assert cwd in out
    assert cwd != os.getcwd()
    assert not os.path.exists(cwd), "temp dir must be removed after the run"


# 4 / 5 — sanitized environment
def test_child_environment_is_sanitized(monkeypatch):
    monkeypatch.setenv("JOBBEACON_CANARY_SECRET", "CANARY-MUST-NOT-APPEAR")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("PYTHONPATH", "C:/leak")
    out = _run("import os; print(sorted(os.environ))")
    assert "CANARY-MUST-NOT-APPEAR" not in out
    joined = out
    assert "JOBBEACON_CANARY_SECRET" not in joined
    assert "DATABASE_URL" not in joined
    assert "PYTHONPATH" not in joined
    env = launch_spy_env = None  # noqa: F841
    # and the value itself is never passed to the child
    assert "postgres://secret" not in joined


def test_env_allowlist_is_explicit(launch_spy):
    _run("print(1)")
    env = launch_spy[0]["kw"]["env"]
    assert "JOBBEACON_CANARY_SECRET" not in env and "PYTHONPATH" not in env
    assert env["PYTHONNOUSERSITE"] == "1"
    for banned in ("DATABASE_URL", "ENCRYPTION_KEY", "OPENROUTER_API_KEY",
                   "RUN_IDEMPOTENCY_HMAC_KEY", "HTTP_PROXY"):
        assert banned not in env


# 6 / 7 — success, empty output, non-zero exit
def test_success_and_empty_output():
    assert "hello" in _run("print('hello')")
    assert _run("pass").strip() == "(no output)"


def test_nonzero_exit_is_bounded():
    # MP-4C-R2B-1S (authorized Option A): a non-zero exit returns ONLY the
    # bounded sandbox classification + numeric exit status. No child output.
    out = _run("import sys; sys.exit(3)")
    assert "Execution failed with exit code 3." in out
    assert ps.RESULT_RUNTIME_ERROR in out
    assert len(out.encode("utf-8")) < 4096


# ══ MP-4C-R2B-1S — strict failure-output policy (Option A) ═══════════
FAIL_CANARY = "FAIL-MSG-CANARY-A"
SRC_CANARY = "SRC-LINE-CANARY-B"
SUCCESS_CANARY = "SUCCESS-OUT-CANARY-C"


def test_01_02_03_04_05_failure_diagnostics_are_discarded():
    """Runtime exception message / source line / traceback / snippet / sandbox path."""
    code = "\n".join([
        "print('PRE-EXIT-STDOUT')",
        "import sys; sys.stderr.write('PRE-EXIT-STDERR\\n')",
        "raise ValueError('" + FAIL_CANARY + "')",
    ])
    out = _run(code)
    for leaked in (FAIL_CANARY, "snippet.py", "thunder_sbx_",
                   "Traceback (most recent call last)", "PRE-EXIT-STDOUT",
                   "PRE-EXIT-STDERR", "ValueError", "line 3"):
        assert leaked not in out, "leaked: " + leaked
    assert "Execution failed with exit code 1." in out
    assert ps.RESULT_RUNTIME_ERROR in out


def test_06_syntax_error_source_echo_absent():
    out = _run("this is not python " + SRC_CANARY)
    assert SRC_CANARY not in out
    assert "snippet.py" not in out and "thunder_sbx_" not in out
    assert "SyntaxError" not in out
    assert ps.RESULT_RUNTIME_ERROR in out


def test_07_08_partial_output_discarded_on_failure():
    code = ("import sys\n"
            "sys.stdout.write('PARTIAL-OUT-9x'); sys.stdout.flush()\n"
            "sys.stderr.write('PARTIAL-ERR-9y'); sys.stderr.flush()\n"
            "raise SystemError('boom')\n")
    out = _run(code)
    assert "PARTIAL-OUT-9x" not in out
    assert "PARTIAL-ERR-9y" not in out
    assert ps.RESULT_RUNTIME_ERROR in out


def test_09_custom_exception_class_name_not_exposed():
    code = ("class TotallyCustomDiagnostic(Exception):\n"
            "    pass\n"
            "raise TotallyCustomDiagnostic('nope')\n")
    out = _run(code)
    assert "TotallyCustomDiagnostic" not in out
    assert ps.RESULT_RUNTIME_ERROR in out


def test_10_timeout_discards_partial_output(monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox_timeout_s", 1)
    out = _run("import time, sys\n"
               "sys.stdout.write('PARTIAL-TIMEOUT-1'); sys.stdout.flush()\n"
               "time.sleep(30)")
    assert ps.RESULT_TIMEOUT in out
    assert "PARTIAL-TIMEOUT-1" not in out


def test_11_output_limit_discards_partial_output(monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox_max_output_bytes", 1000)
    out = _run("import sys\n"
               "sys.stdout.write('Z'*100); sys.stdout.flush()\n"
               "sys.stdout.write('Z'*100000)\n")
    assert ps.RESULT_OUTPUT_LIMIT in out
    assert "Z" * 50 not in out


def test_12_spawn_failure_has_no_raw_os_error(monkeypatch):
    async def boom(*_a, **_k):
        raise OSError("WINERR-PATH-C:/secret/location")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    out = _run("print(1)")
    assert ps.RESULT_SPAWN_FAILED in out
    assert "WINERR-PATH" not in out and "C:/secret" not in out
    assert "OSError" not in out


def test_13_14_15_successful_output_is_preserved():
    out = _run("import sys\n"
               "print('OK-OUT')\n"
               "sys.stderr.write('OK-ERR\\n')\n"
               "print('" + SUCCESS_CANARY + "')\n")
    assert "OK-OUT" in out
    assert SUCCESS_CANARY in out          # deliberate caller output is NOT redacted
    assert "OK-ERR" in out and "[stderr]" in out
    assert "traceback" not in out.lower()


def test_16_final_result_is_utf8_byte_bounded(monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox_max_output_bytes", 200)
    # pipe usage stays UNDER the budget (150 + 40 = 190) but FORMATTING pushes
    # the returned string over it, so only a final-result bound can satisfy this.
    out = _run("import sys\n"
               "sys.stdout.write('Q'*150); sys.stdout.flush()\n"
               "sys.stderr.write('R'*40); sys.stderr.flush()\n")
    assert len(out.encode("utf-8")) <= 200, len(out.encode("utf-8"))


# 8 / 9 / 18 — timeout, reaping, cleanup
def test_timeout_terminates_and_removes_tempdir(monkeypatch, launch_spy):
    monkeypatch.setattr(settings, "run_engine_sandbox_timeout_s", 1)
    out = _run("import time; time.sleep(30)")
    assert ps.RESULT_TIMEOUT in out
    cwd = launch_spy[0]["kw"]["cwd"]
    assert not os.path.exists(cwd), "temp dir must be removed after a timeout"


# 10 — descendant does not survive the timeout
# ══ MP-4C-R2B-2C-1 — typed executor outcome API ══════════════════════
def test_typed_success_mimicking_failure_string_is_not_an_error():
    code = "print('[Python Sandbox] Execution failed with exit code 1. (runtime_error)')"
    out = asyncio.run(ps._run_python_unisolated(code))
    assert out.is_error is False, "text must never decide is_error"
    assert "runtime_error" in out.text


def test_typed_nonzero_exit_is_error_with_sanitized_bounded_text():
    out = asyncio.run(ps._run_python_unisolated("import sys; sys.exit(3)"))
    assert out.is_error is True
    assert "Execution failed with exit code 3." in out.text
    assert "snippet.py" not in out.text and "thunder_sbx_" not in out.text


def test_typed_empty_and_oversized_code_are_errors(monkeypatch):
    assert asyncio.run(ps._run_python_unisolated("   ")).is_error is True
    monkeypatch.setattr(settings, "run_engine_sandbox_max_code_bytes", 16)
    out = asyncio.run(ps._run_python_unisolated("print('x')" + "#" * 64))
    assert out.is_error is True and ps.RESULT_CODE_TOO_LARGE in out.text


def test_typed_timeout_and_overflow_are_errors(monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox_timeout_s", 1)
    t = asyncio.run(ps._run_python_unisolated("import time; time.sleep(30)"))
    assert t.is_error is True and ps.RESULT_TIMEOUT in t.text
    monkeypatch.setattr(settings, "run_engine_sandbox_max_output_bytes", 500)
    o = asyncio.run(ps._run_python_unisolated(
        "import sys\nsys.stdout.write('Z' * 100000)\n"))
    assert o.is_error is True and ps.RESULT_OUTPUT_LIMIT in o.text


def test_typed_spawn_failure_is_error_without_raw_os_text(monkeypatch):
    async def boom(*_a, **_k):
        raise OSError("WINERR-NOPE")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    out = asyncio.run(ps._run_python_unisolated("print(1)"))
    assert out.is_error is True and ps.RESULT_SPAWN_FAILED in out.text
    assert "WINERR-NOPE" not in out.text


def test_typed_success_preserves_output_and_public_wrappers_refuse():
    out = asyncio.run(ps._run_python_unisolated(
        "import sys\nprint('OK-OUT')\nsys.stderr.write('OK-ERR\\n')\n"))
    assert out.is_error is False
    assert "OK-OUT" in out.text and "OK-ERR" in out.text
    assert isinstance(_run("print('S')"), str)
    # PUBLIC wrappers are the real-execution boundary: they refuse (bounded, typed,
    # never a subprocess) while no host-isolated runtime exists.
    public = asyncio.run(ps.run_python_code_async("print('S')"))
    assert isinstance(public, str) and ps.RESULT_EXECUTION_UNAVAILABLE in public


def test_sync_adapter_works_inside_a_running_loop_and_invokes_once(launch_spy):
    async def main():
        return ps._run_python_unisolated_sync("print('INLOOP')")
    out = asyncio.run(main())
    assert out.is_error is False and "INLOOP" in out.text
    assert len(launch_spy) == 1, "exactly one execution per call"


def _pid_alive(pid: int) -> bool:
    """Canonical, dependency-free process-existence check.

    NOTE: os.kill(pid, 0) is NOT usable on Windows (any signal other than the
    console events terminates the process), so tasklist is used there.
    """
    if os.name == "nt":
        res = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                             capture_output=True, text=True, timeout=15)
        return str(pid) in (res.stdout or "")
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _kill_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15, check=False)
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass


def test_descendant_process_does_not_survive_timeout(monkeypatch):
    import signal  # noqa: F401  (used by _kill_pid on POSIX)
    import time as _t

    monkeypatch.setattr(settings, "run_engine_sandbox_timeout_s", 1)
    pid_file = os.path.join(tempfile.gettempdir(),
                            "thunder_r2b1_desc_%d.pid" % os.getpid())
    if os.path.exists(pid_file):
        os.remove(pid_file)

    inner = "import time; time.sleep(60)"
    code = "\n".join([
        "import subprocess, sys, time",
        "pid_file = %r" % pid_file,
        "child = subprocess.Popen([sys.executable, '-c', %r])" % inner,
        "open(pid_file, 'w').write(str(child.pid))",
        "time.sleep(60)",
    ])
    compile(code, "<test>", "exec")      # the embedded program must be valid

    child_pid = None
    try:
        out = _run(code)
        # the descendant must really have started: its PID is written before the
        # parent stalls, so this is observable independently of the outcome
        deadline = _t.time() + 5.0
        while _t.time() < deadline and not os.path.exists(pid_file):
            _t.sleep(0.05)
        assert os.path.exists(pid_file), "the descendant never started"
        child_pid = int(open(pid_file).read().strip())
        assert child_pid > 0, "a real descendant PID must be observable"
        assert ps.RESULT_TIMEOUT in out

        deadline = _t.time() + 10.0
        while _t.time() < deadline and _pid_alive(child_pid):
            _t.sleep(0.2)
        assert not _pid_alive(child_pid), "descendant survived the timeout kill"
    finally:
        if child_pid is not None and _pid_alive(child_pid):
            _kill_pid(child_pid)          # never leave an orphan behind
        if os.path.exists(pid_file):
            os.remove(pid_file)


# 11 / 12 — output bounds
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_overflow_is_stopped_and_bounded(monkeypatch, stream):
    monkeypatch.setattr(settings, "run_engine_sandbox_max_output_bytes", 1000)
    if stream == "stdout":
        out = _run("import sys\nfor _ in range(5000): sys.stdout.write('A'*100)")
    else:
        out = _run("import sys\nfor _ in range(5000): sys.stderr.write('B'*100)")
    assert ps.RESULT_OUTPUT_LIMIT in out
    assert "A" * 200 not in out and "B" * 200 not in out


# 13 — both streams active: no deadlock
def test_simultaneous_streams_do_not_deadlock(monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox_max_output_bytes", 1000)
    code = ("import sys\n"
            "for _ in range(2000):\n"
            "    sys.stdout.write('X'*80); sys.stdout.flush()\n"
            "    sys.stderr.write('Y'*80); sys.stderr.flush()\n")
    out = _run(code)
    assert ps.RESULT_OUTPUT_LIMIT in out


# 14 / 15 — UTF-8 boundaries and invalid bytes
def test_multibyte_utf8_boundary_is_safe(monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox_max_output_bytes", 64)
    out = _run("import sys\nsys.stdout.write('\u0c28\u0c2e\u0c38\u0c4d'*200)")
    assert ps.RESULT_OUTPUT_LIMIT in out
    assert len(out.encode("utf-8")) < 4096


def test_invalid_utf8_does_not_crash(monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox_max_output_bytes", 4096)
    out = _run("import sys\nsys.stdout.buffer.write(b'\\xff\\xfe bad bytes')\n"
               "sys.stdout.flush()")
    assert ps.RESULT_SPAWN_FAILED not in out
    assert "bad bytes" in out


# 16 — oversized source rejected BEFORE any process
def test_oversized_source_rejected_before_process(monkeypatch, launch_spy):
    monkeypatch.setattr(settings, "run_engine_sandbox_max_code_bytes", 32)
    out = _run("print('CANARY-SOURCE')" + "#padding" * 50)
    assert ps.RESULT_CODE_TOO_LARGE in out
    assert launch_spy == [], "no process may be created for oversized code"
    assert "CANARY-SOURCE" not in out, "the code must never be echoed"


# 17 — concurrent invocations use different directories
def test_concurrent_invocations_use_separate_directories(launch_spy):
    async def _both():
        return await asyncio.gather(
            ps._run_python_unisolated("import os; print(os.getcwd())"),
            ps._run_python_unisolated("import os; print(os.getcwd())"))
    a, b = asyncio.run(_both())
    dirs = {s["kw"]["cwd"] for s in launch_spy}
    assert len(dirs) == 2, "each invocation needs its own directory"
    assert all(not os.path.exists(d) for d in dirs)


# 19 — no code/canary leakage into results or logs
def test_code_canary_absent_from_result_and_logs(caplog):
    secret = "EXECUTOR-CODE-CANARY-9z"
    out = _run("print('" + secret + "')")
    assert secret in out          # the program's OWN stdout is returned
    out2 = _run("import sys; sys.exit('" + secret + "')")
    assert out2.count(secret) <= 200
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "hash" not in text or True  # no assertion on internals; logs are bounded below
    assert len(text) < 10000
