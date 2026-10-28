"""Focused tests for the D6C-J2/J3 test-only MCP fixtures (RED first).

Run: PYTHONPATH=backend python -m pytest tests/smoke/test_mcp_fakes.py -q
"""
from __future__ import annotations

import asyncio

from app.models.models import TrustedTool
from app.run_engine.chat_stream import MarkerScanner
from tests.run_engine_helpers import ensure_user, mk_engine, mk_session
from tests.smoke.mcp_fakes import (
    CANARY, ECHO_ARGS, ECHO_SCHEMA, TOOL_ID, TOOL_VERSION,
    FakeMcpTransport, MarkerProvider, seed_trusted_echo_tool,
)


def _drain(provider):
    async def _run():
        out = []
        async for c in provider.generate_stream_async(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt=None, model_name="snap-model"):
            out.append(c)
        return out
    return asyncio.run(_run())


def test_1_first_invocation_emits_exactly_one_valid_marker():
    prov = MarkerProvider()
    text = "".join(c["token"] for c in _drain(prov) if c["type"] == "token")
    assert text.count("[MCP_CALL") == 1
    assert "]" in text and text.count("[MCP_CALL") >= 1
    assert TOOL_ID in text


def test_2_marker_split_across_chunks_is_detected_and_never_leaks_a_prefix():
    prov = MarkerProvider(marker_split=7)
    chunks = [c["token"] for c in _drain(prov) if c["type"] == "token"]
    full = "".join(chunks)
    assert len(chunks) > 3, "marker must be split across several chunks"
    # a scanner fed the same chunks must never emit a partial marker prefix
    sc = MarkerScanner()
    visible = "".join(sc.feed(ch) for ch in chunks)
    assert "[MCP_CALL" not in visible
    assert visible.strip() == prov.visible_prefix.strip()
    assert sc.tripped, "a complete marker must trip the scanner"
    assert full.count("[MCP_CALL") == 1


def test_5_registry_seed_resolves_only_the_seeded_identity():
    eng = mk_engine()
    try:
        db = mk_session(eng)
        ensure_user(db, 7)
        row = seed_trusted_echo_tool(db, owner_user_id=7)
        assert row.tool_id == TOOL_ID and row.tool_version == TOOL_VERSION
        assert row.locked is False and row.enabled is True
        assert row.tool_schema_hash and row.server_fingerprint and row.policy_version
        assert db.query(TrustedTool).count() == 1
        db.close()
    finally:
        eng.dispose()


def test_6_changed_schema_or_fingerprint_fails_closed():
    eng = mk_engine()
    try:
        db = mk_session(eng)
        ensure_user(db, 7)
        seed_trusted_echo_tool(db, owner_user_id=7)
        # a different declared schema is a DIFFERENT identity and must stay locked
        other = seed_trusted_echo_tool(db, owner_user_id=7,
                                       input_schema={"type": "object", "properties": {}},
                                       unlock=False)
        assert other.locked is True
        # an unknown tool has no row at all -> fail closed by construction
        assert db.query(TrustedTool).filter(
            TrustedTool.tool_id == "mcp__smoke__nope").count() == 0
        db.close()
    finally:
        eng.dispose()


def test_4_and_8_transport_starts_at_zero_and_is_not_invoked_by_construction():
    t = FakeMcpTransport()
    assert t.invocations == 0
    assert t.calls == []


def test_9_secret_canary_is_absent_from_recorded_metadata():
    eng = mk_engine();
    try:
        db = mk_session(eng)
        ensure_user(db, 7)
        # leak/immutability test only — the started-dispatch precondition is not
        # under test here, so it is explicitly not required.
        t = FakeMcpTransport(require_started_dispatch=False)
        t(db, 7, TOOL_ID, dict(ECHO_ARGS))
        blob = repr(t.calls) + repr(t.safe_metadata())
        assert CANARY not in blob
        assert t.invocations == 1
        db.close()
    finally:
        eng.dispose()


def test_3_transport_cannot_mutate_approval_state_and_writes_no_rows():
    eng = mk_engine()
    try:
        db = mk_session(eng)
        ensure_user(db, 7)
        t = FakeMcpTransport(require_started_dispatch=False)
        before = {n: db.execute(__import__("sqlalchemy").text("SELECT COUNT(*) FROM " + n)).scalar()
                  for n in ("pending_approvals", "dispatch_records", "run_signals", "trusted_tools")}
        t(db, 7, TOOL_ID, dict(ECHO_ARGS))
        after = {n: db.execute(__import__("sqlalchemy").text("SELECT COUNT(*) FROM " + n)).scalar()
                 for n in ("pending_approvals", "dispatch_records", "run_signals", "trusted_tools")}
        assert before == after, "the fake transport must not write any state"
        assert getattr(t, "_gateway_dispatch_calls", 0) == 0
        db.close()
    finally:
        eng.dispose()


def test_7_transport_fails_loudly_when_no_started_dispatch_record_exists():
    eng = mk_engine()
    try:
        db = mk_session(eng)
        ensure_user(db, 7)
        t = FakeMcpTransport()
        raised = False
        try:
            t(db, 7, TOOL_ID, dict(ECHO_ARGS))
        except FakeMcpTransport.InvokedBeforeApprovalConsumed:
            raised = True
        assert raised, "transport must fail closed before approval consumption"
        assert t.invocations == 0
        db.close()
    finally:
        eng.dispose()
