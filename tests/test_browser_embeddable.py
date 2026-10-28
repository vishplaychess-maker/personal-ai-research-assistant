"""GET /api/browser/embeddable — built-in browser framability probe.

Covers: auth requirement, the strict http/https scheme allow-list (javascript:
and file: must answer embeddable=false, never a probe), X-Frame-Options and
CSP frame-ancestors classification, non-HTML content types, HTTP errors, and
the unreachable-target case — all against a monkeypatched httpx.Client so no
network is touched. The auth-gate tests run against the real app router.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

import app.routes.browser as browser_module
from tests.auth_helpers import auth_headers, register_and_login


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    yield


class _FakeStreamResponse:
    """Minimal stand-in for httpx's streaming response context manager."""

    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _FakeClient:
    """Async-context-manager httpx.Client replacement (headers only)."""

    responses: dict[str, _FakeStreamResponse] = {}
    calls: list[tuple[str, str]] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method: str, url: str):
        browser_module  # keep import reference (readability)
        _FakeClient.calls.append((method, url))
        response = _FakeClient.responses[url]
        # Mimic the "headers only, body never read" contract: the fake is
        # returned as a context manager and no .read() is expected.
        return _FakeStreamContext(response)


class _FakeStreamContext:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def fake_probe(monkeypatch):
    _FakeClient.responses = {}
    _FakeClient.calls = []
    monkeypatch.setattr(browser_module.httpx, "Client", _FakeClient)
    return _FakeClient


def _embeddable(c, headers, url="https://example.com"):
    return c.get(
        "/api/browser/embeddable",
        params={"url": url},
        headers=auth_headers(register_and_login(c)[1]),
    )


def test_probe_requires_authentication():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert (
            c.get("/api/browser/embeddable", params={"url": "https://example.com"}).status_code
            in (401, 403)
        )


def test_non_http_schemes_rejected_without_probing(fake_probe):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        for url in ("javascript:alert(1)", "file:///C:/Windows", "data:text/html,<p>x", "ftp://x"):
            body = _embeddable(c, fake_probe, url=url).json()
            assert body["embeddable"] is False, url
        assert fake_probe.calls == [], "no network probe may fire for disallowed schemes"


def test_x_frame_options_variants(fake_probe):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        fake_probe.responses["https://google.com"] = _FakeStreamResponse(
            200, {"content-type": "text/html", "x-frame-options": "DENY"}
        )
        body = _embeddable(c, fake_probe, "https://google.com").json()
        assert body["embeddable"] is False and "X-Frame-Options" in body["reason"]

        fake_probe.responses["https://github.com"] = _FakeStreamResponse(
            200, {"content-type": "text/html", "x-frame-options": "SAMEORIGIN"}
        )
        body = _embeddable(c, fake_probe, "https://github.com").json()
        assert body["embeddable"] is False


def test_csp_frame_ancestors_variants(fake_probe):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        fake_probe.responses["https://strict.example"] = _FakeStreamResponse(
            200,
            {"content-type": "text/html", "content-security-policy": "default-src 'self'; frame-ancestors 'none'"},
        )
        assert _embeddable(c, fake_probe, "https://strict.example").json()["embeddable"] is False

        fake_probe.responses["https://self.example"] = _FakeStreamResponse(
            200,
            {"content-type": "text/html", "content-security-policy": "frame-ancestors 'self'"},
        )
        assert _embeddable(c, fake_probe, "https://self.example").json()["embeddable"] is False

        fake_probe.responses["https://open.example"] = _FakeStreamResponse(
            200,
            {"content-type": "text/html", "content-security-policy": "frame-ancestors https://trusted.example"},
        )
        assert _embeddable(c, fake_probe, "https://open.example").json()["embeddable"] is True


def test_embeddable_site_without_headers_is_allowed(fake_probe):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        fake_probe.responses["https://example.com"] = _FakeStreamResponse(
            200, {"content-type": "text/html"}
        )
        body = _embeddable(c, fake_probe).json()
        assert body == {"embeddable": True, "reason": None}


def test_non_html_content_type_is_refused(fake_probe):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        fake_probe.responses["https://example.com/report.pdf"] = _FakeStreamResponse(
            200, {"content-type": "application/pdf"}
        )
        body = _embeddable(c, fake_probe, "https://example.com/report.pdf").json()
        assert body["embeddable"] is False


def test_http_error_status_is_refused(fake_probe):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        fake_probe.responses["https://gone.example"] = _FakeStreamResponse(
            404, {"content-type": "text/html"}
        )
        body = _embeddable(c, fake_probe, "https://gone.example").json()
        assert body["embeddable"] is False and "404" in body["reason"]


def test_unreachable_target_answers_not_embeddable(monkeypatch):
    import httpx as real_httpx

    from fastapi.testclient import TestClient

    from app.main import app

    class UnreachableClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url):
            raise real_httpx.ConnectError("connection refused")

    monkeypatch.setattr(browser_module.httpx, "Client", UnreachableClient)
    with TestClient(app) as c:
        body = _embeddable(c, UnreachableClient).json()
        assert body["embeddable"] is False
