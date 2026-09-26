"""Recursive redaction for approval-domain display/audit fields.

``display_summary`` and ``event_metadata`` may be built from partially
untrusted material. Before storage they are run through ``redact``:

* dict keys are matched **exactly (case-insensitive) or by anchored suffix** —
  never a bare substring — so token-usage metadata (``token_count``,
  ``input_tokens`` ...) survives while ``api_key`` / ``authorization`` /
  ``password`` / ``secret`` / ``access_token`` / ``cookie`` / ``*_api_key`` /
  ``*_secret`` ... are replaced with ``"[REDACTED]"``;
* every string value is scrubbed for JWTs, Fernet blobs, ``Bearer`` headers,
  and provider-key formats regardless of its key.

Approval storage is not a secret vault (spec 1B.1 sec.6) — this is defence in
depth, not the primary control. The primary control is: raw arguments and
credentials are never passed into these fields in the first place.
"""

from __future__ import annotations

import re

_SENSITIVE_KEYS_EXACT = frozenset({
    "authorization", "proxy-authorization",
    "cookie", "set-cookie",
    "x-csrf-token", "csrf_token", "csrf",
    "api_key", "apikey", "api-key", "x-api-key",
    "secret", "client_secret", "client-secret",
    "password", "passwd", "pwd",
    "access_token", "refresh_token", "id_token",
    "session_token", "auth_token", "bearer_token",
    "private_key", "secret_key", "encryption_key",
})
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key", "-api-key", "_apikey",
    "_secret", "-secret",
    "_password", "-password",
    "_access_token", "_refresh_token", "_id_token", "_auth_token",
    "_private_key", "_secret_key",
)

_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
_FERNET = re.compile(r"gAAAAA[A-Za-z0-9_\-]{20,}")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}")
_OPENROUTER = re.compile(r"sk-or-v1-[A-Za-z0-9]{8,}")
_PROVIDER_KEY = re.compile(r"sk-[A-Za-z0-9]{20,}")

_MAX_STR = 2000


def _is_sensitive_key(k: object) -> bool:
    if not isinstance(k, str):
        return False
    kl = k.strip().lower()
    return kl in _SENSITIVE_KEYS_EXACT or kl.endswith(_SENSITIVE_KEY_SUFFIXES)


def _scrub(s: str) -> str:
    s = _JWT.sub("[REDACTED_JWT]", s)
    s = _FERNET.sub("[REDACTED_FERNET]", s)
    s = _BEARER.sub("Bearer [REDACTED]", s)
    s = _OPENROUTER.sub("sk-or-v1-[REDACTED]", s)
    s = _PROVIDER_KEY.sub("[REDACTED_KEY]", s)
    if len(s) > _MAX_STR:
        s = s[:_MAX_STR] + "…"
    return s


def redact(obj, _depth: int = 0):
    if _depth > 60:
        return "[REDACTED_DEPTH]"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_sensitive_key(k):
                out[str(k)] = "[REDACTED]"
            else:
                out[str(k)] = redact(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(x, _depth + 1) for x in obj]
    if isinstance(obj, str):
        return _scrub(obj)
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _scrub(str(obj))
