"""Open Interpreter — LLM-powered code execution backend (optional feature).

Open Interpreter lets an LLM write and run Python/JS/shell code on this
machine. The ``interpreter`` package is an OPTIONAL dependency: it is imported
lazily, and when it is missing every entry point returns a structured
``{"status": "unavailable", ...}`` result with install instructions instead of
crashing. Nothing here is imported at app startup.

Permission model (maps onto the existing Ask/Auto/Full levels):

* ``ask_every_time`` (DEFAULT, fail-safe): Open Interpreter is configured with
  ``auto_run=False`` — it may GENERATE code but is never allowed to execute
  it. The proposed code is returned to the caller (the route shows it to the
  user); only a subsequent, explicit ``run_approved_code`` call with the exact
  reviewed code executes anything.
* ``auto_approve``: code is generated and executed automatically
  (``auto_run=True``), under a hard timeout and output cap.
* ``full_access``: identical to auto, explicitly with full local system access
  (no extra sandboxing is applied; the user acknowledged the risk in the UI).

User isolation: a FRESH ``Interpreter`` instance is created per request — no
conversation state is shared between users or requests. ``user_id`` is only
used for logging.

Stability: every OI interaction runs in a worker thread under
``threading.Event`` timeouts; a hung interpreter can delay one request but can
never wedge the app or crash startup.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Hard cap for one generate/execute round-trip (seconds).
EXECUTION_TIMEOUT_S = 60.0
#: Hard cap for the ask-mode code-generation phase (seconds).
GENERATION_TIMEOUT_S = 60.0
#: Poll interval while harvesting the proposed code (seconds).
_POLL_INTERVAL_S = 0.2
#: Output truncation — OI sessions can produce very large transcripts.
MAX_OUTPUT_CHARS = 8000

#: Permission levels that allow automatic execution.
_AUTO_LEVELS = {"auto_approve", "full_access"}

_CODE_FENCE_RE = re.compile(r"```([\w+#.-]*)[ \t]*\n(.*?)```", re.DOTALL)

_INSTALL_HINT = (
    "Open Interpreter is not installed on this machine. Install it with "
    "`pip install open-interpreter` (Python library) or the standalone "
    "installer from https://www.openinterpreter.com, then restart the app."
)


class InterpreterTimeout(Exception):
    """A generation/execution round-trip exceeded its hard timeout."""


def _import_interpreter():
    """Return the ``interpreter`` module, or ``None`` when not installed.

    Kept as a module-level function so tests can monkeypatch it.
    """
    try:
        import interpreter as interpreter_module  # optional dependency

        return interpreter_module
    except Exception:  # noqa: BLE001 — ImportError, broken install, anything
        return None


def interpreter_status() -> Dict[str, Any]:
    """Availability probe for the UI/status endpoint. Never raises."""
    module = _import_interpreter()
    available = module is not None
    status: Dict[str, Any] = {
        "available": available,
        "install_hint": None if available else _INSTALL_HINT,
    }
    if available:
        status["version"] = str(getattr(module, "__version__", "unknown"))
    return status


def _normalize_level(permission_level: Optional[str]) -> str:
    """Map an arbitrary level string to a known one. Unknown values fail
    safe to ``ask_every_time``."""
    from app.services.capability_preferences import parse_permission_level

    parsed = parse_permission_level(permission_level)
    return parsed.value if parsed is not None else "ask_every_time"


def _new_interpreter(permission_level: str):
    """Create a fresh, isolated Interpreter configured for the level.

    Caller guarantees the module exists. Never raises for configuration —
    unknown attributes are tolerated so older/newer package versions differ
    without breaking us.
    """
    module = _import_interpreter()
    if module is None:
        raise RuntimeError("Open Interpreter is not installed")
    instance = module.Interpreter()
    instance.auto_run = permission_level in _AUTO_LEVELS
    instance.verbose = False
    # Bounded transcript: OI can produce huge outputs.
    try:
        instance.max_output = MAX_OUTPUT_CHARS
    except Exception:  # noqa: BLE001 — attribute name differs across versions
        pass
    try:
        instance.messages = []
    except Exception:  # noqa: BLE001
        pass
    return instance


# ── Message harvesting ─────────────────────────────────────────────────────


def _message_text(content: Any) -> str:
    """Flatten an OI message content (str or list of typed parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                value = part.get("content", part.get("text", ""))
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _extract_proposal(messages: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """Last ```-fenced code block in the transcript (language, code)."""
    for message in reversed(messages or []):
        text = _message_text(message.get("content"))
        matches = _CODE_FENCE_RE.findall(text or "")
        if matches:
            language, code = matches[-1]
            return (language.strip() or "python"), code.strip()
    return None, None


def _summarize_messages(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the executed result: final assistant text + executed blocks."""
    output_text = ""
    blocks: List[Dict[str, str]] = []
    for message in messages or []:
        role = str(message.get("role", ""))
        text = _message_text(message.get("content"))
        if role == "assistant" and text.strip():
            output_text = text.strip()[-MAX_OUTPUT_CHARS:]
        elif role in ("computer", "tool", "function") and text.strip():
            blocks.append({"output": text.strip()[-MAX_OUTPUT_CHARS:]})
    return {
        "output": output_text,
        "executed_outputs": blocks[-20:],
        "messages_count": len(messages or []),
    }


# ── Threaded execution with hard timeout ───────────────────────────────────


def _run_with_timeout(target, timeout_s: float) -> None:
    """Run ``target`` in a daemon thread; raise InterpreterTimeout on overrun.

    A daemon thread that overruns is abandoned (it cannot be force-killed
    safely); it can never block app shutdown.
    """
    done = threading.Event()
    failure: List[BaseException] = []

    def wrapper():
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 — surfaced to the caller
            failure.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=wrapper, daemon=True, name="open-interpreter-run")
    thread.start()
    if not done.wait(timeout_s):
        raise InterpreterTimeout(f"Open Interpreter did not finish within {timeout_s:g}s")
    if failure:
        raise failure[0]


# ── Public API ─────────────────────────────────────────────────────────────


def _generate_proposal(prompt: str, permission_level: str) -> Tuple[Optional[str], Optional[str]]:
    """Ask-mode: generate proposed code WITHOUT executing it.

    Runs OI with ``auto_run=False`` in a daemon thread and polls its
    transcript for a code fence. When OI pauses at its interactive
    confirmation the code is already in the transcript, so the proposal is
    harvested and the thread abandoned — execution never happens because
    auto_run is False.
    """
    instance = _new_interpreter(permission_level)
    started = threading.Event()

    def target():
        started.set()
        try:
            if hasattr(instance, "respond"):
                instance.respond(prompt)
            else:
                instance.chat(prompt, display=False)
        except Exception as exc:  # noqa: BLE001 — proposal may already be in messages
            logger.debug("open_interpreter: generation thread ended (%s)", type(exc).__name__)

    _run_with_timeout(target, GENERATION_TIMEOUT_S)
    return _extract_proposal(instance.messages)


def execute_code(prompt: str, user_id: int, permission_level: str) -> Dict[str, Any]:
    """Run the Open Interpreter flow for one prompt (spec API).

    Returns one of:
      * ``{"status": "unavailable", "message": ...}`` — package not installed.
      * ``{"status": "needs_approval", "code", "language", "message"}`` — ask
        mode: proposed code for the user to review; NOTHING was executed.
      * ``{"status": "executed", "output", "executed_outputs", ...}`` — auto/
        full mode: the agent loop ran under the hard timeout.
      * ``{"status": "timeout" | "error", "message": ...}`` — contained.
    """
    level = _normalize_level(permission_level)
    logger.info(
        "open_interpreter: user %s requested execution at level '%s'", user_id, level
    )

    module = _import_interpreter()
    if module is None:
        return {"status": "unavailable", "message": _INSTALL_HINT}

    if level == "ask_every_time":
        try:
            language, code = _generate_proposal(prompt, level)
        except InterpreterTimeout:
            return {
                "status": "timeout",
                "message": "Open Interpreter took too long to propose code.",
            }
        except Exception as exc:  # noqa: BLE001 — contained, never crashes the app
            logger.exception("open_interpreter: proposal failed for user %s", user_id)
            return {"status": "error", "message": f"Code generation failed: {exc}"}
        if not code:
            return {
                "status": "error",
                "message": "Open Interpreter did not propose any code. Try rephrasing the request.",
            }
        return {
            "status": "needs_approval",
            "code": code,
            "language": language or "python",
            "permission_level": level,
            "message": "Review the proposed code. Approve it to run on this machine.",
        }

    # auto_approve / full_access — automatic execution.
    instance = _new_interpreter(level)
    try:
        _run_with_timeout(
            lambda: (
                instance.respond(prompt)
                if hasattr(instance, "respond")
                else instance.chat(prompt, display=False)
            ),
            EXECUTION_TIMEOUT_S,
        )
    except InterpreterTimeout:
        return {
            "status": "timeout",
            "message": f"Open Interpreter exceeded the {EXECUTION_TIMEOUT_S:g}s execution limit.",
        }
    except Exception as exc:  # noqa: BLE001 — contained
        logger.exception("open_interpreter: execution failed for user %s", user_id)
        return {"status": "error", "message": f"Open Interpreter failed: {exc}"}

    summary = _summarize_messages(instance.messages)
    return {
        "status": "executed",
        "permission_level": level,
        **summary,
    }


def run_approved_code(
    code: str, language: str, user_id: int, permission_level: str
) -> Dict[str, Any]:
    """Execute an EXPLICIT user-approved code block (the ask-mode approval
    path). Only the exact reviewed ``code`` runs — through OI's computer
    environment, under the same hard timeout and output caps."""
    level = _normalize_level(permission_level)
    module = _import_interpreter()
    if module is None:
        return {"status": "unavailable", "message": _INSTALL_HINT}

    instance = _new_interpreter(level)
    outputs: List[str] = []

    def target():
        computer = getattr(instance, "computer", None)
        if computer is None or not hasattr(computer, "run"):
            raise RuntimeError("Open Interpreter computer module is unavailable")
        result = computer.run(language or "python", code)
        if isinstance(result, list):
            outputs.extend(_message_text(item) for item in result)
        else:
            outputs.append(_message_text(result))

    try:
        _run_with_timeout(target, EXECUTION_TIMEOUT_S)
    except InterpreterTimeout:
        return {
            "status": "timeout",
            "message": f"The approved code exceeded the {EXECUTION_TIMEOUT_S:g}s execution limit.",
        }
    except Exception as exc:  # noqa: BLE001 — contained
        logger.exception("open_interpreter: approved run failed for user %s", user_id)
        return {"status": "error", "message": f"Execution failed: {exc}"}

    joined = "\n".join(part for part in outputs if part)[-MAX_OUTPUT_CHARS:]
    return {
        "status": "executed",
        "permission_level": level,
        "output": joined,
        "executed_outputs": [{"output": part} for part in outputs if part][-20:],
    }
