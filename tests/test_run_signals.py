"""Phase 1B.2R MP-0 — allowlisted run_signals.payload schema."""

from __future__ import annotations

import datetime as dt

import pytest

from app.run_engine.signals import (
    build_signal_payload,
    parse_signal_payload,
    SignalPayloadError,
)


def test_build_and_parse_approval_decided():
    s = build_signal_payload("approval_decided", approval_id="appr-1",
                             decision="approved", reason_code="user_ok")
    got = parse_signal_payload("approval_decided", s)
    assert got == {"approval_id": "appr-1", "decision": "approved",
                   "reason_code": "user_ok"}


def test_build_cancel_requested_minimal_and_with_reason():
    assert parse_signal_payload("cancel_requested",
                                build_signal_payload("cancel_requested")) == {}
    s = build_signal_payload("cancel_requested", reason_code="user_abort")
    assert parse_signal_payload("cancel_requested", s) == {"reason_code": "user_abort"}


def test_server_ts_is_normalised_utc_iso():
    s = build_signal_payload("cancel_requested",
                             server_ts=dt.datetime(2026, 1, 2, 3, 4, 5))
    got = parse_signal_payload("cancel_requested", s)
    assert got["server_ts"].startswith("2026-01-02T03:04:05")


def test_missing_required_field_rejected():
    with pytest.raises(SignalPayloadError):
        build_signal_payload("approval_decided", approval_id="a1")   # no decision
    with pytest.raises(SignalPayloadError):
        build_signal_payload("approval_decided", decision="approved")  # no approval_id


def test_disallowed_decision_value_rejected():
    with pytest.raises(SignalPayloadError):
        build_signal_payload("approval_decided", approval_id="a1", decision="maybe")


def test_free_text_reason_code_rejected():
    for bad in ("raw prompt text", "DROP TABLE runs", "x" * 60, "Bearer abc"):
        with pytest.raises(SignalPayloadError):
            build_signal_payload("cancel_requested", reason_code=bad)


def test_unknown_kind_rejected():
    with pytest.raises(SignalPayloadError):
        build_signal_payload("do_something_weird")
    with pytest.raises(SignalPayloadError):
        parse_signal_payload("do_something_weird", "{}")


def test_parse_rejects_disallowed_or_nested_fields():
    # a raw client dumped extra keys / nested JSON into a stored row
    with pytest.raises(SignalPayloadError):
        parse_signal_payload("approval_decided",
                             '{"approval_id":"a1","decision":"approved","prompt":"hi"}')
    with pytest.raises(SignalPayloadError):
        parse_signal_payload("cancel_requested", '{"reason_code":{"nested":1}}')
    with pytest.raises(SignalPayloadError):
        parse_signal_payload("cancel_requested", '["not","an","object"]')
    with pytest.raises(SignalPayloadError):
        parse_signal_payload("cancel_requested", "not json")


def test_parse_rejects_oversized_payload():
    with pytest.raises(SignalPayloadError):
        parse_signal_payload("cancel_requested", '{"reason_code":"' + "x" * 600 + '"}')


def test_credentials_and_headers_never_fit_the_schema():
    for secret in ("sk-abcdef0123456789", "Bearer tok", "Cookie: sid=1"):
        with pytest.raises(SignalPayloadError):
            build_signal_payload("approval_decided", approval_id="a1",
                                 decision="approved", reason_code=secret)
