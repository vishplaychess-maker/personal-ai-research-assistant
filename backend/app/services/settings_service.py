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
from app.models.models import AppSetting, UserSetting


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
    row = db.query(UserSetting).filter(UserSetting.user_id == user_id).first()
    if row is None:
        row = UserSetting(
            user_id=user_id,
            llm_provider=llm_provider,
            api_key=api_key,
            model=model,
        )
        db.add(row)
    else:
        row.llm_provider = llm_provider
        row.api_key = api_key
        row.model = model
    db.commit()
    db.refresh(row)
    return row


def get_user_llm_config(db: DBSession, user_id: int) -> Optional[dict]:
    """Return a provider config dict for the LLM factory, or None.

    Returns None when the user has not set a provider, so callers fall
    back to the global .env configuration and the cached singleton.
    """
    row = get_user_settings(db, user_id)
    if row is None or not row.llm_provider:
        return None
    return {
        "provider": row.llm_provider,
        "api_key": row.api_key or None,
        "model": row.model or None,
    }
