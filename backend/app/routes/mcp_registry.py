"""Official MCP server registry API (static catalog + per-user config).

GET   /api/mcp/registry                      — catalog merged with this user's prefs
PATCH /api/mcp/registry/{server_id}/config   — validate, encrypt, and save config

Kept separate from ``/api/mcp/servers`` (the existing user-defined stdio server
manager) so neither feature changes the other's contract.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services import mcp_config_service

router = APIRouter(prefix="/api/mcp/registry", tags=["mcp-registry"])
logger = logging.getLogger(__name__)


class OfficialMcpServerView(BaseModel):
    id: str
    name: str
    description: str
    required_config_keys: List[str]
    enabled: bool
    status: str
    configured_keys: List[str]


class McpServerConfigUpdate(BaseModel):
    enabled: bool
    config: Optional[Dict[str, str]] = None


@router.get("", response_model=List[OfficialMcpServerView])
def list_official_servers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return mcp_config_service.list_server_views(db, current_user.id)


@router.patch("/{server_id}/config", response_model=OfficialMcpServerView)
def update_official_server_config(
    server_id: str,
    payload: McpServerConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    try:
        return mcp_config_service.save_server_config(
            db,
            current_user.id,
            server_id,
            enabled=payload.enabled,
            provided=payload.config,
        )
    except mcp_config_service.McpServerNotFound:
        raise HTTPException(status_code=404, detail="Unknown MCP server")
    except mcp_config_service.McpConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except mcp_config_service.McpConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError:
        logger.warning("MCP config encryption unavailable")
        raise HTTPException(
            status_code=500,
            detail=(
                "API key encryption is unavailable: ENCRYPTION_KEY is not "
                "configured on the server."
            ),
        )
