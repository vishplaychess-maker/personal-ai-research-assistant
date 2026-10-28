"""
Python Code Sandbox Tool for the AI Research Agent.

Executes Python code provided by the LLM inside the container's Python
interpreter, with a hard 15-second timeout, and returns stdout/stderr so
the LLM can debug or report results to the user.

This is a code-interpreter style tool: the LLM writes Python code, and the
tool runs it and returns the output. It is intentionally isolated to the
container and bounded by a timeout.

Usage (as a LangChain-style tool):
    from app.tools.python_sandbox import execute_python_code
    result = execute_python_code.invoke({"code": "print(1 + 1)"})

Usage (standalone):
    from app.tools.python_sandbox import run_python_code
    output = run_python_code("print(2 ** 10)")
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 4_000
CODE_TIMEOUT_SECONDS = 10

# CORRECTED (MP-4C): ``setrlimit`` is NOT implemented — no ``preexec_fn`` is
# passed to the subprocess, so NO hard memory or CPU cap is enforced. The only
# enforced bounds are the wall-clock timeout, the shared output byte budget,
# the code-size ceiling, the sanitized environment, the isolated cwd and
# deterministic process-tree termination. Do not claim harder limits.
_SANDBOX_MEMORY_BYTES = 256 * 1024 * 1024  # 256 MB — NOT ENFORCED (no setrlimit)
_SANDBOX_CPU_SECONDS = 8                    # 8s CPU — NOT ENFORCED (no setrlimit)

# Marker pattern the LLM is instructed (via system prompt) to wrap code in,
# mirroring terminal_executor's [PROPOSED_COMMAND: ...] convention.
PYTHON_CODE_PATTERN = re.compile(r"\[PYTHON_CODE:\s*(.+?)\]", re.DOTALL)


def extract_python_code(text: str) -> Optional[str]:
    """Extract the first [PYTHON_CODE: ...] block from the LLM's response."""
    if not text:
        return None
    match = PYTHON_CODE_PATTERN.search(text)
    if match:
        code = match.group(1).strip()
        return code.rstrip("]") if code else None
    return None


def format_code_result(code: str, output: str) -> str:
    """Format a code execution result for injection into the LLM context."""
    return (
        "=== Python Code Result ===\n"
        "Code:\n```python\n%s\n```\n\n"
        "Output:\n%s\n"
        "=== End of Python Code Result ==="
    ) % (code, output)


# ── Phase 1B.2R MP-4C-R2B-1 — hardened, bounded launch ───────────────
# Stable, bounded result codes embedded in the returned text.
RESULT_EMPTY_CODE = "empty_code"
RESULT_CODE_TOO_LARGE = "code_too_large"
RESULT_TIMEOUT = "timeout"
RESULT_OUTPUT_LIMIT = "output_limit_exceeded"
RESULT_SPAWN_FAILED = "spawn_failed"
# MP-4C-R2B-1S — the ONLY failure classification a caller may see.
RESULT_RUNTIME_ERROR = "runtime_error"
# MP-4C policy — real execution refused: no host-isolated runtime exists.
RESULT_EXECUTION_UNAVAILABLE = "execution_unavailable"


def _bound_result(text: str, limit: int) -> str:
    """Enforce the configured UTF-8 byte ceiling on the FINAL returned value.

    Pipe reading is bounded separately (shared stdout+stderr budget); formatting
    can still push the assembled string over the limit, so every returned result
    goes through here. Multibyte truncation is done on whole codepoints so a
    split UTF-8 sequence is never emitted and never raises.
    """
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker = "... [truncated] ..."
    mb = marker.encode("utf-8")
    # Keep the TAIL as well as the head: the stable classification code lives at
    # the very end, and truncating it away would destroy the failure contract.
    if limit <= len(mb):
        return _decode_tail(raw[-limit:])
    budget = limit - len(mb)
    head_n = budget // 2
    tail_n = budget - head_n
    return (_decode_head(raw[:head_n]) + marker
            + _decode_tail(raw[len(raw) - tail_n:]))


def _decode_head(b: bytes) -> str:
    """Decode as much as possible from the FRONT, never splitting a codepoint."""
    while b:
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            b = b[:-1]
    return ""


def _decode_tail(b: bytes) -> str:
    """Decode as much as possible from the BACK, never splitting a codepoint."""
    while b:
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            b = b[1:]
    return ""


def max_code_bytes() -> int:
    from app.config import settings
    return max(1, int(getattr(settings, "run_engine_sandbox_max_code_bytes", 8192)))


def max_output_bytes() -> int:
    from app.config import settings
    return max(1, int(getattr(settings, "run_engine_sandbox_max_output_bytes",
                                   MAX_OUTPUT_CHARS)))


def code_timeout_s() -> int:
    from app.config import settings
    return max(1, int(getattr(settings, "run_engine_sandbox_timeout_s",
                                   CODE_TIMEOUT_SECONDS)))


def _isolated_env(tmpdir: str) -> dict:
    """An EXPLICIT minimal allowlist. The parent environment is NEVER inherited.

    Notably absent: credentials/secrets, DATABASE_URL, ENCRYPTION_KEY, tokens,
    cookies, PYTHONPATH and every other unlisted parent variable. The -I flag
    also makes the interpreter ignore ALL PYTHON* variables, so the child cannot
    be influenced through the environment even if one were added here.
    """
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": tmpdir, "TEMP": tmpdir, "TMP": tmpdir,
    }
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR"):
            value = os.environ.get(key)
            if value:
                env[key] = value
    else:
        env["LANG"] = "C.UTF-8"
        env["LC_ALL"] = "C.UTF-8"
        env["PATH"] = "/usr/bin:/bin"
    return env


async def _bounded_read(stream, state: dict, limit: int) -> bytes:
    """Read one pipe under a SHARED byte budget, so the bound is enforced while
    reading rather than after an unbounded accumulation."""
    out = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        room = limit - state["used"]
        if room <= 0:
            state["overflow"] = True
            break
        out += chunk[:room]
        state["used"] += min(len(chunk), room)
        if len(chunk) > room:
            state["overflow"] = True
            break
    return bytes(out)


async def _terminate_tree(proc) -> None:
    """Kill the child AND its descendants, then reap. POSIX -> process group;
    Windows -> new process group + taskkill /T, falling back to kill()."""
    if proc is None or proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=False, shell=False)
            except Exception:  # noqa: BLE001
                pass
            if proc.returncode is None:
                proc.kill()
        else:
            import signal as _signal
            try:
                os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass


class SandboxOutcome:
    """Phase 1B.2R MP-4C-R2B-2C-1 - TRUSTED structured executor outcome.

    is_error comes from executor-owned control flow, NEVER from inspecting the
    returned text: a successful program may print a string identical to a
    sandbox failure message, so text alone can never decide error status.
    Iterable as (text, is_error) for the gateway callback contract.
    """
    __slots__ = ("text", "is_error")

    def __init__(self, text: str, is_error: bool) -> None:
        self.text = text
        self.is_error = bool(is_error)

    def __iter__(self):
        return iter((self.text, self.is_error))


async def _run_python_async(code: str) -> "SandboxOutcome":
    """The PERMANENT refusal boundary every public wrapper goes through.

    MP-4C policy: this module NEVER executes model-generated code. The launcher
    below is NOT a sandbox - it bounds resources and output but cannot prevent
    host filesystem or network access - so it must never be reachable from a
    public wrapper under ANY configuration.

    This refusal deliberately does NOT consult
    ``sandbox_policy.real_python_execution_available()``. That predicate is the
    ONE authority for the APPROVED DURABLE BROKER ROUTE
    (app.run_engine.worker -> executor service -> one hardened container), which
    does not call this module. Keeping this function unconditional means flipping
    the authority to True can never re-open the in-process host subprocess: there
    is exactly one real execution route and it is broker-mediated.
    """
    del code                                  # never inspected, never spawned
    return SandboxOutcome(
        "[Python Sandbox] Unavailable: this direct path never executes model "
        "code. Real execution is permitted only through an approved durable run "
        "using the isolated executor. (%s)" % RESULT_EXECUTION_UNAVAILABLE, True)


async def _run_python_unisolated(code: str) -> "SandboxOutcome":
    """INTERNAL TEST BOUNDARY — the bounded (but NOT host-isolated) launcher.

    Reached from production ONLY through ``_run_python_async`` above, which
    refuses while no host-isolated runtime exists. It is named ``_unisolated``
    so no caller can mistake it for a sandbox. Its only direct caller is
    ``tests/test_mp4c_r2b1_python_executor.py`` (bounds coverage).

    Run *code* in an isolated per-invocation directory with a bounded,
    sanitized subprocess.

    Containment provided: sanitized environment, bounded code size, bounded
    output, wall-clock timeout, descendant termination, cwd/artifact isolation
    and deterministic cleanup. This is NOT container/VM/namespace isolation and
    does not prevent arbitrary filesystem or network access by the child.
    """
    raw = (code or "").encode("utf-8")
    if not raw.strip():
        return SandboxOutcome("[Python Sandbox] Error: empty code. (%s)" % RESULT_EMPTY_CODE, True)
    if len(raw) > max_code_bytes():
        return SandboxOutcome(("[Python Sandbox] Error: code exceeds %d bytes. (%s)"
                % (max_code_bytes(), RESULT_CODE_TOO_LARGE)), True)

    limit = max_output_bytes()
    timeout_s = code_timeout_s()
    tmpdir = tempfile.mkdtemp(prefix="thunder_sbx_")
    script = os.path.join(tmpdir, "snippet.py")
    proc = None
    try:
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(code)

        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True   # setsid -> enables killpg tree-kill

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,          # controlled ABSOLUTE interpreter path
                "-I", "-B", "-u",        # isolated, no bytecode, unbuffered
                script,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,              # isolated working directory
                env=_isolated_env(tmpdir),
                **kwargs,
            )
        except Exception:  # noqa: BLE001 — bounded, leaks no host paths
            return SandboxOutcome(("[Python Sandbox] Error: could not start process. (%s)"
                    % RESULT_SPAWN_FAILED), True)

        state = {"used": 0, "overflow": False}
        loop = asyncio.get_running_loop()
        gather_task = asyncio.ensure_future(asyncio.gather(
            _bounded_read(proc.stdout, state, limit),
            _bounded_read(proc.stderr, state, limit)))
        deadline = loop.time() + timeout_s
        timed_out = False
        try:
            # A reader can only finish at EOF, so the tree must be terminated the
            # MOMENT the byte budget is exhausted (or the wall clock expires) —
            # otherwise a child still writing into a full pipe blocks until the
            # timeout and the overflow is misclassified as a timeout.
            while not gather_task.done():
                if state["overflow"]:
                    await _terminate_tree(proc)      # closes pipes -> readers EOF
                    break
                if loop.time() >= deadline:
                    timed_out = True
                    await _terminate_tree(proc)
                    break
                await asyncio.sleep(0.05)
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(gather_task, timeout=5)
            except asyncio.TimeoutError:
                stdout_bytes, stderr_bytes = b"", b""
        except asyncio.CancelledError:
            gather_task.cancel()
            await _terminate_tree(proc)
            raise

        # Deterministic classification: an exhausted budget is ALWAYS reported as
        # an output-limit result, never masked by the termination it triggers.
        if state["overflow"]:
            return SandboxOutcome(_bound_result(
                "[Python Sandbox] Output exceeded %d bytes. (%s)"
                % (limit, RESULT_OUTPUT_LIMIT), limit), True)
        if timed_out:
            return SandboxOutcome(_bound_result(
                "[Python Sandbox] Code killed: exceeded %ds timeout. (%s)"
                % (timeout_s, RESULT_TIMEOUT), limit), True)

        returncode = await proc.wait()
        if returncode:
            # MP-4C-R2B-1S (authorized Option A): a FAILED run returns ONLY a
            # bounded, sandbox-generated classification plus the numeric exit
            # status. ALL child stdout/stderr is discarded — tracebacks, source
            # lines, snippet.py, sandbox and host paths, and exception type or
            # message (which user code can spoof, and which may carry secrets)
            # never become public. No traceback text is parsed and no
            # canary/secret blacklist is used: the policy is structural.
            return SandboxOutcome(_bound_result(
                "[Python Sandbox] Execution failed with exit code %d. (%s)"
                % (returncode, RESULT_RUNTIME_ERROR), limit), True)

        # exit 0 — the program's deliberate output IS the approved tool result.
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        parts = []
        if stdout.strip():
            parts.append(stdout.strip())
        if stderr.strip():
            parts.append("[stderr]\n" + stderr.strip())
        output = "\n\n".join(parts) if parts else "(no output)"

        logger.info("Python sandbox: exit %d, %d bytes output",
                    returncode or 0, state["used"])
        return SandboxOutcome(_bound_result(output, limit), False)

    except Exception:  # noqa: BLE001 — bounded code only, never a raw traceback
        return SandboxOutcome(_bound_result(
            "[Python Sandbox] Error: execution failed. (%s)" % RESULT_SPAWN_FAILED,
            max_output_bytes()), True)
    finally:
        if proc is not None and proc.returncode is None:
            await _terminate_tree(proc)
        shutil.rmtree(tmpdir, ignore_errors=True)


def _run_python_unisolated_sync(code: str) -> "SandboxOutcome":
    """INTERNAL TEST BOUNDARY sync adapter (mirrors run_python_code_outcome_sync).

    Only tests/test_mp4c_r2b1_python_executor.py calls it directly; production
    reaches execution only through _run_python_async, which refuses.
    """
    from app.services.async_bridge import run_coro_sync

    return run_coro_sync(lambda: _run_python_unisolated(code))


def run_python_code(code: str) -> str:
    """Synchronous wrapper that runs Python code and returns its output."""
    return asyncio.run(_run_python_async(code)).text


async def run_python_code_async(code: str) -> str:
    """Async wrapper for use inside a running event loop (streaming)."""
    return (await _run_python_async(code)).text


async def run_python_code_outcome_async(code: str) -> "SandboxOutcome":
    """Phase 1B.2R MP-4C-R2B-2C-1 — TYPED entry point for the future gateway
    callback. Executes the submitted code EXACTLY ONCE and returns the trusted
    SandboxOutcome: bounded text plus an is_error bit taken from executor-owned
    exit/timeout/overflow/spawn control flow — never parsed from the text."""
    return await _run_python_async(code)


def run_python_code_outcome_sync(code: str) -> "SandboxOutcome":
    """Synchronous typed adapter for a SYNC gateway callback.

    Uses the repository's app.services.async_bridge.run_coro_sync: asyncio.run()
    when no event loop is running, otherwise a one-shot worker thread owning its
    own loop — so calling this while a loop IS active is safe (a bare
    asyncio.run() would raise). Exactly one execution per call; this does not
    start a second execution engine.

    Containment note: the executor is NOT a filesystem or network sandbox.
    """
    from app.services.async_bridge import run_coro_sync

    return run_coro_sync(lambda: _run_python_async(code))


@tool
def execute_python_code(code: str) -> str:
    """Execute a snippet of Python code and return its stdout/stderr.

    Use this tool when the user asks you to perform calculations, data
    analysis, or write/run code. Write clean, self-contained Python code;
    the tool runs it in the container's Python interpreter with a 15-second
    timeout and returns the output (or an error) so you can debug or report
    the result to the user.

    Args:
        code: The Python source code to execute.

    Returns:
        The captured stdout/stderr, or an error message.
    """
    if not code or not code.strip():
        return "[Python Sandbox] Error: empty code."

    return run_python_code(code)
