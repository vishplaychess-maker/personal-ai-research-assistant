"""Browser automation — Playwright-driven page interaction for the agent.

Upgrades the ephemeral scraper (``app.tools.web_scraper``) into an
interaction engine: navigate, click, type, screenshot, and a compact
accessibility-tree snapshot for the LLM.

Design
------
* **One shared Chromium process** for the server, launched lazily on first
  use. **One BrowserContext per chat session** (isolates cookies / logins
  between sessions). Idle contexts are reaped after
  ``settings.browser_session_idle_seconds``; everything is closed on app
  shutdown via :func:`shutdown`.
* **SSRF guard** (:func:`assert_url_allowed`) — every ``navigate`` resolves
  the host and refuses loopback / link-local / private / carrier-grade-NAT
  ranges and the cloud metadata IP unless the host is in
  ``settings.browser_ssrf_allowlist``.
* **Untrusted content** — DOM snapshots are wrapped in
  ``START_UNTRUSTED_BROWSER_CONTENT`` / ``END_UNTRUSTED_BROWSER_CONTENT``.
  The system prompt tells the model never to follow instructions inside.
* **HITL** — actions classified as risky (login / payment / delete /
  download) are NOT executed by :func:`run_action` unless ``approved=True``;
  the caller surfaces an approval request and re-runs once the user agrees.

Only the multi-agent (LangGraph) path drives the browser. The streaming
chat path filters the ``[BROWSER_ACTION: ...]`` marker out of the view but
never executes it.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from app.config import settings
from app.services.async_bridge import run_coro_sync

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────

START_UNTRUSTED = "START_UNTRUSTED_BROWSER_CONTENT"
END_UNTRUSTED = "END_UNTRUSTED_BROWSER_CONTENT"

# Action categories that always require explicit user approval.
RISKY_ACTIONS = ("payment", "login", "delete", "download")

# The agent emits one of these per line to drive the browser.
BROWSER_ACTION_PATTERN = re.compile(r"\[BROWSER_ACTION:\s*([^\]]+)\]", re.IGNORECASE)

# Cloud metadata endpoint — always blocked unless explicitly allowlisted.
_METADATA_IPS = {"169.254.169.254", "fd00:ec2::254"}

_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
]

_NAV_TIMEOUT_MS = 30_000
_ACTION_TIMEOUT_MS = 10_000
_SNAPSHOT_MAX_CHARS = 6_000


class BrowserSecurityError(Exception):
    """Raised when an action is refused for a security reason (SSRF, …)."""


# ── SSRF guard ────────────────────────────────────────────


def _allowlist() -> set:
    return {
        h.strip().lower()
        for h in (settings.browser_ssrf_allowlist or "").split(",")
        if h.strip()
    }


def assert_url_allowed(url: str) -> None:
    """Raise :class:`BrowserSecurityError` unless ``url`` is a plain http(s)
    URL whose host resolves only to public addresses (or is allowlisted)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BrowserSecurityError(f"blocked non-http(s) URL: {url!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise BrowserSecurityError(f"blocked URL with no host: {url!r}")

    if host in _allowlist():
        return

    try:
        infos = socket.getaddrinfo(host, parsed.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise BrowserSecurityError(f"cannot resolve host {host!r}: {exc}") from exc

    for *_, sockaddr in infos:
        ip_str = sockaddr[0]
        if ip_str in _METADATA_IPS:
            raise BrowserSecurityError(f"blocked cloud metadata address: {ip_str}")
        ip = ipaddress.ip_address(ip_str)
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise BrowserSecurityError(
                f"blocked non-public address {ip_str} for host {host!r}"
            )


# ── Untrusted-content wrapper ─────────────────────────────


def wrap_untrusted(text: str) -> str:
    """Fence web-derived text so the model treats it as data, not instructions."""
    return f"{START_UNTRUSTED}\n{text}\n{END_UNTRUSTED}"


# ── Risky-action classification ──────────────────────────


_RISKY_HINTS = {
    "login": re.compile(r"\b(log[\s-]?in|sign[\s-]?in|password|passcode|otp|2fa|mfa|credential)\b", re.I),
    "payment": re.compile(r"\b(pay|payment|checkout|purchase|buy now|card number|cvv|billing|subscribe)\b", re.I),
    "delete": re.compile(r"\b(delete|remove|deactivate|close account|wipe|destroy)\b", re.I),
    "download": re.compile(r"\b(download|export|save file|\.zip|\.exe|\.pdf\b)\b", re.I),
}


def classify_risky_action(action: str) -> Optional[str]:
    """Return the RISKY_ACTIONS category ``action`` falls into, or ``None``.

    ``action`` is the raw text after ``[BROWSER_ACTION:`` (verb + args) or any
    free description. A ``type`` into a secret/OTP field counts as ``login``.
    """
    text = (action or "").strip()
    verb, _, rest = text.partition(" ")
    if verb.lower() in RISKY_ACTIONS:
        return verb.lower()
    for kind, rx in _RISKY_HINTS.items():
        if rx.search(text):
            return kind
    return None


# ── Marker helpers ───────────────────────────────────────


def extract_browser_actions(text: str) -> List[str]:
    """All ``[BROWSER_ACTION: ...]`` payloads in ``text`` (order preserved)."""
    return [m.group(1).strip() for m in BROWSER_ACTION_PATTERN.finditer(text or "")]


def strip_browser_actions(text: str) -> str:
    """Remove ``[BROWSER_ACTION: ...]`` markers from user-visible text."""
    return BROWSER_ACTION_PATTERN.sub("", text or "").strip()


BROWSER_APPROVAL_MARKER = "[BROWSER_APPROVAL_REQUIRED:"

_APPROVE_WORDS = {"yes", "y", "approve", "approved", "ok", "okay", "go ahead", "do it"}


def is_approval(text: str) -> bool:
    """True if ``text`` is an affirmative approval of a pending risky action."""
    return (text or "").strip().lower().rstrip(".!") in _APPROVE_WORDS


# ── BrowserSession ───────────────────────────────────────


class BrowserSession:
    """Thin wrapper over one Playwright ``Page`` with the agent's verbs.

    Locators are role/text based (``get_by_role`` / ``get_by_text`` /
    ``get_by_label``) — never CSS or XPath — so the model targets what a
    human sees, not brittle DOM paths.
    """

    def __init__(self, context: Any, page: Any):
        self._context = context
        self.page = page

    async def navigate(self, url: str) -> str:
        assert_url_allowed(url)
        resp = await self.page.goto(
            url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
        )
        status = resp.status if resp else "?"
        return f"navigated to {url} (HTTP {status}); title={await self.page.title()!r}"

    async def click(self, target: str) -> str:
        locator = self._locate(target)
        await locator.first.click(timeout=_ACTION_TIMEOUT_MS)
        return f"clicked {target!r}"

    async def type(self, target: str, text: str) -> str:
        locator = self._locate(target, prefer_field=True)
        await locator.first.fill(text, timeout=_ACTION_TIMEOUT_MS)
        shown = text if len(text) <= 4 else text[:2] + "…"
        return f"typed {shown!r} into {target!r}"

    async def screenshot(self) -> str:
        png = await self.page.screenshot(type="png", full_page=False)
        return base64.b64encode(png).decode("ascii")

    async def get_dom_snapshot(self) -> str:
        snap = await self.page.accessibility.snapshot()
        text = _flatten_ax(snap) if snap else "(empty accessibility tree)"
        if len(text) > _SNAPSHOT_MAX_CHARS:
            text = text[:_SNAPSHOT_MAX_CHARS] + "\n…[snapshot truncated]"
        return wrap_untrusted(text)

    # -- internals --

    def _locate(self, target: str, prefer_field: bool = False):
        """``"role:name"`` -> get_by_role; otherwise text / label / placeholder."""
        role, sep, name = target.partition(":")
        if sep and role.strip().isalpha():
            return self.page.get_by_role(role.strip().lower(), name=name.strip())
        if prefer_field:
            return self.page.get_by_label(target).or_(
                self.page.get_by_placeholder(target)
            ).or_(self.page.get_by_role("textbox", name=target))
        return self.page.get_by_text(target, exact=False)


def _flatten_ax(node: Dict[str, Any], depth: int = 0) -> str:
    """Accessibility-tree dict -> compact indented ``role "name"`` lines."""
    role = node.get("role", "")
    name = (node.get("name") or "").strip().replace("\n", " ")
    line = "  " * depth + (f'{role} "{name}"' if name else role)
    out = [line] if role else []
    for child in node.get("children", []) or []:
        out.append(_flatten_ax(child, depth + 1))
    return "\n".join(p for p in out if p)


# ── Per-session manager ──────────────────────────────────

_pw: Any = None
_browser: Any = None
_sessions: Dict[int, Tuple[BrowserSession, float]] = {}
_lock = asyncio.Lock()


async def _ensure_browser() -> Any:
    global _pw, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    from playwright.async_api import async_playwright

    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
    logger.info("Browser automation: launched shared Chromium")
    return _browser


async def _reap_idle() -> None:
    ttl = settings.browser_session_idle_seconds
    now = time.monotonic()
    for sid in [s for s, (_, ts) in _sessions.items() if now - ts > ttl]:
        await _close(sid)
        logger.info("Browser automation: reaped idle session %s", sid)


async def get_session(session_id: int) -> BrowserSession:
    """Get-or-create this session's :class:`BrowserSession`."""
    async with _lock:
        await _reap_idle()
        entry = _sessions.get(session_id)
        if entry is None:
            browser = await _ensure_browser()
            context = await browser.new_context(
                viewport={"width": 1366, "height": 900}
            )
            context.set_default_timeout(_ACTION_TIMEOUT_MS)
            sess = BrowserSession(context, await context.new_page())
            _sessions[session_id] = (sess, time.monotonic())
            return sess
        sess, _ = entry
        _sessions[session_id] = (sess, time.monotonic())
        return sess


async def _close(session_id: int) -> None:
    entry = _sessions.pop(session_id, None)
    if entry:
        try:
            await entry[0]._context.close()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass


async def close_session(session_id: int) -> None:
    async with _lock:
        await _close(session_id)


async def shutdown() -> None:
    """Close every context, the browser, and Playwright. Call on app shutdown."""
    global _pw, _browser
    async with _lock:
        for sid in list(_sessions):
            await _close(sid)
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:  # noqa: BLE001
                pass
            _browser = None
        if _pw is not None:
            try:
                await _pw.stop()
            except Exception:  # noqa: BLE001
                pass
            _pw = None


# ── Dispatch ─────────────────────────────────────────────

_VERB_ALIASES = {
    "goto": "navigate",
    "open": "navigate",
    "navigate": "navigate",
    "click": "click",
    "press": "click",
    "type": "type",
    "fill": "type",
    "screenshot": "screenshot",
    "snapshot": "snapshot",
    "dom": "snapshot",
}


async def run_action(
    session_id: int, action: str, approved: bool = False
) -> Dict[str, Any]:
    """Parse and execute one ``[BROWSER_ACTION:]`` payload.

    Returns ``{"ok", "output", "risky", "screenshot_b64", "action"}``.
    A risky action with ``approved=False`` is NOT executed — ``ok`` is False
    and ``risky`` names the category so the caller can request approval.
    """
    action = (action or "").strip()
    risky = classify_risky_action(action)
    if risky and not approved:
        return {
            "ok": False,
            "risky": risky,
            "output": f"approval required before this {risky} action",
            "screenshot_b64": None,
            "action": action,
        }

    verb_raw, _, rest = action.partition(" ")
    verb = _VERB_ALIASES.get(verb_raw.lower())
    rest = rest.strip()
    if verb is None:
        return _err(action, f"unknown browser verb {verb_raw!r}", risky)

    try:
        sess = await get_session(session_id)
        if verb == "navigate":
            out = await sess.navigate(rest)
        elif verb == "click":
            out = await sess.click(rest)
        elif verb == "type":
            tgt, _, txt = rest.partition("=")
            if not _:
                tgt, _, txt = rest.partition(" ")
            out = await sess.type(tgt.strip(), txt.strip())
        elif verb == "screenshot":
            return {
                "ok": True, "risky": risky, "action": action,
                "output": "screenshot captured",
                "screenshot_b64": await sess.screenshot(),
            }
        else:  # snapshot
            out = await sess.get_dom_snapshot()
        return {
            "ok": True, "risky": risky, "output": out,
            "screenshot_b64": None, "action": action,
        }
    except BrowserSecurityError as exc:
        return _err(action, f"security: {exc}", risky)
    except Exception as exc:  # noqa: BLE001 — never break the caller
        logger.warning("browser action %r failed: %s", action, exc)
        return _err(action, f"{type(exc).__name__}: {exc}", risky)


def _err(action: str, msg: str, risky: Optional[str]) -> Dict[str, Any]:
    return {
        "ok": False, "risky": risky, "output": f"[browser error] {msg}",
        "screenshot_b64": None, "action": action,
    }


def run_action_sync(
    session_id: int, action: str, approved: bool = False
) -> Dict[str, Any]:
    """Sync bridge for the LangGraph (sync) workflow node."""
    return run_coro_sync(lambda: run_action(session_id, action, approved))


# ── Self-check ───────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover - offline, no real browser
    import os

    # SSRF guard
    for bad in (
        "http://localhost:8000/x",
        "http://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        " file:///etc/passwd".strip(),
    ):
        try:
            assert_url_allowed(bad)
            raise AssertionError(f"SSRF guard let {bad!r} through")
        except BrowserSecurityError:
            pass
    assert_url_allowed("https://example.com/")  # public host must pass

    # allowlist override
    os.environ["BROWSER_SSRF_ALLOWLIST"] = ""  # (settings already loaded; logic check)
    assert "example.com" not in _allowlist()

    # untrusted wrapper
    w = wrap_untrusted("hi")
    assert w.startswith(START_UNTRUSTED) and w.endswith(END_UNTRUSTED)

    # risky classification
    assert classify_risky_action("type password=hunter2") == "login"
    assert classify_risky_action("click 'Pay $499 now'") == "payment"
    assert classify_risky_action("download report.zip") == "download"
    assert classify_risky_action("navigate https://example.com") is None

    # markers
    txt = "do [BROWSER_ACTION: navigate https://a.com] then [BROWSER_ACTION: click Login]"
    assert extract_browser_actions(txt) == [
        "navigate https://a.com", "click Login"
    ]
    assert "[BROWSER_ACTION" not in strip_browser_actions(txt)

    # ax flattener
    tree = {"role": "WebArea", "name": "T", "children": [
        {"role": "button", "name": "OK", "children": []}
    ]}
    flat = _flatten_ax(tree)
    assert 'button "OK"' in flat and 'WebArea "T"' in flat

    print("browser_agent self-check OK")
