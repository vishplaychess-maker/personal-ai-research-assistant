"""SPA static mount (Phase 1 - .exe packaging)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.static_mount import _is_backend_path, mount_frontend, resolve_static_dir


def _make_dist(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><title>Thunder</title></html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return dist


def test_no_mount_when_dist_missing(caplog):
    app = FastAPI()
    before = len(app.routes)
    with caplog.at_level(logging.WARNING):
        mounted = mount_frontend(app, Path("no-such-dist-dir-xyz"))
    assert mounted is False
    assert len(app.routes) == before
    assert "serving API only" in caplog.text


def test_no_mount_when_index_missing(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    app = FastAPI()
    assert mount_frontend(app, dist) is False


def test_mount_serves_index_assets_and_spa_fallback(tmp_path):
    dist = _make_dist(tmp_path)
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"pong": True}

    assert mount_frontend(app, dist) is True
    test_client = TestClient(app)
    assert test_client.get("/").status_code == 200
    assert "Thunder" in test_client.get("/").text
    assert test_client.get("/assets/app.js").status_code == 200
    deep_link = test_client.get("/settings/profile")
    assert deep_link.status_code == 200 and "Thunder" in deep_link.text
    assert test_client.get("/api/ping").json() == {"pong": True}
    assert test_client.get("/api/does-not-exist").status_code == 404


def test_resolve_static_dir(tmp_path):
    dist = _make_dist(tmp_path)
    assert resolve_static_dir(dist) == dist
    assert resolve_static_dir(tmp_path / "missing") is None


@pytest.mark.parametrize("path,expected", [
    ("/api/messages", True),
    ("/api/health", True),
    ("/health", True),
    ("/api", True),
    ("/settings", False),
    ("/", False),
    ("/chat/abc", False),
])
def test_backend_path_classification(path, expected):
    assert _is_backend_path(path) is expected


def test_real_app_keeps_health_and_setup_routes():
    """The real app serves /health, the public setup status, and the SPA."""
    from app import paths
    from app.main import app

    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "healthy", "service": "thunder-ai-backend"}
    # /api/setup/status is public (one boolean); /api/setup/configure remains
    # loopback-only and is covered by tests/test_setup_loopback.py.
    assert client.get("/api/setup/status").status_code == 200
    if paths.static_dir_available():
        assert client.get("/").status_code == 200
