"""Grok configuration and provider-resolution integration.

Covers the two places a wrong default would silently break the feature: the
settings surface (env parsing, fail-closed validation) and the point where the
app decides "this user is signed in with a subscription, not an API key".
"""

import datetime
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.database import SessionLocal
from app.models.models import ProviderOAuthToken, User, UserProvider
from app.routes import providers as providers_route
from app.services.llm_providers import _build_provider, get_provider
from app.services.llm_providers.grok_provider import GrokProvider
from app.services.oauth_grok import DEFAULT_CLIENT_ID
from app.services.settings_service import provider_config_for_row

DEVICE_URL = "https://auth.x.ai/oauth2/device/code"
TOKEN_URL = "https://auth.x.ai/oauth2/token"

# The values xAI's own OIDC discovery document publishes. If these ever change,
# the endpoints moved and the code must follow - not the other way round.
OIDC_ENDPOINTS = {
    "grok_oauth_device_auth_url": DEVICE_URL,
    "grok_oauth_token_url": TOKEN_URL,
    "grok_oauth_userinfo_url": "https://auth.x.ai/oauth2/userinfo",
    "grok_oauth_revocation_url": "https://auth.x.ai/oauth2/revoke",
}


# ── settings surface ────────────────────────────────────────


def test_grok_sign_in_is_off_by_default():
    settings = Settings(_env_file=None)

    assert settings.grok_oauth_enabled is False
    assert settings.grok_oauth_client_id == ""
    assert settings.grok_api_key == ""


def test_endpoint_defaults_match_the_published_oidc_document():
    settings = Settings(_env_file=None)

    for field, expected in OIDC_ENDPOINTS.items():
        assert getattr(settings, field) == expected
    assert settings.grok_oauth_subscription_base_url == "https://cli-chat-proxy.grok.com/v1"


def test_default_scope_grant_is_the_grok_cli_one():
    """`grok.access` (which third-party write-ups claim) is not a real scope."""
    settings = Settings(_env_file=None)

    assert settings.grok_oauth_scopes == [
        "openid",
        "profile",
        "email",
        "offline_access",
        "grok-cli:access",
        "api:access",
    ]
    assert "grok.access" not in settings.grok_oauth_scopes


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("openid profile", ["openid", "profile"]),
        ("openid,profile", ["openid", "profile"]),
        ('["openid", "profile"]', ["openid", "profile"]),
        ("", ["openid", "profile", "email", "offline_access", "grok-cli:access", "api:access"]),
    ],
)
def test_scopes_env_accepts_the_forms_people_actually_type(monkeypatch, raw, expected):
    monkeypatch.setenv("GROK_OAUTH_SCOPES", raw)

    assert Settings(_env_file=None).grok_oauth_scopes == expected


def test_a_blank_enabled_flag_still_fails_loudly(monkeypatch):
    """A pydantic bool cannot parse "": the operator must see that, not silence."""
    monkeypatch.setenv("GROK_OAUTH_ENABLED", "")

    with pytest.raises(Exception):
        Settings(_env_file=None)


def test_enabling_sign_in_over_plaintext_http_is_refused():
    with pytest.raises(Exception, match="https"):
        Settings(_env_file=None, grok_oauth_enabled=True, grok_oauth_token_url="http://auth.x.ai/oauth2/token")


def test_a_sub_second_poll_interval_is_rejected():
    with pytest.raises(Exception, match="at least 1"):
        Settings(_env_file=None, grok_oauth_poll_interval_s=0.2)


def test_client_id_is_public_and_overridable():
    from app.services.oauth_grok import GrokOAuthService

    assert GrokOAuthService(device_auth_url=DEVICE_URL, token_url=TOKEN_URL)._client_id == DEFAULT_CLIENT_ID
    overridden = Settings(_env_file=None, grok_oauth_client_id="rotated-id")
    assert overridden.grok_oauth_client_id == "rotated-id"


def test_no_client_secret_setting_exists():
    """xAI's device client is public; a secret field would invite a fake one."""
    assert not [name for name in Settings.model_fields if "grok" in name and "secret" in name]


# ── provider resolution ─────────────────────────────────────


@pytest.fixture
def encryption_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture
def grok_user(encryption_key):
    """A user with an active Grok provider row (API key saved) + cleanup."""
    db = SessionLocal()
    name = f"grokresolve_{uuid.uuid4().hex[:10]}"
    user = User(username=name, email=f"{name}@test.com")
    db.add(user)
    db.commit()
    db.refresh(user)
    row = UserProvider(
        user_id=user.id,
        provider_name="grok",
        encrypted_api_key="",
        api_key="",
        default_model="grok-4",
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    from app.services.encryption_service import encrypt_key

    row.encrypted_api_key = encrypt_key("xai-SECRET-key")
    db.commit()
    db.refresh(row)
    provider_row_id, user_id = row.id, user.id
    db.close()
    try:
        yield user_id, provider_row_id
    finally:
        db = SessionLocal()
        db.query(ProviderOAuthToken).filter(ProviderOAuthToken.user_id == user_id).delete()
        db.query(UserProvider).filter(UserProvider.id == provider_row_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()
        db.close()


class _row_with_session:
    """Loads the provider row and keeps its session open.

    ``provider_config_for_row`` finds the OAuth row through the ORM session the
    provider row is attached to, exactly as a request does; a detached instance
    would take the API-key path for the wrong reason.
    """

    def __init__(self, row_id: int):
        self._row_id = row_id

    def __enter__(self) -> UserProvider:
        self.db = SessionLocal()
        return self.db.query(UserProvider).filter(UserProvider.id == self._row_id).one()

    def __exit__(self, *exc_info):
        self.db.close()
        return False


def _mark_connected(user_id: int, status: str = "connected") -> None:
    db = SessionLocal()
    try:
        from app.services.oauth_grok import DeviceCode
        from app.services.oauth_token_store import OAuthTokenStore

        store = OAuthTokenStore(session_factory=lambda: db)
        store.save_pending_login(
            db,
            user_id,
            DeviceCode(
                device_code="dc-x",
                user_code="ABCD-1234",
                verification_uri="https://accounts.x.ai/oauth2/device",
                verification_uri_complete=None,
                expires_in=900,
                interval=5,
            ),
        )
        from app.services.oauth_grok import TokenSet

        store.save_tokens(db, user_id, TokenSet(access_token="at", refresh_token="rt", expires_in=3600))
        if status != "connected":
            row = (
                db.query(ProviderOAuthToken)
                .filter(ProviderOAuthToken.user_id == user_id)
                .one()
            )
            row.status = status
            db.commit()
    finally:
        db.close()


def test_a_connected_sign_in_replaces_the_api_key_config(grok_user, monkeypatch):
    user_id, row_id = grok_user
    _mark_connected(user_id)
    monkeypatch.setattr("app.config.settings.grok_oauth_enabled", True)

    with _row_with_session(row_id) as row:
        config = provider_config_for_row(row)

    assert config["provider"] == "grok"
    assert config["auth_mode"] == "oauth"
    assert config["user_id"] == user_id
    # The stored API key is withheld, so the provider cannot fall back to it.
    assert config["api_key"] is None


def test_a_pending_sign_in_keeps_the_api_key_path(grok_user, monkeypatch):
    user_id, row_id = grok_user
    _mark_connected(user_id, status="pending")
    monkeypatch.setattr("app.config.settings.grok_oauth_enabled", True)

    with _row_with_session(row_id) as row:
        config = provider_config_for_row(row)

    assert "auth_mode" not in config
    assert config["api_key"] == "xai-SECRET-key"


def test_the_stored_key_is_decrypted_when_the_feature_is_off(grok_user, monkeypatch):
    user_id, row_id = grok_user
    _mark_connected(user_id)
    monkeypatch.setattr("app.config.settings.grok_oauth_enabled", False)

    with _row_with_session(row_id) as row:
        config = provider_config_for_row(row)

    assert config["api_key"] == "xai-SECRET-key"
    assert "user_id" not in config


def test_a_broken_credential_lookup_cannot_break_the_provider_list(
    grok_user, monkeypatch
):
    user_id, row_id = grok_user
    monkeypatch.setattr("app.config.settings.grok_oauth_enabled", True)

    def explode(*args, **kwargs):
        raise RuntimeError("database is locked")

    # settings_service reaches the store lazily, so patch the class itself.
    monkeypatch.setattr(
        "app.services.oauth_token_store.OAuthTokenStore.account", explode
    )

    with _row_with_session(row_id) as row:
        config = provider_config_for_row(row)

    assert config["api_key"] == "xai-SECRET-key"


# ── registry ────────────────────────────────────────────────


def test_grok_is_a_known_provider_everywhere_it_matters():
    assert "grok" in providers_route.KNOWN_PROVIDERS
    assert providers_route.PROVIDER_LABELS["grok"] == "Grok (xAI)"
    # No prefix gate: a Grok row may legitimately hold no key at all.
    assert providers_route.PREFIXES["grok"] == ""


def test_the_factory_builds_a_grok_provider_instead_of_falling_back():
    assert isinstance(_build_provider("grok", api_key="xai-k"), GrokProvider)
    assert _build_provider("groq") is not None


def test_a_user_id_selects_subscription_mode():
    oauth = get_provider(config={"provider": "grok", "user_id": 7, "model": "grok-4"})
    keyed = get_provider(config={"provider": "grok", "api_key": "xai-k", "model": "grok-4"})

    assert oauth.uses_oauth is True
    assert keyed.uses_oauth is False
    # Subscription traffic must go to the chat proxy, not the metered API.
    assert oauth._base_url() == "https://cli-chat-proxy.grok.com/v1"
    assert keyed._base_url() == "https://api.x.ai/v1"


def test_the_fallback_chain_never_silently_replaces_a_dead_sign_in():
    """Grok is not in the free-tier chain: a failed subscription is not masked."""
    from app.services.llm_providers import _FALLBACK_FREE_PROVIDERS, build_fallback_chain

    assert "grok" not in _FALLBACK_FREE_PROVIDERS
    chain = build_fallback_chain({"provider": "grok", "user_id": 7})
    assert chain[0]["provider"] == "grok"


def test_oauth_row_timestamps_stay_naive_utc():
    """The provider tables compare against datetime.utcnow(); mixing offsets breaks it."""
    row = ProviderOAuthToken(
        user_id=1, provider_name="grok", status="pending",
        access_expires_at=datetime.datetime.utcnow(),
    )
    assert row.access_expires_at.tzinfo is None
