"""Sandboxed desktop automation for the packaged app (opt-in, local only).

Surface: read/write files inside one workspace root, run a short list of
commands, capture the screen into memory, and read the clipboard.

What the guardrails actually guarantee
--------------------------------------
* **Paths** are confined to ``workspace_dir``: relative inputs are joined to the
  root, absolute inputs are checked, and the resolved real path (symlinks
  followed) must still be under it. System and user-private trees are refused
  even if the workspace were misconfigured to point at them.
* **No shell.** Commands are executed as an argv list with ``shell=False``, so
  quoting, pipes, redirection and ``&&`` are literal arguments rather than
  syntax. ``dir``/``ls``/``cat``/``type``/``echo`` are implemented in-process so
  the common cases never need ``cmd.exe``.
* **No inherited environment.** The child gets a small allowlist
  (PATH/Windows essentials) with HOME/TEMP redirected into the workspace, so the
  app's own secrets (``ENCRYPTION_KEY``, ``JWT_SECRET``, provider keys) are not
  visible to it.
* **Nothing is written to disk except by ``write_file``** — screenshots live in
  memory only, and the clipboard is read-only.

Honest limits (why this is a guardrail, not a jail)
---------------------------------------------------
* A whitelist around general-purpose interpreters is not a sandbox: ``python
  script.py`` runs whatever the script says, and ``npm run`` runs a project's
  scripts. The eval flags (``python -c``, ``node -e``) are refused, path
  arguments are confined and the environment is scrubbed, but code that is
  already inside the workspace can still do what that code does.
* Outbound network from a child process cannot be blocked from inside CPython.
  If egress must be impossible, run the app in a container/VM or behind an OS
  firewall rule; this module does not pretend to offer that.
* The clipboard read is a deliberate exception to the workspace boundary: it is
  read-only, capped, and there is no write path at all.
"""

from __future__ import annotations

import datetime
import enum
import io
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MAX_READ_BYTES = 1_048_576
MAX_WRITE_BYTES = 5_242_880
MAX_OUTPUT_BYTES = 65_536
MAX_SCREENSHOT_BYTES = 5_242_880
MAX_CLIPBOARD_CHARS = 65_536
MAX_TREE_ENTRIES = 500
MAX_TREE_DEPTH = 4

#: Backups of overwritten files live inside the workspace, so the sandbox keeps
#: holding them (an outside backup directory would be an escape by itself).
BACKUP_DIR = ".backups"

#: Directories the UI walks past: noise for a user, and huge to enumerate.
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", BACKUP_DIR, "__pycache__", ".tmp"}

#: Programs that may run. Everything else is refused, including the "blocked"
#: list below, which only exists to give a clearer message.
ALLOWED_PROGRAMS = ("python", "python3", "node", "npm", "git", "pip")
INTERNAL_COMMANDS = ("dir", "ls", "cat", "type", "echo")
BLOCKED_COMMANDS = (
    "rm", "del", "erase", "rd", "rmdir", "format", "diskpart", "diskcomp",
    "net", "reg", "regedit", "shutdown", "taskkill", "powershell", "pwsh",
    "cmd", "wsl", "curl", "wget", "ssh", "scp", "bitsadmin", "schtasks",
)

#: Flags that turn an argument into code execution or a directory escape.
DANGEROUS_FLAGS: Dict[str, Tuple[str, ...]] = {
    "python": ("-c", "-m", "-i", "-", "--eval"),
    "python3": ("-c", "-m", "-i", "-", "--eval"),
    "node": ("-e", "--eval", "-p", "--print", "-i"),
    "npm": (),
    "git": (
        "-c", "-C", "--exec-path", "--git-dir", "--work-tree",
        "--upload-pack", "--receive-pack", "--config-env", "--namespace",
    ),
    "pip": (),
}

#: Subcommands each tool may use. Anything else is refused.
ALLOWED_SUBCOMMANDS: Dict[str, Tuple[str, ...]] = {
    "npm": ("install", "ci", "run", "test", "ls", "--version", "-v"),
    "git": (
        "status", "log", "diff", "add", "commit", "branch", "checkout",
        "switch", "restore", "init", "show", "ls-files", "rev-parse", "stash",
        "tag",
    ),
    "pip": ("install",),
}

#: pip flags that reach the network, read arbitrary requirement files, or write
#: outside the workspace venv.
_PIP_BLOCKED_FLAGS = (
    "-r", "--requirement", "-e", "--editable", "-i", "--index-url",
    "--extra-index-url", "--target", "--prefix", "--root", "--user",
    "--trusted-host", "--find-links", "-f", "--no-index", "--upgrade-strategy",
)

_PROTECTED_PREFIXES = ("c:/windows", "c:/program files", "c:/programdata")

PATH_DENIED = "Access denied: path outside workspace."
COMMAND_DENIED = "Command blocked by the desktop automation policy."


class SandboxViolationError(PermissionError):
    """A request would step outside the workspace."""


class CommandBlockedError(SandboxViolationError):
    """A command is refused by the whitelist, the flag policy, or its timeout.

    Subclasses SandboxViolationError so callers that only catch the sandbox
    error still treat it as a refusal, while routes can report the two cases
    with different copy.
    """


class PermissionLevel(str, enum.Enum):
    """How much confirmation a user wants before sandboxed actions run.

    The sandbox (workspace boundary + command whitelist) always applies. This
    level only decides whether an action is allowed to run at all:

    * ``ASK_EVERY_TIME`` — every action raises :class:`PermissionRequired`.
    * ``AUTO_APPROVE`` — sandbox-permitted actions run; destructive/system
      commands stay refused by the whitelist before this level is consulted.
    * ``FULL_ACCESS`` — same sandbox, no permission prompts.
    """

    ASK_EVERY_TIME = "ask_every_time"
    AUTO_APPROVE = "auto_approve"
    FULL_ACCESS = "full_access"


DEFAULT_PERMISSION_LEVEL = PermissionLevel.AUTO_APPROVE
#: Stable string values used by the API/UI; keep in sync with PermissionLevel.
PERMISSION_LEVELS = tuple(level.value for level in PermissionLevel)
PERMISSION_REQUIRED_MESSAGE = (
    "Blocked: Requires 'Auto approve' or 'Full access' permission"
)


class PermissionRequired(SandboxViolationError):
    """The stored permission level requires explicit approval before this action."""

    def __init__(self, message: str = PERMISSION_REQUIRED_MESSAGE) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class CommandResult:
    """Metadata plus bounded output; the caller renders it, the log never does."""

    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    duration_ms: int = 0
    timed_out: bool = False


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class DesktopAutomationService:
    """One workspace, one policy, injectable OS seams for tests."""

    def __init__(
        self,
        workspace_dir: str,
        *,
        timeout_s: int = 30,
        runner=None,
        screenshotter=None,
        clipboard_reader=None,
        permission_level: "PermissionLevel | str" = DEFAULT_PERMISSION_LEVEL,
    ) -> None:
        self._root = Path(workspace_dir).expanduser().resolve()
        self._timeout_s = max(1, int(timeout_s))
        self._runner = runner or subprocess.run
        self._screenshotter = screenshotter or self._grab_screen
        self._clipboard_reader = clipboard_reader or self._read_clipboard_os
        self._permission_level = self._coerce_permission_level(permission_level)

    @staticmethod
    def _coerce_permission_level(value: "PermissionLevel | str") -> PermissionLevel:
        """Unknown/missing values fall back to the safe default, never to full."""
        try:
            return PermissionLevel(value)
        except (ValueError, TypeError):
            return DEFAULT_PERMISSION_LEVEL

    @property
    def permission_level(self) -> PermissionLevel:
        return self._permission_level

    def _require_permission(self) -> None:
        if self._permission_level is PermissionLevel.ASK_EVERY_TIME:
            raise PermissionRequired()

    # ── workspace ────────────────────────────────────────────

    @property
    def workspace(self) -> Path:
        return self._root

    def ensure_workspace(self) -> Path:
        """Create the workspace root and its scratch directory."""
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / ".tmp").mkdir(exist_ok=True)
        return self._root

    def resolve(self, path: str) -> Path:
        """Resolve a user path inside the workspace or refuse it."""
        raw = (path or "").strip()
        if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
            raise SandboxViolationError(PATH_DENIED)
        candidate = Path(raw)
        target = candidate if candidate.is_absolute() else self._root / candidate
        try:
            resolved = target.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise SandboxViolationError(PATH_DENIED) from exc
        root = self._root.resolve()
        if resolved != root and root not in resolved.parents:
            raise SandboxViolationError(PATH_DENIED)
        self._assert_not_protected(resolved)
        return resolved

    @staticmethod
    def _assert_not_protected(path: Path) -> None:
        """Second line of defence: never touch Windows or a user profile tree.

        ``%LOCALAPPDATA%\\Temp`` is exempt because it is scratch space rather
        than private data (test runs and portable builds live there); only this
        machine's own temp directory qualifies, so another user's profile is
        still refused.
        """
        lowered = str(path).lower().replace("\\", "/")
        for prefix in _PROTECTED_PREFIXES:
            if lowered == prefix or lowered.startswith(prefix + "/"):
                raise SandboxViolationError(PATH_DENIED)
        parts = lowered.split("/")
        if len(parts) >= 5 and parts[1] == "users" and parts[3] in (
            "appdata", "documents", "desktop", "downloads", "onedrive",
        ):
            if _is_under_process_temp(lowered):
                return
            raise SandboxViolationError(PATH_DENIED)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._root.resolve()).as_posix()

    # ── files ────────────────────────────────────────────────

    def read_file(self, path: str) -> str:
        self._require_permission()
        target = self.resolve(path)
        if not target.is_file():
            raise SandboxViolationError("File not found in workspace.")
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            raise SandboxViolationError(
                f"File is larger than {MAX_READ_BYTES // 1024} KiB."
            )
        raw = target.read_bytes()
        if b"\x00" in raw[:8192]:
            raise SandboxViolationError("Binary files cannot be read as text.")
        logger.info("desktop: read %s (%d bytes)", self._relative(target), len(raw))
        return raw.decode("utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> bool:
        self._require_permission()
        target = self.resolve(path)
        data = (content or "").encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise SandboxViolationError(
                f"Content is larger than {MAX_WRITE_BYTES // 1024} KiB."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        backed_up = False
        if target.exists():
            backup = (
                self._root / BACKUP_DIR
                / f"{self._relative(target).replace('/', '__')}.{_utc_stamp()}.bak"
            )
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            backed_up = True
        target.write_bytes(data)
        logger.info(
            "desktop: wrote %s (%d bytes, backup=%s)",
            self._relative(target),
            len(data),
            backed_up,
        )
        return True

    def list_workspace(self) -> List[Dict[str, object]]:
        """Breadth-first listing for the UI tree (bounded depth and count)."""
        self._require_permission()
        self.ensure_workspace()
        entries: List[Dict[str, object]] = []
        queue: List[Tuple[Path, int]] = [(self._root, 0)]
        while queue and len(entries) < MAX_TREE_ENTRIES:
            current, depth = queue.pop(0)
            try:
                children = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                continue
            for child in children:
                if len(entries) >= MAX_TREE_ENTRIES:
                    break
                if child.is_dir() and child.name in _SKIP_DIRS:
                    continue
                try:
                    size = child.stat().st_size if child.is_file() else 0
                except OSError:
                    size = 0
                entries.append(
                    {
                        "path": self._relative(child),
                        "name": child.name,
                        "is_dir": child.is_dir(),
                        "size": size,
                        "depth": depth + 1,
                    }
                )
                if child.is_dir() and depth + 1 < MAX_TREE_DEPTH:
                    queue.append((child, depth + 1))
        return entries

    # ── commands ─────────────────────────────────────────────

    def run_command(
        self,
        cmd: str,
        args: Optional[Sequence[str]] = None,
        timeout: Optional[int] = None,
    ) -> CommandResult:
        self._require_permission()
        command = (cmd or "").strip().lower()
        argv = [str(a) for a in (args or [])]
        for arg in argv:
            if "\x00" in arg or "\n" in arg or "\r" in arg:
                raise CommandBlockedError(COMMAND_DENIED)

        if command in BLOCKED_COMMANDS:
            logger.info("desktop: refused a blocked command")
            raise CommandBlockedError(COMMAND_DENIED)
        if command in INTERNAL_COMMANDS:
            return self._run_internal(command, argv)
        if command not in ALLOWED_PROGRAMS:
            raise CommandBlockedError(COMMAND_DENIED)

        self._validate_args(command, argv)
        executable = self._resolve_executable(command)
        self.ensure_workspace()
        started = time.monotonic()
        try:
            completed = self._runner(
                [executable, *argv],
                cwd=str(self._root),
                env=self._child_env(),
                shell=False,
                capture_output=True,
                timeout=max(1, int(timeout or self._timeout_s)),
            )
        except subprocess.TimeoutExpired as exc:
            logger.info("desktop: command timed out (%s)", command)
            return CommandResult(
                command=command,
                returncode=-1,
                stdout=_text(exc.stdout),
                stderr=f"Command timed out after {max(1, int(timeout or self._timeout_s))}s.",
                truncated=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
            )
        except OSError as exc:
            raise SandboxViolationError(
                f"Could not start '{command}' ({type(exc).__name__})."
            ) from exc

        duration = int((time.monotonic() - started) * 1000)
        stdout, cut_out = _truncate(_text(completed.stdout))
        stderr, cut_err = _truncate(_text(completed.stderr))
        # Metadata only: the command line and its output are never logged.
        logger.info(
            "desktop: ran %s (argc=%d, exit=%s, %dms, truncated=%s)",
            command,
            len(argv),
            completed.returncode,
            duration,
            cut_out or cut_err,
        )
        return CommandResult(
            command=command,
            returncode=int(completed.returncode),
            stdout=stdout,
            stderr=stderr,
            truncated=cut_out or cut_err,
            duration_ms=duration,
        )

    def _run_internal(self, command: str, argv: List[str]) -> CommandResult:
        """dir/ls/cat/type/echo without a shell (so no parsing is involved)."""
        started = time.monotonic()
        try:
            if command in ("dir", "ls"):
                text = "\n".join(
                    f"{'d' if e['is_dir'] else '-'} {e['size']:>10} {e['path']}"
                    for e in self.list_workspace()
                )
                result = CommandResult(command=command, returncode=0, stdout=text)
            elif command in ("cat", "type"):
                if not argv:
                    raise SandboxViolationError("Usage: cat <path>")
                result = CommandResult(
                    command=command, returncode=0, stdout=self.read_file(argv[0])
                )
            else:  # echo
                result = CommandResult(command=command, returncode=0, stdout=" ".join(argv))
        except SandboxViolationError:
            raise
        else:
            stdout, truncated = _truncate(result.stdout)
            return CommandResult(
                command=command,
                returncode=0,
                stdout=stdout,
                truncated=truncated,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

    def _validate_args(self, command: str, argv: List[str]) -> None:
        dangerous = DANGEROUS_FLAGS.get(command, ())
        for arg in argv:
            lowered = arg.lower()
            if lowered in dangerous or lowered.split("=", 1)[0] in {
                flag.split("=", 1)[0] for flag in dangerous
            }:
                logger.info("desktop: refused a dangerous flag for %s", command)
                raise CommandBlockedError(COMMAND_DENIED)
            if command == "pip" and lowered.split("=", 1)[0] in _PIP_BLOCKED_FLAGS:
                raise CommandBlockedError(COMMAND_DENIED)
            if _looks_like_path(arg):
                self.resolve(arg)  # raises when it escapes the workspace
        subcommands = ALLOWED_SUBCOMMANDS.get(command)
        if subcommands is not None:
            first = next((a for a in argv if not a.startswith("-")), "")
            if first.lower() not in subcommands:
                raise CommandBlockedError(COMMAND_DENIED)
        if command == "pip" and not argv:
            raise CommandBlockedError(COMMAND_DENIED)

    def _resolve_executable(self, command: str) -> str:
        """Locate the binary, pinning pip to the workspace virtualenv."""
        if command == "pip":
            for candidate in (
                self._root / ".venv" / "Scripts" / "pip.exe",
                self._root / ".venv" / "bin" / "pip",
            ):
                if candidate.is_file():
                    return str(candidate)
            raise CommandBlockedError(
                "pip is only available from the workspace virtualenv "
                "(create it at <workspace>/.venv)."
            )
        for name in (command, f"{command}.exe"):
            found = shutil.which(name)
            if found:
                return found
        raise CommandBlockedError(f"'{command}' is not installed.")

    def _child_env(self) -> Dict[str, str]:
        """A minimal environment: no app secrets, HOME/TEMP inside the workspace."""
        keep = (
            "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL",
        )
        env = {key: os.environ[key] for key in keep if key in os.environ}
        scratch = self._root / ".tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "HOME": str(self._root),
                "USERPROFILE": str(self._root),
                "TEMP": str(scratch),
                "TMP": str(scratch),
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return env

    # ── screen and clipboard ─────────────────────────────────

    def take_screenshot(self) -> bytes:
        """Capture the screen as PNG bytes held in memory — never written to disk."""
        self._require_permission()
        try:
            data = self._screenshotter()
        except SandboxViolationError:
            raise
        except Exception as exc:  # noqa: BLE001 - capture failures are bounded
            logger.warning("desktop: screenshot failed (%s)", type(exc).__name__)
            raise SandboxViolationError(
                "Screenshot capture is unavailable on this session."
            ) from exc
        raw = bytes(data or b"")
        if not raw:
            raise SandboxViolationError("Screenshot capture returned no image.")
        if len(raw) > MAX_SCREENSHOT_BYTES:
            raise SandboxViolationError("Screenshot is too large to analyse.")
        logger.info("desktop: captured a screenshot (%d bytes)", len(raw))
        return raw

    def get_clipboard(self) -> str:
        """Read the clipboard (read-only by design; there is no write method)."""
        self._require_permission()
        try:
            text = self._clipboard_reader()
        except SandboxViolationError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("desktop: clipboard read failed (%s)", type(exc).__name__)
            raise SandboxViolationError("Clipboard is unavailable.") from exc
        value = str(text or "")
        if len(value) > MAX_CLIPBOARD_CHARS:
            value = value[:MAX_CLIPBOARD_CHARS]
        logger.info("desktop: read clipboard (%d chars)", len(value))
        return value

    @staticmethod
    def _grab_screen() -> bytes:
        from PIL import ImageGrab  # imported lazily: only needed by this feature

        image = ImageGrab.grab()
        try:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        finally:
            image.close()

    @staticmethod
    def _read_clipboard_os() -> str:
        if os.name != "nt":
            raise SandboxViolationError("Clipboard is only supported on Windows.")
        import ctypes

        cf_unicode_text = 13
        user32 = ctypes.windll.user32
        if not user32.OpenClipboard(None):
            raise SandboxViolationError("Clipboard is busy.")
        try:
            handle = user32.GetClipboardData(cf_unicode_text)
            if not handle:
                return ""
            kernel32 = ctypes.windll.kernel32
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()


def _is_under_process_temp(lowered_path: str) -> bool:
    """True when the path is inside this process's temp directory."""
    try:
        import tempfile

        root = str(Path(tempfile.gettempdir()).resolve()).lower().replace("\\", "/")
    except Exception:  # noqa: BLE001 - no temp dir means no exemption
        return False
    return lowered_path == root or lowered_path.startswith(root + "/")


def _looks_like_path(arg: str) -> bool:
    if not arg or arg.startswith("-"):
        return False
    if len(arg) > 1 and arg[1] == ":":
        return True
    return "/" in arg or "\\" in arg or arg in (".", "..")


def _text(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _truncate(text: str, limit: Optional[int] = None) -> Tuple[str, bool]:
    # Resolve the module constant at call time so a configured/tested cap is
    # always the one in force (a default argument would freeze it at import).
    cap = MAX_OUTPUT_BYTES if limit is None else max(0, int(limit))
    text = text or ""
    if len(text) <= cap:
        return text, False
    return text[:cap], True
