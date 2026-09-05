"""
Settings routes for Phase 4 — persistent application settings.

GET   /api/settings/memory   — Read the current memory-enabled setting
PATCH /api/settings/memory   — Update the memory-enabled setting

The setting is persisted in SQLite and survives Docker restarts.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.settings_service import (
    get_memory_enabled,
    set_memory_enabled,
    get_user_settings,
    save_user_settings,
)
from app.services.cookie_service import require_csrf
from app.services.auth_service import get_current_user
from app.services.encryption_service import decrypt_key
from app.services.encryption import mask_api_key
from app.models.models import User
from app.config import settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ── Schemas ────────────────────────────────────────────────


class MemorySettingResponse(BaseModel):
    enabled: bool


class MemorySettingUpdate(BaseModel):
    enabled: bool


# ── Endpoints ──────────────────────────────────────────────


@router.get("/memory", response_model=MemorySettingResponse)
def read_memory_setting(db: Session = Depends(get_db)):
    """
    Get the current memory-enabled setting.

    Returns the value from the database, falling back to the
    config default if no row exists yet.
    """
    return MemorySettingResponse(enabled=get_memory_enabled(db))


@router.patch("/memory", response_model=MemorySettingResponse)
def update_memory_setting(
    payload: MemorySettingUpdate,
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    """
    Update the memory-enabled setting and persist it to the database.

    Accepts and returns:
      { "enabled": true }
    """
    return MemorySettingResponse(enabled=set_memory_enabled(db, payload.enabled))


class SettingsResponse(BaseModel):
    llm_provider: str
    api_key: str
    model: str


class SettingsUpdate(BaseModel):
    llm_provider: str
    api_key: str = ""
    model: str = ""


def _validate_api_key_format(provider: str, api_key: str) -> None:
    """Reject obviously mismatched API keys (e.g. an NVIDIA key saved for
    OpenRouter). An empty key is allowed — it means "use the server default".

    Provider key prefixes:
      - openrouter  -> sk-or-
      - nvidia      -> nvapi-
      - huggingface -> hf_
    """
    if not api_key:
        return
    expected_prefix = {
        "openrouter": "sk-or-",
        "nvidia": "nvapi-",
        "huggingface": "hf_",
    }.get(provider)
    if expected_prefix and not api_key.startswith(expected_prefix):
        raise HTTPException(
            status_code=400,
            detail=(
                f"The API key does not look like a {provider} key "
                f"(expected prefix '{expected_prefix}'). Double-check the key "
                "and the selected provider."
            ),
        )


@router.get("", response_model=SettingsResponse)
def read_user_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the current user's LLM provider settings (defaulting to globals).

    The stored key is returned masked only — never plaintext or ciphertext.
    """
    row = get_user_settings(db, current_user.id)
    if row is not None:
        return SettingsResponse(
            llm_provider=row.llm_provider or settings.llm_provider,
            api_key=mask_api_key(decrypt_key(row.api_key) if row.api_key else ""),
            model=row.model or "",
        )
    return SettingsResponse(
        llm_provider=settings.llm_provider,
        api_key="",
        model="",
    )


@router.put("", response_model=SettingsResponse)
def update_user_settings(
    payload: SettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Update the current user's LLM provider, API key, and model.

    A masked key echoed back by the client (contains ``****``) leaves the
    stored key unchanged.
    """
    provider = (payload.llm_provider or "").strip().lower()
    if provider not in ("ollama", "openrouter", "nvidia", "huggingface", "google", "modelslab"):
        raise HTTPException(status_code=400, detail="Invalid llm_provider")
    api_key = (payload.api_key or "").strip()
    if "****" in api_key:
        api_key = None  # client echoed the masked value — keep stored key
    else:
        _validate_api_key_format(provider, api_key)
    try:
        row = save_user_settings(
            db,
            user_id=current_user.id,
            llm_provider=provider,
            api_key=api_key,
            model=(payload.model or "").strip(),
        )
    except RuntimeError:
        raise HTTPException(
            status_code=500,
            detail=(
                "API key encryption is unavailable: ENCRYPTION_KEY is not "
                "configured on the server. Generate one with "
                "`python scripts/generate_encryption_key.py` and set it in "
                "the backend environment."
            ),
        )
    return SettingsResponse(
        llm_provider=row.llm_provider,
        api_key=mask_api_key(decrypt_key(row.api_key) if row.api_key else ""),
        model=row.model or "",
    )
