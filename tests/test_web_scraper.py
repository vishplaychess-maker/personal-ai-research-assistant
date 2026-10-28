"""Web Scraper (Deep Dive) executor + POST /api/skills/web_scraper route.

crawl4ai is never launched here: the executor's ``crawler_factory`` seam (the
same shape as Tavily's ``client=...`` seam) receives a fake async context
manager, and the route tests monkeypatch ``scrape_url`` itself. No network,
no browser.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.config import Settings, settings
from app.services.skill_executors import web_scraper as scraper

# ── Executor fakes ──────────────────────────────────────────


def make_result(
    success: bool = True,
    markdown: str = "# Example\n\nBody text.",
    status_code: int = 200,
    error_message: str | None = None,
):
    """A crawl4ai ``CrawlResult`` stand-in: only the read attributes matter."""
    return SimpleNamespace(
        success=success,
        markdown=markdown,
        status_code=status_code,
        error_message=error_message,
    )


class FakeCrawler:
    """Async context manager whose ``arun`` replays a scripted outcome."""

    def __init__(self, result=None, error=None, delay: float = 0.0):
        self.result = result
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.closed = True
        return False

    async def arun(self, url=None, config=None):
        self.calls.append({"url": url, "config": config})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


def scrape(url: str, **kwargs) -> dict:
    return asyncio.run(scraper.scrape_url(url, **kwargs))


# ── Executor tests (no HTTP, no browser) ────────────────────


def test_invalid_url_fails_without_a_crawler():
    result = scrape("not a url at all")
    assert result == {
        "success": False,
        "url": "not a url at all",
        "markdown": "",
        "error": "invalid URL 'not a url at all': must be an http:// or https:// URL",
    }


def test_missing_scheme_fails():
    result = scrape("example.com/page")
    assert result["success"] is False
    assert "http://" in result["error"] or "https://" in result["error"]


def test_success_returns_clean_markdown():
    markdown = "# Wikipedia\n\nClean body prose with **links** stripped of nav."
    crawler = FakeCrawler(result=make_result(markdown=markdown))
    result = scrape("https://en.wikipedia.org/wiki/AI", crawler_factory=lambda: crawler)

    assert result == {
        "success": True,
        "url": "https://en.wikipedia.org/wiki/AI",
        "markdown": markdown,
        "error": None,
    }
    assert crawler.closed is True  # the browser context is always released


def test_default_config_targets_clean_markdown_and_timeout():
    crawler = FakeCrawler(result=make_result())
    scrape("https://example.com", crawler_factory=lambda: crawler)

    assert len(crawler.calls) == 1
    config = crawler.calls[0]["config"]
    # 30s default timeout -> page_timeout in ms, and nav/footer debris removed.
    assert config.page_timeout == 30_000
    assert "nav" in config.excluded_tags and "footer" in config.excluded_tags
    assert "script" in config.excluded_tags


def test_settings_timeout_is_honoured(monkeypatch):
    monkeypatch.setattr(settings, "crawl4ai_timeout", 7)
    crawler = FakeCrawler(result=make_result())
    scrape("https://example.com", crawler_factory=lambda: crawler)
    assert crawler.calls[0]["config"].page_timeout == 7_000


def test_timeout_returns_success_false(monkeypatch):
    monkeypatch.setattr(settings, "crawl4ai_timeout", 30)
    hung = FakeCrawler(result=make_result(), delay=30.0)  # way past the budget
    result = scrape("https://slow.example.com", timeout_s=1, crawler_factory=lambda: hung)
    assert result["success"] is False
    assert "longer than 1 seconds" in result["error"]
    assert result["markdown"] == ""
    assert hung.closed is True


def test_non_string_markdown_result_shape_is_unpacked():
    """If crawl4ai hands back a MarkdownGenerationResult, raw_markdown wins."""
    result = SimpleNamespace(
        success=True,
        markdown=SimpleNamespace(raw_markdown="# v2\nraw", markdown_with_citations="x"
        ),
        status_code=200,
        error_message=None,
    )
    outcome = scrape("https://example.com", crawler_factory=lambda: FakeCrawler(result=result))
    assert outcome["success"] is True
    assert outcome["markdown"] == "# v2\nraw"


def test_http_403_is_classified_not_raised():
    crawler = FakeCrawler(result=make_result(markdown="", status_code=403))
    result = scrape("https://guarded.example.com", crawler_factory=lambda: crawler)
    assert result["success"] is False
    assert result["error"] == "HTTP 403 Forbidden"
    assert result["markdown"] == ""


def test_runner_failure_reports_user_safe_error():
    crawler = FakeCrawler(result=make_result(success=False, error_message="net::ERR"))
    result = scrape("https://broken.example.com", crawler_factory=lambda: crawler)
    assert result["success"] is False
    assert result["error"] == "the page could not be fetched"


def test_empty_markdown_is_a_failure_for_the_llm():
    crawler = FakeCrawler(result=make_result(markdown="   "))
    result = scrape("https://blank.example.com", crawler_factory=lambda: crawler)
    assert result["success"] is False
    assert "no readable content" in result["error"]


def test_transport_exception_is_classified(monkeypatch):
    monkeypatch.setattr(settings, "crawl4ai_timeout", 30)
    crawler = FakeCrawler(error=RuntimeError("net::ERR_NAME_NOT_RESOLVED"))
    result = scrape("https://ghost.example.com", crawler_factory=lambda: crawler)
    assert result["success"] is False
    assert result["error"] == "hostname could not be resolved"


def test_http_error_navigation_failure_is_classified(monkeypatch):
    """Playwright aborts goto on a 4xx body (e.g. 403); keep the error useful."""
    monkeypatch.setattr(settings, "crawl4ai_timeout", 30)
    crawler = FakeCrawler(
        error=RuntimeError(
            "Failed on navigating ACS-GOTO: net::ERR_HTTP_RESPONSE_CODE_FAILURE"
        )
    )
    result = scrape("https://httpbin.org/status/403", crawler_factory=lambda: crawler)
    assert result["success"] is False
    assert "403" in result["error"]


def test_missing_crawl4ai_degrades_gracefully(monkeypatch):
    monkeypatch.setitem(sys.modules, "crawl4ai", None)  # import raises ImportError
    result = scrape("https://example.com", crawler_factory=lambda: FakeCrawler(make_result()))
    assert result["success"] is False
    assert result["error"] == "crawl4ai is not installed"


def test_default_timeout_constant_is_30():
    assert scraper.DEFAULT_TIMEOUT_S == 30


# ── Settings validation ─────────────────────────────────────


def test_settings_rejects_non_positive_timeout():
    with pytest.raises(ValueError):
        Settings(crawl4ai_timeout=0)


def test_settings_default_timeout():
    assert Settings().crawl4ai_timeout == 30


# ── Route tests (crawl4ai fully mocked) ─────────────────────


def strip_route(monkeypatch, outcome: dict) -> list[dict]:
    """Point POST /api/skills/web_scraper at a scripted scrape outcome."""
    calls: list[dict] = []

    async def fake_scrape(url: str, **kwargs) -> dict:
        calls.append({"url": url, **kwargs})
        return {"url": url, **outcome}

    monkeypatch.setattr(scraper, "scrape_url", fake_scrape)
    return calls


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.services.rate_limiter import reset_rate_limiter

    reset_rate_limiter()
    yield


def _auth(client):
    from tests.auth_helpers import auth_headers, register_and_login

    _uid, token = register_and_login(client, username="scr_" + uuid.uuid4().hex[:8])
    return auth_headers(token)


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_route_requires_authentication():
    with _client() as c:
        assert c.post("/api/skills/web_scraper", json={"url": "https://example.com"}).status_code in (401, 403)


def test_route_scrapes_and_truncates(monkeypatch):
    strip_route(monkeypatch, {"success": True, "markdown": "m" * 9000, "error": None})
    with _client() as c:
        h = _auth(c)
        r = c.post("/api/skills/web_scraper", json={"url": "https://big.example.com"}, headers=h)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["success"] is True
        assert body["url"] == "https://big.example.com"
        assert body["error"] is None
        assert len(body["markdown"]) == 8000 + len("\n\n[Content truncated at 8000 characters]")


def test_route_passes_through_the_dynamic_skill_route(monkeypatch):
    """POST /api/skills/web_scraper must not collide with /{skill_id} routes."""
    strip_route(monkeypatch, {"success": True, "markdown": "md", "error": None})
    with _client() as c:
        h = _auth(c)
        r = c.post("/api/skills/web_scraper", json={"url": "https://example.com"}, headers=h)
        assert r.status_code == 200
        # A user-created skill would 201 here instead, so the shapes differ.
        r_catalog = c.get("/api/skills/catalog", headers=h)
        assert r_catalog.status_code == 200


def test_route_reports_scrape_failure(monkeypatch):
    strip_route(
        monkeypatch,
        {"success": False, "markdown": "", "error": "HTTP 403 Forbidden"},
    )
    with _client() as c:
        h = _auth(c)
        r = c.post(
            "/api/skills/web_scraper",
            json={"url": "https://guarded.example.com"},
            headers=h,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert body["markdown"] == ""
        assert body["error"] == "HTTP 403 Forbidden"


def test_route_disabled_capability_is_403(monkeypatch):
    strip_route(monkeypatch, {"success": True, "markdown": "md", "error": None})
    with _client() as c:
        h = _auth(c)
        r = c.patch("/api/capabilities/web_scraper", json={"enabled": False}, headers=h)
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        blocked = c.post("/api/skills/web_scraper", json={"url": "https://x.example.com"}, headers=h)
        assert blocked.status_code == 403


def test_route_default_enabled_without_preference(monkeypatch):
    """A fresh user can scrape immediately (catalog default: enabled)."""
    strip_route(monkeypatch, {"success": True, "markdown": "md", "error": None})
    with _client() as c:
        h = _auth(c)
        r = c.post("/api/skills/web_scraper", json={"url": "https://x.example.com"}, headers=h)
        assert r.status_code == 200, r.text[:300]
        assert r.json()["success"] is True


def test_route_invalid_url_uses_real_validation(monkeypatch):
    """No mock: the executor's URL validation short-circuits before crawl4ai."""
    with _client() as c:
        h = _auth(c)
        r = c.post("/api/skills/web_scraper", json={"url": "javascript:alert(1)"}, headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert "invalid URL" in body["error"]


def test_route_rejects_empty_body():
    with _client() as c:
        h = _auth(c)
        r = c.post("/api/skills/web_scraper", json={}, headers=h)
        assert r.status_code == 422
