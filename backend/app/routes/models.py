"""
Router for listing available models.

GET /api/models — Returns models from the configured LLM provider.
"""

from typing import Any

import httpx
from fastapi import APIRouter

from app.config import settings
from app.schemas.sessions import ModelInfo, ModelListResponse
from app.services.llm_providers import get_provider

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("", response_model=ModelListResponse)
async def list_models():
    """
    Fetch the list of available chat models from the configured LLM provider.

    For Ollama: fetches from GET /api/tags (local installed models).
    For OpenRouter/NVIDIA: returns curated lists of known available models.

    Returns model names, sizes, and last-modified timestamps where available.
    If the provider is unreachable, returns an empty list with an error field.
    """
    provider = get_provider()
    provider_name = provider.name.lower()

    # For Ollama, use the existing direct API call for richer metadata
    if "ollama" in provider_name:
        return await _list_ollama_models()

    # For cloud providers, use the provider's model list
    try:
        model_names = provider.fetch_available_chat_models()
    except Exception:
        return ModelListResponse(
            models=[],
            error=f"{provider.name} is not reachable.",
        )

    if model_names is None:
        return ModelListResponse(
            models=[],
            error=f"{provider.name} is not configured or unreachable. "
                  "Check your API key and network connection.",
        )

    models = [ModelInfo(name=name) for name in model_names]
    return ModelListResponse(models=models, error=None)


async def _list_ollama_models() -> ModelListResponse:
    """Fetch models from Ollama's GET /api/tags endpoint."""
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
