"""Report Export — API routes.

POST /api/export — render the requested content as a PDF or DOCX download.

Authentication: required (same bearer scheme as the rest of the API).
CSRF: enforced via the double-submit dependency (state-changing POST).

The response is a ``StreamingResponse`` wrapping an in-memory buffer —
nothing is written to disk, so there is no path-traversal surface. The
``Content-Disposition`` filename is generated server-side from sanitized
input; the client never controls it.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services import export_service
from app.services.export_service import ExportFormat, ExportType

router = APIRouter(prefix="/api/export", tags=["export"])

logger = logging.getLogger(__name__)

_MEDIA_TYPES = {
    ExportFormat.PDF: "application/pdf",
    ExportFormat.DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
}


class ExportRequest(BaseModel):
    """Body of POST /api/export.

    ``type`` selects the payload shape:
      * ``research_report`` → requires ``data.report_text`` (markdown-ish)
      * ``chat``            → requires ``data.messages`` [{role, content}]
      * ``knowledge_graph`` → requires ``data.graph`` {nodes, links}
    """

    type: ExportType
    format: ExportFormat = ExportFormat.PDF
    title: Optional[str] = Field(default=None, max_length=export_service.MAX_TITLE_LEN)
    data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _sanitize_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = export_service.sanitize_text(v, max_chars=export_service.MAX_TITLE_LEN).strip()
        return cleaned or None

    @field_validator("data")
    @classmethod
    def _data_must_be_dict(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        # json body guarantees dict; guard against odd truthy values anyway.
        return v or {}


def _build_content(req: ExportRequest) -> Dict[str, Any]:
    """Extract and validate the type-specific payload from ``data``."""
    data = req.data or {}

    if req.type is ExportType.RESEARCH_REPORT:
        report_text = data.get("report_text")
        if not isinstance(report_text, str) or not report_text.strip():
            raise HTTPException(
                status_code=422,
                detail="research_report export requires data.report_text (non-empty string)",
            )
        citations = data.get("citations") or []
        citation_lines: List[str] = []
        if isinstance(citations, list):
            for c in citations[:50]:
                if isinstance(c, dict):
                    marker = export_service.sanitize_text(c.get("marker") or "", 20)
                    filename = export_service.sanitize_text(c.get("filename") or "", 120)
                    page = c.get("page_number")
                    snippet = export_service.sanitize_text(c.get("snippet") or "", 300)
                    label = f"{marker} {filename}".strip()
                    if isinstance(page, int):
                        label += f" (p. {page})"
                    if snippet:
                        label += f" — {snippet}"
                    if label:
                        citation_lines.append(label)
        body = export_service.sanitize_text(report_text)
        if citation_lines:
            body += "\n\n## Sources\n" + "\n".join(f"- {line}" for line in citation_lines)
        return {"report_text": body}

    if req.type is ExportType.CHAT:
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(
                status_code=422,
                detail="chat export requires data.messages (non-empty list of {role, content})",
            )
        if len(messages) > export_service.MAX_CHAT_MESSAGES:
            raise HTTPException(
                status_code=413,
                detail=f"Too many chat messages (max {export_service.MAX_CHAT_MESSAGES})",
            )
        cleaned: List[Dict[str, str]] = []
        for m in messages:
            if not isinstance(m, dict):
                raise HTTPException(status_code=422, detail="messages entries must be objects")
            role = export_service.sanitize_text(m.get("role", "user"), 20) or "user"
            content = export_service.sanitize_text(m.get("content", ""))
            created = export_service.sanitize_text(m.get("created_at") or "", 40)
            cleaned.append({"role": role, "content": content, "created_at": created})
        return {"chat_messages": cleaned}

    # ExportType.KNOWLEDGE_GRAPH
    graph = data.get("graph")
    if not isinstance(graph, dict) or not graph.get("nodes"):
        raise HTTPException(
            status_code=422,
            detail="knowledge_graph export requires data.graph with a non-empty nodes list",
        )
    return {
        "graph": {
            "nodes": [n if isinstance(n, dict) else {} for n in graph.get("nodes", [])],
            "links": [l if isinstance(l, dict) else {} for l in graph.get("links", [])],
        }
    }


@router.post("")
def export_report(
    req: ExportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> StreamingResponse:
    """Render an export document (PDF or DOCX) and stream it as a download.

    The document is generated in memory; nothing touches the filesystem.
    """
    content = _build_content(req)
    title = req.title or req.type.value.replace("_", " ").title()

    try:
        if req.format is ExportFormat.PDF:
            payload = export_service.generate_pdf(title, **content)
        else:
            payload = export_service.generate_docx(title, **content)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Export generation failed (type=%s format=%s)", req.type, req.format)
        raise HTTPException(status_code=500, detail="Failed to generate export document") from exc

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{export_service.sanitize_filename(title)}_{stamp}.{req.format.value}"

    logger.info(
        "Export generated: user=%s type=%s format=%s bytes=%d",
        current_user.id, req.type.value, req.format.value, len(payload),
    )

    import io as _io

    return StreamingResponse(
        _io.BytesIO(payload),
        media_type=_MEDIA_TYPES[req.format],
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
