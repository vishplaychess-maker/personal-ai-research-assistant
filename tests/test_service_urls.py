"""Service base URL building.

OLLAMA_HOST / CHROMADB_HOST are often exported as "host:port" (the machine
that reproduced the chat failure had OLLAMA_HOST=0.0.0.0:11434). Appending the
configured port again produced "http://0.0.0.0:11434:11434", which httpx
rejects with InvalidURL; and a wildcard bind address must be mapped to loopback
because these URLs are client targets.
"""

from __future__ import annotations

import app.config as config


def test_host_without_port_appends_configured_port():
    assert config._service_base_url("localhost", 11434) == "http://localhost:11434"


def test_host_with_explicit_port_is_not_doubled():
    assert config._service_base_url("ollama.internal:9999", 11434) == "http://ollama.internal:9999"


def test_wildcard_bind_host_is_rewritten_to_loopback():
    assert config._service_base_url("0.0.0.0:11434", 11434) == "http://127.0.0.1:11434"
    assert config._service_base_url("0.0.0.0", 11434) == "http://127.0.0.1:11434"


def test_host_with_scheme_is_preserved():
    assert config._service_base_url("https://ollama.internal:443/", 11434) == "https://ollama.internal:443"


def test_empty_host_falls_back_to_localhost():
    assert config._service_base_url("", 8000) == "http://localhost:8000"


def test_ipv6_host_with_and_without_port():
    assert config._service_base_url("[::1]", 11434) == "http://[::1]:11434"
    assert config._service_base_url("[::1]:11434", 11434) == "http://[::1]:11434"
    assert config._service_base_url("[::]", 11434) == "http://[::1]:11434"


def test_settings_properties_use_the_helper(monkeypatch):
    monkeypatch.setattr(config.settings, "ollama_host", "0.0.0.0:11434")
    monkeypatch.setattr(config.settings, "ollama_port", 11434)
    assert config.settings.ollama_url == "http://127.0.0.1:11434"
    monkeypatch.setattr(config.settings, "chromadb_host", "chroma:8000")
    monkeypatch.setattr(config.settings, "chromadb_port", 8000)
    assert config.settings.chromadb_url == "http://chroma:8000"
