"""agent-desktop (Vercel Labs) computer-use wrapper — opt-in, permission-gated.

`agent-desktop` is a Rust CLI (``npm install -g agent-desktop``) that reads a
desktop app's OS accessibility tree as JSON with per-element refs, and acts on
those refs headlessly (no pointer movement, no screenshots) on
Windows/macOS/Linux. This module is Thunder AI's confined bridge to that CLI.
It is SEPARATE from the older sandboxed ``desktop_automation`` file/command
surface.

Guardrails (each enforced here, each covered by tests):

* **Fail closed**: disabled unless ``AGENT_DESKTOP_ENABLED=true``, the
  ``agent_desktop`` capability is enabled for the user, AND at least one app is
  listed in ``AGENT_DESKTOP_ALLOWED_APPS``. An empty allowlist can never match.
* **Command allowlist**: only ``status``/``snapshot`` (read) and ``click``,
  ``type``, ``set-value``, ``key``, ``scroll`` (act). Clipboard writes, session
  management, and MCP serving are refused.
* **Headless only**: ``--headed`` (physical input) is refused at every level.
* **Permission ladder**: the user's Ask/Auto/Full level gates every call —
  including READS, because an accessibility snapshot can contain private
  desktop content. Acting commands must reference a snapshot ref issued by this
  process (``@<id>:e<n>``), else ``STALE_REF``.
* **No shell, scrubbed env, hard timeout, bounded output**; typed text travels
  on stdin, never argv.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.config import settings
from app.services.desktop_automation import PermissionLevel, PermissionRequired

logger = logging.getLogger(__name__)

READ_COMMANDS = frozenset({"status", "snapshot"})
ACT_COMMANDS = frozenset({"click", "type", "set-value", "key", "scroll"})
ALLOWED_COMMANDS = READ_COMMANDS | ACT_COMMANDS

#: agent-desktop refs look like @s8f3k2p9:e8 (snapshot id + element index).
_REF_RE = re.compile(r"^@([A-Za-z0-9]{4,24}):e\d+$")
_APP_NAME_RE = re.compile(r"^[\w .+&'-]{1,64}$")
MAX_TEXT_CHARS = 4000
#: Only these parent variables cross into the child process.
_CHILD_ENV_KEYS = ("PATH", "SystemRoot", "WINDIR", "COMSPEC")
#: Flags the allowlisted commands may take; anything else is refused.
_ALLOWED_FLAGS = {
    "--app", "--window-id", "--root", "--stdin", "-i", "--interactive", "--compact",
}

STALE_REF = "STALE_REF"


class DesktopAgentError(RuntimeError):
    """The CLI refused, errored, or answered outside its JSON contract."""


class DesktopAgentDisabled(DesktopAgentError):
    """The feature is not armed end-to-end (flag, capability, or app list)."""


class DesktopAgentTimeout(DesktopAgentError):
    """The CLI did not answer within the hard timeout."""


@dataclass(frozen=True)
class DesktopAgentResult:
    command: str
    data: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0


#: Snapshot ids issued by this process; act refs must belong to one of them.
_SNAPSHOT_IDS: set = set()


def reset_snapshot_ids() -> None:
    """Per-task hygiene hook (tests and task start)."""
    _SNAPSHOT_IDS.clear()


def is_enabled() -> bool:
    """Master flag AND a non-empty app allowlist: no apps -> nothing is safe."""
    return bool(settings.agent_desktop_enabled) and bool(_allowed_apps())


def _allowed_apps() -> List[str]:
    raw = (settings.agent_desktop_allowed_apps or "").strip()
    if not raw:
        return []
    return [part.strip().casefold() for part in raw.split(",") if part.strip()]


def _executable() -> str:
    explicit = (settings.agent_desktop_command or "").strip()
    if explicit:
        return explicit
    found = shutil.which("agent-desktop")
    if not found:
        raise DesktopAgentDisabled(
            "agent-desktop CLI not found. Install it with "
            "`npm install -g agent-desktop` or set AGENT_DESKTOP_COMMAND."
        )
    return found


def _coerce_level(value: Any) -> PermissionLevel:
    if isinstance(value, PermissionLevel):
        return value
    try:
        return PermissionLevel(str(value))
    except ValueError:
        # Unknown values must never escalate: fall back to the strictest rung.
        return PermissionLevel.ASK_EVERY_TIME


def _check_ref(ref: str) -> str:
    value = (ref or "").strip()
    match = _REF_RE.match(value)
    if not match:
        raise DesktopAgentError(
            "Invalid element ref (expected @<snapshot>:e<n> from a fresh snapshot)."
        )
    if match.group(1) not in _SNAPSHOT_IDS:
        raise DesktopAgentError(
            f"{STALE_REF}: this ref did not come from a snapshot issued in this task."
        )
    return value


def _extract_app(args: Sequence[str]) -> Optional[str]:
    for i, arg in enumerate(args):
        if arg == "--app" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--app="):
            return arg.split("=", 1)[1]
    return None


def _guard_args(command: str, args: Sequence[str]) -> None:
    if "--headed" in args:
        raise DesktopAgentError("Headed (physical) input is never allowed from the server.")
    for arg in args:
        if arg.startswith("--") and arg.split("=", 1)[0] not in _ALLOWED_FLAGS:
            raise DesktopAgentError(f"Unsupported agent-desktop flag: {arg}")
        if arg.startswith("-") and not arg.startswith("--") and arg not in {"-i"}:
            raise DesktopAgentError(f"Unsupported agent-desktop flag: {arg}")
    app = _extract_app(args)
    if command != "status":
        if app is None:
            raise DesktopAgentError("Desktop actions must name a target app (--app).")
        if not _APP_NAME_RE.match(app):
            raise DesktopAgentError("Target app name contains invalid characters.")
        if app.casefold() not in _allowed_apps():
            raise DesktopAgentError(
                f"App '{app}' is not in the allowed apps for agent-desktop."
            )
    if command in ACT_COMMANDS:
        refs = [a for a in args if a.startswith("@")]
        if not refs:
            raise DesktopAgentError("Acting commands require an element ref.")
        for ref in refs:
            _check_ref(ref)


def _child_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    for key in _CHILD_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _envelope(command: str, stdout: str) -> Dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DesktopAgentError("agent-desktop returned a non-JSON envelope.") from exc
    if not isinstance(payload, dict) or payload.get("command") != command:
        raise DesktopAgentError("agent-desktop envelope mismatch.")
    if not payload.get("ok"):
        error = payload.get("error") or {}
        code = str(error.get("code") or "DESKTOP_AGENT_ERROR")
        raise DesktopAgentError(f"agent-desktop {command} failed ({code}).")
    return payload


def run(
    command: str,
    args: Sequence[str] = (),
    *,
    permission_level: Any = PermissionLevel.ASK_EVERY_TIME,
    stdin_text: Optional[str] = None,
    runner=None,
) -> DesktopAgentResult:
    """One CLI invocation under every guardrail above. Raises, never acts half-way."""
    if not is_enabled():
        raise DesktopAgentDisabled(
            "agent-desktop is not armed (ENABLED flag, capability, or app allowlist)."
        )
    if command not in ALLOWED_COMMANDS:
        raise DesktopAgentError(f"Command '{command}' is not on the allowlist.")

    level = _coerce_level(permission_level)
    if level is PermissionLevel.ASK_EVERY_TIME:
        # Reads are gated too: snapshots can expose private desktop content.
        raise PermissionRequired()

    argv_args = [str(a) for a in args]
    _guard_args(command, argv_args)
    if stdin_text is not None:
        if command not in {"type", "set-value"}:
            raise DesktopAgentError(f"stdin is not accepted for '{command}'.")
        if len(stdin_text) > MAX_TEXT_CHARS:
            raise DesktopAgentError("Typed text exceeds the size cap.")

    argv = [_executable(), command, *argv_args]
    started = time.monotonic()
    try:
        proc = (runner or subprocess.run)(
            argv,
            capture_output=True,
            text=True,
            timeout=float(settings.agent_desktop_timeout_s),
            input=stdin_text,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise DesktopAgentTimeout(
            f"agent-desktop {command} timed out; nothing further is attempted."
        ) from exc

    duration_ms = int((time.monotonic() - started) * 1000)
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        raise DesktopAgentError(f"agent-desktop {command} exited {proc.returncode}.")
    payload = _envelope(command, proc.stdout)

    data = payload.get("data") or {}
    if command == "snapshot":
        snapshot_id = str(data.get("snapshot_id") or "")
        if snapshot_id:
            _SNAPSHOT_IDS.add(snapshot_id)
        cap = int(settings.agent_desktop_max_output_chars)
        blob = json.dumps(data, ensure_ascii=False)
        if len(blob) > cap:
            data = {
                "truncated_tree": blob[: max(0, cap - 1)].rstrip() + "…",
                "snapshot_id": data.get("snapshot_id"),
                "truncated": True,
            }
    return DesktopAgentResult(command=command, data=data, duration_ms=duration_ms)


# ── convenience surface used by the orchestrator and tests ────────────────


def read_ui(app: str, *, permission_level, runner=None) -> DesktopAgentResult:
    return run(
        "snapshot", ["--app", app, "-i", "--compact"],
        permission_level=permission_level, runner=runner,
    )


def click(ref: str, app: str, *, permission_level, runner=None) -> DesktopAgentResult:
    return run("click", ["--app", app, ref], permission_level=permission_level, runner=runner)


def type_text(ref: str, text: str, app: str, *, permission_level, runner=None) -> DesktopAgentResult:
    # Text travels on stdin, never argv (keeps it out of process listings).
    return run(
        "type", ["--app", app, ref, "--stdin"],
        permission_level=permission_level, stdin_text=text, runner=runner,
    )


def status(*, permission_level, runner=None) -> DesktopAgentResult:
    return run("status", [], permission_level=permission_level, runner=runner)
