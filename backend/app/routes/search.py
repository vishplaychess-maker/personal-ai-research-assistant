"""
Phase 5C — Conversation search endpoint.

Search across all messages in all sessions using SQLite LIKE.

GET /api/search?q=query
  Returns matching messages with session info, sorted by recency.
  Limited to 50 results, max query length 200 chars.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.documents import SearchResult

router = APIRouter(tags=["search"])

MAX_QUERY_LENGTH = 200
MAX_RESULTS = 50


@router.get("/api/search", response_model=List[SearchResult])
def search_messages(
    q: Optional[str] = Query(None, min_length=1, max_length=MAX_QUERY_LENGTH),
    db: Session = Depends(get_db),
):
    """
    Search across all messages in all sessions.

    Uses SQLite LIKE for case-insensitive substring matching.
    Results are ordered by message creation time (most recent first),
    limited to 50 results, and each snippet is truncated to 150 characters.
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="Search query is required")

    query_text = q.strip()

    # Use SQLAlchemy text() with parameterized bind to prevent injection
    sql = sa_text(
        """
        SELECT
            m.id AS message_id,
            m.session_id,
            rs.title AS session_title,
            m.role,
            m.content,
            SUBSTR(m.content, 1, 150) AS snippet,
            m.created_at
        FROM messages m
        JOIN research_sessions rs ON rs.id = m.session_id
        WHERE m.content LIKE '%' || :q || '%'
        ORDER BY m.created_at DESC
        LIMIT :limit
        """
    )

    rows = db.execute(sql, {"q": query_text, "limit": MAX_RESULTS}).fetchall()

    results: List[SearchResult] = []
    for row in rows:
        results.append(
            SearchResult(
                session_id=row.session_id,
                session_title=row.session_title,
                message_id=row.message_id,
                role=row.role,
                content=row.content,
                snippet=row.snippet,
                created_at=row.created_at,
            )
        )

    return results
