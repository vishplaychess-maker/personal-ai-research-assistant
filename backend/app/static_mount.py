"""Serve the built React frontend (frontend/dist) from FastAPI.

Mounting strategy (HARD invariant)
----------------------------------
The SPA is mounted at "/" as the LAST route on the app, after every /api,
/health and docs route. Starlette matches routes in registration order, so
every explicitly-registered API endpoint wins and the mount only ever sees
paths nothing else claimed.

Fail-closed: when frontend/dist is missing (headless backend, tests, a broken
build), mount_frontend() logs a bounded warning and adds NO route - the API
still runs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.paths import get_static_dir

logger = logging.getLogger(__name__)

#: Full request paths that must never fall back to index.html.
_BACKEND_EXACT = frozenset({
    "/health", "/api", "/api/", "/docs", "/redoc", "/openapi.json",
})


def _is_backend_path(path: str) -> bool:
    """True for API/health/docs paths that must 404 rather than serve the SPA."""
    clean = "/" + (path or "").lstrip("/")
    if clean in _BACKEND_EXACT:
        return True
    return clean.startswith("/api/")


class SpaStaticFiles(StaticFiles):
    """StaticFiles with index.html fallback for unknown non-API paths.

    StaticFiles(html=True) only rewrites directory requests and 404.html; the
    extra fallback here is what makes client-side routes such as /settings
    deep-link correctly. API paths are excluded so a mistyped endpoint still
    returns a JSON 404 instead of the SPA shell.
    """

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not _is_backend_path(scope.get("path", "")):
                return await super().get_response("index.html", scope)
            raise


def resolve_static_dir(static_dir: Optional[Path] = None) -> Optional[Path]:
    """Return a usable SPA build dir (must contain index.html), else None."""
    try:
        candidate = Path(static_dir) if static_dir is not None else get_static_dir()
    except Exception as exc:  # noqa: BLE001 - resolution must never raise
        logger.warning("frontend: could not resolve static dir (%s)", type(exc).__name__)
        return None
    try:
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
    except OSError as exc:
        logger.warning("frontend: static dir check failed (%s)", type(exc).__name__)
    return None


def mount_frontend(app: FastAPI, static_dir: Optional[Path] = None) -> bool:
    """Mount the SPA at "/" when a build exists. Returns True when mounted.

    MUST be called after every router and endpoint is registered so /api wins.
    """
    resolved = resolve_static_dir(static_dir)
    if resolved is None:
        try:
            shown = static_dir if static_dir is not None else get_static_dir()
        except Exception:  # noqa: BLE001
            shown = "<unresolved>"
        logger.warning(
            "frontend: build not found at %s - serving API only (headless mode)",
            shown,
        )
        return False
    try:
        app.mount("/", SpaStaticFiles(directory=str(resolved), html=True), name="frontend")
    except Exception as exc:  # noqa: BLE001 - never block startup on a mount failure
        logger.warning("frontend: mount failed (%s) - serving API only", type(exc).__name__)
        return False
    logger.info("frontend: mounted SPA build from %s", resolved)
    return True


__all__ = ["SpaStaticFiles", "mount_frontend", "resolve_static_dir"]
