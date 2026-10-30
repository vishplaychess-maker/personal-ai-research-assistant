"""Read-only context collection for the OpenBot orchestrator (Phase 1).

Sources:
* workspace tree + git status/log (only when desktop automation names a real
  workspace; paths are confined to that root and output is bounded),
* official MCP server credentials from the registry, queried through their
  provider APIs (GitHub / Notion / PostgreSQL) with short timeouts.

Nothing here writes, executes user code, or logs credentials. Every failure is
a bounded warning, never an exception.
"""

from __future__ import annotations

import ast
import fnmatch
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

import httpx

from app.config import settings
from app.services import mcp_config_service, mcp_registry
from app.services.encryption_service import decrypt_key

logger = logging.getLogger(__name__)

MAX_FILE_ENTRIES = 30
MAX_GIT_LINES = 25
MAX_BLOCK_CHARS = 1800
HTTP_TIMEOUT_S = 8.0
MAX_SEARCH_RESULTS = 100
MAX_SEARCH_DEPTH = 6
MAX_SUMMARY_CHARS = 180

_SYSTEM_PREFIXES = ("c:/windows", "c:/program files", "c:/programdata")
_USER_PROFILE_RE = re.compile(
    r"^[a-z]:/users/[^/]+/(appdata|documents|desktop|downloads|onedrive)"
)

#: Never surface these names (or Secret-looking files) in agent context.
_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", ".tmp", ".pytest_cache",
    ".superpowers", "secrets", "data", "dist", "build",
}
_SENSITIVE_TOKENS = ("env", "secret", "credential", "token", ".key", ".pem", ".db")


def _is_safe_entry(name: str) -> bool:
    lowered = name.lower()
    if lowered in _SKIP_DIRS:
        return False
    if lowered.startswith(".env"):
        return False
    return not any(token in lowered for token in _SENSITIVE_TOKENS)


def _is_safe_git_line(line: str) -> bool:
    lowered = line.lower()
    return not any(token in lowered for token in _SENSITIVE_TOKENS)


def _workspace_search_root() -> Path:
    """Resolved workspace root for the recursive search tool."""
    raw = (settings.workspace_dir or "").strip()
    if not raw:
        raise ValueError("workspace_dir is not configured")
    root = Path(raw).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace_dir is not a directory")
    normalized = str(root).lower().replace("\\", "/")
    for prefix in _SYSTEM_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            raise ValueError("workspace_dir must not point at a system directory")
    if _USER_PROFILE_RE.match(normalized):
        raise ValueError("workspace_dir must not point at a user profile folder")
    return root


def _module_summary(path: Path) -> str:
    """First docstring line (or first meaningful line) of a Python file."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(8192)
        text = raw.decode("utf-8", errors="replace")
        try:
            doc = ast.get_docstring(ast.parse(text))
        except (SyntaxError, ValueError):
            doc = None
        if doc:
            first = " ".join(doc.strip().splitlines()[0].split())
        else:
            first = ""
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    first = stripped
                    break
        return first[:MAX_SUMMARY_CHARS]
    except OSError:
        return ""


def search_workspace_files(
    directory: str = ".",
    pattern: str = "*",
    max_results: int = MAX_SEARCH_RESULTS,
) -> List[dict]:
    """List files under a workspace directory, confined and read-only.

    Security:
    * the workspace root is re-validated on every call and cannot be a system
      or user-profile directory;
    * relative directories resolve against the workspace and absolute
      directories are accepted only when their real path stays inside it;
    * ``..``, symlink escapes, glob path separators, and sensitive file names
      are rejected/skipped;
    * traversal is depth- and result-bounded, and only a short docstring line
      is read from Python files.
    """
    root = _workspace_search_root()
    raw_dir = (directory or ".").strip()
    if "\x00" in raw_dir:
        raise ValueError("invalid directory")
    candidate = Path(raw_dir)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=True)
    if resolved != root and not resolved.is_relative_to(root):
        raise ValueError("directory is outside the workspace")
    if not resolved.is_dir():
        raise ValueError("directory is not a directory")

    clean_pattern = (pattern or "*").strip()
    if (
        not clean_pattern
        or ".." in clean_pattern
        or "/" in clean_pattern
        or "\\" in clean_pattern
        or clean_pattern.startswith("~")
    ):
        raise ValueError("pattern must be a file-name glob without path separators")

    limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
    results: List[dict] = []
    for dirpath, dirnames, filenames in os.walk(resolved):
        current = Path(dirpath)
        depth = len(current.relative_to(resolved).parts)
        if depth >= MAX_SEARCH_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(
                name for name in dirnames
                if _is_safe_entry(name) and not name.startswith(".")
            )
        for name in sorted(filenames):
            if not _is_safe_entry(name):
                continue
            if not fnmatch.fnmatch(name.lower(), clean_pattern.lower()):
                continue
            path = current / name
            try:
                stat = path.stat()
            except OSError:
                continue
            results.append({
                "path": path.relative_to(root).as_posix(),
                "size": stat.st_size,
                "description": _module_summary(path) if path.suffix.lower() == ".py" else "",
            })
            if len(results) >= limit:
                return results
    return results

Emitter = Callable[..., Any]


@dataclass
class ContextBundle:
    blocks: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n\n".join(self.blocks)


def _truncate(text: str, limit: int = MAX_BLOCK_CHARS) -> str:
    compact = (text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _run_git(root: Path, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=3,
            shell=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if proc.returncode != 0:
            return None
        return proc.stdout
    except Exception as exc:  # noqa: BLE001 - context is best-effort
        logger.warning("git context failed (%s)", type(exc).__name__)
        return None


def _workspace_context(emit: Emitter) -> ContextBundle:
    bundle = ContextBundle()
    if not settings.desktop_automation_enabled:
        return bundle
    root = Path(settings.workspace_dir).expanduser()
    try:
        root = root.resolve(strict=True)
    except OSError:
        bundle.warnings.append(
            "Workspace context is unavailable (workspace_dir does not exist)."
        )
        return bundle
    if not root.is_dir():
        return bundle

    try:
        entries = sorted(
            (p.name for p in root.iterdir() if _is_safe_entry(p.name)),
            key=str.lower,
        )[:MAX_FILE_ENTRIES]
        bundle.blocks.append(
            "Workspace files (top level):\n" + "\n".join(f"- {name}" for name in entries)
        )
        bundle.tools.append("workspace")
        emit("tool", agent="context", tool="workspace", message="Listed workspace files")
        emit("output", agent="context", output=_truncate(", ".join(entries), 500))
    except OSError as exc:
        bundle.warnings.append(f"Workspace listing failed ({type(exc).__name__}).")
        return bundle

    branch = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _run_git(root, "status", "--porcelain")
    log = _run_git(root, "log", "-n", "5", "--oneline")
    if branch or status is not None or log:
        emit("tool", agent="context", tool="git", message="Read git branch/status/log")
        lines = []
        if branch:
            lines.append(f"Branch: {branch.strip()}")
        if status is not None:
            dirty = [
                line for line in status.splitlines()
                if line.strip() and _is_safe_git_line(line)
            ][:MAX_GIT_LINES]
            lines.append("Working tree changes:\n" + ("\n".join(dirty) if dirty else "(clean)"))
        if log:
            lines.append("Recent commits:\n" + "\n".join(log.splitlines()[:5]))
        text = _truncate("\n".join(lines))
        bundle.blocks.append(text)
        bundle.tools.extend(["workspace", "git"])
        emit("output", agent="context", output=text)
    else:
        bundle.warnings.append("Git context is unavailable for the workspace.")
    return bundle


def _github_context(token: str) -> str:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ThunderAI-Agent",
    }
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        me = client.get(mcp_registry.GITHUB_CHECK_URL, headers=headers)
        me.raise_for_status()
        profile = me.json()
        repos = client.get(
            "https://api.github.com/user/repos?per_page=5&sort=updated",
            headers=headers,
        )
        names = []
        if repos.status_code == 200:
            names = [r.get("full_name", "") for r in repos.json()[:5] if r.get("full_name")]
    parts = [f"GitHub user: {profile.get('login', 'unknown')}"]
    if names:
        parts.append("Recently updated repositories:\n" + "\n".join(f"- {n}" for n in names))
    return "\n".join(parts)


def _notion_context(token: str) -> str:
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        resp = client.post(
            "https://api.notion.com/v1/search",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            json={"page_size": 5},
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    titles = []
    for item in results[:5]:
        props = item.get("title") or item.get("properties") or {}
        title = ""
        if isinstance(props, list) and props:
            title = "".join(part.get("plain_text", "") for part in props[0].get("text", []))
        elif isinstance(props, dict):
            for value in props.values():
                if isinstance(value, dict) and value.get("type") == "title":
                    title = "".join(t.get("plain_text", "") for t in value.get("title", []))
                    break
        titles.append(title or item.get("id", "untitled"))
    return "Notion pages:\n" + "\n".join(f"- {t}" for t in titles)


def _postgres_context(url: str) -> str:
    from sqlalchemy import create_engine, text

    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT current_database(), current_user")
            ).first()
        return f"PostgreSQL database: {row[0]} (user {row[1]})"
    finally:
        engine.dispose()


_CONFIG_KEY = {
    "github-mcp": "GITHUB_TOKEN",
    "notion-mcp": "NOTION_TOKEN",
    "postgres-mcp": "POSTGRES_URL",
}


def _fetch_mcp_context(server_id: str, value: str) -> str:
    if server_id == "github-mcp":
        return _github_context(value)
    if server_id == "notion-mcp":
        return _notion_context(value)
    if server_id == "postgres-mcp":
        return _postgres_context(value)
    raise ValueError("unknown MCP server")


def _mcp_context(db, user_id: int, emit: Emitter) -> ContextBundle:
    bundle = ContextBundle()
    if db is None:
        return bundle
    for server in mcp_registry.list_servers():
        pref = mcp_config_service.get_preference(db, user_id, server.id)
        if pref is None or not pref.enabled:
            continue
        plain = mcp_config_service.decrypt_stored_config(pref.config_json)
        key_name = _CONFIG_KEY[server.id]
        value = (plain.get(key_name) or "").strip()
        if not value:
            bundle.warnings.append(f"{server.name} MCP is enabled but has no saved credentials.")
            continue
        emit(
            "tool",
            agent="context",
            tool=server.id,
            message=f"Reading {server.name} context",
        )
        try:
            result = _truncate(_fetch_mcp_context(server.id, value))
            bundle.blocks.append(f"{server.name} context:\n{result}")
            bundle.tools.append(server.id)
            emit("output", agent="context", output=result)
        except Exception as exc:  # noqa: BLE001 - never leak credentials/URLs
            logger.warning("%s context failed (%s)", server.id, type(exc).__name__)
            bundle.warnings.append(
                f"{server.name} context is unavailable ({type(exc).__name__})."
            )
    return bundle


def collect(db, user_id: int, emit: Emitter) -> ContextBundle:
    """Collect workspace + MCP context, emitting progress frames as it goes."""
    bundle = _workspace_context(emit)
    try:
        mcp_bundle = _mcp_context(db, user_id, emit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("MCP context collection failed (%s)", type(exc).__name__)
        mcp_bundle = ContextBundle(warnings=["MCP context collection failed."])
    bundle.blocks.extend(mcp_bundle.blocks)
    bundle.tools.extend(mcp_bundle.tools)
    bundle.warnings.extend(mcp_bundle.warnings)
    return bundle
