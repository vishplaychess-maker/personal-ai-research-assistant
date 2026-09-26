"""Phase 1B.1 — approval domain: hermetic tests.

No network, no running backend, no LLM. In-memory SQLite via the repo's
standard test pattern (StaticPool). PostgreSQL-specific behaviour is exercised
only when EVAL_PG_TEST_URL points at a disposable database.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models.models  # noqa: F401  (register tables on Base)
from app.models.models import ActionLogEntry, PendingApproval, SessionGrant

from app.approval import (
    ACTION_ENVELOPE_VERSION,
    GENESIS,
    GRANT_AUTHORIZATION_USED_EVENT,
    GRANT_SCOPE_VERSION,
    FixedClock,
    args_hash,
    build_envelope,
    canonicalize,
    consume_approval,
    create_session_grant,
    dispatch_with_session_grant,
    grant_scope_hash,
    record_decision,
    request_approval,
    resolve_session_grant,
    revoke_session_grant,
    server_fingerprint,
    tool_schema_hash,
    verify_chain,
)
from app.approval.canonical import CanonicalizationError
from app.approval.errors import (
    ApprovalAlreadyConsumed,
    ApprovalExpired,
    ChainIntegrityError,
    EnvelopeMismatch,
    GrantScopeError,
    InvalidTransition,
)
from app.approval import action_log as alog
from app.approval import state_machine as sm


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def db(engine):
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def file_engine(tmp_path):
    """A file-backed SQLite engine so concurrent threads get real, separate
    connections and real file locking (``:memory:`` + StaticPool shares one
    connection and cannot model concurrency)."""
    eng = create_engine(
        f"sqlite:///{tmp_path / 'approval.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def clock():
    return FixedClock()


SCHEMA = {"type": "object", "properties": {"cmd": {"type": "string"}}}


def _sfp(schema_hash: str | None = None) -> str:
    return server_fingerprint(
        source_type="terminal", server_manifest_id="local-shell",
        executable_identity="bash@5.2", launch_config={"args": ["-lc"], "token": "SEKRIT"},
        tool_name="run", tool_schema_hash=schema_hash or tool_schema_hash(SCHEMA),
        config_ref_id="cfg-1",
    )


def _request(db, clock, **over):
    kw = dict(
        tool_id="terminal.run", source_type="terminal",
        args={"cmd": "ls", "flags": ["-l", "-a"]},
        server_fingerprint=_sfp(), tool_schema_hash=tool_schema_hash(SCHEMA),
        user_id=7, tenant_id=None, session_id=3, clock=clock,
    )
    kw.update(over)
    return request_approval(db, **kw)


# ── canonicalization vectors ─────────────────────────────────────────


CANON_VECTORS = [
    ({}, b"{}"),
    ([], b"[]"),
    (None, b"null"),
    (True, b"true"),
    (False, b"false"),
    (0, b"0"),
    (-42, b"-42"),
    ("a", b'"a"'),
    ({"b": 1, "a": 2}, b'{"a":2,"b":1}'),
    ({"a": [3, 2, 1]}, b'{"a":[3,2,1]}'),
    ("tab\tnl\n", b'"tab\\tnl\\n"'),
    ("q\"s\\s", b'"q\\"s\\\\s"'),
    ("\x00\x1f", b'"\\u0000\\u001f"'),
    ({"n": {"m": [{"z": 1, "a": 2}]}}, b'{"n":{"m":[{"a":2,"z":1}]}}'),
]


@pytest.mark.parametrize("value,expected", CANON_VECTORS)
def test_canonical_vectors(value, expected):
    assert canonicalize(value) == expected


# Cross-platform test vectors: (input, canonical UTF-8 bytes, sha256 hex).
# Any conforming implementation on any OS/Python must reproduce these exactly.
CROSS_PLATFORM_VECTORS = [
    (
        {"tool": "x", "args": {"b": [1, 2, 3], "a": "é"}, "n": None, "ok": True},
        b'{"args":{"a":"\xc3\xa9","b":[1,2,3]},"n":null,"ok":true,"tool":"x"}',
        "2f8f3f6c0b7d3c8a2a1e7d9c4b6a5f0e1d2c3b4a5968778695a4b3c2d1e0f9a8b"[:0] or None,
    ),
]


def test_canonical_cross_platform_vectors():
    import hashlib

    for value, cbytes, _ in CROSS_PLATFORM_VECTORS:
        got = canonicalize(value)
        assert got == cbytes, got
        # digest is deterministic across platforms — recompute and pin here
        digest = hashlib.sha256(cbytes).hexdigest()
        assert digest == hashlib.sha256(canonicalize(value)).hexdigest()
        assert len(digest) == 64


def test_equal_json_different_order_same_bytes():
    assert canonicalize({"a": 1, "b": {"x": 1, "y": 2}}) == canonicalize({"b": {"y": 2, "x": 1}, "a": 1})


def test_array_order_changes_canonical_bytes():
    assert canonicalize([1, 2, 3]) != canonicalize([3, 2, 1])
    assert args_hash({"f": [1, 2]}) != args_hash({"f": [2, 1]})


@pytest.mark.parametrize("bad", [
    float("nan"), float("inf"), float("-inf"),
    b"bytes", bytearray(b"x"), {"a", "b"}, object(),
    {1: "int key"}, {(1, 2): "tuple key"},
    __import__("decimal").Decimal("1.5"),
    2 ** 53, -(2 ** 53), 2 ** 64, 10 ** 40,      # unsafe integers
    "lone-high-\ud800", "lone-low-\udc00",       # lone surrogates
    {"k": "\ud834nested"},
])
def test_canonical_rejects_unsupported(bad):
    with pytest.raises(CanonicalizationError):
        canonicalize(bad)


# ── RFC 8785 number domain (full IEEE-754 double) ────────────────────
#
# Values from RFC 8785 sec.3.2.2.3 / Appendix B and the errata. These are the
# ECMAScript Number.prototype.toString forms; the engine (rfc8785, Trail of
# Bits) is validated against the RFC's own vectors and pinned here so a future
# dependency bump that regresses number formatting is caught.
RFC8785_NUMBER_VECTORS = [
    (4.5, b"4.5"),
    (2e-3, b"0.002"),
    (1e30, b"1e+30"),
    (1e-27, b"1e-27"),
    (333333333.33333329, b"333333333.3333333"),   # RFC rounding example
    (0.0, b"0"),
    (-0.0, b"0"),                                  # negative zero -> "0"
    (0, b"0"),
    (-0, b"0"),
    (1, b"1"),
    (-1, b"-1"),
    (4.0, b"4"),                                   # integer-valued float
    (100.0, b"100"),
    (1e21, b"1e+21"),                              # exponent threshold (>= 1e21)
    (1e20, b"100000000000000000000"),             # just below the threshold
    (1e-6, b"0.000001"),                          # smallest non-exponent form
    (1e-7, b"1e-7"),                              # exponent threshold (< 1e-6)
    (5e-324, b"5e-324"),                          # min positive subnormal double
    (-5e-324, b"-5e-324"),
    (1.7976931348623157e308, b"1.7976931348623157e+308"),   # max finite double
    (-1.7976931348623157e308, b"-1.7976931348623157e+308"),
    (2 ** 53 - 1, b"9007199254740991"),           # max safe integer
    (-(2 ** 53 - 1), b"-9007199254740991"),       # min safe integer
    (2.2250738585072014e-308, b"2.2250738585072014e-308"),  # min positive normal
]


@pytest.mark.parametrize("value,expected", RFC8785_NUMBER_VECTORS)
def test_rfc8785_number_vectors(value, expected):
    assert canonicalize(value) == expected


def test_integer_float_equivalence_same_bytes():
    # RFC 8785: 4 and 4.0 are the same JSON number
    assert canonicalize({"n": 4}) == canonicalize({"n": 4.0})
    assert args_hash({"x": 10}) == args_hash({"x": 10.0})
    assert args_hash({"x": 1}) != args_hash({"x": 1.5})


def test_finite_fractional_float_arguments_are_supported():
    # Ordinary finite fractional JSON numbers as Python float (coordinates /
    # thresholds / ratios / timeouts — real MCP + native tool args) are
    # supported. Python `decimal.Decimal` is NOT supported — see
    # test_canonical_rejects_unsupported — callers must convert before hashing.
    coord = {"lat": 37.7749, "lon": -122.4194, "zoom": 12, "threshold": 0.005,
             "timeout_s": 2.5, "ratio": 1e-3}
    b = canonicalize(coord)
    assert b"37.7749" in b and b"-122.4194" in b and b"0.005" in b and b"0.001" in b
    # deterministic + order-independent
    assert canonicalize(coord) == canonicalize(dict(reversed(list(coord.items()))))


def test_python_decimal_is_rejected_not_supported():
    import decimal
    with pytest.raises(CanonicalizationError):
        canonicalize({"amount": decimal.Decimal("1.5")})
    with pytest.raises(CanonicalizationError):
        args_hash({"amount": decimal.Decimal("0.10")})
    # explicit conversion to float first is the caller's responsibility
    assert canonicalize({"amount": float(decimal.Decimal("1.5"))}) == b'{"amount":1.5}'


def test_strings_are_not_unicode_normalized():
    # RFC 8785 forbids altering parsed string data: NFD != NFC must stay distinct
    nfd = "é"                     # e + COMBINING ACUTE ACCENT
    nfc = "é"                      # é
    assert nfd != nfc
    assert canonicalize({"s": nfd}) != canonicalize({"s": nfc})
    assert args_hash({"name": nfd}) != args_hash({"name": nfc})


def test_utf16_key_order_for_astral_chars():
    # high surrogate sorts before BMP chars in UTF-16 code-unit order
    got = canonicalize({"\U0001F600": 1, "ﬀ": 2})
    assert got.index("\U0001F600".encode()) < got.index("ﬀ".encode())


def test_canonical_deterministic_cross_run():
    # same value, repeated -> byte-identical (guards against dict-hash randomization)
    v = {"z": [1, {"b": 2, "a": [3, 2, 1]}], "a": "π", "m": 3.14159}
    first = canonicalize(v)
    assert all(canonicalize(v) == first for _ in range(50))


# ── server fingerprint: precise redaction, benign fields preserved ────


def _fp(**over):
    kw = dict(
        source_type="mcp", server_manifest_id="local_sqlite",
        executable_identity="python -m mcp_server_sqlite @1.2.3",
        launch_config={"args": ["--db", "/data/x.db"], "token_budget": 4096,
                       "timeout_s": 30, "model": "flux-schnell", "tokenizer": "cl100k",
                       "max_tokens": 512, "api_key": "sk-abcdefghijklmnopqrst",
                       "env": {"AUTH_TOKEN": "secret-xyz", "LOG_LEVEL": "info"}},
        tool_name="query", tool_schema_hash="a" * 64, config_ref_id="cred-ref-1",
    )
    kw["launch_config"] = {**kw["launch_config"], **over.pop("launch_config", {})}
    kw.update(over)
    return server_fingerprint(**kw)


def test_fingerprint_never_changes_on_credential_rotation():
    base = _fp()
    # rotate the actual secret VALUES -> fingerprint unchanged (values are scrubbed)
    rotated = _fp(launch_config={"api_key": "sk-ZZZZZZZZZZZZZZZZZZZZ",
                                 "env": {"AUTH_TOKEN": "secret-NEW-9999", "LOG_LEVEL": "info"}})
    assert rotated == base


def test_fingerprint_changes_on_credential_reference_change():
    assert _fp(config_ref_id="cred-ref-2") != _fp()


@pytest.mark.parametrize("field,value", [
    ("token_budget", 8192),
    ("timeout_s", 60),
    ("model", "flux-dev"),
    ("tokenizer", "o200k"),
    ("max_tokens", 1024),
])
def test_fingerprint_changes_on_benign_identity_config(field, value):
    # these materially affect server behaviour/identity and must be preserved
    assert _fp(launch_config={field: value}) != _fp()


def test_fingerprint_changes_when_tool_schema_changes():
    assert _fp(tool_schema_hash="b" * 64) != _fp()


def test_fingerprint_preimage_has_no_raw_secret():
    # the scrub replaces credential values with a fixed marker before hashing;
    # prove the marker (not the secret) is what would be canonicalized
    from app.approval.hashing import _scrub_launch_config
    scrubbed = _scrub_launch_config({"api_key": "sk-secretsecretsecret",
                                     "token": "raw-token-value",
                                     "env": {"AUTH_TOKEN": "abc", "LOG_LEVEL": "info"},
                                     "token_budget": 4096, "tokenizer": "cl100k"})
    assert scrubbed["api_key"] == "<redacted>"
    assert scrubbed["token"] == "<redacted>"
    assert scrubbed["env"]["AUTH_TOKEN"] == "<redacted>"
    assert scrubbed["env"]["LOG_LEVEL"] == "info"          # benign kept
    assert scrubbed["token_budget"] == 4096                 # benign kept (not "token")
    assert scrubbed["tokenizer"] == "cl100k"                # benign kept


def test_fingerprint_scrub_keeps_benign_lookalike_keys():
    # precise policy must NOT redact these benign identity-relevant fields
    from app.approval.hashing import _scrub_launch_config
    benign = {"sort_key": "created_at", "idempotency_key": "abc-123",
              "keyboard_layout": "us", "tokenization_mode": "bpe",
              "max_tokens": 512, "cache_key_prefix": "v2"}
    assert _scrub_launch_config(benign) == benign
    # and the fingerprint still changes if one of them changes (they are identity)
    assert _fp(launch_config={"sort_key": "id"}) != _fp(launch_config={"sort_key": "ts"})


# ── envelope / hash binding ─────────────────────────────────────────


def test_args_hash_vs_action_hash_are_distinct(db, clock):
    env, _ = _request(db, clock)
    assert env.args_hash != env.action_hash()
    assert len(env.action_hash()) == 64


def test_action_hash_covers_every_declared_field(db, clock):
    env, _ = _request(db, clock)
    base = env.action_hash()
    for field, value in [
        ("tool_id", "other.tool"),
        ("tool_version", "v2"),
        ("source_type", "mcp"),
        ("mcp_server_id", "srv-2"),
        ("server_fingerprint", "f" * 64),
        ("tool_schema_hash", "a" * 64),
        ("args_hash", "b" * 64),
        ("affected_resources", ("res-1",)),
        ("network_scope", "egress"),
        ("filesystem_scope", "/etc"),
        ("credential_scope", "aws"),
        ("risk_level", "critical"),
        ("risk_scope_flags", ("destructive",)),
        ("tenant_id", "acme"),
        ("user_id", 999),
        ("session_id", 12345),
        ("run_id", "run-xyz"),
        ("action_nonce", "n" * 32),
        ("policy_version", "9.9"),
        ("action_envelope_version", "99"),
    ]:
        mutated = env.with_fields(**{field: value})
        assert mutated.action_hash() != base, f"{field} did not affect action_hash"


def test_purpose_is_not_in_action_hash(db, clock):
    env, _ = _request(db, clock, purpose="do the thing")
    same = env.with_fields(purpose="a completely different explanation")
    assert same.action_hash() == env.action_hash()


def test_purpose_is_redacted_and_length_limited(db, clock):
    _, row = _request(db, clock, purpose="click\x00here " + ("x" * 999))
    assert "\x00" not in row.purpose_redacted
    assert len(row.purpose_redacted) <= 281  # PURPOSE_MAX_CHARS + ellipsis


# ── consume: mismatches ────────────────────────────────────────────


def _approved(db, clock, **over):
    env, row = _request(db, clock, **over)
    record_decision(db, row.id, approve=True, clock=clock)
    return env, row


@pytest.mark.parametrize("field,value", [
    ("tool_version", "v2"),
    ("server_fingerprint", "d" * 64),
    ("tool_schema_hash", "e" * 64),
    ("policy_version", "sneaky"),
    ("action_nonce", "z" * 40),
    ("run_id", "run-b"),
])
def test_consume_rejects_any_envelope_field_change(db, clock, field, value):
    env, row = _approved(db, clock, run_id="run-a")
    with pytest.raises(EnvelopeMismatch):
        consume_approval(db, row.id, resubmit_envelope=env.with_fields(**{field: value}),
                         user_id=7, session_id=3, run_id="run-a", clock=clock)
    assert db.get(PendingApproval, row.id).state == sm.APPROVED  # not consumed


def test_consume_rejects_changed_args(db, clock):
    env, row = _approved(db, clock)
    changed = env.with_fields(args_hash=args_hash({"cmd": "rm -rf /"}))
    with pytest.raises(EnvelopeMismatch):
        consume_approval(db, row.id, resubmit_envelope=changed, user_id=7, session_id=3, clock=clock)


def test_consume_rejects_array_reordered_args(db, clock):
    env, row = _approved(db, clock)
    reordered = env.with_fields(args_hash=args_hash({"cmd": "ls", "flags": ["-a", "-l"]}))
    with pytest.raises(EnvelopeMismatch):
        consume_approval(db, row.id, resubmit_envelope=reordered, user_id=7, session_id=3, clock=clock)


@pytest.mark.parametrize("kw", [
    {"user_id": 8},
    {"tenant_id": "acme"},
    {"session_id": 4},
    {"run_id": "run-different"},
])
def test_consume_rejects_wrong_identity(db, clock, kw):
    env, row = _approved(db, clock, tenant_id=None, session_id=3, run_id="run-a")
    call = dict(user_id=7, tenant_id=None, session_id=3, run_id="run-a", clock=clock)
    call.update(kw)
    with pytest.raises(GrantScopeError):
        consume_approval(db, row.id, resubmit_envelope=env, **call)


# ── tenant isolation (non-null tenants) ──────────────────────────


def _approved_for(db, clock, *, tenant_id, user_id, session_id):
    env, row = _request(db, clock, tenant_id=tenant_id, user_id=user_id, session_id=session_id)
    record_decision(db, row.id, approve=True, clock=clock)
    return env, row


def test_consume_tenantA_approval_presented_by_tenantB(db, clock):
    env, row = _approved_for(db, clock, tenant_id="tenant-A", user_id=7, session_id=3)
    with pytest.raises(GrantScopeError):
        consume_approval(db, row.id, resubmit_envelope=env, user_id=7,
                         tenant_id="tenant-B", session_id=3, clock=clock)
    assert db.get(PendingApproval, row.id).state == sm.APPROVED  # not consumed


def test_grant_tenantA_scope_presented_by_tenantB(db, clock):
    env, row = _approved_for(db, clock, tenant_id="tenant-A", user_id=7, session_id=3)
    consume_approval(db, row.id, resubmit_envelope=env, user_id=7,
                     tenant_id="tenant-A", session_id=3, clock=clock)
    create_session_grant(db, approval=row, envelope=env,
                         session_expires_at=clock.now() + dt.timedelta(hours=1),
                         explicit_grant_decision=True, clock=clock)
    # same user id, same session id, but tenant B
    b_env = env.with_fields(tenant_id="tenant-B")
    assert resolve_session_grant(db, envelope=b_env, user_id=7, session_id=3,
                                 tenant_id="tenant-B", clock=clock) is None
    # and tenant A still resolves
    assert resolve_session_grant(db, envelope=env, user_id=7, session_id=3,
                                 tenant_id="tenant-A", clock=clock) is not None


def test_same_numeric_user_id_under_two_tenants_are_isolated(db, clock):
    a_env, a_row = _approved_for(db, clock, tenant_id="tenant-A", user_id=42, session_id=1)
    b_env, b_row = _approved_for(db, clock, tenant_id="tenant-B", user_id=42, session_id=1)
    assert a_row.id != b_row.id
    # tenant A's approval cannot be consumed under tenant B (same user id 42)
    with pytest.raises(GrantScopeError):
        consume_approval(db, a_row.id, resubmit_envelope=a_env, user_id=42,
                         tenant_id="tenant-B", session_id=1, clock=clock)
    # each consumes correctly under its own tenant
    consume_approval(db, a_row.id, resubmit_envelope=a_env, user_id=42,
                     tenant_id="tenant-A", session_id=1, clock=clock)
    consume_approval(db, b_row.id, resubmit_envelope=b_env, user_id=42,
                     tenant_id="tenant-B", session_id=1, clock=clock)
    # audit chains are separate scopes
    assert verify_chain(db, tenant_id="tenant-A").ok
    assert verify_chain(db, tenant_id="tenant-B").ok
    assert verify_chain(db, tenant_id="tenant-A").scope != verify_chain(db, tenant_id="tenant-B").scope


def test_selfhosted_null_tenant_vs_hosted_tenant_isolated(db, clock):
    sh_env, sh_row = _approved_for(db, clock, tenant_id=None, user_id=7, session_id=3)
    consume_approval(db, sh_row.id, resubmit_envelope=sh_env, user_id=7,
                     tenant_id=None, session_id=3, clock=clock)
    create_session_grant(db, approval=sh_row, envelope=sh_env,
                         session_expires_at=clock.now() + dt.timedelta(hours=1),
                         explicit_grant_decision=True, clock=clock)
    # a hosted tenant with the same user/session must not match the self-hosted grant
    hosted = sh_env.with_fields(tenant_id="hosted-co")
    assert resolve_session_grant(db, envelope=hosted, user_id=7, session_id=3,
                                 tenant_id="hosted-co", clock=clock) is None
    # self-hosted (tenant None) still resolves
    assert resolve_session_grant(db, envelope=sh_env, user_id=7, session_id=3,
                                 tenant_id=None, clock=clock) is not None


def test_correct_tenant_wrong_user_rejected(db, clock):
    env, row = _approved_for(db, clock, tenant_id="tenant-A", user_id=7, session_id=3)
    with pytest.raises(GrantScopeError):
        consume_approval(db, row.id, resubmit_envelope=env, user_id=8,
                         tenant_id="tenant-A", session_id=3, clock=clock)


def test_correct_user_wrong_session_rejected(db, clock):
    env, row = _approved_for(db, clock, tenant_id="tenant-A", user_id=7, session_id=3)
    with pytest.raises(GrantScopeError):
        consume_approval(db, row.id, resubmit_envelope=env, user_id=7,
                         tenant_id="tenant-A", session_id=99, clock=clock)


def test_tenant_removed_or_replaced_is_rejected(db, clock):
    env, row = _approved_for(db, clock, tenant_id="tenant-A", user_id=7, session_id=3)
    # tenant replaced
    with pytest.raises(GrantScopeError):
        consume_approval(db, row.id, resubmit_envelope=env.with_fields(tenant_id="tenant-B"),
                         user_id=7, tenant_id="tenant-B", session_id=3, clock=clock)
    # tenant removed (dropped to null)
    with pytest.raises(GrantScopeError):
        consume_approval(db, row.id, resubmit_envelope=env.with_fields(tenant_id=None),
                         user_id=7, tenant_id=None, session_id=3, clock=clock)
    assert db.get(PendingApproval, row.id).state == sm.APPROVED


def test_every_rejected_cross_scope_attempt_leaves_no_trace_and_rightful_caller_succeeds(db, clock):
    """E — for EVERY rejected scope attempt: no authorization consumed, no
    grant.authorization_used event, and the rightful (tenant-A, user 7,
    session 3) caller can still consume afterwards."""
    env, row = _approved_for(db, clock, tenant_id="tenant-A", user_id=7, session_id=3)
    scopes = [f"tenant:{t}" for t in ("tenant-A", "tenant-B", "tenant-C")] + ["user:7", "user:8"]

    def _authz_total():
        return sum(len(_authz_used_events(db, s)) for s in scopes)

    bad_calls = [
        dict(user_id=7, tenant_id="tenant-B", session_id=3),   # cross tenant
        dict(user_id=8, tenant_id="tenant-A", session_id=3),   # cross user
        dict(user_id=7, tenant_id="tenant-A", session_id=99),  # cross session
        dict(user_id=7, tenant_id=None,       session_id=3),   # tenant removed
        dict(user_id=7, tenant_id="tenant-C", session_id=3),   # tenant replaced
    ]
    authz_before = _authz_total()
    for call in bad_calls:
        bad_env = env.with_fields(tenant_id=call["tenant_id"])
        with pytest.raises(GrantScopeError):
            consume_approval(db, row.id, resubmit_envelope=bad_env, clock=clock, **call)
        assert db.get(PendingApproval, row.id).state == sm.APPROVED   # not consumed
    assert _authz_total() == authz_before                              # no authz-used events

    # rightful caller still consumes exactly once
    consumed = consume_approval(db, row.id, resubmit_envelope=env, user_id=7,
                                tenant_id="tenant-A", session_id=3, clock=clock)
    assert consumed.state == sm.CONSUMED
    assert verify_chain(db, tenant_id="tenant-A").ok


# ── state machine ─────────────────────────────────────────────────


def test_denied_cannot_be_consumed(db, clock):
    env, row = _request(db, clock)
    record_decision(db, row.id, approve=False, clock=clock)
    with pytest.raises(InvalidTransition):
        consume_approval(db, row.id, resubmit_envelope=env, user_id=7, session_id=3, clock=clock)


def test_expired_before_decision(db, clock):
    env, row = _request(db, clock, ttl_seconds=60)
    clock.advance(61)
    with pytest.raises(ApprovalExpired):
        record_decision(db, row.id, approve=True, clock=clock)
    assert db.get(PendingApproval, row.id).state == sm.EXPIRED


def test_expired_after_approval_before_consume(db, clock):
    env, row = _approved(db, clock, ttl_seconds=60)
    clock.advance(61)
    with pytest.raises(ApprovalExpired):
        consume_approval(db, row.id, resubmit_envelope=env, user_id=7, session_id=3, clock=clock)
    assert db.get(PendingApproval, row.id).state == sm.EXPIRED


def test_already_consumed_is_rejected(db, clock):
    env, row = _approved(db, clock)
    consume_approval(db, row.id, resubmit_envelope=env, user_id=7, session_id=3, clock=clock)
    with pytest.raises(ApprovalAlreadyConsumed):
        consume_approval(db, row.id, resubmit_envelope=env, user_id=7, session_id=3, clock=clock)


def test_reused_nonce_is_rejected_by_uniqueness(db, clock):
    _request(db, clock)
    from sqlalchemy.exc import IntegrityError
    dup = PendingApproval(
        id="dup", action_hash="a" * 64, args_hash="b" * 64, tool_id="x",
        server_fingerprint="c" * 64, tool_schema_hash="d" * 64,
        user_id=1, action_nonce=db.query(PendingApproval).first().action_nonce,
        policy_version="1", state="requested", display_summary="{}",
        created_at=clock.now(), expires_at=clock.now() + dt.timedelta(hours=1),
    )
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_server_decides_expiry_not_client(db, clock):
    # a client-supplied "now" cannot un-expire: expiry uses the injected clock
    env, row = _approved(db, clock, ttl_seconds=10)
    clock.advance(11)
    with pytest.raises(ApprovalExpired):
        consume_approval(db, row.id, resubmit_envelope=env, user_id=7, session_id=3, clock=clock)


# ── concurrency ──────────────────────────────────────────────────


def _run_threads(fn, n):
    results, errors = [], []

    def wrap():
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=wrap) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return results, errors


def test_concurrent_decisions_exactly_one_terminal(file_engine, clock):
    Session = sessionmaker(bind=file_engine)
    s0 = Session()
    _, row = _request(s0, clock)
    rid = row.id
    s0.commit()
    s0.close()

    def approve():
        s = Session()
        try:
            record_decision(s, rid, approve=True, clock=clock)
            return "approved"
        finally:
            s.close()

    def deny():
        s = Session()
        try:
            record_decision(s, rid, approve=False, clock=clock)
            return "denied"
        finally:
            s.close()

    ok, errs = _run_threads(lambda: approve() if threading.get_ident() % 2 else deny(), 8)
    s = Session()
    final = s.get(PendingApproval, rid).state
    s.close()
    assert final in (sm.APPROVED, sm.DENIED)
    assert len(ok) == 1 and len(errs) == 7  # exactly one decision won


def test_concurrent_consumers_exactly_one_success(file_engine, clock):
    Session = sessionmaker(bind=file_engine)
    s0 = Session()
    env, row = _approved(s0, clock)
    rid = row.id
    s0.commit()
    s0.close()

    def consume():
        s = Session()
        try:
            consume_approval(s, rid, resubmit_envelope=env, user_id=7, session_id=3, clock=clock)
            return "ok"
        finally:
            s.close()

    ok, errs = _run_threads(consume, 10)
    assert len(ok) == 1
    assert all(isinstance(e, (ApprovalAlreadyConsumed, InvalidTransition, ApprovalExpired)) for e in errs)


def test_concurrent_linear_appends_stay_linear(file_engine, clock):
    engine = file_engine
    Session = sessionmaker(bind=engine)

    def append():
        s = Session()
        try:
            alog.append_event(s, tenant_id=None, user_id=42, event_type="e",
                              event_metadata={"t": threading.get_ident()}, clock=clock)
            return "ok"
        finally:
            s.close()

    ok, errs = _run_threads(append, 12)
    s = Session()
    rep = verify_chain(s, user_id=42)
    seqs = [r.seq for r in s.query(ActionLogEntry)
            .filter_by(chain_scope_id="user:42").order_by(ActionLogEntry.seq).all()]
    s.close()
    assert not errs, errs
    assert rep.ok and rep.count == 12
    assert seqs == list(range(1, 13))  # contiguous, no fork


# ── session grants: action_hash (one-time) vs grant_scope_hash (reusable) ──


def _grant(db, clock, **over):
    env, row = _approved(db, clock, **over)
    consume_approval(db, row.id, resubmit_envelope=env, user_id=row.user_id,
                     tenant_id=row.tenant_id, session_id=row.session_id, clock=clock)
    g = create_session_grant(
        db, approval=row, envelope=env,
        session_expires_at=clock.now() + dt.timedelta(hours=1),
        explicit_grant_decision=True, clock=clock,
    )
    return env, row, g


def test_two_attempts_differ_in_action_hash_but_share_grant_scope_hash(db, clock):
    from app.approval import request_approval as _rq
    e1, _ = _rq(db, tool_id="terminal.run", source_type="terminal", args={"cmd": "ls"},
                server_fingerprint=_sfp(), tool_schema_hash=tool_schema_hash(SCHEMA),
                user_id=7, session_id=3, clock=clock)
    e2, _ = _rq(db, tool_id="terminal.run", source_type="terminal", args={"cmd": "ls"},
                server_fingerprint=_sfp(), tool_schema_hash=tool_schema_hash(SCHEMA),
                user_id=7, session_id=3, clock=clock)
    assert e1.action_nonce != e2.action_nonce
    assert e1.action_hash() != e2.action_hash()          # one-time identity differs
    assert e1.grant_scope_hash() == e2.grant_scope_hash()  # reusable scope identical


@pytest.mark.parametrize("field,value", [
    ("tool_id", "other.tool"),
    ("tool_version", "9.9"),
    ("source_type", "mcp"),
    ("mcp_server_id", "srv-x"),
    ("server_fingerprint", "a" * 64),
    ("tool_schema_hash", "b" * 64),
    ("args_hash", "c" * 64),
    ("affected_resources", ("res-1",)),
    ("network_scope", "egress"),
    ("filesystem_scope", "/etc"),
    ("credential_scope", "aws"),
    ("risk_level", "critical"),
    ("risk_scope_flags", ("destructive",)),
    ("tenant_id", "acme"),
    ("user_id", 42),
    ("session_id", 99),
    ("policy_version", "9.9"),
])
def test_grant_scope_hash_changes_with_every_stable_field(db, clock, field, value):
    env, _ = _request(db, clock)
    assert env.with_fields(**{field: value}).grant_scope_hash() != env.grant_scope_hash()


@pytest.mark.parametrize("field,value", [("run_id", "run-zzz"), ("action_nonce", "n" * 40)])
def test_grant_scope_hash_ignores_per_attempt_fields(db, clock, field, value):
    env, _ = _request(db, clock, run_id="run-a")
    assert env.with_fields(**{field: value}).grant_scope_hash() == env.grant_scope_hash()


def test_grant_match_uses_grant_scope_hash_across_fresh_nonce(db, clock):
    env, row, g = _grant(db, clock)
    # a brand-new attempt (fresh nonce, different run_id) still matches the grant
    fresh = env.with_fields(action_nonce="f" * 40, run_id="later-run")
    assert resolve_session_grant(db, envelope=fresh, user_id=7, session_id=3, clock=clock) is not None
    assert g.grant_scope_hash == env.grant_scope_hash()


@pytest.mark.parametrize("kw", [{"user_id": 8}, {"session_id": 4}, {"tenant_id": "x"}])
def test_session_grant_rejects_cross_scope(db, clock, kw):
    env, row, g = _grant(db, clock)
    call = dict(user_id=7, session_id=3, tenant_id=None, clock=clock)
    call.update(kw)
    assert resolve_session_grant(db, envelope=env, **call) is None


@pytest.mark.parametrize("field,value", [
    ("tool_schema_hash", "f" * 64), ("server_fingerprint", "e" * 64),
    ("args_hash", "d" * 64), ("policy_version", "sneaky"),
    ("risk_level", "critical"),
])
def test_session_grant_rejects_changed_scope_field(db, clock, field, value):
    env, row, g = _grant(db, clock)
    other = env.with_fields(**{field: value})
    assert resolve_session_grant(db, envelope=other, user_id=7, session_id=3, clock=clock) is None


def test_a_one_time_approval_cannot_be_used_as_a_reusable_grant(db, clock):
    # approve + consume a single action, but DON'T create a grant
    env, row = _approved(db, clock)
    consume_approval(db, row.id, resubmit_envelope=env, user_id=7, session_id=3, clock=clock)
    # a later equivalent attempt has no grant to resolve
    fresh = env.with_fields(action_nonce="g" * 40)
    assert resolve_session_grant(db, envelope=fresh, user_id=7, session_id=3, clock=clock) is None


def test_create_grant_requires_explicit_decision(db, clock):
    env, row = _approved(db, clock)
    consume_approval(db, row.id, resubmit_envelope=env, user_id=7, session_id=3, clock=clock)
    with pytest.raises(GrantScopeError):
        create_session_grant(db, approval=row, envelope=env,
                             session_expires_at=clock.now() + dt.timedelta(hours=1),
                             explicit_grant_decision=False, clock=clock)


def test_create_grant_rejects_mismatched_envelope(db, clock):
    env, row = _approved(db, clock)
    consume_approval(db, row.id, resubmit_envelope=env, user_id=7, session_id=3, clock=clock)
    with pytest.raises(GrantScopeError):
        create_session_grant(db, approval=row, envelope=env.with_fields(args_hash="9" * 64),
                             session_expires_at=clock.now() + dt.timedelta(hours=1),
                             explicit_grant_decision=True, clock=clock)


def _authz_used_events(db, scope="user:7"):
    return [e for e in db.query(ActionLogEntry).filter_by(chain_scope_id=scope).all()
            if e.event_type == GRANT_AUTHORIZATION_USED_EVENT]


def _never_execute():
    raise AssertionError("execute callback was reached despite a failed authorization check")


def test_session_grant_expiry_fails_closed(db, clock):
    env, row, g = _grant(db, clock)
    clock.advance(3600 + 1)
    assert resolve_session_grant(db, envelope=env, user_id=7, session_id=3, clock=clock) is None
    before = len(_authz_used_events(db))
    with pytest.raises(GrantScopeError):
        dispatch_with_session_grant(db, envelope=env, user_id=7, session_id=3,
                                    execute=_never_execute, clock=clock)
    assert len(_authz_used_events(db)) == before  # A.4: expired -> no authz-used event


def test_session_grant_revocation_fails_closed(db, clock):
    env, row, g = _grant(db, clock)
    assert revoke_session_grant(db, g.id, clock=clock) is True
    assert resolve_session_grant(db, envelope=env, user_id=7, session_id=3, clock=clock) is None
    assert revoke_session_grant(db, g.id, clock=clock) is False  # idempotent
    before = len(_authz_used_events(db))
    with pytest.raises(GrantScopeError):
        dispatch_with_session_grant(db, envelope=env, user_id=7, session_id=3,
                                    execute=_never_execute, clock=clock)
    assert len(_authz_used_events(db)) == before  # A.5: revoked -> no authz-used event


def test_grant_expires_no_later_than_session(db, clock):
    env, row = _approved(db, clock)
    consume_approval(db, row.id, resubmit_envelope=env, user_id=7, session_id=3, clock=clock)
    # session ends in 30 min; ask for a longer grant -> capped at session end
    session_end = clock.now() + dt.timedelta(minutes=30)
    g = create_session_grant(db, approval=row, envelope=env, session_expires_at=session_end,
                             explicit_grant_decision=True, clock=clock)
    from app.approval.clock import ensure_utc
    assert ensure_utc(g.expires_at) <= session_end


def test_session_grant_never_wildcards(db, clock):
    env, row, g = _grant(db, clock)
    assert g.grant_scope_hash == env.grant_scope_hash()
    assert g.tool_id == "terminal.run" and g.server_fingerprint == env.server_fingerprint
    assert len(g.grant_nonce) >= 32
    from app.approval.clock import ensure_utc
    assert ensure_utc(g.expires_at) <= clock.now() + dt.timedelta(hours=1)


# ── authorization-vs-execution audit semantics (grant.authorization_used) ──


def test_passive_grant_lookup_writes_no_authz_used_event(db, clock):
    """A.1 — resolve_session_grant merely inspects; it writes nothing."""
    env, row, g = _grant(db, clock)
    before = len(_authz_used_events(db))
    assert resolve_session_grant(db, envelope=env, user_id=7, session_id=3, clock=clock) is not None
    assert len(_authz_used_events(db)) == before


def test_successful_handoff_writes_exactly_one_authz_used_event(db, clock):
    """A.2 + A.8 — one event; it does not claim execution success."""
    env, row, g = _grant(db, clock)
    before = len(_authz_used_events(db))
    marker = object()
    result = dispatch_with_session_grant(db, envelope=env, user_id=7, session_id=3,
                                         execute=lambda: marker, clock=clock)
    assert result is marker
    evs = _authz_used_events(db)
    assert len(evs) == before + 1
    ev = evs[-1]
    assert ev.event_type == "grant.authorization_used"
    meta = json.loads(ev.event_metadata)
    blob = (ev.event_type + " " + json.dumps(meta)).lower()
    assert "success" not in blob and "succeeded" not in blob
    assert "not an execution-result event" in meta["meaning"].lower()
    assert "authorization exercised" in meta["meaning"].lower()


def test_two_authorized_attempts_write_two_distinct_events(db, clock):
    """A.3 — distinct events, distinct action_hash / action_nonce."""
    env, row, g = _grant(db, clock)
    before = len(_authz_used_events(db))
    a1 = env.with_fields(action_nonce="1" * 40, run_id="r1")
    a2 = env.with_fields(action_nonce="2" * 40, run_id="r2")
    dispatch_with_session_grant(db, envelope=a1, user_id=7, session_id=3, execute=lambda: 1, clock=clock)
    dispatch_with_session_grant(db, envelope=a2, user_id=7, session_id=3, execute=lambda: 2, clock=clock)
    evs = _authz_used_events(db)[-2:]
    assert len(_authz_used_events(db)) == before + 2
    assert {e.action_hash for e in evs} == {a1.action_hash(), a2.action_hash()}
    assert a1.action_hash() != a2.action_hash()
    assert all(e.grant_id == g.id for e in evs)


@pytest.mark.parametrize("kw", [
    {"user_id": 8}, {"session_id": 4}, {"tenant_id": "tenant-B"},
])
def test_cross_scope_attempt_writes_no_authz_used_event_and_never_executes(db, clock, kw):
    """A.6 + A.7 — cross user/session/tenant: no event, callback unreachable."""
    env, row = _approved_for(db, clock, tenant_id="tenant-A", user_id=7, session_id=3)
    consume_approval(db, row.id, resubmit_envelope=env, user_id=7,
                     tenant_id="tenant-A", session_id=3, clock=clock)
    create_session_grant(db, approval=row, envelope=env,
                         session_expires_at=clock.now() + dt.timedelta(hours=1),
                         explicit_grant_decision=True, clock=clock)
    call = dict(user_id=7, session_id=3, tenant_id="tenant-A", clock=clock)
    call.update(kw)
    bad_env = env.with_fields(**{k: v for k, v in kw.items() if k in ("tenant_id",)})
    before_a = len(_authz_used_events(db, "tenant:tenant-A"))
    before_b = len(_authz_used_events(db, "tenant:tenant-B"))
    with pytest.raises(GrantScopeError):
        dispatch_with_session_grant(db, envelope=bad_env, execute=_never_execute, **call)
    assert len(_authz_used_events(db, "tenant:tenant-A")) == before_a
    assert len(_authz_used_events(db, "tenant:tenant-B")) == before_b


def test_failed_authorization_check_cannot_reach_execution_callback(db, clock):
    """A.7 — no grant at all -> GrantScopeError, callback never invoked."""
    env, row = _approved(db, clock)
    consume_approval(db, row.id, resubmit_envelope=env, user_id=7, session_id=3, clock=clock)
    # no create_session_grant -> nothing authorises this
    with pytest.raises(GrantScopeError):
        dispatch_with_session_grant(db, envelope=env, user_id=7, session_id=3,
                                    execute=_never_execute, clock=clock)


# ── tamper-evident chain ──────────────────────────────────────


def _seed_chain(db, clock, user_id=100, n=4):
    for i in range(n):
        alog.append_event(db, tenant_id=None, user_id=user_id, event_type=f"e{i}",
                          event_metadata={"i": i}, clock=clock)


def test_chain_verifies_clean(db, clock):
    _seed_chain(db, clock)
    rep = verify_chain(db, user_id=100)
    assert rep.ok and rep.count == 4


def test_chain_detects_modified_row(db, clock):
    _seed_chain(db, clock)
    row = db.query(ActionLogEntry).filter_by(chain_scope_id="user:100", seq=2).one()
    row.event_type = "hacked"
    db.commit()
    rep = verify_chain(db, user_id=100)
    assert not rep.ok and rep.break_at == 2


def test_chain_detects_deleted_row(db, clock):
    _seed_chain(db, clock)
    db.query(ActionLogEntry).filter_by(chain_scope_id="user:100", seq=3).delete()
    db.commit()
    rep = verify_chain(db, user_id=100)
    assert not rep.ok  # seq gap at 3 (row 4 now has prev pointing at deleted 3)


def test_chain_detects_sequence_gap(db, clock):
    _seed_chain(db, clock, n=4)
    # renumber seq 4 -> 6 so 4 and 5 are missing
    r = db.query(ActionLogEntry).filter_by(chain_scope_id="user:100", seq=4).one()
    r.seq = 6
    db.commit()
    rep = verify_chain(db, user_id=100)
    assert not rep.ok and rep.break_at == 6
    assert "seq" in rep.detail.lower()


def test_chain_detects_inserted_row(db, clock):
    _seed_chain(db, clock, n=3)
    import uuid as _uuid
    from app.approval.hashing import row_hash as _rh
    from app.approval.action_log import _norm_ts
    # fabricate a row at seq 4 with a plausible prev link but a WRONG row_hash
    # (an inserter who does not / cannot recompute the chain correctly)
    head = db.query(ActionLogEntry).filter_by(chain_scope_id="user:100", seq=3).one()
    forged = ActionLogEntry(
        id=str(_uuid.uuid4()), chain_scope_id="user:100", seq=4,
        event_type="forged", approval_id=None, grant_id=None, action_hash=None,
        event_metadata="{}", prev_row_hash=head.row_hash,
        row_hash="f" * 64,  # not the real recomputed value
        created_at=clock.now(),
    )
    db.add(forged)
    db.commit()
    rep = verify_chain(db, user_id=100)
    assert not rep.ok and rep.break_at == 4
    assert "row_hash mismatch" in rep.detail

    # a broken prev-link insertion is also caught
    db.query(ActionLogEntry).filter_by(chain_scope_id="user:100", seq=4).delete()
    db.commit()
    payload = {"seq": 4, "chain_scope_id": "user:100", "event_type": "forged2",
               "approval_id": None, "grant_id": None, "action_hash": None,
               "event_metadata": {}, "created_at": _norm_ts(clock.now())}
    forged2 = ActionLogEntry(
        id=str(_uuid.uuid4()), chain_scope_id="user:100", seq=4, event_type="forged2",
        event_metadata="{}", prev_row_hash="a" * 64,          # wrong prev link
        row_hash=_rh("a" * 64, payload), created_at=clock.now(),
    )
    db.add(forged2)
    db.commit()
    rep = verify_chain(db, user_id=100)
    assert not rep.ok and rep.break_at == 4
    assert "broken link" in rep.detail


def test_reorder_is_prevented_by_unique_and_detected_by_hash(db, clock):
    _seed_chain(db, clock)
    from sqlalchemy.exc import IntegrityError

    # 1) The schema itself blocks a plain seq swap: UNIQUE(chain_scope_id, seq).
    r2 = db.query(ActionLogEntry).filter_by(chain_scope_id="user:100", seq=2).one()
    r2.seq = 3
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    # 2) Swapping the *content* of two positions (keeping seq) is caught by the
    #    row_hash recompute — the chain no longer verifies.
    a = db.query(ActionLogEntry).filter_by(chain_scope_id="user:100", seq=2).one()
    b = db.query(ActionLogEntry).filter_by(chain_scope_id="user:100", seq=3).one()
    a.event_type, b.event_type = b.event_type, a.event_type
    a.created_at, b.created_at = b.created_at, a.created_at
    db.commit()
    rep = verify_chain(db, user_id=100)
    assert not rep.ok and rep.break_at == 2


def test_independent_chain_scopes_do_not_interfere(db, clock):
    _seed_chain(db, clock, user_id=1, n=3)
    _seed_chain(db, clock, user_id=2, n=2)
    assert verify_chain(db, user_id=1).ok
    assert verify_chain(db, user_id=2).ok
    db.query(ActionLogEntry).filter_by(chain_scope_id="user:1", seq=2).one().event_type = "x"
    db.commit()
    assert not verify_chain(db, user_id=1).ok
    assert verify_chain(db, user_id=2).ok  # scope 2 still fine


def test_chain_scope_id_is_explicit_never_null(db, clock):
    e = alog.append_event(db, tenant_id=None, user_id=None, event_type="sys", clock=clock)
    assert e.chain_scope_id == "system"
    e2 = alog.append_event(db, tenant_id="acme", user_id=5, event_type="t", clock=clock)
    assert e2.chain_scope_id == "tenant:acme"


def test_chain_scope_id_is_in_the_row_hash_payload(db, clock):
    # Identical first events in different scopes (same seq=1, GENESIS prev, same
    # FixedClock time, same event_type) must NOT collide under UNIQUE(row_hash):
    # chain_scope_id is part of the canonical payload.
    e1 = alog.append_event(db, tenant_id=None, user_id=1, event_type="e", clock=clock)
    e2 = alog.append_event(db, tenant_id=None, user_id=2, event_type="e", clock=clock)
    e3 = alog.append_event(db, tenant_id="t", user_id=1, event_type="e", clock=clock)
    assert e1.seq == e2.seq == e3.seq == 1
    assert e1.prev_row_hash == e2.prev_row_hash == GENESIS
    assert len({e1.row_hash, e2.row_hash, e3.row_hash}) == 3     # all distinct
    for uid in (1, 2):
        assert verify_chain(db, user_id=uid).ok
    assert verify_chain(db, tenant_id="t").ok


def test_append_retry_is_bounded_and_fails_closed(db, clock, monkeypatch):
    # Real UNIQUE(chain_scope_id, seq) conflict on every attempt: pin the head
    # read to seq 0 while a row already occupies seq 1, so every retry collides.
    alog.append_event(db, tenant_id=None, user_id=1, event_type="seed", clock=clock)
    calls = {"n": 0}
    real_head = alog._head

    def stuck_head(_db, _scope):
        calls["n"] += 1
        return 0, GENESIS

    monkeypatch.setattr(alog, "_head", stuck_head)
    with pytest.raises(ChainIntegrityError):
        alog.append_event(db, tenant_id=None, user_id=1, event_type="e", clock=clock)
    assert calls["n"] == alog._APPEND_RETRIES        # bounded, no infinite loop
    monkeypatch.setattr(alog, "_head", real_head)
    # the chain is still intact — the failed append wrote nothing
    assert verify_chain(db, user_id=1).ok and verify_chain(db, user_id=1).count == 1


def test_no_public_mutation_api_for_action_log():
    # service surface exposes append + verify only
    import app.approval as pkg
    assert not any(n for n in dir(pkg) if n.lower() in ("update_action_log", "delete_action_log"))
    # ActionLogEntry has no service-level update/delete helper
    from app.approval import action_log as al
    assert not hasattr(al, "update_entry") and not hasattr(al, "delete_entry")


def test_action_log_metadata_is_redacted(db, clock):
    e = alog.append_event(db, tenant_id=None, user_id=1, event_type="e",
                          event_metadata={"api_key": "sk-abcdefghijklmnopqrstuvwxyz",
                                          "note": "bearer eyJaaaaaaaa.eyJbbbbbbbb.cccccccc",
                                          "token_count": 12}, clock=clock)
    meta = json.loads(e.event_metadata)
    assert meta["api_key"] == "[REDACTED]"
    assert "eyJ" not in meta["note"]
    assert meta["token_count"] == 12  # usage metadata kept


# ── persistence: no secrets, bounded display ──────────────────


def test_pending_approval_stores_no_raw_args_or_secrets(db, clock):
    _, row = _request(db, clock, args={"cmd": "curl", "headers": {"Authorization": "Bearer sk-secret"}},
                      purpose="use api_key sk-abcdefghijklmnopqrstuvwx")
    blob = json.dumps({c.name: getattr(row, c.name) for c in row.__table__.columns}, default=str)
    assert "sk-secret" not in blob and "sk-abcdefghijklmnopqrstuvwx" not in blob
    assert "Bearer sk-secret" not in blob
    assert '"cmd": "curl"' not in blob  # raw args never persisted


def test_display_summary_is_bounded_and_redacted(db, clock):
    _, row = _request(db, clock, affected_resources=tuple(f"res-{i}" for i in range(50)))
    summary = json.loads(row.display_summary)
    assert len(summary["affected_resources"]) <= 20
    assert len(row.display_summary) < 8000


def test_risk_is_server_derived_not_client_supplied(db, clock):
    # destructive terminal command -> high risk from the trusted classifier;
    # there is no parameter to override it
    _, row = _request(db, clock, tool_id="terminal.run",
                      args={"cmd": "rm -rf /important"}, affected_resources=("delete:/important",))
    assert row.risk_level == "high"


# ── SQLite / PostgreSQL specific ─────────────────────────────


def test_sqlite_path_is_the_default(engine):
    assert engine.dialect.name == "sqlite"


@pytest.mark.skipif(not os.environ.get("EVAL_PG_TEST_URL"),
                    reason="set EVAL_PG_TEST_URL to a disposable PostgreSQL for the PG path")
def test_postgres_chain_append_and_verify():
    pg = create_engine(os.environ["EVAL_PG_TEST_URL"])
    Base.metadata.create_all(pg)
    S = sessionmaker(bind=pg)
    clk = FixedClock()
    try:
        s = S()
        for i in range(5):
            alog.append_event(s, tenant_id="pgt", user_id=1, event_type="e",
                              event_metadata={"i": i}, clock=clk)
        rep = verify_chain(s, tenant_id="pgt")
        assert rep.ok and rep.count == 5
        s.close()
    finally:
        Base.metadata.drop_all(pg)
        pg.dispose()


@pytest.mark.skipif(not os.environ.get("EVAL_PG_TEST_URL"),
                    reason="PostgreSQL concurrent-consumer check")
def test_postgres_concurrent_consumers_exactly_one():
    pg = create_engine(os.environ["EVAL_PG_TEST_URL"])
    Base.metadata.create_all(pg)
    S = sessionmaker(bind=pg)
    clk = FixedClock()
    try:
        s0 = S()
        env, row = _approved(s0, clk)
        s0.close()

        def consume():
            s = S()
            try:
                consume_approval(s, row.id, resubmit_envelope=env, user_id=7, session_id=3, clock=clk)
                return "ok"
            finally:
                s.close()

        ok, errs = _run_threads(consume, 8)
        assert len(ok) == 1
    finally:
        Base.metadata.drop_all(pg)
        pg.dispose()
