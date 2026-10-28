"""
Terminal Executor Tool for the AI Research Agent.

IMPORTANT SAFETY DESIGN:
  This tool does NOT execute commands directly.  It is used by the
  LangGraph workflow as a "command proposal" mechanism:

  1.  The LLM proposes a command via the system prompt instructions.
  2.  The ``generate_answer`` node detects the proposed command in the
      LLM response using the ``COMMAND_PATTERN`` regex.
  3.  The workflow interrupts and asks the user for approval.
  4.  Only after the user approves does ``run_command`` execute the
      command via ``asyncio.create_subprocess_shell``.

This two-phase design (propose → approve → execute) ensures no command
runs without explicit human consent.

Usage (called by the workflow, NOT by the LLM directly):
    from app.tools.terminal_executor import run_command

    result = run_command("ls -la /tmp")
"""

import asyncio
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────

# Maximum characters to return from command output (~4 000 tokens)
MAX_OUTPUT_CHARS = 4_000

# Timeout for individual commands (seconds)
COMMAND_TIMEOUT_SECONDS = 30

# ── Command detection pattern ──────────────────────────────
# The LLM is instructed (via system prompt) to wrap proposed commands
# in this exact format so the workflow can detect them reliably.
#
# Example LLM output:
#   I'll list the files in the current directory.
#   [PROPOSED_COMMAND: ls -la]
#
COMMAND_PATTERN = re.compile(
    r"\[PROPOSED_COMMAND:\s*(.+?)\]",
    re.DOTALL,
)


def extract_proposed_command(text: str) -> Optional[str]:
    """Extract the first proposed command from the LLM's response text.

    Returns the command string if found, otherwise None.
    """
    match = COMMAND_PATTERN.search(text)
    if match:
        cmd = match.group(1).strip()
        # Clean up any trailing bracket artifacts
        cmd = cmd.rstrip("]")
        return cmd
    return None


def _strip_command_from_response(text: str, command: str) -> str:
    """Remove the [PROPOSED_COMMAND: ...] block from the LLM response.

    Returns the cleaned text suitable for showing to the user before
    approval (e.g. "I'll list the files in the current directory.").
    """
    cleaned = COMMAND_PATTERN.sub("", text).strip()
    return cleaned if cleaned else "I'd like to run a command for you."


# ── Command execution ──────────────────────────────────────


def run_command(command: str) -> str:
    """Execute a shell command and return its output.

    This function is ONLY called after human approval.  It must never
    be invoked directly from the LLM response pipeline.

    Args:
        command: The shell command to execute.

    Returns:
        A string containing stdout + stderr (truncated to MAX_OUTPUT_CHARS).
    """
    return asyncio.run(_run_command_async(command))


async def _run_command_async(command: str) -> str:
    """Async implementation of command execution."""
    logger.info("Executing approved command: %s", command)

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=COMMAND_TIMEOUT_SECONDS,
        )

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

        # Combine output
        parts = []
        if stdout.strip():
            parts.append(stdout.strip())
        if stderr.strip():
            parts.append(f"[stderr]\n{stderr.strip()}")

        output = "\n\n".join(parts) if parts else "(no output)"

        # Include exit code if non-zero
        if process.returncode and process.returncode != 0:
            output += f"\n\n[Exit code: {process.returncode}]"

        # Truncate to limit
        if len(output) > MAX_OUTPUT_CHARS:
            output = (
                output[:MAX_OUTPUT_CHARS]
                + f"\n\n[Output truncated at {MAX_OUTPUT_CHARS} chars]"
            )

        logger.info(
            "Command completed (exit %d): %d chars output",
            process.returncode or 0,
            len(output),
        )
        return output

    except asyncio.TimeoutError:
        return f"[Terminal] Command timed out after {COMMAND_TIMEOUT_SECONDS}s: {command}"
    except Exception as exc:
        error_msg = f"[Terminal] Error executing command: {type(exc).__name__}: {exc}"
        logger.error(error_msg, exc_info=True)
        return error_msg


# ── Approval prompt formatting ─────────────────────────────


def format_approval_message(command: str, preamble: str = "") -> str:
    """Format the message shown to the user when asking for approval.

    ADVISOR BEHAVIOUR:
    - Explain what the command does.
    - Warn if it has side effects (delete, modify, overwrite).
    - Suggest safer alternatives when applicable.

    Args:
        command: The command proposed by the LLM.
        preamble: Optional text the LLM wrote before proposing the command.

    Returns:
        A user-friendly message asking for approval.
    """
    parts = []
    if preamble:
        parts.append(preamble)
    parts.append(f"\n\nI'd like to run this command:\n`{command}`\n")
    parts.append("Reply **yes** to approve, or **no** to deny.")
    return "".join(parts)


def format_deny_message(command: str) -> str:
    """Format the message shown to the LLM when the user denies a command."""
    return (
        f"The user denied the command: `{command}`\n"
        "Please try to answer the user's question without executing that command."
    )


def format_result_message(command: str, output: str) -> str:
    """Format the command result for injection into the LLM context."""
    return (
        f"=== Command Result ===\n"
        f"Command: `{command}`\n\n"
        f"Output:\n{output}\n"
        f"=== End of Command Result ==="
    )
