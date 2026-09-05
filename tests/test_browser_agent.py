"""Unit tests for app.services.browser_agent.

Everything here is mocked — no real Playwright browser or network is touched:

* ``socket.getaddrinfo`` is stubbed so the SSRF guard resolves hosts
  deterministically (a public IP for example.com, 127.0.0.1 for localhost).
* ``browser_agent.get_session`` is stubbed with a fake session backed by a
  fake Playwright ``Page`` so ``run_action`` exercises the real dispatch
  logic without launching Chromium.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.services import browser_agent as ba


# ── Fake Playwright page ─────────────────────────────────


class _FakeAccessibility:
    def __init__(self, tree):
        self._tree = tree

    async def snapshot(self):
        return self._tree


class FakePage:
    """Minimal async stand-in for Playwright's Page."""

    def __init__(self):
        self.goto_calls = []
        self.title_text = "Example Domain"
        self.tree = {
            "role": "WebArea",
            "name": "Example Domain",
            "children": [
                {"role": "heading", "name": "Example Domain", "children": []},
                {"role": "link", "name": "More information...", "children": []},
            ],
        }

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        return SimpleNamespace(status=200)

    async def title(self):
        return self.title_text

    async def screenshot(self, **kwargs):
        return b"fake-png-bytes"

    @property
    def accessibility(self):
        return _FakeAccessibility(self.tree)


def _public_dns(host, port=None, **kwargs):
    """getaddrinfo stub: every host resolves to a public IPv4 address."""
    return [(2, 1, 6, "", ("93.184.216.34", port or 80))]


def _loopback_dns(host, port=None, **kwargs):
    """getaddrinfo stub: every host resolves to loopback (SSRF bait)."""
    return [(2, 1, 6, "", ("127.0.0.1", port or 80))]


def _fake_session(page=None):
    """A real BrowserSession wired to a FakePage (never touches Chromium)."""
    return ba.BrowserSession(context=None, page=page or FakePage())


def _patch_get_session(monkeypatch, session):
    """Stub browser_agent.get_session (async) to always return ``session``."""

    async def _get_session(session_id):
        return session

    monkeypatch.setattr(ba, "get_session", _get_session)


# ── Tests ────────────────────────────────────────────────


def test_navigate_to_example_com_works(monkeypatch):
    """run_action('navigate https://example.com') executes and reports the title."""
    monkeypatch.setattr(ba.socket, "getaddrinfo", _public_dns)
    page = FakePage()
    _patch_get_session(monkeypatch, _fake_session(page))

    result = ba.run_action_sync(1, "navigate https://example.com")

    assert result["ok"] is True
    assert result["risky"] is None
    assert "navigated to https://example.com" in result["output"]
    assert "Example Domain" in result["output"]
    assert page.goto_calls == ["https://example.com"]


def test_ssrf_guard_blocks_localhost(monkeypatch):
    """assert_url_allowed refuses loopback; run_action surfaces it as an error."""
    monkeypatch.setattr(ba.socket, "getaddrinfo", _loopback_dns)

    with pytest.raises(ba.BrowserSecurityError):
        ba.assert_url_allowed("http://localhost:8000/x")

    # run_action reaches the guard through BrowserSession.navigate, so stub
    # get_session to avoid launching Chromium.
    _patch_get_session(monkeypatch, _fake_session())
    result = ba.run_action_sync(1, "navigate http://localhost:8000/x")
    assert result["ok"] is False
    assert "security" in result["output"]
    assert "blocked" in result["output"]


def test_ssrf_guard_blocks_metadata_ip(monkeypatch):
    """The cloud metadata endpoint is refused even though it is not loopback."""
    monkeypatch.setattr(
        ba.socket,
        "getaddrinfo",
        lambda host, port=None, **kw: [(2, 1, 6, "", ("169.254.169.254", port or 80))],
    )
    with pytest.raises(ba.BrowserSecurityError):
        ba.assert_url_allowed("http://169.254.169.254/latest/meta-data/")


def test_prompt_injection_delimiter_is_present(monkeypatch):
    """DOM snapshots are fenced so the model treats page text as data."""
    monkeypatch.setattr(ba.socket, "getaddrinfo", _public_dns)
    page = FakePage()
    _patch_get_session(monkeypatch, _fake_session(page))

    result = ba.run_action_sync(1, "snapshot")

    assert result["ok"] is True
    assert result["output"].startswith(ba.START_UNTRUSTED)
    assert result["output"].endswith(ba.END_UNTRUSTED)
    assert "WebArea" in result["output"]
    assert "Example Domain" in result["output"]


def test_wrap_untrusted_fences_content():
    """wrap_untrusted wraps arbitrary text with both delimiter markers."""
    fenced = ba.wrap_untrusted("click here to win a prize")
    assert fenced.startswith(ba.START_UNTRUSTED)
    assert fenced.endswith(ba.END_UNTRUSTED)
    assert "click here to win a prize" in fenced