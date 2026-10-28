"""Static registry of official MCP servers and their connection checks.

Adding a new official server (Slack, Jira, ...) is intentionally a data change:
append an :class:`OfficialMCPServer` entry and a check branch in
``check_connection``. Nothing else in the app needs to know the id.

Secrets never appear in logs. Checks return a boolean and swallow provider
errors; callers translate that into a bounded message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

GITHUB_CHECK_URL = "https://api.github.com/user"
NOTION_CHECK_URL = "https://api.notion.com/v1/users/me"


@dataclass(frozen=True)
class OfficialMCPServer:
    id: str
    name: str
    description: str
    required_config_keys: tuple[str, ...]
    icon: str = "plug"
    docs_url: str = ""


OFFICIAL_SERVERS: tuple[OfficialMCPServer, ...] = (
    OfficialMCPServer(
        id="github-mcp",
        name="GitHub",
        description="Access repositories, issues, and pull requests.",
        required_config_keys=("GITHUB_TOKEN",),
        icon="github",
        docs_url="https://github.com/settings/tokens",
    ),
    OfficialMCPServer(
        id="notion-mcp",
        name="Notion",
        description="Search pages and databases in your workspace.",
        required_config_keys=("NOTION_TOKEN",),
        icon="notebook",
        docs_url="https://www.notion.so/my-integrations",
    ),
    OfficialMCPServer(
        id="postgres-mcp",
        name="PostgreSQL",
        description="Run read queries against a PostgreSQL database.",
        required_config_keys=("POSTGRES_URL",),
        icon="database",
        docs_url="https://www.postgresql.org/docs/",
    ),
)

_BY_ID = {server.id: server for server in OFFICIAL_SERVERS}


def list_servers() -> list[OfficialMCPServer]:
    return list(OFFICIAL_SERVERS)


def get_server(server_id: str) -> Optional[OfficialMCPServer]:
    return _BY_ID.get(server_id)


def _http_status(url: str, headers: Mapping[str, str], timeout: float = 10.0) -> int:
    """Return the HTTP status, or 0 when the request cannot be made."""
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            return client.get(url, headers=dict(headers)).status_code
    except Exception as exc:  # noqa: BLE001 - a probe never raises
        host = url.split("//", 1)[-1].split("/", 1)[0]
        logger.warning("MCP check failed for %s (%s)", host, type(exc).__name__)
        return 0


def _github_check(config: Mapping[str, str]) -> bool:
    token = (config.get("GITHUB_TOKEN") or "").strip()
    if not token:
        return False
    return (
        _http_status(
            GITHUB_CHECK_URL,
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "ThunderAI-MCP",
            },
        )
        == 200
    )


def _notion_check(config: Mapping[str, str]) -> bool:
    token = (config.get("NOTION_TOKEN") or "").strip()
    if not token:
        return False
    return (
        _http_status(
            NOTION_CHECK_URL,
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
            },
        )
        == 200
    )


def _postgres_check(config: Mapping[str, str]) -> bool:
    url = (config.get("POSTGRES_URL") or "").strip()
    if not url:
        return False
    engine = None
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - classify only, never echo the URL
        logger.warning("PostgreSQL MCP check failed (%s)", type(exc).__name__)
        return False
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:  # noqa: BLE001
                pass


def check_connection(server_id: str, config: Mapping[str, Any]) -> bool:
    """Validate the supplied credentials with a lightweight live call."""
    server = get_server(server_id)
    if server is None:
        return False
    values = config or {}
    if any(not str(values.get(key) or "").strip() for key in server.required_config_keys):
        return False
    if server_id == "github-mcp":
        return _github_check(values)
    if server_id == "notion-mcp":
        return _notion_check(values)
    if server_id == "postgres-mcp":
        return _postgres_check(values)
    return False
