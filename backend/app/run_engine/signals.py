"""Allowlisted ``run_signals.payload`` schema (Phase 1B.2R — MP-0).

``run_signals.payload`` MUST NOT become an unrestricted JSON dumping ground.
This module is the ONLY sanctioned way to build and parse it. It accepts a
closed allowlist of safe identifiers / decisions / reason codes / server
timestamps — and rejects everything else (raw prompts, raw tool arguments,
credentials, tokens, cookies, headers, unrestricted error text, arbitrary
client JSON).

No worker, route, or tool execution here — pure serialization + validation.
"""

from __future__ import annotations

import datetime as _dt
import json
import re

SIGNAL_KINDS = frozenset({"approval_decided", "cancel_requested"})

# fields the payload may carry, and their allowed shapes
_DECISIONS = frozenset({"approved", "denied"})
# short, machine-readable reason codes only (kebab/snake, bounded length)
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
# opaque server-owned ids (uuid4-ish or short tokens) — never free text
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# per-kind: {field: validator}
_ALLOWED: dict[str, dict[str, object]] = {
    "approval_decided": {
        "approval_id": _ID_RE,
        "decision": _DECISIONS,
        "reason_code": _REASON_CODE_RE,   # optional
    },
    "cancel_requested": {
        "reason_code": _REASON_CODE_RE,   # optional
    },
}
_REQUIRED: dict[str, frozenset[str]] = {
    "approval_decided": frozenset({"approval_id", "decision"}),
    "cancel_requested": frozenset(),
}

_MAX_PAYLOAD_CHARS = 512


class SignalPayloadError(ValueError):
    """The signal payload is malformed or carries a disallowed field/value."""


def _validate_field(kind: str, field: str, value: object) -> None:
    allowed = _ALLOWED[kind]
    if field == "server_ts":
        if not isinstance(value, str) or len(value) > 40:
            raise SignalPayloadError("server_ts must be a short ISO-8601 string")
        return
    if field not in allowed:
        raise SignalPayloadError(f"{kind}: field {field!r} is not allowed")
    rule = allowed[field]
    if isinstance(rule, frozenset):
        if value not in rule:
            raise SignalPayloadError(f"{kind}.{field}: {value!r} not in {sorted(rule)}")
    elif isinstance(rule, re.Pattern):
        if not isinstance(value, str) or not rule.match(value):
            raise SignalPayloadError(f"{kind}.{field}: {value!r} fails {rule.pattern}")
    else:  # pragma: no cover - defensive
        raise SignalPayloadError(f"{kind}.{field}: no validator")


def build_signal_payload(
    kind: str,
    *,
    approval_id: str | None = None,
    decision: str | None = None,
    reason_code: str | None = None,
    server_ts: _dt.datetime | None = None,
) -> str:
    """Build a validated JSON string for ``run_signals.payload``. Raises
    :class:`SignalPayloadError` on anything outside the allowlist."""
    if kind not in SIGNAL_KINDS:
        raise SignalPayloadError(f"unknown signal kind {kind!r}")
    fields: dict[str, object] = {}
    if approval_id is not None:
        fields["approval_id"] = approval_id
    if decision is not None:
        fields["decision"] = decision
    if reason_code is not None:
        fields["reason_code"] = reason_code
    if server_ts is not None:
        ts = server_ts if server_ts.tzinfo else server_ts.replace(tzinfo=_dt.timezone.utc)
        fields["server_ts"] = ts.astimezone(_dt.timezone.utc).isoformat()

    missing = _REQUIRED[kind] - set(fields)
    if missing:
        raise SignalPayloadError(f"{kind}: missing required field(s) {sorted(missing)}")
    for field, value in fields.items():
        _validate_field(kind, field, value)

    text = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    if len(text) > _MAX_PAYLOAD_CHARS:
        raise SignalPayloadError("signal payload exceeds the size cap")
    return text


def parse_signal_payload(kind: str, text: str | None) -> dict:
    """Parse + re-validate a stored payload. Raises :class:`SignalPayloadError`
    if it is not a flat JSON object of allowlisted fields (defence in depth —
    a row written by an older/broken path must not be trusted)."""
    if kind not in SIGNAL_KINDS:
        raise SignalPayloadError(f"unknown signal kind {kind!r}")
    if text in (None, ""):
        if _REQUIRED[kind]:
            raise SignalPayloadError(f"{kind}: empty payload but fields are required")
        return {}
    if len(text) > _MAX_PAYLOAD_CHARS:
        raise SignalPayloadError("signal payload exceeds the size cap")
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise SignalPayloadError("signal payload is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise SignalPayloadError("signal payload must be a JSON object")
    for field, value in obj.items():
        if not isinstance(field, str):
            raise SignalPayloadError("signal payload keys must be strings")
        if isinstance(value, (dict, list)):
            raise SignalPayloadError(f"{field!r}: nested structures are not allowed")
        _validate_field(kind, field, value)
    missing = _REQUIRED[kind] - set(obj)
    if missing:
        raise SignalPayloadError(f"{kind}: missing required field(s) {sorted(missing)}")
    return obj
