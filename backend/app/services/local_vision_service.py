"""Local Vision (ZERO API cost) — describe an image with a LOCAL Ollama model.

Off by default (``local_vision_enabled``). When on, an attached image
(JPEG / PNG / WebP, at most ``local_vision_max_bytes``) is described by a
LOCAL Ollama vision model (``local_vision_model``, default "moondream") through
the same ``/api/generate`` endpoint the rest of the app already uses. NO paid
API is ever called — this is deliberately the cheapest possible vision path.

Fail-closed (HARD invariant)
---------------------------
Every failure — flag off, unsupported type, oversize, connection error,
timeout, non-200, or an empty response — becomes a bounded status plus a
fixed-template notice. Nothing here ever raises into a chat path: the chat
continues normally no matter what vision does.

Privacy (HARD invariant)
------------------------
Image bytes and the generated description are NEVER logged. Log lines carry
only the sha256 digest (truncated) and the byte count. Error paths log
``type(exc).__name__`` only — never ``str(exc)``, which can quote upstream text.

Reuse (no second implementation)
--------------------------------
* The Ollama call uses the SAME ``settings.ollama_url`` and ``httpx`` the rest
  of the backend uses (``app.routes.models``, ``app.services.ollama_client``).
* Type detection is magic-byte sniffing — a filename or client content type is
  never trusted.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

# ── bounded outcome vocabulary (status slugs, never content) ─────────

STATUS_OK = "ok"
STATUS_DISABLED = "disabled"
STATUS_UNSUPPORTED_TYPE = "unsupported_type"
STATUS_TOO_LARGE = "too_large"
STATUS_FAILED = "failed"

# ── bounded user-facing notices (fixed templates, never content) ─────

DISABLED_NOTICE = "Local image analysis is turned off."
UNSUPPORTED_TYPE_NOTICE = ("I could not analyze that image. Only JPEG, PNG, and "
                           "WebP images are supported.")
TOO_LARGE_NOTICE = ("I could not analyze that image because it is larger than "
                    "the configured limit.")
#: The ONE notice shown for a genuine analysis failure (connection / timeout /
#: non-200 / empty response). Fixed template: it never echoes upstream text.
FAILED_NOTICE = "I could not analyze the image. The chat will continue normally."

#: The EXACT untrusted line prepended on success. The "(untrusted)" marker is
#: load-bearing: a model description is DATA derived from a user-supplied image,
#: never an instruction.
CONTEXT_PREFIX = "[Image description (untrusted)]: "
#: The bounded line appended on failure so the model tells the user plainly.
NOTICE_PREFIX = "[Image analysis (untrusted)]: "
NOTICE_SUFFIX = " Tell the user this plainly and answer their message normally."

PROMPT = "Describe this image factually in up to 150 words."

#: Magic-byte MIME types accepted. Anything else fails closed.
ALLOWED_TYPES = ("image/jpeg", "image/png", "image/webp")

#: Process-local cache of SUCCESSFUL descriptions, keyed by the image sha256.
#: The frontend pre-analysis (POST /api/vision/describe) and the chat pre-model
#: hook routinely see the SAME attachment; without this a CPU-only host would
#: run the local model twice for one turn. Bounded + TTL; never persisted.
_CACHE_MAX = 8
_CACHE_TTL_S = 600.0
_cache: "OrderedDict[str, Tuple[float, VisionResult]]" = OrderedDict()
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class VisionResult:
    """The truthful outcome of one local-image description attempt. Carries a
    bounded status, the description (on success only) and a fixed-template
    notice. Never carries image bytes."""

    status: str
    description: Optional[str] = None
    notice: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK and bool(self.description)


def enabled() -> bool:
    """True only when the server-owned flag authorizes local vision."""
    return bool(getattr(settings, "local_vision_enabled", False))


def _max_bytes() -> int:
    try:
        return max(1, int(getattr(settings, "local_vision_max_bytes", 5242880)))
    except (TypeError, ValueError):
        return 5242880


def _timeout_s() -> float:
    try:
        value = float(getattr(settings, "local_vision_timeout_s", 60.0))
    except (TypeError, ValueError):
        return 60.0
    return value if value > 0 else 60.0


def sniff_image_type(raw: bytes) -> Optional[str]:
    """Magic-byte sniff. Returns a MIME type or None. A filename / client
    content type is never trusted."""
    if not raw:
        return None
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode_image_data_url(data_url: str) -> Optional[bytes]:
    """Decode a data:image/...;base64,... URL to raw bytes, or None.

    A non-data URL (e.g. an http(s) link) is deliberately NOT fetched — that
    would be an SSRF surface. The payload is bounded BEFORE decoding so a
    hostile data URL cannot force a huge allocation."""
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    header, sep, payload = data_url.partition(",")
    if not sep or ";base64" not in header:
        return None
    # base64 is 4 chars per 3 bytes; reject an over-long payload pre-decode.
    if len(payload) > (int(_max_bytes() * 4 / 3) + 64):
        return None
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None


def _cache_get(digest: str) -> Optional[VisionResult]:
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(digest)
        if entry is None:
            return None
        ts, result = entry
        if now - ts > _CACHE_TTL_S:
            _cache.pop(digest, None)
            return None
        _cache.move_to_end(digest)
        return result


def _cache_put(digest: str, result: VisionResult) -> None:
    with _cache_lock:
        _cache[digest] = (time.monotonic(), result)
        _cache.move_to_end(digest)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def clear_cache() -> None:
    """Drop every cached description (tests / explicit operator reset)."""
    with _cache_lock:
        _cache.clear()


def _ollama_base() -> str:
    """A WELL-FORMED Ollama base URL.

    Prefers the canonical ``settings.ollama_url``. That property blindly
    appends ``ollama_port`` to ``ollama_host``, so the common Ollama
    environment convention ``OLLAMA_HOST="host:port"`` yields
    ``http://host:port:port`` — an invalid URL that would make EVERY local
    vision call fail. This repairs that case (and an absent port) rather than
    propagating a malformed URL into the retry-free fail-closed path."""
    base = str(getattr(settings, "ollama_url", "") or "").strip().rstrip("/")
    try:
        import httpx

        parsed = httpx.URL(base)
        if parsed.host and parsed.port is not None:
            return base
    except Exception:  # noqa: BLE001 - fall through to the repaired form
        pass

    host = str(getattr(settings, "ollama_host", "") or "").strip()
    try:
        port = int(getattr(settings, "ollama_port", 11434) or 11434)
    except (TypeError, ValueError):
        port = 11434
    if host.startswith("[") and "]" in host:
        inner, _, rest = host[1:].partition("]")
        host = inner
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif host.count(":") == 1:
        h, _, p = host.partition(":")
        if p.isdigit():
            host, port = h, int(p)
    host = host.strip() or "127.0.0.1"
    if host in ("0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _post_ollama(image_b64: str) -> Tuple[bool, Optional[str]]:
    """(ok, description). The ONLY network call, isolated so tests can patch
    it without ever touching the network. Never logs content."""
    import httpx

    model = str(getattr(settings, "local_vision_model", "moondream") or "moondream")
    payload = {
        "model": model,
        "prompt": PROMPT,
        "images": [image_b64],
        "stream": False,
    }
    with httpx.Client(timeout=_timeout_s()) as client:
        resp = client.post(f"{_ollama_base()}/api/generate", json=payload)
    if resp.status_code != 200:
        return False, None
    try:
        data = resp.json()
    except ValueError:
        return False, None
    text = data.get("response") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        return False, None
    return True, text.strip()


def describe_image(raw_bytes: bytes) -> VisionResult:
    """Describe one image entirely on-box. NEVER raises and NEVER blocks the
    caller with anything other than a bounded (optionally timed-out) call.

    Ordering is deliberate: flag -> type -> size -> cache -> model. An
    oversize / unsupported image therefore makes NO Ollama call at all."""
    if not enabled():
        return VisionResult(status=STATUS_DISABLED, notice=DISABLED_NOTICE)
    if not isinstance(raw_bytes, (bytes, bytearray)) or not raw_bytes:
        return VisionResult(status=STATUS_UNSUPPORTED_TYPE,
                            notice=UNSUPPORTED_TYPE_NOTICE)
    raw = bytes(raw_bytes)
    if sniff_image_type(raw) not in ALLOWED_TYPES:
        return VisionResult(status=STATUS_UNSUPPORTED_TYPE,
                            notice=UNSUPPORTED_TYPE_NOTICE)
    if len(raw) > _max_bytes():
        return VisionResult(status=STATUS_TOO_LARGE, notice=TOO_LARGE_NOTICE)

    digest = hashlib.sha256(raw).hexdigest()
    cached = _cache_get(digest)
    if cached is not None:
        return cached

    # Only the hash and the byte count ever reach the log.
    logger.info("local_vision: analyzing bytes=%d sha256=%s", len(raw), digest[:12])
    image_b64 = base64.b64encode(raw).decode("ascii")
    try:
        ok, description = _post_ollama(image_b64)
    except Exception as exc:  # noqa: BLE001 - vision must never break chat
        logger.warning("local_vision: ollama call failed (%s)", type(exc).__name__)
        return VisionResult(status=STATUS_FAILED, notice=FAILED_NOTICE)
    if not ok or not description:
        logger.warning("local_vision: ollama returned no usable description")
        return VisionResult(status=STATUS_FAILED, notice=FAILED_NOTICE)

    result = VisionResult(status=STATUS_OK, description=description, notice="")
    _cache_put(digest, result)
    return result


def context_line(result: Optional[VisionResult]) -> Optional[str]:
    """The EXACT untrusted context line prepended on success, or None.

    On failure this returns None: the bounded notice is surfaced separately
    (the route response / the frontend banner) so a failed image never
    contaminates the model context with invented content."""
    if result is None or result.status != STATUS_OK:
        return None
    text = (result.description or "").strip()
    if not text:
        return None
    return CONTEXT_PREFIX + text


def notice_line(result: Optional[VisionResult]) -> Optional[str]:
    """The bounded failure line for the pre-model context, or None on success.
    Same shape as the YouTube ingest failure line: tell the user plainly."""
    if result is None or result.status == STATUS_OK:
        return None
    return NOTICE_PREFIX + (result.notice or FAILED_NOTICE) + NOTICE_SUFFIX


__all__ = [
    "VisionResult", "STATUS_OK", "STATUS_DISABLED", "STATUS_UNSUPPORTED_TYPE",
    "STATUS_TOO_LARGE", "STATUS_FAILED",
    "DISABLED_NOTICE", "UNSUPPORTED_TYPE_NOTICE", "TOO_LARGE_NOTICE",
    "FAILED_NOTICE", "CONTEXT_PREFIX", "NOTICE_PREFIX", "PROMPT", "ALLOWED_TYPES",
    "enabled", "sniff_image_type", "decode_image_data_url", "describe_image",
    "_ollama_base",
    "context_line", "notice_line", "clear_cache",
]
