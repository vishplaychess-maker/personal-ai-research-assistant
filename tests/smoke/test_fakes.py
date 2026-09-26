"""Focused TDD tests for the D6B smoke fakes (MP-3C3D6B).

Run:  PYTHONPATH=backend python -m pytest tests/smoke/test_fakes.py -q
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.run_engine.chat_stream import ACTION_MARKERS


def _drain(provider) -> list:
    """Collect every chunk the fake provider yields."""

    async def _run():
        out = []
        async for chunk in provider.generate_stream_async(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt=None, model_name="snap-model",
        ):
            out.append(chunk)
        return out

    return asyncio.run(_run())


def test_response_is_large_enough_to_exercise_publish_and_close():
    """The response must exceed the text-event flush threshold so that BOTH the
    mid-stream `publish()` flush AND the final `close()` flush actually run."""
    from tests.smoke.fakes import SMOKE_RESPONSE

    assert len(SMOKE_RESPONSE.encode("utf-8")) > int(settings.run_event_text_flush_bytes)


def test_chunks_reconstruct_the_response_exactly_and_are_utf8_safe():
    from tests.smoke.fakes import SMOKE_RESPONSE, DeterministicChatProvider

    chunks = _drain(DeterministicChatProvider())
    tokens = [c["token"] for c in chunks if c["type"] == "token"]

    assert len(tokens) > 1, "must stream several chunks"
    assert "".join(tokens) == SMOKE_RESPONSE
    # every chunk is independently valid UTF-8 (no split codepoint, no lone surrogate)
    for t in tokens:
        t.encode("utf-8")
    # the final chunk is a partial one, so close() has something left to flush
    sizes = [len(t.encode("utf-8")) for t in tokens]
    assert min(sizes) < max(sizes), "expected an uneven final partial chunk"


def test_response_contains_multibyte_characters():
    from tests.smoke.fakes import SMOKE_RESPONSE

    assert len(SMOKE_RESPONSE) != len(SMOKE_RESPONSE.encode("utf-8")), "must include multibyte text"
    assert any(ord(ch) > 127 for ch in SMOKE_RESPONSE)


def test_response_carries_no_executable_or_deferred_tool_marker():
    from tests.smoke.fakes import SMOKE_RESPONSE

    for marker in ACTION_MARKERS:
        assert marker not in SMOKE_RESPONSE
    assert "[" not in SMOKE_RESPONSE and "]" not in SMOKE_RESPONSE


def test_response_is_markdown_neutral_so_dom_text_can_be_compared_exactly():
    from tests.smoke.fakes import SMOKE_RESPONSE

    # no whitespace runs, no leading/trailing space, no newlines -> browser text
    # normalisation is a no-op and the comparison stays exact.
    assert SMOKE_RESPONSE == SMOKE_RESPONSE.strip()
    assert "  " not in SMOKE_RESPONSE
    assert "\n" not in SMOKE_RESPONSE and "\t" not in SMOKE_RESPONSE
    for ch in "*_#`>|~":
        assert ch not in SMOKE_RESPONSE


def test_done_chunk_repeats_the_full_response():
    from tests.smoke.fakes import SMOKE_RESPONSE, DeterministicChatProvider

    chunks = _drain(DeterministicChatProvider())
    done = [c for c in chunks if c["type"] == "done"]
    assert len(done) == 1
    assert done[0]["response"] == SMOKE_RESPONSE
    assert chunks[-1]["type"] == "done", "done must be the terminal chunk"


def test_factory_records_the_snapshot_and_refuses_a_second_invocation():
    from tests.smoke.fakes import (
        DeterministicChatProviderFactory, SmokeInvocationLimitExceeded,
    )

    factory = DeterministicChatProviderFactory()
    assert factory.invocations == 0

    class _Snap:
        provider = "snap-prov"
        model = "snap-model"
        messages = [1, 2, 3]

    provider = factory(_Snap(), db=None, user_id=7)
    assert factory.invocations == 1
    assert factory.snapshots == [{"provider": "snap-prov", "model": "snap-model",
                                  "message_count": 3, "user_id": 7}]

    _drain(provider)   # first real invocation is fine

    second = factory(_Snap(), db=None, user_id=7)
    assert factory.invocations == 2
    with pytest.raises(SmokeInvocationLimitExceeded):
        _drain(second)


def test_factory_defaults_allow_exactly_one_invocation():
    from tests.smoke.fakes import DeterministicChatProviderFactory

    factory = DeterministicChatProviderFactory()
    assert factory.max_invocations == 1
