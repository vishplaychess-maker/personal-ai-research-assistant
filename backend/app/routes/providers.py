"""
Multiple Providers Manager routes.

GET    /api/providers          — List the current user's saved providers
POST   /api/providers          — Add a provider config
PUT    /api/providers/{id}     — Edit a provider config
DELETE /api/providers/{id}     — Delete a provider config
GET    /api/providers/models   — Models grouped by the user's providers
"""

from datetime import datetime
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User
from app.schemas.sessions import ModelInfo, is_free_model
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services.encryption import mask_api_key
from app.services.llm_providers import get_provider
from app.services.settings_service import (
    create_user_provider,
    decrypt_stored_provider_key,
    delete_user_provider,
    get_user_provider,
    list_user_providers,
    provider_config_for_row,
    update_user_provider,
)

router = APIRouter(prefix="/api/providers", tags=["providers"])

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = {
    "glm", "ollama", "openrouter", "nvidia", "huggingface", "google", "modelslab",
    "groq", "together", "mistral", "cohere",
}

PROVIDER_LABELS = {
    "glm": "GLM 5.3 Flash (Free)",
    "ollama": "Ollama",
    "openrouter": "OpenRouter",
    "nvidia": "NVIDIA",
    "huggingface": "Hugging Face",
    "google": "Google AI",
    "modelslab": "ModelsLab",
    "groq": "Groq",
    "together": "Together AI",
    "mistral": "Mistral",
    "cohere": "Cohere",
}

PREFIXES = {
    "glm": "",  # keyless local router
    "openrouter": "sk-or-",
    "nvidia": "nvapi-",
    "huggingface": "hf_",
    "google": "AIza",
    "groq": "gsk_",
    "together": "tgp-",
    "mistral": "",
    "cohere": "",
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
    api_key: str  # masked only, e.g. "sk-****1234" — never plaintext/ciphertext
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


def _is_masked_echo(api_key: Optional[str]) -> bool:
    """True when the client echoed a masked key back (nothing to update)."""
    return bool(api_key) and "****" in api_key


def _to_response(row) -> ProviderResponse:
    """Build an API response with a masked key (never plaintext/ciphertext)."""
    return ProviderResponse(
        id=row.id,
        provider_name=row.provider_name,
        api_key=mask_api_key(decrypt_stored_provider_key(row)),
        default_model=row.default_model,
        is_active=row.is_active,
        created_at=row.created_at,
    )


def _encryption_unavailable(exc: RuntimeError) -> HTTPException:
    logger.warning("Provider API-key encryption failed: %s", exc)
    return HTTPException(
        status_code=500,
        detail=(
            "API key encryption is unavailable: ENCRYPTION_KEY is not "
            "configured on the server. Generate one with "
            "`python scripts/generate_encryption_key.py` and set it in the "
            "backend environment."
        ),
    )


# ── Endpoints ────────────────────────────────


@router.get("", response_model=List[ProviderResponse])
def list_providers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all saved provider configs for the current user (active first).

    API keys are returned masked (e.g. ``sk-****1234``) — never plaintext
    or encrypted values.
    """
    return [_to_response(row) for row in list_user_providers(db, current_user.id)]


@router.post("", response_model=ProviderResponse, status_code=201)
def add_provider(
    payload: ProviderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Add a new provider config for the current user (key stored encrypted)."""
    provider_name = _validate_provider_name(payload.provider_name)
    _validate_api_key_format(provider_name, payload.api_key or "")
    try:
        row = create_user_provider(
            db,
            user_id=current_user.id,
            provider_name=provider_name,
            api_key=payload.api_key or "",
            default_model=payload.default_model or "",
            is_active=payload.is_active,
        )
    except RuntimeError as exc:
        raise _encryption_unavailable(exc)
    return _to_response(row)


@router.put("/{provider_id}", response_model=ProviderResponse)
def edit_provider(
    provider_id: int,
    payload: ProviderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Edit an existing provider config (owner-scoped).

    A masked key echoed back by the client (contains ``****``) is treated
    as "leave the stored key unchanged"; a real key is re-encrypted.
    """
    if payload.provider_name is not None:
        payload.provider_name = _validate_provider_name(payload.provider_name)
    api_key: Optional[str] = payload.api_key
    if _is_masked_echo(api_key):
        api_key = None  # client echoed the masked value — keep stored key
    elif api_key is not None:
        provider_name = payload.provider_name or (
            get_user_provider(db, current_user.id, provider_id).provider_name
            if get_user_provider(db, current_user.id, provider_id)
            else ""
        )
        _validate_api_key_format(provider_name, api_key)
    try:
        row = update_user_provider(
            db,
            user_id=current_user.id,
            provider_id=provider_id,
            provider_name=payload.provider_name,
            api_key=api_key,
            default_model=payload.default_model,
            is_active=payload.is_active,
        )
    except RuntimeError as exc:
        raise _encryption_unavailable(exc)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider config not found")
    return _to_response(row)


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
            prov = get_provider(config=provider_config_for_row(row))
            names = prov.fetch_available_chat_models() or []
        except Exception:
            names = []
        groups.append(
            ProviderModelGroup(
                provider=row.provider_name,
                provider_label=PROVIDER_LABELS.get(row.provider_name, row.provider_name),
                provider_id=row.id,
                models=[ModelInfo(name=n, is_free=is_free_model(n, row.provider_name)) for n in names],
            )
        )
    return groups
