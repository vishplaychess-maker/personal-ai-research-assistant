"""Knowledge Graph API.

GET /api/knowledge-graph          — full graph {nodes, links} for the UI
GET /api/knowledge-graph/search   — subgraph around ?q=<term>
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User
from app.services.auth_service import get_current_user
from app.services import knowledge_graph

router = APIRouter(prefix="/api/knowledge-graph", tags=["knowledge-graph"])


@router.get("")
def get_graph(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, List[Dict[str, Any]]]:
    """Return the current user's knowledge graph as ``{"nodes", "links"}``."""
    return knowledge_graph.get_all_graph(db, current_user.id)


@router.get("/search")
def search_graph(
    q: str = Query("", description="Entity name substring to search for"),
    radius: int = Query(1, ge=1, le=3),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, List[Dict[str, Any]]]:
    """Return the subgraph of the current user's graph around entities matching ``q``."""
    return knowledge_graph.search_graph(db, current_user.id, q, radius=radius)
