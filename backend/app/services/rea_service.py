"""REA MCP integration for the OpenBot orchestrator (opt-in).

REA (``rea-agents``) is a reverse-engineering MCP server with 200+ tools. The
orchestrator only reaches a curated set of stateless, read-only inspection
tools, and every path argument must resolve inside ``workspace_dir`` after
symlinks are followed. Debugger/session/write tools are never allowlisted.

The integration is deliberately fail-closed:

* ``REA_MCP_ENABLED`` is off by default; with it off nothing is spawned.
* A missing ``rea`` executable is a bounded :class:`ReaUnavailable` error.
* Unknown tools and out-of-workspace paths raise before the server is spawned.
* Output is truncated to ``REA_MAX_OUTPUT_CHARS``.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from app.config import settings
from app.services import mcp_service
from app.services.mcp_service import MCPServerCfg

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 2
_MAX_WALK_ENTRIES = 500
_MAX_WALK_DEPTH = 3

#: Stateless, read-only inspection tools. Anything that opens a persistent
#: session, launches a debugger, writes files, or talks to a device is excluded.
ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "inspect_binary_layout",
        "inspect_byte_entropy",
        "inspect_pe_imports",
        "inspect_pe_resources",
        "inspect_pe_load_config",
        "inspect_pe_certificates",
        "inspect_elf_notes",
        "inspect_native_exports",
        "inspect_go_binary",
        "inspect_rust_binary",
        "inspect_python_pyc",
        "inspect_sqlite_database",
        "inspect_desktop_application",
        "inspect_plist",
        "inspect_apk_signature",
        "inspect_wasm_artifact",
        "inspect_managed_artifact",
        "inspect_devicetree",
        "inspect_apple_ips",
        "identify_flutter_build",
    }
)

#: Argument names whose values are filesystem paths and must stay in-workspace.
_PATH_KEYS = frozenset(
    {
        "path",
        "input_path",
        "snapshot_path",
        "glue_paths",
        "candidate_paths",
        "source_paths",
    }
)

_SUFFIX_TOOL: dict[str, str] = {
    ".exe": "inspect_pe_imports",
    ".dll": "inspect_pe_imports",
    ".sys": "inspect_pe_imports",
    ".scr": "inspect_pe_imports",
    ".cpl": "inspect_pe_imports",
    ".mui": "inspect_pe_imports",
    ".so": "inspect_elf_notes",
    ".elf": "inspect_elf_notes",
    ".o": "inspect_elf_notes",
    ".ko": "inspect_elf_notes",
    ".pyc": "inspect_python_pyc",
    ".sqlite": "inspect_sqlite_database",
    ".sqlite3": "inspect_sqlite_database",
    ".db": "inspect_sqlite_database",
    ".wasm": "inspect_wasm_artifact",
    ".apk": "inspect_apk_signature",
    ".plist": "inspect_plist",
    ".dylib": "inspect_binary_layout",
    ".bin": "inspect_binary_layout",
    ".img": "inspect_binary_layout",
    ".rom": "inspect_binary_layout",
    ".efi": "inspect_binary_layout",
}

_ARTIFACT_SUFFIXES = frozenset(_SUFFIX_TOOL)

_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".tmp",
    "dist",
    "build",
}

_EXPLICIT_PATH_RE = re.compile(
    r"""["']?([A-Za-z0-9_.\\/\-]+\.(?:%s))["']?"""
    % "|".join(sorted(s.lstrip(".") for s in _ARTIFACT_SUFFIXES)),
    re.IGNORECASE,
)

_SYSTEM_PREFIXES = ("c:/windows", "c:/program files", "c:/programdata")
_USER_PROFILE_RE = re.compile(
    r"^[a-z]:/users/[^/]+/(appdata|documents|desktop|downloads|onedrive)"
)


class ReaUnavailable(RuntimeError):
    """REA is disabled, not installed, or the workspace is unusable."""


class ReaToolNotAllowed(RuntimeError):
    """The requested tool is not on the read-only allowlist."""


class ReaConfinementError(RuntimeError):
    """A path argument escaped the workspace or does not exist."""


def is_enabled() -> bool:
    return bool(settings.rea_mcp_enabled)


def _shim() -> tuple[str, tuple[str, ...]] | None:
    extra = tuple(shlex.split(settings.rea_mcp_args or "mcp"))
    configured = (settings.rea_mcp_command or "").strip()
    if configured:
        return configured, extra
    found = shutil.which("rea")
    if not found:
        return None
    if os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
        # A .cmd shim is not directly executable by the MCP stdio spawner.
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        return comspec, ("/c", found, *extra)
    return found, extra


def build_config() -> MCPServerCfg:
    shim = _shim()
    if shim is None:
        raise ReaUnavailable(
            "REA is not installed (install rea-agents or set REA_MCP_COMMAND)."
        )
    command, args = shim
    return MCPServerCfg(
        id=0,
        name="rea",
        command=command,
        args=args,
        env=None,
        tool_allowlist=ALLOWED_TOOLS,
    )


def workspace_root() -> Path:
    raw = (settings.workspace_dir or "").strip()
    if not raw:
        raise ReaUnavailable("workspace_dir is not configured.")
    try:
        root = Path(raw).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ReaUnavailable("workspace_dir does not exist.") from exc
    if not root.is_dir():
        raise ReaUnavailable("workspace_dir is not a directory.")
    normalized = str(root).lower().replace("\\", "/")
    for prefix in _SYSTEM_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            raise ReaUnavailable("workspace_dir must not point at a system directory.")
    if _USER_PROFILE_RE.match(normalized):
        raise ReaUnavailable(
            "workspace_dir must not point at a user profile folder."
        )
    return root


def _confine_path(raw: Any, root: Path) -> str:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ReaConfinementError("Path arguments must be non-empty strings.")
    candidate = Path(raw.strip())
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ReaConfinementError(f"{raw!r} does not exist in the workspace.") from exc
    if resolved != root and not resolved.is_relative_to(root):
        raise ReaConfinementError(f"{raw!r} is outside the workspace.")
    return str(resolved)


def confine_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with every path-valued argument resolved in-workspace."""
    if not isinstance(arguments, dict):
        raise ReaConfinementError("Tool arguments must be an object.")
    root = workspace_root()
    confined: dict[str, Any] = {}
    for key, value in arguments.items():
        if key not in _PATH_KEYS:
            confined[key] = value
            continue
        if isinstance(value, (list, tuple)):
            confined[key] = [_confine_path(item, root) for item in value]
        else:
            confined[key] = _confine_path(value, root)
    return confined


def tool_for_path(path: str | Path) -> str:
    return _SUFFIX_TOOL.get(Path(path).suffix.lower(), "inspect_binary_layout")


def _query_paths(query: str) -> list[str]:
    return [match.group(1) for match in _EXPLICIT_PATH_RE.finditer(query or "")]


def find_workspace_candidates(
    query: str, limit: int = MAX_CANDIDATES
) -> tuple[list[Path], list[str]]:
    """Pick workspace artifacts to inspect, preferring paths named in the query.

    Explicit paths outside the workspace are refused (and reported); they are
    never passed to REA. When the query names nothing usable, a bounded walk
    collects the first artifacts in deterministic order.
    """
    root = workspace_root()
    warnings: list[str] = []
    explicit: list[Path] = []
    for raw in _query_paths(query):
        try:
            resolved = Path(_confine_path(raw, root))
        except ReaConfinementError:
            warnings.append(f"Skipped {raw!r}: it is not inside the workspace.")
            continue
        if resolved.is_file():
            explicit.append(resolved)

    if explicit:
        return explicit[:limit], warnings

    found: list[Path] = []
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        if depth >= _MAX_WALK_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name.lower() not in _SKIP_DIRS and not name.startswith(".")
            )
        for name in sorted(filenames):
            visited += 1
            if visited > _MAX_WALK_ENTRIES:
                break
            if Path(name).suffix.lower() in _ARTIFACT_SUFFIXES:
                found.append(Path(dirpath) / name)
                if len(found) >= limit:
                    break
        if len(found) >= limit or visited > _MAX_WALK_ENTRIES:
            break

    if not found:
        warnings.append(
            "No analyzable artifact was found in the workspace "
            "(looked for " + ", ".join(sorted(_ARTIFACT_SUFFIXES)) + ")."
        )
    return found, warnings


def _truncate(text: str) -> str:
    limit = max(500, int(settings.rea_max_output_chars))
    compact = (text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def call_tool(
    tool: str, arguments: dict[str, Any], timeout_s: float | None = None
) -> tuple[str, bool]:
    """Call one allowlisted REA tool with confined arguments.

    Raises :class:`ReaUnavailable`, :class:`ReaToolNotAllowed`, or
    :class:`ReaConfinementError` before any server process is spawned.
    """
    if not is_enabled():
        raise ReaUnavailable("REA MCP is disabled (REA_MCP_ENABLED is false).")
    if tool not in ALLOWED_TOOLS:
        raise ReaToolNotAllowed(f"REA tool {tool!r} is not allowlisted.")
    safe_arguments = confine_arguments(arguments)
    cfg = build_config()
    text, is_error = mcp_service.call_tool(
        cfg,
        tool,
        safe_arguments,
        timeout_s=timeout_s if timeout_s is not None else settings.rea_mcp_timeout_s,
    )
    return _truncate(text), is_error


def discover_tools() -> list[dict]:
    """Discover REA tools and return only the allowlisted subset."""
    cfg = build_config()
    tools = mcp_service.discover_tools(cfg)
    return [tool for tool in tools if tool.get("name") in ALLOWED_TOOLS]


def allowed_tool_names() -> tuple[str, ...]:
    return tuple(sorted(ALLOWED_TOOLS))


def artifact_suffixes() -> tuple[str, ...]:
    return tuple(sorted(_ARTIFACT_SUFFIXES))
