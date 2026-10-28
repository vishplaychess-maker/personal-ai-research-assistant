"""Phase 1B.2R MP-0 — encrypted run-payload store: context-bound envelope.

Hermetic: in-memory SQLite + a per-test Fernet ENCRYPTION_KEY. No worker, no
route. Proves the versioned encrypted envelope, ciphertext-swap resistance
(protection comes from the authenticated envelope + trusted expected context,
NOT from any adjacent plaintext digest), and every fail-closed path.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models.models  # noqa: F401
from app.models.models import RunEvent, RunPayload, RunSignal, RunStep
from app.run_engine import payloads as P
from app.run_engine.errors import (
    RunPayloadAccessError,
    RunPayloadActionHashMismatch,
    RunPayloadContextMismatch,
    RunPayloadEncodingError,
    RunPayloadEncryptionUnavailable,
    RunPayloadEnvelopeError,
    RunPayloadEnvelopeVersionError,
    RunPayloadError,
    RunPayloadExpired,
    RunPayloadIntegrityError,
    RunPayloadSchemeError,
    RunPayloadTooLarge,
)

CANARY = "sk-CANARY-secret-0123456789abcdefABCDEF"


@pytest.fixture
def key(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", k)
    return k


@pytest.fixture
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


_CTX = dict(tenant_id=None, user_id=7, run_id="r1", step_id="s1",
            payload_type="tool_args", action_hash=None)


def _seal(db, *, plaintext="hello", **over):
    ctx = {**_CTX, **over}
    return P.seal_payload(
        db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"], run_id=ctx["run_id"],
        step_id=ctx["step_id"], payload_type=ctx["payload_type"], plaintext=plaintext,
        envelope_action_hash=ctx["action_hash"],
    )


def _open(db, pid, **over):
    ctx = {**_CTX, **over}
    return P.open_payload(
        db, pid, expected_tenant_id=ctx["tenant_id"], expected_user_id=ctx["user_id"],
        expected_run_id=ctx["run_id"], expected_step_id=ctx["step_id"],
        expected_payload_type=ctx["payload_type"], expected_action_hash=ctx["action_hash"],
    )


# ── round trips ─────────────────────────────────────────────────────


def test_nonempty_utf8_round_trip(key, db):
    text_in = '{"q": "hello-world-unicode", "n": 42}'
    row = _seal(db, plaintext=text_in)
    assert row.ciphertext.startswith("gAAAAA")
    assert row.byte_len == len(text_in.encode("utf-8"))
    assert _open(db, row.id) == text_in


def test_unicode_payload_round_trip(key, db):
    text_in = "café 世界 \U0001f30d \" \\ \n\t"
    row = _seal(db, plaintext=text_in)
    assert _open(db, row.id) == text_in


def test_empty_payload_round_trip_no_sentinel(key, db):
    row = _seal(db, plaintext="")
    assert row.byte_len == 0
    # decrypting the token yields a canonical JSON envelope whose payload is ""
    import json
    env = json.loads(Fernet(key).decrypt(row.ciphertext.encode()).decode())
    assert env["payload"] == "" and env["version"] == 1
    assert _open(db, row.id) == ""


def test_interface_is_utf8_str_only(key, db):
    import inspect
    assert inspect.signature(P.seal_payload).parameters["plaintext"].annotation == "str"
    with pytest.raises(ValueError):
        P.seal_payload(db, tenant_id=None, user_id=7, run_id="r1", step_id=None,
                       payload_type="other", plaintext=b"raw-bytes")  # type: ignore[arg-type]


# ── size bounds ────────────────────────────────────────────────────


def test_maximum_size_is_accepted(key, db, monkeypatch):
    monkeypatch.setattr(P.settings, "run_payload_max_bytes", 32)
    row = _seal(db, plaintext="x" * 32)
    assert row.byte_len == 32
    assert _open(db, row.id) == "x" * 32


def test_oversized_is_rejected_before_encryption_and_writes_nothing(key, db, monkeypatch):
    monkeypatch.setattr(P.settings, "run_payload_max_bytes", 16)
    calls = {"n": 0}
    real = P._cipher()

    class Spy:
        def encrypt(self, d):
            calls["n"] += 1
            return real.encrypt(d)

    monkeypatch.setattr(P, "_cipher", lambda: Spy())
    with pytest.raises(RunPayloadTooLarge):
        _seal(db, plaintext="y" * 17)
    assert calls["n"] == 0                       # rejected BEFORE encryption
    assert db.query(RunPayload).count() == 0


# ── crypto failure modes (all fail closed, typed) ──────────────────


def test_tampered_ciphertext_fails_closed(key, db):
    row = _seal(db, plaintext=CANARY)
    body = list(row.ciphertext)
    body[-4] = "A" if body[-4] != "A" else "B"
    row.ciphertext = "".join(body)
    db.commit()
    with pytest.raises(RunPayloadIntegrityError):
        _open(db, row.id)


def test_malformed_ciphertext_fails_closed(key, db):
    row = _seal(db, plaintext="hi")
    row.ciphertext = "not-a-valid-fernet-token!!!"
    db.commit()
    with pytest.raises(RunPayloadIntegrityError):
        _open(db, row.id)


def test_wrong_key_fails_closed(key, db, monkeypatch):
    row = _seal(db, plaintext=CANARY)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(RunPayloadIntegrityError):
        _open(db, row.id)


def test_key_rotation_multifernet_still_opens(key, db, monkeypatch):
    """MultiFernet: a new primary + the old key as fallback still decrypts."""
    row = _seal(db, plaintext=CANARY)
    new = Fernet.generate_key().decode()
    from cryptography.fernet import MultiFernet, Fernet as F
    monkeypatch.setattr(P, "_cipher", lambda: MultiFernet([F(new.encode()), F(key.encode())]))
    assert _open(db, row.id) == CANARY


def test_missing_key_on_seal_fails_closed_and_writes_nothing(db, monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr("app.config.settings.encryption_key", "", raising=False)
    with pytest.raises(RunPayloadEncryptionUnavailable):
        _seal(db, plaintext="x")
    assert db.query(RunPayload).count() == 0


def test_missing_key_on_open_fails_closed(key, db, monkeypatch):
    row = _seal(db, plaintext="x")
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr("app.config.settings.encryption_key", "", raising=False)
    with pytest.raises(RunPayloadEncryptionUnavailable):
        _open(db, row.id)


def test_unsupported_scheme_fails_closed(key, db):
    row = _seal(db, plaintext="hello")
    row.enc_scheme = "fernet.v99"
    db.commit()
    with pytest.raises(RunPayloadSchemeError):
        _open(db, row.id)


# ── lifecycle: expiry + deletion ──────────────────────────────────


def test_expired_payload_fails_closed(key, db):
    row = _seal(db, plaintext="hello")
    row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    db.commit()
    with pytest.raises(RunPayloadExpired):
        _open(db, row.id)


def test_soft_deleted_payload_fails_closed(key, db):
    row = _seal(db, plaintext="hello")
    row.deleted_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    with pytest.raises(RunPayloadAccessError):
        _open(db, row.id)


def test_purged_payload_is_gone(key, db):
    row = _seal(db, plaintext="hello")
    pid = row.id
    assert P.purge_payload(db, pid) is True
    assert db.get(RunPayload, pid) is None
    with pytest.raises(RunPayloadAccessError):
        _open(db, pid)
    assert P.purge_payload(db, pid) is False


# ── context binding: envelope vs TRUSTED expected ─────────────────


def test_each_context_field_mismatch_fails_closed(key, db):
    row = _seal(db, plaintext="s", tenant_id="acme", user_id=7, run_id="rX",
                step_id="sX", payload_type="tool_output")
    ok = dict(tenant_id="acme", user_id=7, run_id="rX", step_id="sX",
              payload_type="tool_output", action_hash=None)
    assert P.open_payload(db, row.id, expected_tenant_id="acme", expected_user_id=7,
                          expected_run_id="rX", expected_step_id="sX",
                          expected_payload_type="tool_output", expected_action_hash=None) == "s"
    for field, bad in (("tenant_id", None), ("tenant_id", "other"), ("run_id", "rY"),
                       ("step_id", "sY"), ("step_id", None), ("payload_type", "tool_args")):
        kw = {**ok, field: bad}
        with pytest.raises(RunPayloadContextMismatch) as ei:
            P.open_payload(db, row.id, expected_tenant_id=kw["tenant_id"],
                           expected_user_id=kw["user_id"], expected_run_id=kw["run_id"],
                           expected_step_id=kw["step_id"], expected_payload_type=kw["payload_type"],
                           expected_action_hash=kw["action_hash"])
        assert ei.value.field == field


def test_user_id_mismatch_fails_closed(key, db):
    row = _seal(db, plaintext="s", user_id=7)
    with pytest.raises(RunPayloadError):        # fast pre-check OR envelope
        _open(db, row.id, user_id=8)


def test_action_hash_none_wrong_and_correct(key, db):
    row = _seal(db, plaintext=CANARY, action_hash="a" * 64)
    with pytest.raises(RunPayloadActionHashMismatch):
        _open(db, row.id, action_hash=None)
    with pytest.raises(RunPayloadActionHashMismatch):
        _open(db, row.id, action_hash="b" * 64)
    assert _open(db, row.id, action_hash="a" * 64) == CANARY


def test_action_hash_null_in_both_is_a_match(key, db):
    row = _seal(db, plaintext="s", action_hash=None)
    assert _open(db, row.id, action_hash=None) == "s"


# ── ciphertext-swap resistance (item 4) ──────────────────────────


def _two_rows(db, *, a_action=None, b_action=None):
    a = _seal(db, plaintext="PAYLOAD-A", tenant_id=None, user_id=7, run_id="runA",
              step_id="stepA", payload_type="tool_args", action_hash=a_action)
    b = _seal(db, plaintext="PAYLOAD-B", tenant_id="acme", user_id=8, run_id="runB",
              step_id="stepB", payload_type="tool_output", action_hash=b_action)
    return a, b


def _open_a(db, pid):
    return P.open_payload(db, pid, expected_tenant_id=None, expected_user_id=7,
                          expected_run_id="runA", expected_step_id="stepA",
                          expected_payload_type="tool_args", expected_action_hash=None)


def test_swap_only_ciphertext_between_rows_fails_closed(key, db):
    a, b = _two_rows(db)
    a.ciphertext = b.ciphertext
    db.commit()
    with pytest.raises(RunPayloadContextMismatch):   # envelope says runB, expected runA
        _open_a(db, a.id)


def test_swap_ciphertext_plus_every_mutable_column_fails_closed(key, db):
    a, b = _two_rows(db)
    # attacker rewrites row A to look exactly like B AND drops in B's ciphertext
    a.ciphertext = b.ciphertext
    a.tenant_id = b.tenant_id
    a.user_id = b.user_id
    a.run_id = b.run_id
    a.step_id = b.step_id
    a.payload_type = b.payload_type
    a.enc_scheme = b.enc_scheme
    a.envelope_action_hash = b.envelope_action_hash
    a.byte_len = b.byte_len
    db.commit()
    # the legitimate caller for run A still supplies run A's TRUSTED context
    with pytest.raises(RunPayloadError):            # RunPayloadAccessError (pre-check) or ContextMismatch
        _open_a(db, a.id)
    # and the row can never be opened AS run A even though its columns say B
    with pytest.raises(RunPayloadError):
        _open_a(db, a.id)


def test_ciphertext_copied_between_runs_steps_users_tenants(key, db):
    a, b = _two_rows(db)
    # only ciphertext copied; row A keeps run-A columns so the pre-check passes
    a.ciphertext = b.ciphertext
    db.commit()
    with pytest.raises(RunPayloadContextMismatch):
        _open_a(db, a.id)                            # envelope run/step/user/tenant = B


def test_swap_resistance_with_action_hash_null_in_both(key, db):
    a, b = _two_rows(db, a_action=None, b_action=None)
    a.ciphertext = b.ciphertext
    db.commit()
    with pytest.raises(RunPayloadContextMismatch):   # protection is NOT from action_hash
        _open_a(db, a.id)


def test_swap_resistance_with_equal_action_hash_in_both(key, db):
    same = "c" * 64
    a, b = _two_rows(db, a_action=same, b_action=same)
    a.ciphertext = b.ciphertext
    db.commit()
    with pytest.raises(RunPayloadContextMismatch):   # equal action_hash does not save the swap
        P.open_payload(db, a.id, expected_tenant_id=None, expected_user_id=7,
                       expected_run_id="runA", expected_step_id="stepA",
                       expected_payload_type="tool_args", expected_action_hash=same)


def test_tenant_null_normalization_does_not_collide(key, db):
    # sealed with a real tenant, opened expecting tenant-null -> mismatch
    row = _seal(db, plaintext="s", tenant_id="acme", user_id=7, run_id="rN",
                step_id="sN", payload_type="tool_args")
    with pytest.raises(RunPayloadContextMismatch) as ei:
        P.open_payload(db, row.id, expected_tenant_id=None, expected_user_id=7,
                       expected_run_id="rN", expected_step_id="sN",
                       expected_payload_type="tool_args", expected_action_hash=None)
    assert ei.value.field == "tenant_id"
    # sealed with tenant "" is normalized to null and matches expected None
    row2 = _seal(db, plaintext="s2", tenant_id="", user_id=7, run_id="rN",
                 step_id="sN", payload_type="tool_args")
    assert P.open_payload(db, row2.id, expected_tenant_id=None, expected_user_id=7,
                          expected_run_id="rN", expected_step_id="sN",
                          expected_payload_type="tool_args", expected_action_hash=None) == "s2"


# ── envelope adversarial (item 9) ────────────────────────────────


def _reseal_raw(db, key, obj_bytes):
    """Put attacker-controlled *authenticated* bytes into a row's ciphertext."""
    row = _seal(db, plaintext="placeholder")
    row.ciphertext = Fernet(key.encode()).encrypt(obj_bytes).decode()
    db.commit()
    return row


def test_non_object_envelope_rejected(key, db):
    for raw in (b'"just a string"', b'[1,2,3]', b'42', b'null'):
        row = _reseal_raw(db, key, raw)
        with pytest.raises(RunPayloadEnvelopeError):
            _open(db, row.id)


def test_malformed_json_envelope_rejected(key, db):
    row = _reseal_raw(db, key, b'{not json')
    with pytest.raises(RunPayloadEnvelopeError):
        _open(db, row.id)


def test_missing_or_extra_envelope_field_rejected(key, db):
    base = {"version": 1, "tenant_id": None, "user_id": 7, "run_id": "r1",
            "step_id": "s1", "payload_type": "tool_args", "action_hash": None,
            "payload_encoding": "utf-8", "payload": "x"}
    import json
    missing = {k: v for k, v in base.items() if k != "payload"}
    extra = {**base, "surprise": "boom"}
    for obj in (missing, extra):
        row = _reseal_raw(db, key, json.dumps(obj).encode())
        with pytest.raises(RunPayloadEnvelopeError):
            _open(db, row.id)


def test_unsupported_envelope_version_rejected(key, db):
    import json
    base = {"tenant_id": None, "user_id": 7, "run_id": "r1", "step_id": "s1",
            "payload_type": "tool_args", "action_hash": None,
            "payload_encoding": "utf-8", "payload": "x"}
    for bad_version in (2, 0, 1.0, "1", True, None):
        row = _reseal_raw(db, key, json.dumps({"version": bad_version, **base}).encode())
        with pytest.raises(RunPayloadEnvelopeVersionError):
            _open(db, row.id)


def test_deeply_nested_hostile_envelope_rejected(key, db):
    deep = "[" * 5000 + "1" + "]" * 5000
    row = _reseal_raw(db, key, deep.encode())
    with pytest.raises(RunPayloadEnvelopeError):
        _open(db, row.id)


def test_wrong_field_type_rejected(key, db):
    import json
    obj = {"version": 1, "tenant_id": None, "user_id": "7", "run_id": "r1",
           "step_id": "s1", "payload_type": "tool_args", "action_hash": None,
           "payload_encoding": "utf-8", "payload": "x"}
    row = _reseal_raw(db, key, json.dumps(obj).encode())
    with pytest.raises(RunPayloadEnvelopeError):
        _open(db, row.id)


def test_unsupported_payload_encoding_rejected(key, db):
    import json
    obj = {"version": 1, "tenant_id": None, "user_id": 7, "run_id": "r1",
           "step_id": "s1", "payload_type": "tool_args", "action_hash": None,
           "payload_encoding": "latin-1", "payload": "x"}
    row = _reseal_raw(db, key, json.dumps(obj).encode())
    with pytest.raises(RunPayloadEncodingError):
        _open(db, row.id)


def test_invalid_utf8_in_decrypted_bytes_rejected(key, db):
    row = _reseal_raw(db, key, b'\xff\xfe\x00 not utf-8')
    with pytest.raises(RunPayloadEncodingError):
        _open(db, row.id)


# ── low-entropy offline-guessing: no adjacent deterministic digest ─


def test_no_plaintext_digest_column_exists():
    cols = {c.name for c in RunPayload.__table__.columns}
    assert "plaintext_sha256" not in cols
    for c in cols:
        assert "sha" not in c.lower() and "hash" not in c.replace("action_hash", "").lower()


def test_low_entropy_payload_leaves_no_guessable_digest(key, db):
    row = _seal(db, plaintext="approved")          # a very low-entropy value
    db.commit()
    stored = " ".join(str(v) for v in db.execute(
        text("SELECT * FROM run_payloads")).one()._mapping.values())
    for cand in ("approved", "denied", "yes", "no", "true", "false", "1", "0"):
        assert hashlib.sha256(cand.encode()).hexdigest() not in stored
        assert hashlib.sha256(cand.encode()).hexdigest()[:32] not in stored
    assert "approved" not in stored               # no plaintext either


# ── canary: nothing in DB rows or logs ──────────────────────────


def test_no_secret_canary_in_any_run_table(key, db):
    _seal(db, plaintext=CANARY, tenant_id="acme", step_id="s1")
    db.add(RunStep(id="rs1", run_id="r1", user_id=7, seq=1, kind="tool_dispatch"))
    db.add(RunEvent(id="re1", run_id="r1", run_seq=1, user_id=7, type="status",
                    data='{"phase":"running"}'))
    db.add(RunSignal(id="sg1", run_id="r1", kind="approval_decided",
                     payload='{"decision":"approved"}', created_by_user_id=7))
    db.commit()
    for tbl in ("run_payloads", "runs", "run_steps", "run_events", "run_signals",
                "action_log"):
        rows = db.execute(text(f"SELECT * FROM {tbl}")).fetchall()
        blob = "".join(str(v) for r in rows for v in r._mapping.values())
        assert CANARY not in blob, f"secret canary leaked into {tbl}"
    ct = db.query(RunPayload).one().ciphertext
    assert CANARY not in ct and ct.startswith("gAAAAA")


def test_no_secret_canary_in_captured_logs(key, db, caplog):
    with caplog.at_level(logging.DEBUG):
        row = _seal(db, plaintext=CANARY)
        assert _open(db, row.id) == CANARY
        row.ciphertext = "gAAAAAtampered"
        db.commit()
        with pytest.raises(RunPayloadError):
            _open(db, row.id)
    assert CANARY not in caplog.text
    assert "gAAAAA" not in caplog.text


# ── encrypt-once / decrypt-once ─────────────────────────────────


def test_encryption_and_decryption_each_happen_exactly_once(key, db, monkeypatch):
    real = P._cipher()
    calls = {"encrypt": 0, "decrypt": 0}

    class Spy:
        def encrypt(self, data):
            calls["encrypt"] += 1
            return real.encrypt(data)

        def decrypt(self, token):
            calls["decrypt"] += 1
            return real.decrypt(token)

    monkeypatch.setattr(P, "_cipher", lambda: Spy())
    row = _seal(db, plaintext="once")
    assert calls == {"encrypt": 1, "decrypt": 0}
    assert _open(db, row.id) == "once"
    assert calls == {"encrypt": 1, "decrypt": 1}
