"""Remote access configuration defaults, validation, and cookie matrix."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_remote_access_defaults_are_local_only():
    settings = Settings(_env_file=None)
    assert settings.remote_access_enabled is False
    assert settings.bind_host == "127.0.0.1"
    assert settings.remote_https is False


def test_non_loopback_bind_requires_remote_opt_in():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, remote_access_enabled=False, bind_host="0.0.0.0")


def test_remote_https_requires_remote_opt_in():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, remote_access_enabled=False, remote_https=True)


def test_remote_opt_in_accepts_wildcard_bind():
    settings = Settings(_env_file=None, remote_access_enabled=True, bind_host="0.0.0.0")
    assert settings.bind_host == "0.0.0.0"


def test_cookie_matrix_remote_https_forces_secure_none():
    settings = Settings(
        _env_file=None,
        remote_access_enabled=True,
        remote_https=True,
        cookie_secure=False,
        cookie_samesite="lax",
    )
    assert settings.remote_cookie_flags == (True, "none")


@pytest.mark.parametrize("remote_https", [False])
def test_cookie_matrix_remote_plain_http_keeps_current(remote_https):
    settings = Settings(
        _env_file=None,
        remote_access_enabled=True,
        remote_https=remote_https,
        cookie_secure=False,
        cookie_samesite="lax",
    )
    assert settings.remote_cookie_flags == (False, "lax")


def test_cookie_matrix_local_keeps_current_values():
    settings = Settings(
        _env_file=None,
        remote_access_enabled=False,
        cookie_secure=True,
        cookie_samesite="strict",
    )
    assert settings.remote_cookie_flags == (True, "strict")
