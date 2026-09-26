"""
Settings routes for Phase 4 — persistent application settings.

GET   /api/settings/memory   — Read the current memory-enabled setting
PATCH /api/settings/memory   — Update the memory-enabled setting

The setting is persisted in SQLite and survives Docker restarts.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from typing import List, Optional

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
from app.run_engine.messages_stream import _SUPPORTED_DURABLE_MODES

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


class ChatModeCapability(BaseModel):
    """MP-3C3D3B — advisory, user-facing capability info for one canonical
    chat mode (``submission._ALLOWED_MODES``). Bounded to exactly these three
    public fields — never a raw setting name, env value, or internal slug
    (see ``messages_stream.unsupported_mode_reason`` for those). Existing
    D3A/D1 backend validation/routing remains the sole authority for what a
    request actually executes; this is UX-only advice for the selector."""

    mode: str
    available: bool
    reason: Optional[str] = None


def _compute_chat_mode_capabilities() -> List[ChatModeCapability]:
    """The ONE place that turns server config into user-facing mode
    availability. Reuses the exact same signals D3A's routes already gate
    on — ``run_messages_stream_durable_enabled`` +
    ``messages_stream._SUPPORTED_DURABLE_MODES`` (durable executability) and
    ``settings.enable_deep_research`` / ``enable_multi_agent`` (the legacy
    deployment-capability gate in ``routes/messages.py``) — so this can never
    advertise a mode the routing layer would then reject. "chat" is always
    available on either path; a mode merely being *recognized* by
    ``submission.validate_request_mode`` is not enough to advertise it."""
    durable = settings.run_messages_stream_durable_enabled

    if durable:
        # MP-3C3D4C — deep_research's durable availability is now
        # config-dependent (the end-to-end worker exists), computed via the
        # SAME authoritative predicate every other mode-aware gate reuses —
        # never a second, drifting readiness check, and never advertised
        # merely because run_deep_research_enabled is set without its full
        # dependency chain.
        from app.run_engine.deep_research_readiness import deep_research_executable
        from app.run_engine.multi_agent_readiness import multi_agent_executable

        dr_ready = deep_research_executable()
        # Phase 1B.2R MP-3C3D5C — same pattern as deep_research immediately
        # above: computed fresh via the ONE authoritative
        # multi_agent_readiness.multi_agent_executable() predicate, never
        # advertised merely because run_multi_agent_enabled is set without
        # its full dependency chain (including D5C's own implementation-
        # ready boundary).
        ma_ready = multi_agent_executable()
        return [
            ChatModeCapability(mode="chat", available=True),
            ChatModeCapability(
                mode="deep_research", available=dr_ready,
                reason=None if dr_ready else "Not yet available for durable chat.",
            ),
            ChatModeCapability(
                mode="multi_agent", available=ma_ready,
                reason=None if ma_ready else "Not yet available for durable chat.",
            ),
        ]
    return [
        ChatModeCapability(mode="chat", available=True),
        ChatModeCapability(
            mode="deep_research", available=settings.enable_deep_research,
            reason=None if settings.enable_deep_research else "Not enabled on this deployment.",
        ),
        ChatModeCapability(
            mode="multi_agent", available=settings.enable_multi_agent,
            reason=None if settings.enable_multi_agent else "Not enabled on this deployment.",
        ),
    ]


class SettingsResponse(BaseModel):
    llm_provider: str
    api_key: str
    model: str
    chat_modes: List[ChatModeCapability] = []


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
    chat_modes = _compute_chat_mode_capabilities()
    if row is not None:
        return SettingsResponse(
            llm_provider=row.llm_provider or settings.llm_provider,
            api_key=mask_api_key(decrypt_key(row.api_key) if row.api_key else ""),
            model=row.model or "",
            chat_modes=chat_modes,
        )
    return SettingsResponse(
        llm_provider=settings.llm_provider,
        api_key="",
        model="",
        chat_modes=chat_modes,
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
        chat_modes=_compute_chat_mode_capabilities(),
    )
