"""MP-4C decision 2 follow-up - durable save-marker grammar + duplicate identity.

Two narrow, content-free concerns shared by the durable save path:

* the versioned marker GRAMMAR block injected into the durable session system
  prompt (Task 8), and
* the stable CONTENT HASH used to suppress an unchanged duplicate marker
  (Task 7).

Both deliberately avoid raw content in any log, exception or plaintext column:
the hash is a sha256 digest over the stably-normalized identity + args, and the
grammar is a fixed template.
"""

from __future__ import annotations

import hashlib

from app.approval.canonical import canonicalize
from app.config import settings

GRAMMAR_VERSION = "v1"

#: The ONE versioned grammar block appended to a durable session system prompt
#: while run_engine_memory_write_enabled is True. Fixed template - never any
#: user or model content.
MEMORY_MARKER_GRAMMAR_V1 = """Syntax: [SAVE_MEMORY: <content>] and [SAVE_DIRECTIVE: <content>]
- Propose ONE fact or directive per marker.
- Never repeat an identical marker after it was approved, denied, or expired.
- Never include secrets, credentials, or sensitive PII.
- Nothing is saved without explicit user approval."""

# Dispositions of a prior proposal carrying the same content hash.
DISPOSITION_APPROVED_SAVED = "approved_saved"
DISPOSITION_DENIED = "denied"
DISPOSITION_EXPIRED = "expired"
DISPOSITION_UNRESOLVED = "unresolved"

_REASON_BY_DISPOSITION = {
    DISPOSITION_APPROVED_SAVED: "save_duplicate_approved",
    DISPOSITION_DENIED: "save_duplicate_denied",
    DISPOSITION_EXPIRED: "save_duplicate_expired",
    DISPOSITION_UNRESOLVED: "save_duplicate_unresolved",
}


def memory_marker_grammar_enabled() -> bool:
    """True only when the server-owned memory-write flag authorizes proposals."""
    return bool(getattr(settings, "run_engine_memory_write_enabled", False))


def compose_durable_system_prompt(system_prompt):
    """Append the grammar block to a durable session system prompt IFF the flag
    is on. Returns the input UNCHANGED when the flag is off (legacy behaviour),
    and is idempotent for a prompt that already carries the block."""
    if not memory_marker_grammar_enabled():
        return system_prompt
    base = (system_prompt or "").strip()
    if not base:
        return MEMORY_MARKER_GRAMMAR_V1
    if MEMORY_MARKER_GRAMMAR_V1 in base:
        return system_prompt
    return base + chr(10) + chr(10) + MEMORY_MARKER_GRAMMAR_V1


def normalize_save_args(kind: str, args: object) -> dict:
    """Stably-normalized identity for ONE save proposal: kind + whitespace-
    collapsed content (+ category for memory). Sorted keys come from the
    canonical encoder, so two equal proposals hash identically."""
    src = args if isinstance(args, dict) else {}
    normalized = {
        "kind": str(kind),
        "content": " ".join(str(src.get("content") or "").split()),
    }
    if kind == "memory":
        normalized["category"] = str(src.get("category") or "preference")
    return normalized


def save_content_hash(kind: str, args: object) -> str:
    """sha256 hex over the normalized identity. A DIGEST only - the content is
    never stored in or returned from this value."""
    return hashlib.sha256(canonicalize(normalize_save_args(kind, args))).hexdigest()


def prior_disposition(db, prior) -> str:
    """Classify a prior proposal carrying the same content hash into one of the
    four bounded dispositions. Fail closed to UNRESOLVED for anything unknown."""
    state = (getattr(prior, "state", "") or "").strip().lower()
    if state == "denied":
        return DISPOSITION_DENIED
    if state == "expired":
        return DISPOSITION_EXPIRED
    if state in ("approved", "consumed"):
        from app.models.models import DispatchRecord
        rec = (db.query(DispatchRecord)
               .filter(DispatchRecord.approval_id == prior.id).first())
        if rec is not None and rec.outcome != "succeeded":
            return DISPOSITION_UNRESOLVED
        return DISPOSITION_APPROVED_SAVED
    return DISPOSITION_UNRESOLVED


def reason_for_disposition(disposition: str) -> str:
    """Bounded Run.status_reason for a suppression disposition (never content)."""
    return _REASON_BY_DISPOSITION.get(disposition, "save_duplicate_unresolved")


__all__ = [
    "GRAMMAR_VERSION", "MEMORY_MARKER_GRAMMAR_V1", "DISPOSITION_APPROVED_SAVED",
    "DISPOSITION_DENIED", "DISPOSITION_EXPIRED", "DISPOSITION_UNRESOLVED",
    "memory_marker_grammar_enabled", "compose_durable_system_prompt",
    "normalize_save_args", "save_content_hash", "prior_disposition",
    "reason_for_disposition",
]
