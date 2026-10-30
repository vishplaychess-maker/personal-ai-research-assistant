"""OpenViking context-database sidecar client (fail-open, timeout-guarded).

Thunder AI talks to a local OpenViking server (default
``http://127.0.0.1:1933``) as a browsable long-term context and skill
registry (``viking://user/<id>/...``). Ground rules for Phase 2:

* Strictly additive. With ``openviking_enabled`` off nothing touches the
  network; with it on, every failure (down server, timeout, error
  envelope) degrades to a safe default. Callers never see exceptions.
* Bounded budget: 10 s for context retrieval, 30 s for skill registration
  (``OPENVIKING_TIMEOUT_S`` / ``OPENVIKING_SKILL_TIMEOUT_S``).
* Per-user scoping: reads and writes target the caller's own
  ``viking://user/<id>`` tree via both ``target_uri`` and the
  ``X-OpenViking-User`` header. No memory text or secrets are logged.

Self-evolving extraction (``session.commit()``) is deliberately NOT wired:
it wedges the sidecar queue for minutes on local 1B models. Integration is
read/write only (preferences, skill registry).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

import httpx

from app.config import settings
from app.services.async_bridge import run_coro_sync

logger = logging.getLogger(__name__)

FIND_PATH = "/api/v1/search/find"
SKILLS_PATH = "/api/v1/skills"
MAX_HITS = 5

#: Skill ids land in ``viking://`` URIs and SKILL.md frontmatter.
_SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")

#: Exact wording from the Phase 2 brief — tests assert on it.
_UNAVAILABLE_MSG = "OpenViking unavailable, falling back to standard context"


def is_enabled() -> bool:
    return bool(settings.openviking_enabled)


def _user_root(user_id: Any) -> str:
    return f"viking://user/{user_id}"


def _headers(user_id: Any) -> Dict[str, str]:
    return {"X-OpenViking-User": str(user_id)}


def _client(timeout_s: float) -> httpx.AsyncClient:
    """One short-lived client per call; tests monkeypatch this seam."""
    return httpx.AsyncClient(
        base_url=settings.openviking_url.rstrip("/"),
        timeout=httpx.Timeout(timeout_s),
    )


def _timeout_s(context: bool) -> float:
    if context:
        return float(settings.openviking_timeout_s)
    return float(settings.openviking_skill_timeout_s)


def _unwrap(data: Any) -> Any:
    """OpenViking envelope: {"result": ...} or {"status": "error", ...}."""
    if not isinstance(data, dict):
        return data
    if data.get("status") == "error":
        message = (data.get("error") or {}).get("message", "unknown error")
        raise RuntimeError(f"OpenViking server error: {message}")
    return data.get("result", data)


async def get_structured_context(user_id: str, query: str) -> Dict[str, Any]:
    """Find memories/skills for the user. Never raises; {} means "no context"."""
    query = (query or "").strip()
    if not is_enabled() or not query:
        return {}
    payload = {
        "query": query,
        "target_uri": _user_root(user_id),
        "limit": MAX_HITS,
    }
    try:
        async with _client(_timeout_s(context=True)) as client:
            response = await client.post(
                FIND_PATH, json=payload, headers=_headers(user_id)
            )
            response.raise_for_status()
            result = _unwrap(response.json())
    except Exception:  # noqa: BLE001 - context DB is an enhancement, never core
        logger.warning(_UNAVAILABLE_MSG)
        return {}

    hits: List[Any] = []
    if isinstance(result, list):
        hits = result
    elif isinstance(result, dict):
        for key in ("memories", "resources", "skills", "contexts", "results"):
            candidate = result.get(key)
            # First NON-EMPTY list wins: the server returns one entry per
            # context type and the leading keys may legitimately be [].
            if isinstance(candidate, list) and candidate:
                hits = candidate
                break

    memories = [
        {
            "uri": hit.get("uri"),
            "level": hit.get("level"),
            "score": hit.get("score"),
            "abstract": hit.get("abstract") or hit.get("description") or "",
        }
        for hit in hits
        if isinstance(hit, dict) and hit.get("uri")
    ]
    return {"memories": memories} if memories else {}


def format_context_block(context: Dict[str, Any]) -> str:
    """Render retrieved hits as a bounded Markdown block for prompts."""
    memories = (context or {}).get("memories") or []
    if not memories:
        return ""
    lines = ["## OpenViking structured context (long-term memory & skills)"]
    for item in memories[:MAX_HITS]:
        score = item.get("score")
        badge = (
            f" (relevance {float(score):.2f})"
            if isinstance(score, (int, float))
            else ""
        )
        abstract = (item.get("abstract") or "(no abstract)").replace("\n", " ")
        lines.append(f"- `{item.get('uri')}`{badge}: {abstract}")
    return "\n".join(lines)


def _skill_markdown(skill_id: str, description: str) -> str:
    safe_description = " ".join((description or "").split())[:500] or skill_id
    return (
        "---\n"
        f"name: {skill_id}\n"
        f"description: {safe_description}\n"
        "version: 1.0.0\n"
        "---\n\n"
        f"# {skill_id}\n\n{safe_description}\n"
    )


async def _skill_exists(skill_id: str) -> bool:
    """Cheap existence probe: the server keeps processing an accepted skill
    registration after the client's POST budget expires (live PoC)."""
    try:
        async with _client(min(5.0, _timeout_s(context=False))) as client:
            response = await client.get(
                f"{SKILLS_PATH}/{skill_id}", headers=_headers("system")
            )
            return response.status_code == 200
    except Exception:  # noqa: BLE001 - probe failure just means "unknown"
        return False


async def register_skill(skill_id: str, description: str) -> bool:
    """Mirror one enabled custom skill into the OpenViking registry.

    Never raises: True when the sidecar accepted the registration OR when a
    POST-timeout probe shows the skill landed (eventual completion).
    """
    if not is_enabled() or not _SKILL_ID_RE.match(skill_id or ""):
        return False
    try:
        async with _client(_timeout_s(context=False)) as client:
            response = await client.post(
                SKILLS_PATH,
                # wait=False: the server compiles SKILL.md with its VLM;
                # inline waiting would blow the 30 s budget (live PoC).
                json={"data": _skill_markdown(skill_id, description), "wait": False},
                headers=_headers("system"),
            )
            response.raise_for_status()
            _unwrap(response.json())
    except Exception:  # noqa: BLE001 - registration is best-effort
        posted = False
    else:
        return True

    if await _skill_exists(skill_id):
        return True
    logger.warning("OpenViking unavailable, skipping skill registration")
    return False


# ── Synchronous entry points (OpenBot worker threads) ─────────────────────


def get_structured_context_sync(user_id: Any, query: str) -> Dict[str, Any]:
    try:
        return run_coro_sync(lambda: get_structured_context(str(user_id), query))
    except Exception:  # noqa: BLE001 - bridge-level failures degrade too
        logger.warning(_UNAVAILABLE_MSG)
        return {}


def register_skill_sync(skill_id: str, description: str) -> bool:
    try:
        return run_coro_sync(lambda: register_skill(skill_id, description))
    except Exception:  # noqa: BLE001
        logger.warning("OpenViking unavailable, skipping skill registration")
        return False
