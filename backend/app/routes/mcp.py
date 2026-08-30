"""MCP server manager routes (Phase 1 — stdio, Python servers only).

GET    /api/mcp/servers
POST   /api/mcp/servers
PUT    /api/mcp/servers/{id}
DELETE /api/mcp/servers/{id}
POST   /api/mcp/servers/{id}/discover
"""

import json
import re
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import MCPServer, User
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services.mcp_service import MCPServerCfg, MCPError, discover_tools

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

_NAME_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


# ── Schemas ──────────────────────────────────

class McpServerIn(BaseModel):
    name: str
    command: str
    args: List[str] = []
    env: Optional[dict] = None
    enabled: bool = True
    tool_allowlist: Optional[List[str]] = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _NAME_RE.match(v or "") or len(v) > 40:
            raise ValueError(
                "name must match ^[a-z0-9]+(?:_[a-z0-9]+)*$ (lowercase, digits, "
                "single underscores) and be at most 40 chars"
            )
        return v


class McpServerUpdate(BaseModel):
    name: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[dict] = None
    enabled: Optional[bool] = None
    tool_allowlist: Optional[List[str]] = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v):
        if v is None:
            return v
        if not _NAME_RE.match(v) or len(v) > 40:
            raise ValueError("invalid name")
        return v


class McpServerOut(BaseModel):
    model_config = ConfigDict(from_attributes=False)
    id: int
    name: str
    command: str
    args: List[str]
    env: Optional[dict]
    enabled: bool
    tool_allowlist: Optional[List[str]]
    tools: list
    last_discovered_at: Optional[datetime]
    last_error: Optional[str]
    created_at: datetime


def _to_out(row: MCPServer) -> McpServerOut:
    def _j(s, default):
        if not s:
            return default
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return default

    return McpServerOut(
        id=row.id, name=row.name, command=row.command,
        args=_j(row.args_json, []), env=_j(row.env_json, None),
        enabled=row.enabled, tool_allowlist=_j(row.tool_allowlist_json, None),
        tools=_j(row.tools_json, []),
        last_discovered_at=row.last_discovered_at, last_error=row.last_error,
        created_at=row.created_at,
    )


def _owned(db: Session, user_id: int, server_id: int) -> MCPServer:
    row = db.query(MCPServer).filter(
        MCPServer.id == server_id, MCPServer.user_id == user_id
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return row


# ── Endpoints ────────────────────────────────

@router.get("/servers", response_model=List[McpServerOut])
def list_servers(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(MCPServer).filter(MCPServer.user_id == user.id).order_by(MCPServer.id).all()
    return [_to_out(r) for r in rows]


@router.post("/servers", response_model=McpServerOut, status_code=201)
def create_server(
    payload: McpServerIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    if db.query(MCPServer).filter(
        MCPServer.user_id == user.id, MCPServer.name == payload.name
    ).first():
        raise HTTPException(status_code=400, detail=f"A server named '{payload.name}' already exists")
    row = MCPServer(
        user_id=user.id, name=payload.name, command=payload.command,
        args_json=json.dumps(payload.args or []),
        env_json=json.dumps(payload.env) if payload.env else None,
        enabled=payload.enabled,
        tool_allowlist_json=json.dumps(payload.tool_allowlist) if payload.tool_allowlist is not None else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.put("/servers/{server_id}", response_model=McpServerOut)
def update_server(
    server_id: int,
    payload: McpServerUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    row = _owned(db, user.id, server_id)
    if payload.name is not None and payload.name != row.name:
        if db.query(MCPServer).filter(
            MCPServer.user_id == user.id,
            MCPServer.name == payload.name,
            MCPServer.id != server_id,
        ).first():
            raise HTTPException(status_code=400, detail=f"A server named '{payload.name}' already exists")
    if payload.name is not None:
        row.name = payload.name
    if payload.command is not None:
        row.command = payload.command
    if payload.args is not None:
        row.args_json = json.dumps(payload.args)
    if payload.env is not None:
        row.env_json = json.dumps(payload.env) if payload.env else None
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.tool_allowlist is not None:
        row.tool_allowlist_json = json.dumps(payload.tool_allowlist)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.delete("/servers/{server_id}", status_code=204)
def delete_server(
    server_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    db.delete(_owned(db, user.id, server_id))
    db.commit()
    return None


@router.post("/servers/{server_id}/discover")
def discover(
    server_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    row = _owned(db, user.id, server_id)
    cfg = MCPServerCfg.from_row(row)
    try:
        tools = discover_tools(cfg)
    except MCPError as exc:
        row.last_error = str(exc)
        db.commit()
        return {"tools": [], "error": str(exc)}
    row.tools_json = json.dumps(tools)
    row.last_discovered_at = datetime.utcnow()
    row.last_error = None
    db.commit()
    return {"tools": tools, "error": None}
