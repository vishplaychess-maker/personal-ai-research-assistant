"""
Multiple Providers Manager routes.

GET    /api/providers          — List the current user's saved providers
POST   /api/providers          — Add a provider config
PUT    /api/providers/{id}     — Edit a provider config
DELETE /api/providers/{id}     — Delete a provider config
GET    /api/providers/models   — Models grouped by the user's providers
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User
from app.schemas.sessions import ModelInfo
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services.llm_providers import get_provider
from app.services.settings_service import (
    create_user_provider,
    delete_user_provider,
    get_user_provider,
    list_user_providers,
    update_user_provider,
)

router = APIRouter(prefix="/api/providers", tags=["providers"])

KNOWN_PROVIDERS = {"ollama", "openrouter", "nvidia", "huggingface", "google", "modelslab"}

PROVIDER_LABELS = {
    "ollama": "Ollama",
    "openrouter": "OpenRouter",
    "nvidia": "NVIDIA",
    "huggingface": "Hugging Face",
    "google": "Google AI",
    "modelslab": "ModelsLab",
}

PREFIXES = {
    "openrouter": "sk-or-",
    "nvidia": "nvapi-",
    "huggingface": "hf_",
    "google": "AIza",
}


# ── Schemas ──────────────────────────────────


class ProviderCreate(BaseModel):
    provider_name: str
    api_key: str = ""
    default_model: str = ""
    is_active: bool = False


class ProviderUpdate(BaseModel):
    provider_name: Optional[str] = None
    api_key: Optional[str] = None
    default_model: Optional[str] = None
    is_active: Optional[bool] = None


class ProviderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_name: str
    api_key: str
    default_model: str
    is_active: bool
    created_at: datetime


class ProviderModelGroup(BaseModel):
    provider: str
    provider_label: str
    provider_id: int
    models: List[ModelInfo]


# ── Helpers ──────────────────────────────────


def _validate_provider_name(provider_name: str) -> str:
    name = (provider_name or "").strip().lower()
    if name not in KNOWN_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown provider '{provider_name}'. "
                f"Valid: {', '.join(sorted(KNOWN_PROVIDERS))}."
            ),
        )
    return name


def _validate_api_key_format(provider: str, api_key: str) -> None:
    if not api_key:
        return
    prefix = PREFIXES.get(provider)
    if prefix and not api_key.startswith(prefix):
        raise HTTPException(
            status_code=400,
            detail=(
                f"The API key does not look like a {PROVIDER_LABELS.get(provider, provider)} "
                f"key (expected prefix '{prefix}'). Double-check the key and provider."
            ),
        )


# ── Endpoints ────────────────────────────────


@router.get("", response_model=List[ProviderResponse])
def list_providers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all saved provider configs for the current user (active first)."""
    return list_user_providers(db, current_user.id)


@router.post("", response_model=ProviderResponse, status_code=201)
def add_provider(
    payload: ProviderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Add a new provider config for the current user."""
    provider_name = _validate_provider_name(payload.provider_name)
    _validate_api_key_format(provider_name, payload.api_key or "")
    return create_user_provider(
        db,
        user_id=current_user.id,
        provider_name=provider_name,
        api_key=payload.api_key or "",
        default_model=payload.default_model or "",
        is_active=payload.is_active,
    )


@router.put("/{provider_id}", response_model=ProviderResponse)
def edit_provider(
    provider_id: int,
    payload: ProviderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Edit an existing provider config (owner-scoped)."""
    if payload.provider_name is not None:
        payload.provider_name = _validate_provider_name(payload.provider_name)
    if payload.api_key is not None:
        provider_name = payload.provider_name or (
            get_user_provider(db, current_user.id, provider_id).provider_name
            if get_user_provider(db, current_user.id, provider_id)
            else ""
        )
        _validate_api_key_format(provider_name, payload.api_key)
    row = update_user_provider(
        db,
        user_id=current_user.id,
        provider_id=provider_id,
        provider_name=payload.provider_name,
        api_key=payload.api_key,
        default_model=payload.default_model,
        is_active=payload.is_active,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Provider config not found")
    return row


@router.delete("/{provider_id}", status_code=204)
def remove_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Delete a provider config (owner-scoped)."""
    if not delete_user_provider(db, current_user.id, provider_id):
        raise HTTPException(status_code=404, detail="Provider config not found")
    return None


@router.get("/models", response_model=List[ProviderModelGroup])
def provider_models(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch models for ALL of the user's saved providers, grouped by provider."""
    rows = list_user_providers(db, current_user.id)
    groups: List[ProviderModelGroup] = []
    for row in rows:
        try:
            prov = get_provider(
                config={
                    "provider": row.provider_name,
                    "api_key": row.api_key or None,
                    "model": row.default_model or None,
                }
            )
            names = prov.fetch_available_chat_models() or []
        except Exception:
            names = []
        groups.append(
            ProviderModelGroup(
                provider=row.provider_name,
                provider_label=PROVIDER_LABELS.get(row.provider_name, row.provider_name),
                provider_id=row.id,
                models=[ModelInfo(name=n) for n in names],
            )
        )
    return groups
