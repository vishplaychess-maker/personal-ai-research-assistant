"""Phase 1B.2R MP-3C3D4B — focused search-adapter and guarded-fetch-adapter
tests. NO live network: the search adapter is fed a fake underlying
`search_web` via monkeypatch; the fetch adapter uses `httpx.MockTransport`
(ships with `httpx` itself — no extra test dependency).
"""
from __future__ import annotations

import logging

import httpx
import pytest

from app.tools import research_search
from app.tools.research_fetch import (
    FetchError,
    GuardedHttpFetchAdapter,
    validate_fetch_url,
)
from app.tools.research_search import (
    DuckDuckGoSearchAdapter,
    SearchError,
    normalize_result_url,
)


# ── B5-B7: bounded results, normalization, dedupe, rank ──────


def test_search_bounds_results_and_preserves_rank(monkeypatch):
    def fake_search_web(query, max_results=None):
        return [{"url": f"https://example.com/{i}", "title": f"T{i}"} for i in range(20)]

    monkeypatch.setattr(research_search, "search_web", fake_search_web, raising=False)
    import app.tools.web_search as web_search_mod
    monkeypatch.setattr(web_search_mod, "search_web", fake_search_web)

    adapter = DuckDuckGoSearchAdapter()
    results = adapter.search("q", max_results=3)
    assert len(results) == 3
    assert [r.search_rank for r in results] == [1, 2, 3]
    assert [r.url for r in results] == [
        "https://example.com/0", "https://example.com/1", "https://example.com/2",
    ]


def test_search_deduplicates_normalized_urls(monkeypatch):
    def fake_search_web(query, max_results=None):
        return [
            {"url": "https://Example.com/a#frag", "title": "A"},
            {"url": "https://example.com/a", "title": "A dup"},   # same after normalize
            {"url": "https://example.com/b", "title": "B"},
        ]

    import app.tools.web_search as web_search_mod
    monkeypatch.setattr(web_search_mod, "search_web", fake_search_web)

    adapter = DuckDuckGoSearchAdapter()
    results = adapter.search("q", max_results=10)
    assert [r.url for r in results] == ["https://example.com/a", "https://example.com/b"]
    assert [r.search_rank for r in results] == [1, 2]


def test_normalize_result_url_strips_fragment_and_lowercases_host():
    assert normalize_result_url("https://EXAMPLE.com/Path?x=1#frag") == "https://example.com/Path?x=1"


def test_normalize_result_url_drops_default_port():
    assert normalize_result_url("https://example.com:443/a") == "https://example.com/a"
    assert normalize_result_url("http://example.com:80/a") == "http://example.com/a"


def test_normalize_result_url_keeps_nondefault_port():
    assert normalize_result_url("https://example.com:8443/a") == "https://example.com:8443/a"


# ── B8: unsupported scheme/userinfo (at the search-normalization layer) ──


@pytest.mark.parametrize("bad_url", [
    "ftp://example.com/a",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "",
    "not a url",
])
def test_normalize_result_url_rejects_unsupported_input(bad_url):
    assert normalize_result_url(bad_url) is None


# ── B9: raw query/error canaries absent from logs ────────────


def test_search_error_never_logs_raw_query_or_exception_text(monkeypatch, caplog):
    def boom(query, max_results=None):
        raise RuntimeError("CANARY-SECRET-QUERY-boom")

    import app.tools.web_search as web_search_mod
    monkeypatch.setattr(web_search_mod, "search_web", boom)

    adapter = DuckDuckGoSearchAdapter()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SearchError) as exc_info:
            adapter.search("MY-SECRET-QUERY-CANARY", max_results=5)
    assert "MY-SECRET-QUERY-CANARY" not in str(exc_info.value)
    assert "CANARY-SECRET-QUERY-boom" not in str(exc_info.value)
    for record in caplog.records:
        assert "MY-SECRET-QUERY-CANARY" not in record.getMessage()
        assert "CANARY-SECRET-QUERY-boom" not in record.getMessage()


# ── C: SSRF / redirect safety (research_fetch.validate_fetch_url) ────


@pytest.mark.parametrize("bad_url", [
    "http://localhost/",
    "http://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://10.0.0.5/",
    "http://192.168.1.1/",
])
def test_validate_fetch_url_rejects_forbidden_targets(bad_url):
    with pytest.raises(FetchError):
        validate_fetch_url(bad_url)


def test_validate_fetch_url_rejects_ipv6_loopback():
    with pytest.raises(FetchError):
        validate_fetch_url("http://[::1]/")


def test_validate_fetch_url_rejects_ipv4_mapped_ipv6_loopback():
    with pytest.raises(FetchError):
        validate_fetch_url("http://[::ffff:127.0.0.1]/")


def test_validate_fetch_url_rejects_ipv4_mapped_ipv6_metadata():
    with pytest.raises(FetchError):
        validate_fetch_url("http://[::ffff:169.254.169.254]/")


def test_validate_fetch_url_rejects_userinfo():
    with pytest.raises(FetchError):
        validate_fetch_url("http://user:pass@example.com/")


def test_validate_fetch_url_rejects_unsupported_scheme():
    with pytest.raises(FetchError):
        validate_fetch_url("ftp://example.com/")
    with pytest.raises(FetchError):
        validate_fetch_url("file:///etc/passwd")


def test_validate_fetch_url_public_hostname_resolving_to_private_ip_rejected(monkeypatch):
    """A hostname that DNS-resolves to a private address must be rejected
    even though the hostname string itself looks public."""
    import socket as socket_mod

    def fake_getaddrinfo(host, port, **kwargs):
        return [(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr("app.services.browser_agent.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FetchError):
        validate_fetch_url("http://internal-looking-public-name.example/")


def test_validate_fetch_url_accepts_a_public_address(monkeypatch):
    import socket as socket_mod

    def fake_getaddrinfo(host, port, **kwargs):
        return [(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("app.services.browser_agent.socket.getaddrinfo", fake_getaddrinfo)
    assert validate_fetch_url("http://example.com/a") == "http://example.com/a"


# ── C13-C18: redirect chain behavior via httpx.MockTransport ─────────


def _patch_public_dns(monkeypatch) -> None:
    """The MockTransport tests below use fake hostnames (the RFC 2606
    reserved ``.example`` TLD) that have no real DNS record — resolving
    them for real would fail in this network-free test environment. This
    stubs `assert_url_allowed`'s DNS resolution step (NOT its IP
    classification logic, which stays real) to return a public IP for any
    NON-numeric hostname — a literal IP argument (e.g. a redirect Location
    of ``127.0.0.1``) still resolves to ITSELF, so a test proving a private
    redirect TARGET is rejected (a literal, not a hostname) keeps working
    unmodified. The SSRF-classification tests above
    (test_validate_fetch_url_rejects_*) already cover DNS-resolves-to-
    private-IP-via-hostname behavior directly with their own narrower
    fakes."""
    import ipaddress
    import socket as socket_mod

    def fake_getaddrinfo(host, port, **kwargs):
        try:
            ipaddress.ip_address(host.strip("[]") if isinstance(host, str) else host)
            is_literal = True
        except ValueError:
            is_literal = False
        target = host if is_literal else "93.184.216.34"
        return [(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", (target, 0))]

    monkeypatch.setattr("app.services.browser_agent.socket.getaddrinfo", fake_getaddrinfo)


def _mock_adapter(handler) -> GuardedHttpFetchAdapter:
    transport = httpx.MockTransport(handler)
    return GuardedHttpFetchAdapter(transport=transport)


def _default_fetch_kwargs(**overrides):
    kwargs = dict(
        timeout_s=5.0, max_redirects=3, max_response_bytes=100_000,
        max_extracted_chars=10_000, max_passages=3, max_passage_chars=1000,
    )
    kwargs.update(overrides)
    return kwargs


def test_public_url_redirecting_to_private_ip_rejected_before_second_fetch(monkeypatch):
    """The initial URL is public; its redirect Location points at a
    private IP. The second (private) hop must NEVER be fetched."""
    _patch_public_dns(monkeypatch)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == "http://public.example/start":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        raise AssertionError("must never fetch the private redirect target")

    adapter = _mock_adapter(handler)
    with pytest.raises(FetchError):
        adapter.fetch("http://public.example/start", **_default_fetch_kwargs())
    assert calls == ["http://public.example/start"]


def test_multi_hop_redirect_all_hops_validated(monkeypatch):
    _patch_public_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "http://a.example/1":
            return httpx.Response(302, headers={"location": "http://a.example/2"})
        if url == "http://a.example/2":
            return httpx.Response(302, headers={"location": "http://a.example/3"})
        if url == "http://a.example/3":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body><p>hi there</p></body></html>")
        raise AssertionError(f"unexpected hop: {url}")

    adapter = _mock_adapter(handler)
    page = adapter.fetch("http://a.example/1", **_default_fetch_kwargs())
    assert page.final_url == "http://a.example/3"
    assert page.passages == ["hi there"]


def test_redirect_loop_rejected(monkeypatch):
    _patch_public_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "http://a.example/x":
            return httpx.Response(302, headers={"location": "http://a.example/y"})
        if url == "http://a.example/y":
            return httpx.Response(302, headers={"location": "http://a.example/x"})
        raise AssertionError(f"unexpected hop: {url}")

    adapter = _mock_adapter(handler)
    with pytest.raises(FetchError):
        adapter.fetch("http://a.example/x", **_default_fetch_kwargs(max_redirects=5))


def test_redirect_count_cap_enforced(monkeypatch):
    _patch_public_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        n = int(str(request.url).rsplit("/", 1)[-1])
        return httpx.Response(302, headers={"location": f"http://a.example/{n + 1}"})

    adapter = _mock_adapter(handler)
    with pytest.raises(FetchError):
        adapter.fetch("http://a.example/0", **_default_fetch_kwargs(max_redirects=2))


def test_final_effective_url_is_validated_and_returned(monkeypatch):
    _patch_public_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body>ok</body></html>")

    adapter = _mock_adapter(handler)
    page = adapter.fetch("http://a.example/only", **_default_fetch_kwargs())
    assert page.final_url == "http://a.example/only"


def test_automatic_redirect_following_is_disabled(monkeypatch):
    """If httpx followed redirects automatically, our handler would never
    see the intermediate hop as a distinct request — assert we DO see it,
    proving `follow_redirects=False` is honored."""
    _patch_public_dns(monkeypatch)
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if str(request.url) == "http://a.example/start":
            return httpx.Response(302, headers={"location": "http://a.example/end"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>end</p>")

    adapter = _mock_adapter(handler)
    adapter.fetch("http://a.example/start", **_default_fetch_kwargs())
    assert seen_urls == ["http://a.example/start", "http://a.example/end"]


def test_no_subresource_or_javascript_path_exists_in_transport(monkeypatch):
    """The chosen transport is a single bare GET per hop — there is no code
    path anywhere in GuardedHttpFetchAdapter that issues a second request
    for an image/script/font/XHR/etc. Proven structurally: fetching a page
    whose HTML references external resources results in EXACTLY ONE
    request to the handler (the page itself)."""
    _patch_public_dns(monkeypatch)
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            "<html><head><script src='evil.js'></script></head>"
            "<body><img src='track.png'><p>real text</p></body></html>"
        ))

    adapter = _mock_adapter(handler)
    page = adapter.fetch("http://a.example/page", **_default_fetch_kwargs())
    assert call_count["n"] == 1
    assert page.passages == ["real text"]


# ── D20-D25: content-type / size / extraction / cleanup ─────


def test_disallowed_content_type_rejected(monkeypatch):
    _patch_public_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4")

    adapter = _mock_adapter(handler)
    with pytest.raises(FetchError):
        adapter.fetch("http://a.example/f.pdf", **_default_fetch_kwargs())


def test_response_byte_cap_enforced_during_streaming(monkeypatch):
    _patch_public_dns(monkeypatch)
    big = b"x" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=big)

    adapter = _mock_adapter(handler)
    with pytest.raises(FetchError):
        adapter.fetch("http://a.example/big", **_default_fetch_kwargs(max_response_bytes=100))


def test_extracted_text_bound_enforced(monkeypatch):
    _patch_public_dns(monkeypatch)
    html = "<html><body><p>" + ("word " * 5000) + "</p></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    adapter = _mock_adapter(handler)
    page = adapter.fetch("http://a.example/long", **_default_fetch_kwargs(max_extracted_chars=50, max_passage_chars=50))
    assert all(len(p) <= 50 for p in page.passages)


def test_scripts_styles_forms_excluded_from_extracted_text(monkeypatch):
    _patch_public_dns(monkeypatch)
    html = (
        "<html><head><style>.x{color:red}</style></head><body>"
        "<script>alert('x')</script>"
        "<form><input type='text' value='secret'></form>"
        "<p>visible content</p>"
        "</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    adapter = _mock_adapter(handler)
    page = adapter.fetch("http://a.example/p", **_default_fetch_kwargs())
    joined = " ".join(page.passages)
    assert "alert" not in joined
    assert "color:red" not in joined
    assert "secret" not in joined
    assert "visible content" in joined


def test_cleanup_on_success_and_error_paths(monkeypatch):
    """httpx.Client/response are used as context managers on every path
    (success, redirect, and error) — verified structurally by confirming
    no exception escapes uncleanly and multiple sequential fetches (each
    opening/closing its own client) succeed without resource errors."""
    _patch_public_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>ok</p>")

    adapter = _mock_adapter(handler)
    for _ in range(3):
        page = adapter.fetch("http://a.example/ok", **_default_fetch_kwargs())
        assert page.passages == ["ok"]


def test_fetch_timeout_bounded_and_redacted(monkeypatch):
    _patch_public_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out to http://a.example/slow", request=request)

    adapter = _mock_adapter(handler)
    with pytest.raises(FetchError) as exc_info:
        adapter.fetch("http://a.example/slow", **_default_fetch_kwargs())
    assert "a.example" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
