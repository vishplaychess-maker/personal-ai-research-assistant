"""First-run auto-generation of the API-key encryption key.

The packaged launcher persists ENCRYPTION_KEY to <data>/encryption.key; the
encryption module ALSO falls back to the same portable file so a source
checkout (uvicorn, tests) can save providers without any env setup.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.services import encryption, secret_store


@pytest.fixture
def no_key(monkeypatch, tmp_path):
    monkeypatch.setenv("THUNDER_AI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(settings, "encryption_key", "")
    return tmp_path


def test_auto_creates_key_and_round_trips(no_key):
    token = encryption.encrypt_api_key("plain-provider-key")
    assert token.startswith(encryption.FERNET_PREFIX)
    assert encryption.decrypt_api_key(token) == "plain-provider-key"
    key_file = no_key / encryption.ENCRYPTION_KEY_FILE
    assert key_file.is_file()
    # the persisted value is a usable Fernet key
    Fernet(key_file.read_text(encoding="utf-8").strip().encode("ascii"))


def test_key_is_persisted_and_reused(no_key):
    first = secret_store.load_or_create_secret(encryption.ENCRYPTION_KEY_FILE)
    second = secret_store.load_or_create_secret(encryption.ENCRYPTION_KEY_FILE)
    assert first and first == second
    token = encryption.encrypt_api_key("x")
    assert encryption.decrypt_api_key(token) == "x"


def test_env_key_wins_and_no_file_is_created(no_key, monkeypatch):
    env_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("ENCRYPTION_KEY", env_key)
    token = encryption.encrypt_api_key("env-secret")
    assert encryption.decrypt_api_key(token) == "env-secret"
    assert not (no_key / encryption.ENCRYPTION_KEY_FILE).exists()
    assert Fernet(env_key.encode("ascii")).decrypt(token.encode("utf-8")).decode() == "env-secret"


def test_settings_key_wins_over_fallback(no_key, monkeypatch):
    settings_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(settings, "encryption_key", settings_key)
    token = encryption.encrypt_api_key("settings-secret")
    assert Fernet(settings_key.encode("ascii")).decrypt(token.encode("utf-8")).decode() == "settings-secret"
    assert not (no_key / encryption.ENCRYPTION_KEY_FILE).exists()


def test_generated_key_is_never_logged(no_key, caplog):
    with caplog.at_level("DEBUG"):
        encryption.encrypt_api_key("whatever")
    generated = (no_key / encryption.ENCRYPTION_KEY_FILE).read_text(encoding="utf-8").strip()
    assert generated
    assert generated not in caplog.text


def test_fail_closed_when_location_is_unusable(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("THUNDER_AI_DATA_DIR", str(blocker))
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(settings, "encryption_key", "")
    with pytest.raises(RuntimeError):
        encryption.encrypt_api_key("x")


def test_generated_key_is_a_valid_fernet_key():
    Fernet(secret_store.generate_fernet_key().encode("ascii"))
