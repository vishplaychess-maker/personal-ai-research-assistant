"""MP-4C - the signed backend->executor EXECUTION GRANT (one definition, both sides).

The credential is the GRANT ITSELF: only a holder of the shared signing secret can
produce a signature that verifies. Identity is carried INSIDE the signed claims, so
a browser or a model cannot choose user / tenant / Run / step / action: those come
from durable server-owned rows at derivation time.

Binding: the EXACT code bytes are bound by code_sha256 (hex of the UTF-8 bytes) plus
code_len, so altered code is refused before anything is reserved or started.

Lifetime: exp - iat <= MAX_TTL_S is enforced, a far-future iat is refused, and expiry
is evaluated with a small SKEW allowance.

This module is pure: no I/O, no logging, no secrets in messages. It records no
nonces and makes no replay decision - replay is the durable nonce store's job.

Canonical encoding is LOCAL and stdlib-only (sorted keys, compact separators) so the
executor image needs no application code. All claim values are scalars, so this is
unambiguous for this claim set.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

GRANT_VERSION = "executor-grant/v1"
#: Domain separation for the signing key (mirrors app/run_engine/submission.py).
DOMAIN = b"thunder-ai/executor-grant/v1"
DOMAIN_EXECUTE = DOMAIN
#: A RESERVATION and an EXECUTION are the same claim set under DIFFERENT signing
#: domains, so a reserve credential cannot verify as an execution one. That makes
#: "reservation alone must never run code" hold with NO extra claim.
DOMAIN_RESERVE = b"thunder-ai/executor-reserve/v1"
PURPOSE_EXECUTE = "execute"
PURPOSE_RESERVE = "reserve"
_BOUND_ACTION_DOMAIN = b"mp4c-reservation/v1"

#: Bounded, machine-readable error codes. Never code, never a token, never a
#: message derived from caller data.
MALFORMED = "executor_grant_malformed"
BAD_SIGNATURE = "executor_grant_bad_signature"
EXPIRED = "executor_grant_expired"
LIFETIME_INVALID = "executor_grant_lifetime_invalid"
CLAIM_MISSING = "executor_grant_claim_missing"
CODE_BINDING_MISMATCH = "executor_code_binding_mismatch"
KEY_MISSING = "executor_grant_key_missing"

MAX_TTL_S = 120
SKEW_S = 30
MIN_KEY_BYTES = 16

#: Env-only, exactly like RUN_IDEMPOTENCY_HMAC_KEY: NOT a Settings field, no
#: default, fails closed when absent or trivial. A FILE secret may supply it.
KEY_ENV = "EXECUTOR_SERVICE_GRANT_KEY"
KEY_FILE_ENV = "EXECUTOR_SERVICE_GRANT_KEY_FILE"
DEFAULT_KEY_FILE = "/run/secrets/executor_grant_key"

#: Claims that MUST be present AND non-blank. tenant_id and approval_id must be
#: PRESENT as keys but may be null (tenant_id is always null in this repo).
REQUIRED_CLAIMS = ("v", "jti", "iat", "exp", "user_id", "run_id", "step_id",
                   "tool_call_id", "action_hash", "code_sha256", "code_len")
REQUIRED_NONBLANK = ("v", "jti", "run_id", "step_id", "tool_call_id",
                     "action_hash", "code_sha256")


class GrantError(Exception):
    """Bounded refusal. The code is a slug from this module; the message is fixed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GrantClaims:
    v: str
    jti: str
    iat: int
    exp: int
    user_id: int
    run_id: str
    step_id: str
    tool_call_id: str
    action_hash: str
    code_sha256: str
    code_len: int
    tenant_id: Optional[str] = None
    approval_id: Optional[str] = None

    def to_claims(self) -> Dict:
        return {
            "v": self.v, "jti": self.jti, "iat": self.iat, "exp": self.exp,
            "user_id": self.user_id, "tenant_id": self.tenant_id,
            "run_id": self.run_id, "step_id": self.step_id,
            "tool_call_id": self.tool_call_id, "approval_id": self.approval_id,
            "action_hash": self.action_hash, "code_sha256": self.code_sha256,
            "code_len": self.code_len,
        }

    @classmethod
    def from_claims(cls, raw: Mapping) -> "GrantClaims":
        if not isinstance(raw, Mapping):
            raise GrantError(MALFORMED)
        for key in REQUIRED_CLAIMS:
            if key not in raw:
                raise GrantError(CLAIM_MISSING)
        for key in REQUIRED_NONBLANK:
            value = raw.get(key)
            if not isinstance(value, str) or not value.strip():
                raise GrantError(CLAIM_MISSING)
        for key in ("iat", "exp", "user_id", "code_len"):
            value = raw.get(key)
            if not isinstance(value, int) or isinstance(value, bool):
                raise GrantError(CLAIM_MISSING)
        for key in ("tenant_id", "approval_id"):
            value = raw.get(key)
            if value is not None and (not isinstance(value, str)
                                      or not value.strip()):
                raise GrantError(CLAIM_MISSING)
        return cls(
            v=raw["v"], jti=raw["jti"], iat=int(raw["iat"]), exp=int(raw["exp"]),
            user_id=int(raw["user_id"]), tenant_id=raw.get("tenant_id"),
            run_id=raw["run_id"], step_id=raw["step_id"],
            tool_call_id=raw["tool_call_id"], approval_id=raw.get("approval_id"),
            action_hash=raw["action_hash"], code_sha256=raw["code_sha256"],
            code_len=int(raw["code_len"]),
        )


def code_sha256(code: str) -> Tuple[str, int]:
    """(hex sha256 of the EXACT UTF-8 bytes, byte length)."""
    data = code.encode("utf-8")
    return hashlib.sha256(data).hexdigest(), len(data)


def canonical_bytes(raw: Mapping) -> bytes:
    return json.dumps(raw, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _domain(purpose: str) -> bytes:
    if purpose == PURPOSE_RESERVE:
        return DOMAIN_RESERVE
    if purpose == PURPOSE_EXECUTE:
        return DOMAIN_EXECUTE
    raise GrantError(MALFORMED)


def _signature(payload_b64: str, key: bytes, domain: bytes = DOMAIN) -> str:
    return hmac.new(key, domain + b"\x00" + payload_b64.encode("ascii"),
                    hashlib.sha256).hexdigest()


def issue(claims: GrantClaims, key: bytes, *,
          purpose: str = PURPOSE_EXECUTE) -> str:
    """Signed token: b64url(canonical_json) + dot + hmac_hex."""
    return issue_raw(claims.to_claims(), key, purpose=purpose)


def issue_raw(raw: Mapping, key: bytes, *,
              purpose: str = PURPOSE_EXECUTE) -> str:
    """Sign an arbitrary claim mapping (used to build deliberately-broken tokens)."""
    payload = _b64e(canonical_bytes(raw))
    return payload + "." + _signature(payload, key, _domain(purpose))


def bound_action_key(claims: GrantClaims) -> str:
    """Deterministic key for the BOUND ACTION a reservation is held against.

    Built from the durable-identity + code-binding claims only (never the token's
    own jti/iat/exp), so a later execution grant carrying the SAME bound action
    matches the reservation without any new claim.
    """
    # NOTE: action_hash is deliberately EXCLUDED. The gateway's committed action
    # hash and the proposal-time ToolCall.args_hash are different computations, so
    # a key containing it would never match between reserve and execute. The
    # server-owned UNIQUE tool_call_id already pins the action.
    raw = {
        "user_id": claims.user_id, "tenant_id": claims.tenant_id,
        "run_id": claims.run_id, "step_id": claims.step_id,
        "tool_call_id": claims.tool_call_id, "approval_id": claims.approval_id,
        "code_sha256": claims.code_sha256, "code_len": claims.code_len,
    }
    return hashlib.sha256(_BOUND_ACTION_DOMAIN + b"\x00"
                          + canonical_bytes(raw)).hexdigest()


def _split(token: object) -> Tuple[str, str]:
    if not isinstance(token, str) or not token.strip():
        raise GrantError(MALFORMED)
    parts = token.strip().split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise GrantError(MALFORMED)
    return parts[0], parts[1]


def _decode(token: object, key: bytes,
            purpose: str = PURPOSE_EXECUTE) -> Mapping:
    """Format -> signature -> decoded claim mapping. Raises GrantError."""
    payload, signature = _split(token)
    expected = _signature(payload, key, _domain(purpose))
    if not hmac.compare_digest(signature, expected):
        raise GrantError(BAD_SIGNATURE)
    try:
        raw = json.loads(_b64d(payload).decode("utf-8"))
    except Exception:  # noqa: BLE001 - any decode failure is malformed input
        raise GrantError(MALFORMED) from None
    if not isinstance(raw, Mapping):
        raise GrantError(MALFORMED)
    return raw


def verify_claims(token: object, claims: GrantClaims, key: bytes, *,
                  code: Optional[str] = None,
                  now: Optional[int] = None,
                  purpose: str = PURPOSE_EXECUTE) -> GrantClaims:
    """Verify a token against EXACTLY the claims the caller believes it holds.

    The presented claims must EQUAL the signed ones. Without this check a caller
    could pass a substituted identity (cross-user, cross-tenant, different action)
    and have it accepted merely because the token itself is well signed - the
    substitution hole this test suite exists to catch.
    """
    raw = _decode(token, key, purpose)
    if canonical_bytes(raw) != canonical_bytes(claims.to_claims()):
        raise GrantError(BAD_SIGNATURE)
    _check_lifetime(claims.iat, claims.exp, now)
    if code is not None:
        _check_code(claims, code)
    return claims


def parse_and_verify(token: object, key: bytes, *, code: Optional[str] = None,
                     now: Optional[int] = None,
                     purpose: str = PURPOSE_EXECUTE) -> GrantClaims:
    """Full verification: format -> signature -> claims -> lifetime -> code binding."""
    raw = _decode(token, key, purpose)
    if raw.get("v") != GRANT_VERSION:
        raise GrantError(MALFORMED)
    claims = GrantClaims.from_claims(raw)
    _check_lifetime(claims.iat, claims.exp, now)
    if code is not None:
        _check_code(claims, code)
    return claims


def _check_lifetime(iat: int, exp: int, now: Optional[int]) -> None:
    current = int(time.time()) if now is None else int(now)
    if exp < iat:
        raise GrantError(LIFETIME_INVALID)
    if exp - iat > MAX_TTL_S:
        raise GrantError(LIFETIME_INVALID)
    if iat > current + SKEW_S:
        # An excessive future timestamp could otherwise outlive the window.
        raise GrantError(LIFETIME_INVALID)
    if exp < current - SKEW_S:
        raise GrantError(EXPIRED)


def _check_code(claims: GrantClaims, code: str) -> None:
    sha, length = code_sha256(code)
    if not (hmac.compare_digest(sha, claims.code_sha256)
            and length == claims.code_len):
        raise GrantError(CODE_BINDING_MISMATCH)


def key_from_env(env: Optional[Mapping] = None) -> bytes:
    """The ONE backend<->executor signing secret. Env or FILE; NO default.

    A present-but-trivial key (< 16 bytes) is treated as unconfigured. The value
    is never logged, never returned in an error and never placed in a claim.
    """
    e = env if env is not None else os.environ
    raw = str(e.get(KEY_ENV) or "").strip()
    if not raw:
        path = str(e.get(KEY_FILE_ENV) or DEFAULT_KEY_FILE)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
        except OSError:
            raw = ""
    if len(raw.encode("utf-8")) < MIN_KEY_BYTES:
        raise GrantError(KEY_MISSING)
    return raw.encode("utf-8")


__all__ = [
    "GRANT_VERSION", "DOMAIN", "MAX_TTL_S", "SKEW_S", "MIN_KEY_BYTES", "KEY_ENV",
    "KEY_FILE_ENV", "DEFAULT_KEY_FILE", "MALFORMED", "BAD_SIGNATURE", "EXPIRED",
    "LIFETIME_INVALID", "CLAIM_MISSING", "CODE_BINDING_MISMATCH", "KEY_MISSING",
    "GrantError", "GrantClaims", "code_sha256", "canonical_bytes", "issue",
    "issue_raw", "verify_claims", "parse_and_verify", "key_from_env",
    "PURPOSE_RESERVE", "PURPOSE_EXECUTE", "DOMAIN_RESERVE", "DOMAIN_EXECUTE",
    "bound_action_key",
]
