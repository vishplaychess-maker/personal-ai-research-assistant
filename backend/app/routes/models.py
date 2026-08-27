"""
Router for listing installed Ollama models.

GET /api/models — Returns models from Ollama's GET /api/tags.
"""

from typing import Any

import httpx
from fastapi import APIRouter

from app.config import settings
from app.schemas.sessions import ModelInfo, ModelListResponse

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("", response_model=ModelListResponse)
async def list_models():
    """
    Fetch the list of installed Ollama models from GET /api/tags.

    Returns model names, sizes, and last-modified timestamps.
    If Ollama is unreachable, returns an empty list with an error field.
    """
    tags_url = f"{settings.ollama_url}/api/tags"

    try:
        async with httpx.AsyncClient(timeout=settings.ollama_tags_timeout) as client:
            resp = await client.get(tags_url)
    except httpx.ConnectError:
        return ModelListResponse(
            models=[],
            error="Ollama is not running. Start it with `ollama serve` and try again.",
        )
    except httpx.TimeoutException:
        return ModelListResponse(
            models=[],
            error="Ollama did not respond in time. The service may be overloaded.",
        )

    if resp.status_code != 200:
        return ModelListResponse(
            models=[],
            error=f"Ollama returned HTTP {resp.status_code}.",
        )

    try:
        data: dict[str, Any] = resp.json()
        raw_models = data.get("models", [])
    except Exception:
        return ModelListResponse(
            models=[],
            error="Failed to parse Ollama response.",
        )

    models: list[ModelInfo] = []
    for m in raw_models:
        name = m.get("name", "unknown")
        if "embed" in name.lower():
            continue
        size_bytes = m.get("size", 0)
        size_str = _format_size(size_bytes) if size_bytes else None
        modified_at = m.get("modified_at", None)

        models.append(ModelInfo(name=name, size=size_str, modified_at=modified_at))

    return ModelListResponse(models=models, error=None)


def _format_size(size_bytes: int) -> str:
    """Format byte size into human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
