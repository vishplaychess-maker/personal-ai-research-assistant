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
import tempfile
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 4_000
CODE_TIMEOUT_SECONDS = 10

# Hard resource limits for untrusted code execution (Linux only).
# These are enforced via preexec_fn on the subprocess.
_SANDBOX_MEMORY_BYTES = 256 * 1024 * 1024  # 256 MB
_SANDBOX_CPU_SECONDS = 8                    # 8s of CPU time (soft wall is 10s)

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


async def _run_python_async(code: str) -> str:
    """Run *code* in a temporary .py file with strict sandbox limits.

    Limits enforced:
    - Wall-clock timeout: 10 seconds
    - CPU time limit: 8 seconds
    - Memory limit: 256 MB (via RLIMIT_AS on Linux)
    - Process group isolation (os.setsid) so kill-tree works on timeout
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp_path = f.name

        # Build a preexec_fn that applies resource limits on Linux.
        # Gracefully degrades on non-Linux (macOS/Windows) or if resource
        # module is unavailable.
        preexec = None
        try:
            import resource as _resource

            def _sandbox_limits():
                os.setsid()  # new process group for clean tree-kill
                _resource.setrlimit(
                    _resource.RLIMIT_AS,
                    (_SANDBOX_MEMORY_BYTES, _SANDBOX_MEMORY_BYTES),
                )
                _resource.setrlimit(
                    _resource.RLIMIT_CPU,
                    (_SANDBOX_CPU_SECONDS, _SANDBOX_CPU_SECONDS),
                )

            preexec = _sandbox_limits
        except (ImportError, OSError, ValueError):
            # Non-Linux or resource limits unsupported; degrade gracefully.
            pass

        process = await asyncio.create_subprocess_exec(
            "python",
            tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=preexec,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=CODE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # Kill the entire process group, not just the child.
            try:
                import signal as _signal
                os.killpg(os.getpgid(process.pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()
            try:
                await asyncio.wait_for(process.communicate(), timeout=2.0)
            except (asyncio.TimeoutError, OSError):
                pass
            return "[Python Sandbox] Code killed: exceeded %ds timeout or 256MB memory limit" % CODE_TIMEOUT_SECONDS

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

        parts = []
        if stdout.strip():
            parts.append(stdout.strip())
        if stderr.strip():
            parts.append("[stderr]\n" + stderr.strip())

        output = "\n\n".join(parts) if parts else "(no output)"

        if process.returncode and process.returncode != 0:
            output += "\n\n[Exit code: %d]" % process.returncode

        if len(output) > MAX_OUTPUT_CHARS:
            output = (
                output[:MAX_OUTPUT_CHARS]
                + "\n\n[Output truncated at %d chars]" % MAX_OUTPUT_CHARS
            )

        logger.info(
            "Python sandbox: exit %d, %d chars output",
            process.returncode or 0, len(output),
        )
        return output

    except Exception as exc:
        error_msg = "[Python Sandbox] Error: %s: %s" % (type(exc).__name__, exc)
        logger.error(error_msg, exc_info=True)
        return error_msg
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def run_python_code(code: str) -> str:
    """Synchronous wrapper that runs Python code and returns its output."""
    return asyncio.run(_run_python_async(code))


async def run_python_code_async(code: str) -> str:
    """Async wrapper for use inside a running event loop (streaming)."""
    return await _run_python_async(code)


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
