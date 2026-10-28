"""D6C-J4 focused tests for the test-only cancellable provider fixture (RED first).

Run: PYTHONPATH=backend python -m pytest tests/smoke/test_cancel_fakes.py -q
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.run_engine.chat_stream import StreamAborted, _adrive
from tests.smoke.cancel_fakes import (
    CANCEL_COMPLETION_SENTINEL,
    CANCEL_PREFIX,
    CANCEL_SENTINEL,
    DEFAULT_CANCEL_STALL_SECONDS,
    CancellableProviderFactory,
)
from tests.smoke.fakes import SmokeInvocationLimitExceeded


def _events(path) -> list:
    p = Path(path)
    if not p.exists():
        return []
    raw = p.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _names(ev) -> list:
    return [e["event"] for e in ev]


def _count(ev, name: str) -> int:
    return sum(1 for e in ev if e["event"] == name)


def _chat_input():
    return SimpleNamespace(messages=[{"role": "user", "content": "hi"}],
                           system_prompt=None, model="snap-model")


def test_a_invocation_count_starts_at_zero_and_selection_is_recorded(tmp_path):
    p = tmp_path / "c.jsonl"
    f = CancellableProviderFactory(record_path=p, stall_seconds=30.0)
    assert f.invocations == 0
    assert f.snapshots == []
    assert _events(p) == []

    f(_chat_input(), db=None, user_id=7)
    assert f.invocations == 1
    ev = _events(p)
    assert _count(ev, "cancel_provider_selected") == 1
    # selection is NOT yet an invocation: nothing has been consumed
    assert _count(ev, "cancel_provider_invoked") == 0


def test_b_prefix_is_yielded_before_the_stall(tmp_path):
    p = tmp_path / "c.jsonl"
    f = CancellableProviderFactory(record_path=p, stall_seconds=30.0)
    prov = f(_chat_input(), db=None, user_id=1)

    async def main():
        agen = prov.generate_stream_async(messages=[], system_prompt=None,
                                          model_name="m")
        got = []
        async for chunk in agen:
            got.append(chunk)
            if len(got) >= 3:
                break
        await agen.aclose()
        return got

    chunks = asyncio.run(main())
    text = "".join(c["token"] for c in chunks)
    assert text == CANCEL_PREFIX[: len(text)]
    assert CANCEL_SENTINEL in text
    assert CANCEL_COMPLETION_SENTINEL not in CANCEL_PREFIX

    ev = _events(p)
    assert _count(ev, "cancel_provider_invoked") == 1
    assert _count(ev, "cancel_provider_chunk") == 3
    assert _count(ev, "cancel_provider_completed") == 0


def test_c_provider_stays_open_until_stopped_and_closes_exactly_once(tmp_path):
    p = tmp_path / "c.jsonl"
    f = CancellableProviderFactory(record_path=p, stall_seconds=30.0)

    async def main():
        prov = f(_chat_input(), db=None, user_id=1)
        agen = prov.generate_stream_async(messages=[], system_prompt=None,
                                          model_name="m")

        async def drain():
            async for _ in agen:
                pass

        task = asyncio.ensure_future(drain())
        await asyncio.sleep(0.5)
        mid = _names(_events(p))
        # the whole prefix is out and the provider is OPEN, producing nothing
        assert "cancel_provider_stalled" in mid, mid
        assert "cancel_provider_completed" not in mid, mid
        assert "cancel_provider_closed" not in mid, mid
        assert task.done() is False, "the provider must still be active"

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    ev = _events(p)
    assert _count(ev, "cancel_provider_cancelled") == 1
    assert _count(ev, "cancel_provider_closed") == 1
    assert _count(ev, "cancel_provider_completed") == 0


def test_d_real_adrive_guard_aborts_the_stalled_provider_exactly_once(tmp_path):
    p = tmp_path / "c.jsonl"
    f = CancellableProviderFactory(record_path=p, stall_seconds=30.0)
    ci = _chat_input()
    tokens: list = []
    idles = {"n": 0}
    now = lambda: _dt.datetime.now(_dt.timezone.utc)

    def on_idle():
        idles["n"] += 1
        raise StreamAborted("lease lost / cancelled")

    async def main():
        prov = f(ci, db=None, user_id=1)
        with pytest.raises(StreamAborted):
            await _adrive(prov, ci, on_token=tokens.append, on_idle=on_idle,
                          deadline=now() + _dt.timedelta(seconds=300),
                          now_fn=now, poll_interval=0.05)
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return leftover

    leftover = asyncio.run(main())
    # the REAL production driver delivered the prefix, then hit the idle guard
    assert "".join(tokens) == CANCEL_PREFIX
    assert idles["n"] >= 1
    assert leftover == [], "no pending asyncio task may survive the abort"

    ev = _events(p)
    assert _count(ev, "cancel_provider_invoked") == 1
    assert _count(ev, "cancel_provider_stalled") == 1
    assert _count(ev, "cancel_provider_cancelled") == 1
    assert _count(ev, "cancel_provider_closed") == 1
    assert _count(ev, "cancel_provider_completed") == 0


def test_e_default_stall_cannot_complete_inside_a_browser_window():
    assert DEFAULT_CANCEL_STALL_SECONDS >= 300.0, DEFAULT_CANCEL_STALL_SECONDS
    f = CancellableProviderFactory()
    assert f.stall_seconds == DEFAULT_CANCEL_STALL_SECONDS
    assert f.max_invocations == 1
    assert CANCEL_COMPLETION_SENTINEL not in CANCEL_PREFIX
    assert CANCEL_SENTINEL in CANCEL_PREFIX


def test_f_default_d6b_j1_j2_j3_fixtures_are_unchanged():
    from tests.smoke import fakes

    assert fakes.SMOKE_RESPONSE.startswith(fakes.SENTINEL_BEGIN)
    assert CANCEL_SENTINEL not in fakes.SMOKE_RESPONSE
    g = fakes.DeterministicChatProviderFactory()
    assert g.max_invocations == 1
    assert g.chunk_delay_s == fakes.DEFAULT_CHUNK_DELAY_S

    from tests.smoke.mcp_fakes import ECHO_RESULT, MARKER

    assert ECHO_RESULT == "SMOKE_ECHO_OK"
    assert MARKER == '[MCP_CALL: mcp__smoke__echo {"text":"ping"}]'


def test_g_invocation_bound_fails_closed(tmp_path):
    f = CancellableProviderFactory(record_path=tmp_path / "c.jsonl",
                                   stall_seconds=30.0, max_invocations=1)
    ci = _chat_input()
    f(ci, db=None, user_id=1)
    second = f(ci, db=None, user_id=1)

    async def main():
        with pytest.raises(SmokeInvocationLimitExceeded):
            async for _ in second.generate_stream_async(
                    messages=[], system_prompt=None, model_name="m"):
                pass

    asyncio.run(main())
