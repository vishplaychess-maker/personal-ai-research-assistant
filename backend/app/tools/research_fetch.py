"""Durable Deep Research — guarded fetch/extract adapter (Phase 1B.2R
MP-3C3D4B).

**Transport decision (D4B, evidence-based — see the D4A plan and this
phase's own STOP-condition evaluation):** this module deliberately does
**NOT** use Playwright/headless-Chromium, unlike the legacy
``app.tools.web_scraper``. Chromium follows redirects and loads
subresources (images, fonts, scripts, XHR) internally with no request
interception anywhere in this repository today (confirmed: neither
``web_scraper.py`` nor ``services/browser_agent.py`` calls
``page.route()``/intercepts requests) — building genuine per-request
validation over that surface is exactly the "material new infrastructure"
the D4B task instructions say to avoid. A plain, single-request-at-a-time
HTTP client (``httpx``, already a dependency) has no subresource problem at
all (a bare GET never triggers a second request) and lets every redirect be
inspected and validated one hop at a time before it is ever followed. This
is the "simpler guarded HTTP fetch/extract path" the task explicitly
prefers when Playwright cannot prove pre-fetch redirect/subresource safety.

**Residual limitation, stated honestly:** a guarded HTTP GET cannot execute
JavaScript, so pages that render their primary content client-side will
extract as sparse/empty text. This is an accepted extraction-quality
tradeoff for D4B's safety requirement, not a security gap — it is the same
class of limitation `search_web`'s snippets already have.

DNS-rebinding / TOCTOU: :func:`app.services.browser_agent.assert_url_allowed`
performs a real DNS resolution and IP-classification check at validation
time, but `httpx` performs its OWN independent DNS resolution when it later
connects — there is a real, honestly-documented gap between "the name we
validated" and "the address the transport actually connects to" for a
TTL=0 DNS-rebinding attacker. True pinning (validate, then connect to that
exact literal address) is NOT implemented in this repository anywhere
(confirmed by inspection of ``browser_agent.py``) and is NOT added here
either — this module has the SAME residual TOCTOU exposure the existing
``assert_url_allowed``/``BrowserSession.navigate`` already has, no better,
no worse. It is recorded here, not hidden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import List, Optional, Protocol
from urllib.parse import urljoin, urlsplit

from app.run_engine.errors import RunEngineError
from app.services.browser_agent import BrowserSecurityError, assert_url_allowed
from app.tools.research_search import normalize_result_url

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "text/plain"})
_USER_AGENT = (
    "Mozilla/5.0 (compatible; ThunderAI-DeepResearch/1.0; "
    "+durable-acquisition; no-js)"
)


class FetchError(RunEngineError):
    """Bounded, non-secret fetch/validation failure — the message is always
    a fixed slug, never a raw exception string, a URL, or page content."""


@dataclass(frozen=True)
class FetchedPage:
    final_url: str          # the validated, canonical URL content was actually read from
    passages: List[str]     # whitespace-normalized text chunks, NOT yet neutralized/hashed


class FetchAdapter(Protocol):
    def fetch(
        self,
        url: str,
        *,
        timeout_s: float,
        max_redirects: int,
        max_response_bytes: int,
        max_extracted_chars: int,
        max_passages: int,
        max_passage_chars: int,
    ) -> FetchedPage:
        """Fetch and extract text from *url*, or raise :class:`FetchError`.
        Never follows a redirect without validating its target first."""
        ...


def _reject_userinfo(url: str) -> None:
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise FetchError("url_rejected")


def validate_fetch_url(url: str) -> str:
    """The one call site every hop (initial URL AND every redirect target)
    must pass through before it is fetched. Normalizes first (lowercases
    scheme+host, strips the fragment, drops an explicit default port —
    reusing :func:`app.tools.research_search.normalize_result_url`, the
    SAME canonicalization the search adapter already applies, so a
    redirect's effective/final URL and a search result's URL are always
    comparable/citable in the identical canonical form) so redirect-loop
    detection cannot be defeated by a harmless case/fragment variation, and
    so the persisted ``final_url`` is genuinely canonical. Then reuses the
    repository's proven SSRF guard (:func:`assert_url_allowed` — real DNS
    resolution; rejects loopback/link-local/private/reserved/multicast/
    unspecified addresses and the cloud metadata IPs) rather than a
    hand-rolled hostname blocklist. Additionally rejects URL userinfo
    (``user:pass@host``), which `assert_url_allowed` does not itself check
    (it validates the resolved host, not the presence of embedded
    credentials).

    Raises with ``from None`` (never chains the underlying exception) —
    ``BrowserSecurityError`` messages embed the resolved IP/host being
    rejected (e.g. a private redirect target); chaining it would let that
    string reach a future ``logging.exception()``/traceback dump via
    ``exc.__cause__`` even though the raised :class:`FetchError` message
    itself is a bounded slug. Matches the repository's own established
    convention for this exact concern
    (``app.run_engine.chat_stream``: "provider exception text is never
    propagated (``raise ... from None``)")."""
    _reject_userinfo(url)
    normalized = normalize_result_url(url)
    if not normalized:
        raise FetchError("url_rejected") from None
    try:
        assert_url_allowed(normalized)
    except BrowserSecurityError:
        raise FetchError("url_rejected") from None
    return normalized


class _TextExtractor(HTMLParser):
    """Minimal, dependency-free HTML-to-text extractor: drops
    scripts/styles/forms/head entirely, collapses everything else to
    whitespace-normalized text blocks. Not a full renderer — it never
    executes anything, never resolves external resources, and cannot be
    used to trigger a second network request (unlike a real browser)."""

    _SKIP_TAGS = frozenset({"script", "style", "head", "noscript", "form", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._skip_tag: Optional[str] = None
        self.chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in self._SKIP_TAGS:
            self._skip_depth = 1
            self._skip_tag = tag

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth and tag == self._skip_tag:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.chunks.append(text)


def _extract_text(html: str, *, max_chars: int) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — a pathological page must not crash acquisition
        pass
    text = "\n".join(parser.chunks)
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def _split_passages(text: str, *, max_passages: int, max_passage_chars: int) -> List[str]:
    """Deterministic, dependency-free passage split: paragraphs (blank-line
    separated), each individually bounded, up to *max_passages*."""
    paragraphs = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]
    passages: List[str] = []
    for p in paragraphs:
        if len(passages) >= max_passages:
            break
        passages.append(p[:max_passage_chars])
    return passages


class GuardedHttpFetchAdapter:
    """The only fetch transport D4B ships: single-hop-at-a-time HTTP GET
    via `httpx`, manual redirect validation, content-type allowlist,
    streamed byte cap, stdlib-only text extraction. No cookies, no auth
    header, no browser profile, no file access, no command execution.

    ``transport``: optional injectable ``httpx.BaseTransport`` (e.g.
    ``httpx.MockTransport`` — ships with ``httpx`` itself, no extra
    dependency) so focused tests never touch the real network. ``None``
    (the default, production behavior) uses httpx's normal transport."""

    def __init__(self, transport=None) -> None:
        self._transport = transport

    def fetch(
        self,
        url: str,
        *,
        timeout_s: float,
        max_redirects: int,
        max_response_bytes: int,
        max_extracted_chars: int,
        max_passages: int,
        max_passage_chars: int,
    ) -> FetchedPage:
        import httpx

        current = validate_fetch_url(url)
        visited = {current}

        for _hop in range(max_redirects + 1):
            try:
                with httpx.Client(
                    follow_redirects=False,
                    timeout=timeout_s,
                    transport=self._transport,
                    headers={"User-Agent": _USER_AGENT, "Accept": "text/html,text/plain"},
                ) as client:
                    with client.stream("GET", current) as resp:
                        if resp.status_code in _REDIRECT_STATUSES:
                            location = resp.headers.get("location")
                            if not location:
                                raise FetchError("redirect_no_location")
                            next_url = validate_fetch_url(urljoin(current, location))
                            if next_url in visited:
                                raise FetchError("redirect_loop")
                            visited.add(next_url)
                            current = next_url
                            continue  # next hop — do NOT read this response's body

                        if resp.status_code != 200:
                            raise FetchError("fetch_http_error")

                        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                        if content_type not in _ALLOWED_CONTENT_TYPES:
                            raise FetchError("content_type_rejected")

                        body = bytearray()
                        for chunk in resp.iter_bytes():
                            body.extend(chunk)
                            if len(body) > max_response_bytes:
                                raise FetchError("response_too_large")

                        try:
                            html = body.decode(resp.encoding or "utf-8", errors="replace")
                        except (LookupError, UnicodeDecodeError):
                            html = body.decode("utf-8", errors="replace")

                        text = _extract_text(html, max_chars=max_extracted_chars)
                        passages = _split_passages(
                            text, max_passages=max_passages, max_passage_chars=max_passage_chars,
                        )
                        return FetchedPage(final_url=current, passages=passages)
            except httpx.TimeoutException:
                # `from None`: httpx exception text commonly embeds the
                # request URL — never chain it (same rationale as
                # validate_fetch_url above).
                raise FetchError("fetch_timeout") from None
            except httpx.HTTPError:
                raise FetchError("fetch_network_error") from None

        raise FetchError("too_many_redirects")
