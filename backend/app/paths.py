"""Portable filesystem paths for Thunder AI (dev checkout vs. frozen .exe).

This module is the single source of truth for every location that differs
between a normal source checkout ("dev mode") and a PyInstaller-frozen .exe:

=================  ==================================  ==========================================
Location           Dev mode                            Frozen (.exe)
=================  ==================================  ==========================================
Writable data      <repo>/data                         %APPDATA%/ThunderAI
SQLite database    <repo>/data/thunder.db              %APPDATA%/ThunderAI/thunder.db
Config JSON        <repo>/data/config.json             %APPDATA%/ThunderAI/config.json
Bundled static     <repo>/frontend/dist                <sys._MEIPASS>/frontend/dist
=================  ==================================  ==========================================

Design rules (HARD invariants)
------------------------------
* Never raise. A packaging/path problem degrades to a bounded warning and a
  best-effort path; it never crashes the process at import time (fail-closed).
* Never log secrets. config.json may hold an API key. Only its
  existence/shape is inspected; the value is never logged.
* No caching. Every helper recomputes from sys/os so tests can monkeypatch
  sys.frozen, sys._MEIPASS and APPDATA.

Environment override
--------------------
THUNDER_AI_DATA_DIR relocates the writable data directory in both modes
(used by the launcher and by tests). It never changes the bundled static path.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

APP_DIR_NAME = "ThunderAI"
DB_FILE_NAME = "thunder.db"
CONFIG_FILE_NAME = "config.json"
_DATA_DIR_ENV = "THUNDER_AI_DATA_DIR"


def is_frozen() -> bool:
    """True when running from a PyInstaller (or similar) frozen bundle.

    Detects both sys.frozen and sys._MEIPASS so one-file and one-dir
    PyInstaller builds are both recognised.
    """
    try:
        if bool(getattr(sys, "frozen", False)):
            return True
        return bool(getattr(sys, "_MEIPASS", None))
    except Exception:  # noqa: BLE001 - detection must never raise
        return False


def get_bundle_dir() -> Optional[Path]:
    """Read-only bundle directory, or None in dev mode.

    sys._MEIPASS is the unpacked one-file bundle; for a frozen process
    without _MEIPASS the directory holding the executable is used.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        try:
            return Path(meipass)
        except (TypeError, ValueError, OSError):
            return None
    if getattr(sys, "frozen", False):
        try:
            return Path(sys.executable).resolve().parent
        except (TypeError, ValueError, OSError):
            return None
    return None


def get_repo_root() -> Path:
    """Repository root in dev mode (backend/app/paths.py -> repo root)."""
    return Path(__file__).resolve().parents[2]


def _appdata_root() -> Path:
    """Windows roaming AppData root, with a portable fallback."""
    override = os.environ.get("APPDATA")
    if override and override.strip():
        return Path(override.strip())
    return Path.home() / "AppData" / "Roaming"


def get_data_dir() -> Path:
    """Writable data directory, created on demand. Never raises."""
    override = os.environ.get(_DATA_DIR_ENV)
    if override and override.strip():
        base = Path(override.strip())
    elif is_frozen():
        base = _appdata_root() / APP_DIR_NAME
    else:
        base = get_repo_root() / "data"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "paths: could not create data directory %s (%s)",
            base,
            type(exc).__name__,
        )
    return base


def get_db_path() -> Path:
    """Path to the portable SQLite database file."""
    return get_data_dir() / DB_FILE_NAME


def get_config_path() -> Path:
    """Path to the portable config.json (first-run settings)."""
    return get_data_dir() / CONFIG_FILE_NAME


def get_database_url() -> str:
    """SQLAlchemy SQLite URL for the portable database file."""
    return "sqlite:///" + get_db_path().as_posix()


def get_static_dir() -> Path:
    """Directory holding the built frontend (frontend/dist).

    Frozen builds look under sys._MEIPASS (both frontend/dist and a flat dist
    layout are accepted); dev mode uses the repo checkout.
    """
    bundle = get_bundle_dir()
    if bundle is not None:
        candidates = (bundle / "frontend" / "dist", bundle / "dist")
        for candidate in candidates:
            try:
                if candidate.is_dir():
                    return candidate
            except OSError:
                continue
        return candidates[0]
    return get_repo_root() / "frontend" / "dist"


def static_dir_available(static_dir: Optional[Path] = None) -> bool:
    """True when a usable SPA build (index.html) exists at the path."""
    try:
        candidate = Path(static_dir) if static_dir is not None else get_static_dir()
        return candidate.is_dir() and (candidate / "index.html").is_file()
    except OSError:
        return False
    except Exception:  # noqa: BLE001 - availability probe must never raise
        return False


def load_config() -> Dict[str, Any]:
    """Read config.json; missing/corrupt/non-object -> {}. Never raises."""
    try:
        path = get_config_path()
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - config must never crash startup
        logger.warning("paths: could not read config file (%s)", type(exc).__name__)
        return {}


def save_config(updates: Dict[str, Any]) -> bool:
    """Merge updates into config.json atomically. True on success.

    Writes to a sibling temp file and os.replaces it, so a crash can never
    leave a half-written config. The value(s) are never logged.
    """
    if not isinstance(updates, dict) or not updates:
        return False
    try:
        path = get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = load_config()
        merged.update(updates)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except Exception as exc:  # noqa: BLE001 - bounded failure, no crash
        logger.warning("paths: could not write config file (%s)", type(exc).__name__)
        return False


__all__ = [
    "APP_DIR_NAME",
    "DB_FILE_NAME",
    "CONFIG_FILE_NAME",
    "is_frozen",
    "get_bundle_dir",
    "get_repo_root",
    "get_data_dir",
    "get_db_path",
    "get_config_path",
    "get_database_url",
    "get_static_dir",
    "static_dir_available",
    "load_config",
    "save_config",
]
