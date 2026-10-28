"""The canonical action envelope.

An envelope is the *complete, trusted* description of a single tool action.
``action_hash`` (see ``hashing.action_hash``) is computed over it, and approval
consumption re-canonicalizes a resubmitted envelope and compares that hash —
``args_hash`` alone is never enough.

Trust rules (spec 1B.1 sec.3):

* ``risk_level``, ``risk_scope_flags`` and the ``*_scope`` fields are set /
  validated by **trusted server-side policy** (``service._classify``). They are
  NEVER taken from model output, tool output, browser content, documents, or
  any client-controlled text.
* ``purpose`` is untrusted display text: it is redacted, length-limited, kept
  out of ``action_hash``, and is never authorization evidence.
* Every field is present in the hash pre-image; an absent value is an explicit
  ``None`` so "absent" and "present & null" produce the same bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from app.approval.errors import EnvelopeError
from app.approval.hashing import (
    ACTION_HASH_FIELDS,
    GRANT_SCOPE_HASH_FIELDS,
    GRANT_SCOPE_VERSION,
    action_hash,
    grant_scope_hash,
)

# Bump when the set/meaning of envelope fields changes.
ACTION_ENVELOPE_VERSION = "1"

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_VALID_SOURCE_TYPES = frozenset(
    {"mcp", "python", "terminal", "browser", "internal", "unknown"}
)
_VALID_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})


def redact_purpose(text: object, *, max_chars: int) -> str:
    """Sanitize untrusted ``purpose`` text for safe storage/display.

    - coerce to str, strip control chars, collapse whitespace runs,
    - hard length-limit with an ellipsis marker.
    Never used as authorization evidence.
    """
    s = "" if text is None else str(text)
    s = _CONTROL_CHARS.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_chars:
        s = s[: max(0, max_chars - 1)].rstrip() + "…"
    return s


@dataclass(frozen=True)
class ActionEnvelope:
    """Immutable, hashable action description. Build via ``build_envelope`` or
    ``ActionEnvelope.from_fields``; do not mutate — use ``with_fields``."""

    action_envelope_version: str
    tool_id: str
    tool_version: str | None
    source_type: str
    mcp_server_id: str | None
    server_fingerprint: str
    tool_schema_hash: str
    args_hash: str
    affected_resources: tuple[str, ...]
    network_scope: str | None
    filesystem_scope: str | None
    credential_scope: str | None
    risk_level: str
    risk_scope_flags: tuple[str, ...]
    tenant_id: str | None
    user_id: int
    session_id: int | None
    run_id: str | None
    action_nonce: str
    policy_version: str
    # untrusted, display-only, NOT in action_hash
    purpose: str = ""
    _validated: bool = field(default=False, repr=False, compare=False)

    # ── construction ────────────────────────────────────────────────────

    @classmethod
    def from_fields(cls, **kw) -> "ActionEnvelope":
        env = cls(
            action_envelope_version=kw.get("action_envelope_version", ACTION_ENVELOPE_VERSION),
            tool_id=kw["tool_id"],
            tool_version=kw.get("tool_version"),
            source_type=kw["source_type"],
            mcp_server_id=kw.get("mcp_server_id"),
            server_fingerprint=kw["server_fingerprint"],
            tool_schema_hash=kw["tool_schema_hash"],
            args_hash=kw["args_hash"],
            affected_resources=tuple(kw.get("affected_resources") or ()),
            network_scope=kw.get("network_scope"),
            filesystem_scope=kw.get("filesystem_scope"),
            credential_scope=kw.get("credential_scope"),
            risk_level=kw["risk_level"],
            risk_scope_flags=tuple(kw.get("risk_scope_flags") or ()),
            tenant_id=kw.get("tenant_id"),
            user_id=kw["user_id"],
            session_id=kw.get("session_id"),
            run_id=kw.get("run_id"),
            action_nonce=kw["action_nonce"],
            policy_version=kw["policy_version"],
            purpose=kw.get("purpose", ""),
        )
        return env.validated()

    def with_fields(self, **changes) -> "ActionEnvelope":
        """Return a copy with some fields replaced (re-validated). Normalizes
        list-valued fields to tuples."""
        for seqf in ("affected_resources", "risk_scope_flags"):
            if seqf in changes and changes[seqf] is not None:
                changes[seqf] = tuple(changes[seqf])
        return replace(self, _validated=False, **changes).validated()

    # ── validation ─────────────────────────────────────────────────────

    def validated(self) -> "ActionEnvelope":
        if not isinstance(self.tool_id, str) or not self.tool_id:
            raise EnvelopeError("tool_id must be a non-empty string")
        if self.source_type not in _VALID_SOURCE_TYPES:
            raise EnvelopeError(f"source_type {self.source_type!r} not in {sorted(_VALID_SOURCE_TYPES)}")
        if self.risk_level not in _VALID_RISK_LEVELS:
            raise EnvelopeError(f"risk_level {self.risk_level!r} not in {sorted(_VALID_RISK_LEVELS)}")
        for hexf in ("server_fingerprint", "tool_schema_hash", "args_hash"):
            v = getattr(self, hexf)
            if not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
                raise EnvelopeError(f"{hexf} must be a 64-char sha256 hex string")
        if not isinstance(self.user_id, int) or isinstance(self.user_id, bool):
            raise EnvelopeError("user_id must be an int")
        if not isinstance(self.action_nonce, str) or len(self.action_nonce) < 16:
            raise EnvelopeError("action_nonce must be a server-generated string (>=16 chars)")
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise EnvelopeError("policy_version must be a non-empty string")
        for seqf in ("affected_resources", "risk_scope_flags"):
            seq = getattr(self, seqf)
            if not isinstance(seq, tuple) or any(not isinstance(x, str) for x in seq):
                raise EnvelopeError(f"{seqf} must be a tuple of strings")
        object.__setattr__(self, "_validated", True)
        return self

    # ── hashing surface ────────────────────────────────────────────────

    def hash_fields(self) -> dict:
        """Exactly ``ACTION_HASH_FIELDS`` -> values. Sequences are sorted so
        order of *flags/resources* is not significant, but note array order IS
        significant for the tool *arguments* (folded into args_hash upstream)."""
        raw = {
            "action_envelope_version": self.action_envelope_version,
            "tool_id": self.tool_id,
            "tool_version": self.tool_version,
            "source_type": self.source_type,
            "mcp_server_id": self.mcp_server_id,
            "server_fingerprint": self.server_fingerprint,
            "tool_schema_hash": self.tool_schema_hash,
            "args_hash": self.args_hash,
            "affected_resources": sorted(self.affected_resources),
            "network_scope": self.network_scope,
            "filesystem_scope": self.filesystem_scope,
            "credential_scope": self.credential_scope,
            "risk_level": self.risk_level,
            "risk_scope_flags": sorted(self.risk_scope_flags),
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "action_nonce": self.action_nonce,
            "policy_version": self.policy_version,
        }
        assert set(raw) == set(ACTION_HASH_FIELDS)  # structural guard
        return raw

    def action_hash(self) -> str:
        return action_hash(self.hash_fields())

    def grant_scope_fields(self) -> dict:
        """Exactly ``GRANT_SCOPE_HASH_FIELDS`` -> values. The STABLE
        authorization template — no ``action_nonce``, no ``run_id``, no
        timestamps, no ``purpose``."""
        raw = {
            "grant_scope_version": GRANT_SCOPE_VERSION,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "tool_id": self.tool_id,
            "tool_version": self.tool_version,
            "source_type": self.source_type,
            "mcp_server_id": self.mcp_server_id,
            "server_fingerprint": self.server_fingerprint,
            "tool_schema_hash": self.tool_schema_hash,
            "args_hash": self.args_hash,
            "affected_resources": sorted(self.affected_resources),
            "network_scope": self.network_scope,
            "filesystem_scope": self.filesystem_scope,
            "credential_scope": self.credential_scope,
            "risk_level": self.risk_level,
            "risk_scope_flags": sorted(self.risk_scope_flags),
            "policy_version": self.policy_version,
        }
        assert set(raw) == set(GRANT_SCOPE_HASH_FIELDS)  # structural guard
        return raw

    def grant_scope_hash(self) -> str:
        return grant_scope_hash(self.grant_scope_fields())

    def display_summary(self, *, max_chars: int) -> dict:
        """Bounded, non-secret dict for showing the user what they're approving.
        Contains identifiers and the redacted purpose only — never raw args."""
        return {
            "tool_id": self.tool_id[:120],
            "source_type": self.source_type,
            "affected_resources": [r[:200] for r in sorted(self.affected_resources)[:20]],
            "risk_level": self.risk_level,
            "risk_scope_flags": sorted(self.risk_scope_flags)[:20],
            "network_scope": (self.network_scope or "")[:120] or None,
            "filesystem_scope": (self.filesystem_scope or "")[:200] or None,
            "credential_scope": (self.credential_scope or "")[:120] or None,
            "purpose": redact_purpose(self.purpose, max_chars=max_chars),
        }


def build_envelope(**kw) -> ActionEnvelope:
    """Thin alias for ``ActionEnvelope.from_fields`` — the public constructor."""
    return ActionEnvelope.from_fields(**kw)
