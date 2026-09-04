"""
Settings service — read/write persistent application settings from SQLite.

The primary setting for Phase 4 is `memory_enabled`, which controls whether
long-term memory extraction and retrieval are active. The setting is stored
in the `app_settings` table and survives Docker restarts.

Falling back to `settings.enable_memory` from config ensures the default
is always available even before any row is written to the database.
"""

from typing import Optional

from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.models.models import AppSetting, UserSetting, UserProvider
from app.services.encryption_service import encrypt_key, decrypt_key


MEMORY_ENABLED_KEY = "memory_enabled"


def get_memory_enabled(db: DBSession) -> bool:
    """
    Read the memory-enabled setting from the database.

    If no row exists yet, fall back to the config default
    (which comes from the .env file or defaults to True).
    """
    row: Optional[AppSetting] = (
        db.query(AppSetting)
        .filter(AppSetting.key == MEMORY_ENABLED_KEY)
        .first()
    )
    if row is None:
        return settings.enable_memory
    return row.value.lower() == "true"


def set_memory_enabled(db: DBSession, enabled: bool) -> bool:
    """
    Persist the memory-enabled setting to the database.

    Creates a new row if one does not exist; updates otherwise.

    Returns the value that was saved.
    """
    row: Optional[AppSetting] = (
        db.query(AppSetting)
        .filter(AppSetting.key == MEMORY_ENABLED_KEY)
        .first()
    )
    value_str = str(enabled).lower()
    if row is None:
        row = AppSetting(key=MEMORY_ENABLED_KEY, value=value_str)
        db.add(row)
    else:
        row.value = value_str
    db.commit()
    return enabled


def get_user_settings(db: DBSession, user_id: int) -> Optional[UserSetting]:
    """Return the user's LLM provider settings row, or None if unset."""
    return db.query(UserSetting).filter(UserSetting.user_id == user_id).first()


def save_user_settings(
    db: DBSession,
    user_id: int,
    llm_provider: str,
    api_key: str,
    model: str,
) -> UserSetting:
    """Upsert the user's LLM provider settings and return the saved row."""
    stored_key = encrypt_key(api_key) if api_key else ""
    row = db.query(UserSetting).filter(UserSetting.user_id == user_id).first()
    if row is None:
        row = UserSetting(
            user_id=user_id,
            llm_provider=llm_provider,
            api_key=stored_key,
            model=model,
        )
        db.add(row)
    else:
        row.llm_provider = llm_provider
        row.api_key = stored_key
        row.model = model
    db.commit()
    db.refresh(row)
    return row


# ── Multiple providers manager ─────────────────────────


def list_user_providers(db: DBSession, user_id: int) -> list:
    """Return all saved provider configs for a user (active first)."""
    rows = (
        db.query(UserProvider)
        .filter(UserProvider.user_id == user_id)
        .order_by(UserProvider.is_active.desc(), UserProvider.created_at.asc())
        .all()
    )
    return rows


def get_user_provider(db: DBSession, user_id: int, provider_id: int) -> Optional[UserProvider]:
    """Return a single provider config owned by the user, or None."""
    return (
        db.query(UserProvider)
        .filter(UserProvider.id == provider_id, UserProvider.user_id == user_id)
        .first()
    )


def _deactivate_other_providers(db: DBSession, user_id: int, except_id: Optional[int] = None) -> None:
    """Ensure only one provider is active at a time."""
    q = db.query(UserProvider).filter(
        UserProvider.user_id == user_id,
        UserProvider.is_active.is_(True),
    )
    if except_id is not None:
        q = q.filter(UserProvider.id != except_id)
    for row in q.all():
        row.is_active = False
    if q.first() is not None:
        db.commit()


def create_user_provider(
    db: DBSession,
    user_id: int,
    provider_name: str,
    api_key: str = "",
    default_model: str = "",
    is_active: bool = False,
) -> UserProvider:
    """Create a provider config for the user. Only one can be active."""
    if is_active:
        _deactivate_other_providers(db, user_id)
    row = UserProvider(
        user_id=user_id,
        provider_name=provider_name.strip().lower(),
        api_key=encrypt_key(api_key) if api_key else "",
        default_model=(default_model or "").strip(),
        is_active=bool(is_active),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_user_provider(
    db: DBSession,
    user_id: int,
    provider_id: int,
    provider_name: Optional[str] = None,
    api_key: Optional[str] = None,
    default_model: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Optional[UserProvider]:
    """Update a provider config owned by the user. Returns None if not found."""
    row = get_user_provider(db, user_id, provider_id)
    if row is None:
        return None
    if provider_name is not None:
        row.provider_name = provider_name.strip().lower()
    if api_key is not None:
        row.api_key = encrypt_key(api_key) if api_key else ""
    if default_model is not None:
        row.default_model = default_model.strip()
    if is_active is not None:
        if is_active:
            _deactivate_other_providers(db, user_id, except_id=provider_id)
        row.is_active = bool(is_active)
    db.commit()
    db.refresh(row)
    return row


def delete_user_provider(db: DBSession, user_id: int, provider_id: int) -> bool:
    """Delete a provider config owned by the user. Returns True if deleted."""
    row = get_user_provider(db, user_id, provider_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def get_active_provider_config(db: DBSession, user_id: int) -> Optional[dict]:
    """Return the config dict of the user's active provider, or None."""
    row = (
        db.query(UserProvider)
        .filter(UserProvider.user_id == user_id, UserProvider.is_active.is_(True))
        .first()
    )
    if row is None or not row.provider_name:
        return None
    return {
        "provider": row.provider_name,
        "api_key": (decrypt_key(row.api_key) if row.api_key else None),
        "model": row.default_model or None,
    }


def get_user_llm_config(db: DBSession, user_id: int) -> Optional[dict]:
    """Return a provider config dict for the LLM factory, or None.

    Priority: active UserProvider row -> legacy user_settings row -> None.
    Returns None when nothing is set, so callers fall back to the global
    .env configuration and the cached singleton.
    """
    active = get_active_provider_config(db, user_id)
    if active is not None:
        return active
    row = get_user_settings(db, user_id)
    if row is None or not row.llm_provider:
        return None
    return {
        "provider": row.llm_provider,
        "api_key": (decrypt_key(row.api_key) if row.api_key else None),
        "model": row.model or None,
    }
