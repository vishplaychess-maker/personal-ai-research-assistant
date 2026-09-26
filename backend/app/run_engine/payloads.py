"""Encrypted-at-rest run payload store (Phase 1B.2R — MP-0 domain helper).

A DOMAIN persistence helper — no worker, no route, no tool execution. It is the
ONLY sanctioned way to persist run-scoped bytes that could contain sensitive
user data and are not already in a ``messages`` row (resolved tool arguments, a
tool result a later step needs, a sub-run input).

Confidentiality model — versioned, context-bound encrypted envelope
-------------------------------------------------------------------

**Why there is no adjacent plaintext digest.** An unkeyed deterministic digest
(``sha256(plaintext)``) stored next to the ciphertext lets an attacker with DB
read access *offline-guess* a low-entropy payload (a short decision string, an
enum, a small integer) by hashing candidates. Fernet already authenticates the
ciphertext (HMAC-SHA256), so such a digest is both unnecessary and a
confidentiality regression. It has been **removed**.

**What binds a ciphertext to its context.** Before encryption, ``seal_payload``
builds one canonical JSON *envelope*::

    {"version": 1,
     "tenant_id": <normalized owner tenant or null>,
     "user_id":   <owner user id (int)>,
     "run_id":    <owning run id>,
     "step_id":   <owning step id or null>,
     "payload_type": <"tool_args" | "tool_output" | "sub_run_input" | "other">,
     "action_hash": <bound Phase 1B.2 action hash or null>,
     "payload_encoding": "utf-8",
     "payload": <the exact UTF-8 string>}

serialized with the repo's RFC 8785 canonicalizer (``app.approval.canonical``)
and encrypted **exactly once** with the raising Fernet/MultiFernet cipher
(``app.services.encryption.get_cipher()``). The whole envelope is inside the
authenticated ciphertext, so every context field is HMAC-protected: swapping a
ciphertext between rows — even together with every mutable external column —
changes the *decrypted* context and fails closed.

**On open**, ``open_payload`` decrypts once, parses the envelope, and — using
**trusted server-supplied** expected values (never client input) — fails closed
unless the envelope ``version`` and every context field
(``tenant_id, user_id, run_id, step_id, payload_type, action_hash``) and the
``payload_encoding`` match. It never returns ``""`` / ``None`` / partial data on
any failure; a legitimately sealed empty string still round-trips.

Scope: **UTF-8 strings / canonical-JSON use cases only** (tool-arg JSON, small
server-assembled objects). Arbitrary binary payloads are NOT supported here.

Other guarantees
----------------
* One ``cipher.encrypt`` per seal, one ``cipher.decrypt`` per open.
* ``byte_len`` (a column, OUTSIDE the ciphertext) is the payload's UTF-8 length.
  It is used only for the pre-encryption size cap and operational reporting and
  **deliberately reveals the payload length** — it is NOT confidential.
* Key rotation: the repo provides only ``MultiFernet`` (re-encrypt under a new
  primary, decrypt with the old key as a fallback). There is **no per-row
  key-version**; ``enc_scheme`` names the *cipher scheme* (``"fernet.v1"``).
* Credentials never live here — provider / MCP secrets stay in
  ``user_providers.encrypted_api_key`` / ``mcp_servers.env_json``, referenced
  by id.
* This module never logs ciphertext, plaintext, decrypted-envelope data, args,
  or secret-bearing exception text.
* Hard delete (drop the row) is the real delete — crypto-shredding.
"""

from __future__ import annotations

import datetime as _dt
import hmac
import json
import uuid

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from app.approval.canonical import canonicalize
from app.approval.errors import CanonicalizationError
from app.config import settings
from app.models.models import RunPayload
from app.run_engine.errors import (
    RunPayloadAccessError,
    RunPayloadActionHashMismatch,
    RunPayloadContextMismatch,
    RunPayloadEncodingError,
    RunPayloadEncryptionUnavailable,
    RunPayloadEnvelopeError,
    RunPayloadEnvelopeVersionError,
    RunPayloadExpired,
    RunPayloadIntegrityError,
    RunPayloadSchemeError,
    RunPayloadTooLarge,
)

ENC_SCHEME = "fernet.v1"
SUPPORTED_SCHEMES = frozenset({ENC_SCHEME})

ENVELOPE_VERSION = 1
_PAYLOAD_ENCODING = "utf-8"
_ENVELOPE_FIELDS = frozenset({
    "version", "tenant_id", "user_id", "run_id", "step_id", "payload_type",
    "action_hash", "payload_encoding", "payload",
})

_VALID_TYPES = frozenset({
    "tool_args", "tool_output", "sub_run_input", "other",
    # Phase 1B.2R MP-3C3D4B — the sealed, versioned, bounded, citation-
    # correspondent structured deep-research source snapshot (see
    # app.run_engine.research_snapshot / app.run_engine.research_sources).
    "research_sources",
    # Phase 1B.2R MP-3C3D5B — the sealed, versioned, bounded inter-agent
    # artifact (see app.run_engine.multi_agent_artifacts).
    "agent_artifact",
})


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _ensure_utc(value: _dt.datetime) -> _dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)


def _norm(value: str | None) -> str | None:
    """Normalize an optional string context field: ``""`` -> ``None`` so a
    NULL owner and an empty owner cannot be confused. Real ids are never
    empty."""
    return value if value else None


def _ct_eq(a: str | None, b: str | None) -> bool:
    """Constant-time equality for (possibly non-ASCII, possibly None) string
    context fields."""
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def _cipher():
    """The repo's canonical Fernet cipher. Raises RuntimeError when
    ENCRYPTION_KEY is unset (callers translate that to a typed fail-closed
    error)."""
    from app.services.encryption import get_cipher

    return get_cipher()


def seal_payload(
    db: Session,
    *,
    tenant_id: str | None,
    user_id: int,
    run_id: str,
    step_id: str | None,
    payload_type: str,
    plaintext: str,
    envelope_action_hash: str | None = None,
    ttl_seconds: int | None = None,
    commit: bool = True,
) -> RunPayload:
    """Build the context-bound envelope, encrypt it **exactly once**, and
    persist a :class:`RunPayload` row. Returns it.

    Raises (nothing written): :class:`RunPayloadTooLarge` (payload over
    ``settings.run_payload_max_bytes``), :class:`RunPayloadEncodingError`
    (payload not valid UTF-8 / not canonicalizable),
    :class:`RunPayloadEncryptionUnavailable` (``ENCRYPTION_KEY`` unset).
    """
    if payload_type not in _VALID_TYPES:
        raise ValueError(f"unknown run payload type: {payload_type!r}")
    if not isinstance(plaintext, str):
        raise ValueError("run payload plaintext must be a str (UTF-8 scope)")

    try:
        raw_len = len(plaintext.encode("utf-8"))     # payload length (NOT confidential)
    except UnicodeEncodeError as exc:
        raise RunPayloadEncodingError("run payload is not valid UTF-8") from exc
    limit = int(settings.run_payload_max_bytes)
    if raw_len > limit:                              # enforced BEFORE any encryption
        raise RunPayloadTooLarge(raw_len, limit)

    tenant_id = _norm(tenant_id)
    step_id = _norm(step_id)
    action_hash = _norm(envelope_action_hash)
    envelope = {
        "version": ENVELOPE_VERSION,
        "tenant_id": tenant_id,
        "user_id": int(user_id),
        "run_id": str(run_id),
        "step_id": step_id,
        "payload_type": payload_type,
        "action_hash": action_hash,
        "payload_encoding": _PAYLOAD_ENCODING,
        "payload": plaintext,
    }
    try:
        canonical = canonicalize(envelope)          # deterministic bytes for encryption
    except CanonicalizationError as exc:
        raise RunPayloadEncodingError("run payload envelope cannot be canonicalized") from exc

    try:
        token = _cipher().encrypt(canonical)        # <- the ONLY encryption call
    except RuntimeError as exc:                      # ENCRYPTION_KEY not configured
        raise RunPayloadEncryptionUnavailable(
            "ENCRYPTION_KEY is not configured — cannot seal a run payload"
        ) from exc

    row = RunPayload(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        user_id=int(user_id),
        run_id=str(run_id),
        step_id=step_id,
        payload_type=payload_type,
        enc_scheme=ENC_SCHEME,
        ciphertext=token.decode("ascii"),           # Fernet tokens are urlsafe-b64 ASCII
        envelope_action_hash=action_hash,
        byte_len=raw_len,
        expires_at=(_now() + _dt.timedelta(seconds=int(ttl_seconds)))
        if ttl_seconds else None,
        created_at=_now(),
    )
    db.add(row)
    if commit:
        db.commit()
    return row


def open_payload(
    db: Session,
    payload_id: str,
    *,
    expected_tenant_id: str | None,
    expected_user_id: int,
    expected_run_id: str,
    expected_step_id: str | None,
    expected_payload_type: str,
    expected_action_hash: str | None,
) -> str:
    """Decrypt once, then FAIL CLOSED unless the authenticated envelope's
    ``version`` and every context field match the **trusted server-supplied**
    expected values. Never returns ``""`` / ``None`` / partial data on failure;
    a legitimately sealed empty string round-trips to ``""``.

    All ``expected_*`` values MUST come from trusted server-side state (the
    ``runs`` / ``run_steps`` rows the caller already loaded), never from an
    untrusted client.
    """
    row = db.get(RunPayload, payload_id)
    if row is None:
        raise RunPayloadAccessError("run payload not found")
    if row.deleted_at is not None:
        raise RunPayloadAccessError("run payload has been deleted")
    if row.expires_at is not None and _ensure_utc(row.expires_at) <= _now():
        raise RunPayloadExpired("run payload has expired")
    if row.enc_scheme not in SUPPORTED_SCHEMES:
        raise RunPayloadSchemeError(f"unsupported enc_scheme {row.enc_scheme!r}")
    # fast, non-authoritative owner pre-check (defence in depth only — the
    # authoritative check is the decrypted envelope below).
    if int(row.user_id) != int(expected_user_id):
        raise RunPayloadAccessError("run payload belongs to a different user")

    try:
        canonical = _cipher().decrypt(row.ciphertext.encode("ascii"))   # ONLY decrypt call
    except RuntimeError as exc:                      # ENCRYPTION_KEY not configured
        raise RunPayloadEncryptionUnavailable(
            "ENCRYPTION_KEY is not configured — cannot open a run payload"
        ) from exc
    except InvalidToken as exc:                      # tampered / wrong key / truncated
        raise RunPayloadIntegrityError(
            "run payload ciphertext failed authentication"
        ) from exc
    except (ValueError, TypeError) as exc:           # non-ASCII / malformed token
        raise RunPayloadIntegrityError("run payload ciphertext is malformed") from exc

    env = _parse_envelope(canonical)

    # ── authoritative context match: envelope vs TRUSTED expected ──
    if not _ct_eq(env["tenant_id"], _norm(expected_tenant_id)):
        raise RunPayloadContextMismatch("tenant_id")
    if int(env["user_id"]) != int(expected_user_id):
        raise RunPayloadContextMismatch("user_id")
    if not _ct_eq(env["run_id"], str(expected_run_id)):
        raise RunPayloadContextMismatch("run_id")
    if not _ct_eq(env["step_id"], _norm(expected_step_id)):
        raise RunPayloadContextMismatch("step_id")
    if not _ct_eq(env["payload_type"], str(expected_payload_type)):
        raise RunPayloadContextMismatch("payload_type")
    if not _ct_eq(env["action_hash"], _norm(expected_action_hash)):
        raise RunPayloadActionHashMismatch(
            "run payload is bound to a different approved action"
        )

    return env["payload"]


def _parse_envelope(canonical: bytes) -> dict:
    """Decode + strictly validate the decrypted envelope. Raises typed,
    non-secret errors on any structural problem."""
    try:
        text = canonical.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunPayloadEncodingError("decrypted run payload is not valid UTF-8") from exc
    try:
        env = json.loads(text)
    except (ValueError, TypeError, RecursionError) as exc:
        raise RunPayloadEnvelopeError("decrypted run payload is not valid JSON") from exc
    if not isinstance(env, dict):
        raise RunPayloadEnvelopeError("decrypted run payload envelope is not a JSON object")
    if set(env) != _ENVELOPE_FIELDS:
        raise RunPayloadEnvelopeError(
            "decrypted run payload envelope has missing or extra fields"
        )
    if (isinstance(env["version"], bool) or not isinstance(env["version"], int)
            or env["version"] != ENVELOPE_VERSION):
        raise RunPayloadEnvelopeVersionError(
            f"unsupported run payload envelope version {env['version']!r}"
        )
    if isinstance(env["user_id"], bool) or not isinstance(env["user_id"], int):
        raise RunPayloadEnvelopeError("envelope user_id must be an integer")
    for f in ("run_id", "payload_type", "payload_encoding", "payload"):
        if not isinstance(env[f], str):
            raise RunPayloadEnvelopeError(f"envelope {f} must be a string")
    for f in ("tenant_id", "step_id", "action_hash"):
        if env[f] is not None and not isinstance(env[f], str):
            raise RunPayloadEnvelopeError(f"envelope {f} must be a string or null")
    if env["payload_encoding"] != _PAYLOAD_ENCODING:
        raise RunPayloadEncodingError(
            f"unsupported run payload encoding {env['payload_encoding']!r}"
        )
    return env


def purge_payload(db: Session, payload_id: str, *, commit: bool = True) -> bool:
    """Hard-delete a sealed payload (crypto-shredding). Returns True if a row
    was removed."""
    row = db.get(RunPayload, payload_id)
    if row is None:
        return False
    db.delete(row)
    if commit:
        db.commit()
    return True
