"""MP-4C — backend↔executor GRANT contract: tests FIRST (RED before implementation).

The shared protocol lives in `mp4c_contract`, importable by BOTH the application
(derivation) and the executor (verification) without either importing the other.

Decisions pinned here (from the approved design):
  * ONE backend↔executor signing secret; the SIGNED GRANT is the credential.
  * TTL 120 s, clock skew ±30 s; exp - iat <= 120; excessive future iat refused.
  * Full claim set bound, plus iat; exact code bytes bound by sha256 + length.
  * The static token map is PROTOTYPE-ONLY and must not be a production fallback.
"""
from __future__ import annotations

import time

import pytest

from mp4c_contract import grant as g

KEY = b"grant-key-0123456789abcdef"          # >= 16 bytes, test-only
OTHER_KEY = b"a-different-key-0123456789"

CODE_A = "print('user-a')"
CODE_B = "print('user-b')"
CANARY = "MP4C_GRANT_CANARY_" + "g" * 12


def _claims(**over):
    now = int(time.time())
    base = {
        "v": g.GRANT_VERSION,
        "jti": "jti-" + "a" * 16,
        "iat": now,
        "exp": now + 120,
        "user_id": 7,
        "tenant_id": None,
        "run_id": "run-1",
        "step_id": "step-1",
        "tool_call_id": "tc-1",
        "approval_id": "appr-1",
        "action_hash": "a" * 64,
        "code_sha256": g.code_sha256(CODE_A)[0],
        "code_len": g.code_sha256(CODE_A)[1],
    }
    base.update(over)
    return g.GrantClaims.from_claims(base)


def _token(**over):
    return g.issue(_claims(**over), KEY)


# ── two distinct legitimate users ───────────────────────────────────


def test_two_distinct_users_produce_two_independently_valid_grants():
    a = _token(user_id=7)
    b = _token(user_id=8, jti="jti-" + "b" * 16)
    ca = g.parse_and_verify(a, KEY, code=CODE_A)
    cb = g.parse_and_verify(b, KEY, code=CODE_A)
    assert (ca.user_id, cb.user_id) == (7, 8)
    assert ca.jti != cb.jti


# ── substitution is detected by the signature ───────────────────────


def test_cross_user_substitution_is_rejected():
    token = _token(user_id=7)
    forged = _claims(user_id=8)
    with pytest.raises(g.GrantError) as exc:
        g.verify_claims(token, forged, KEY, code=CODE_A)
    assert exc.value.code == g.BAD_SIGNATURE


def test_tenant_substitution_is_rejected():
    token = _token(user_id=7, tenant_id="t-1")
    forged = _claims(user_id=7, tenant_id="t-2")
    with pytest.raises(g.GrantError) as exc:
        g.verify_claims(token, forged, KEY, code=CODE_A)
    assert exc.value.code == g.BAD_SIGNATURE


def test_a_grant_for_one_user_does_not_verify_for_another_users_claims():
    a = _token(user_id=7)
    b = _claims(user_id=8, jti="jti-" + "b" * 16)
    with pytest.raises(g.GrantError):
        g.verify_claims(a, b, KEY, code=CODE_A)


# ── code binding ────────────────────────────────────────────────────


def test_code_substitution_is_rejected():
    token = _token()                       # bound to CODE_A
    with pytest.raises(g.GrantError) as exc:
        g.parse_and_verify(token, KEY, code=CODE_B)
    assert exc.value.code == g.CODE_BINDING_MISMATCH


def test_exact_bytes_are_bound_not_a_normalised_form():
    """A trailing newline is a DIFFERENT program and must not verify."""
    token = _token(code_sha256=g.code_sha256(CODE_A)[0],
                   code_len=g.code_sha256(CODE_A)[1])
    with pytest.raises(g.GrantError) as exc:
        g.parse_and_verify(token, KEY, code=CODE_A + "\n")
    assert exc.value.code == g.CODE_BINDING_MISMATCH


def test_code_length_is_bound_as_well_as_the_hash():
    sha, ln = g.code_sha256(CODE_A)
    claims = _claims(code_sha256=sha, code_len=ln + 1)
    with pytest.raises(g.GrantError) as exc:
        g.verify_claims(g.issue(claims, KEY), claims, KEY, code=CODE_A)
    assert exc.value.code == g.CODE_BINDING_MISMATCH


# ── authentication ──────────────────────────────────────────────────


def test_missing_credential_is_rejected():
    for empty in (None, "", "   "):
        with pytest.raises(g.GrantError) as exc:
            g.parse_and_verify(empty, KEY, code=CODE_A)
        assert exc.value.code == g.MALFORMED


def test_wrong_key_signature_is_rejected():
    token = g.issue(_claims(), OTHER_KEY)
    with pytest.raises(g.GrantError) as exc:
        g.parse_and_verify(token, KEY, code=CODE_A)
    assert exc.value.code == g.BAD_SIGNATURE


def test_malformed_token_shapes_are_rejected():
    for bad in ("not-a-token", "a.b.c", "....", "!!!" + "." + "00" * 32):
        with pytest.raises(g.GrantError):
            g.parse_and_verify(bad, KEY, code=CODE_A)


# ── lifetime / clock ────────────────────────────────────────────────


def test_lifetime_longer_than_the_maximum_is_refused():
    now = int(time.time())
    claims = _claims(iat=now, exp=now + g.MAX_TTL_S + 1)
    with pytest.raises(g.GrantError) as exc:
        g.verify_claims(g.issue(claims, KEY), claims, KEY, code=CODE_A)
    assert exc.value.code == g.LIFETIME_INVALID


def test_expired_grant_is_refused():
    now = int(time.time())
    claims = _claims(iat=now - 300, exp=now - 200)
    with pytest.raises(g.GrantError) as exc:
        g.verify_claims(g.issue(claims, KEY), claims, KEY, code=CODE_A)
    assert exc.value.code == g.EXPIRED


def test_excessive_future_timestamp_is_refused():
    now = int(time.time())
    claims = _claims(iat=now + 600, exp=now + 700)
    with pytest.raises(g.GrantError) as exc:
        g.verify_claims(g.issue(claims, KEY), claims, KEY, code=CODE_A)
    assert exc.value.code == g.LIFETIME_INVALID


def test_within_skew_is_accepted_and_exp_still_bounds_it():
    now = int(time.time())
    fresh = _claims(iat=now - 10, exp=now + 110)
    assert g.verify_claims(g.issue(fresh, KEY), fresh, KEY, code=CODE_A).user_id == 7


# ── required claims ─────────────────────────────────────────────────


@pytest.mark.parametrize("missing", ["jti", "iat", "exp", "user_id", "run_id",
                                     "step_id", "tool_call_id", "action_hash",
                                     "code_sha256", "code_len", "v"])
def test_missing_required_claim_is_rejected(missing):
    raw = _claims().to_claims()
    raw.pop(missing)
    if missing in ("iat", "exp"):
        token = g.issue_raw(raw, KEY)
        with pytest.raises(g.GrantError) as exc:
            g.parse_and_verify(token, KEY, code=CODE_A)
        assert exc.value.code in (g.CLAIM_MISSING, g.MALFORMED)
    else:
        with pytest.raises(g.GrantError) as exc:
            g.GrantClaims.from_claims(raw)
        assert exc.value.code == g.CLAIM_MISSING


def test_blank_identity_or_action_claims_are_rejected():
    for field in ("run_id", "step_id", "tool_call_id", "action_hash", "jti"):
        with pytest.raises(g.GrantError) as exc:
            g.GrantClaims.from_claims(_claims().to_claims() | {field: "   "})
        assert exc.value.code == g.CLAIM_MISSING


def test_tenant_may_be_absent_but_the_key_must_be_present():
    ok = _claims().to_claims()      # tenant_id present, value None
    assert g.GrantClaims.from_claims(ok).tenant_id is None
    without = _claims().to_claims()
    without.pop("tenant_id")
    assert g.GrantClaims.from_claims(without).tenant_id is None  # optional, explicit


# ── the static token map is prototype-only ──────────────────────────


def test_static_token_map_cannot_represent_arbitrary_users():
    """PINNED FINDING: one token covers exactly ONE identity, with no wildcard."""
    from executor_service.identity import IdentityRegistry

    reg = IdentityRegistry({"tok-a": {"user_id": 7},
                            "tok-b": {"user_id": 8, "tenant_id": "t-1"}})
    assert len(reg) == 2
    assert reg.resolve("tok-a").user_id == 7
    assert reg.resolve("tok-b").user_id == 8
    for unknown in ("tok-c", "7", "*", "user:7", "", None):
        assert reg.resolve(unknown) is None, unknown
    assert not hasattr(reg, "for_user")
    assert "user_id" not in reg.resolve("tok-a").__dict__.get("wildcard", "")


def test_production_request_path_does_not_use_the_static_map():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "executor_service", "service.py"),
               encoding="utf-8").read()
    for forbidden in ("IdentityRegistry", "registry_from_env", "load_registry",
                      "EXECUTOR_SERVICE_TOKENS"):
        assert forbidden not in src, forbidden


def test_signing_key_loader_never_returns_a_default_and_stays_env_or_file():
    with pytest.raises(g.GrantError) as exc:
        g.key_from_env({})
    assert exc.value.code == g.KEY_MISSING
    short = {"EXECUTOR_SERVICE_GRANT_KEY": "short"}
    with pytest.raises(g.GrantError):
        g.key_from_env(short)
    ok = {"EXECUTOR_SERVICE_GRANT_KEY": "k" * 32}
    assert g.key_from_env(ok) == b"k" * 32
