"""Launcher helpers (Phase 1 - .exe packaging entry point)."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

import launcher


def test_find_free_port_returns_bindable_port():
    port = launcher.find_free_port()
    assert isinstance(port, int)
    assert launcher.DEFAULT_PORT <= port < launcher.DEFAULT_PORT + launcher.PORT_SCAN_LIMIT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_find_free_port_skips_busy_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        busy_port = busy.getsockname()[1]
        port = launcher.find_free_port("127.0.0.1", busy_port, 10)
        assert port > busy_port


def test_find_free_port_exhaustion_is_bounded():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        busy_port = busy.getsockname()[1]
        with pytest.raises(RuntimeError):
            launcher.find_free_port("127.0.0.1", busy_port, 1)


def test_apply_local_env_defaults_sets_and_preserves(monkeypatch):
    monkeypatch.setattr(os, "environ", {"PRODUCTION_MODE": "true"})
    launcher.apply_local_env_defaults(9123)
    assert os.environ["PRODUCTION_MODE"] == "true"
    assert os.environ["COOKIE_SECURE"] == "false"
    assert os.environ["COOKIE_SAMESITE"] == "lax"
    assert os.environ["FRONTEND_ORIGIN"] == "http://localhost:9123"


def test_cookie_flags_remote_https(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    launcher.apply_local_env_defaults(8080, remote_access_enabled=True, remote_https=True)
    assert os.environ["COOKIE_SECURE"] == "true"
    assert os.environ["COOKIE_SAMESITE"] == "none"


def test_cookie_flags_remote_plain_http_keeps_local_defaults(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    launcher.apply_local_env_defaults(8080, remote_access_enabled=True, remote_https=False)
    assert os.environ["COOKIE_SECURE"] == "false"
    assert os.environ["COOKIE_SAMESITE"] == "lax"


def test_cookie_flags_local_defaults(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    launcher.apply_local_env_defaults(8080)
    assert os.environ["COOKIE_SECURE"] == "false"
    assert os.environ["COOKIE_SAMESITE"] == "lax"


def test_cookie_flags_operator_env_wins(monkeypatch):
    monkeypatch.setattr(
        os, "environ",
        {"COOKIE_SECURE": "true", "COOKIE_SAMESITE": "none"},
    )
    launcher.apply_local_env_defaults(8080, remote_access_enabled=True, remote_https=True)
    assert os.environ["COOKIE_SECURE"] == "true"
    assert os.environ["COOKIE_SAMESITE"] == "none"


@pytest.mark.parametrize(
    "cli_host,remote,expected",
    [
        (None, False, "127.0.0.1"),
        (None, True, "0.0.0.0"),
        ("127.0.0.1", True, "127.0.0.1"),
        ("192.168.1.20", True, "192.168.1.20"),
    ],
)
def test_resolve_bind_host(cli_host, remote, expected):
    assert launcher.resolve_bind_host(cli_host, remote) == expected


def test_remote_notice_is_ascii_and_lists_lan_urls():
    lines = launcher.format_remote_access_notice(8080, ["192.168.1.20"])
    text = "\n".join(lines)
    assert all(ord(char) < 128 for char in text)
    assert "http://192.168.1.20:8080" in text
    assert "login" in text.lower()


def test_lan_addresses_never_raise():
    assert isinstance(launcher._lan_ipv4_addresses(), list)


def test_ensure_backend_on_path():
    launcher._ensure_backend_on_path()
    assert str(Path(launcher.__file__).resolve().parent) in sys.path


def test_build_parser_defaults():
    args = launcher.build_parser().parse_args([])
    assert args.host is None
    assert args.port == 8080
    assert args.no_browser is False
    assert args.remote_access is False
    assert args.remote_https is False


def test_ensure_jwt_secret_creates_and_reuses(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("THUNDER_AI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with caplog.at_level("INFO"):
        launcher.ensure_jwt_secret()
    first = os.environ.get("JWT_SECRET")
    assert first
    assert first not in caplog.text
    secret_file = tmp_path / launcher.JWT_SECRET_FILE
    assert secret_file.is_file()
    assert secret_file.read_text(encoding="utf-8").strip() == first

    monkeypatch.delenv("JWT_SECRET", raising=False)
    launcher.ensure_jwt_secret()
    assert os.environ["JWT_SECRET"] == first


def test_ensure_jwt_secret_respects_operator_value(monkeypatch, tmp_path):
    monkeypatch.setenv("THUNDER_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "operator-secret")
    launcher.ensure_jwt_secret()
    assert os.environ["JWT_SECRET"] == "operator-secret"
    assert not (tmp_path / launcher.JWT_SECRET_FILE).exists()


def test_ensure_jwt_secret_fail_closed(monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("THUNDER_AI_DATA_DIR", str(blocker))
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with caplog.at_level("WARNING"):
        launcher.ensure_jwt_secret()  # must not raise
    assert "JWT_SECRET" in caplog.text or "JWT secret" in caplog.text


def test_ensure_encryption_key_creates_and_reuses(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("THUNDER_AI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with caplog.at_level("INFO"):
        launcher.ensure_encryption_key()
    first = os.environ.get("ENCRYPTION_KEY")
    assert first
    assert first not in caplog.text
    key_file = tmp_path / launcher.ENCRYPTION_KEY_FILE
    assert key_file.is_file()
    assert key_file.read_text(encoding="utf-8").strip() == first

    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    launcher.ensure_encryption_key()
    assert os.environ["ENCRYPTION_KEY"] == first


def test_ensure_encryption_key_respects_operator_value(monkeypatch, tmp_path):
    monkeypatch.setenv("THUNDER_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENCRYPTION_KEY", "operator-encryption-key")
    launcher.ensure_encryption_key()
    assert os.environ["ENCRYPTION_KEY"] == "operator-encryption-key"
    assert not (tmp_path / launcher.ENCRYPTION_KEY_FILE).exists()


def test_ensure_encryption_key_fail_closed(monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("THUNDER_AI_DATA_DIR", str(blocker))
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with caplog.at_level("WARNING"):
        launcher.ensure_encryption_key()  # must not raise
    assert "ENCRYPTION_KEY" in caplog.text
