"""Owner decision 2 (MP-4C): model-initiated persistent writes need approval.

Verified here: the two model-marker write processors persist NOTHING, never
touch the DB, never log the requested content, and emit a bounded user-visible
notice; the canonical proposal authority is Run-bound, so a legacy workflow
without a canonical Run cannot create or resume a proposal (documented
limitation, fail closed); explicit user-initiated saves are a different path.
"""

from __future__ import annotations

import inspect

import pytest

from app.config import settings


class _NoDb:
    def __getattr__(self, name):  # pragma: no cover - only on violation
        raise AssertionError("model marker write touched the DB: " + str(name))


def test_memory_marker_writes_nothing_and_emits_a_notice():
    from app.tools.memory_tool import (MEMORY_WRITE_REQUIRES_APPROVAL_NOTICE,
                                       process_memory_markers)

    cleaned, saved = process_memory_markers(
        "keep me [SAVE_MEMORY: the user likes tea] please", _NoDb(), 1)
    assert saved == 0
    assert "SAVE_MEMORY" not in cleaned and "likes tea" not in cleaned
    assert MEMORY_WRITE_REQUIRES_APPROVAL_NOTICE in cleaned


def test_directive_marker_writes_nothing_and_emits_a_notice():
    from app.tools.directive_tool import (DIRECTIVE_WRITE_REQUIRES_APPROVAL_NOTICE,
                                          process_directive_markers)

    cleaned, saved = process_directive_markers(
        "ok [SAVE_DIRECTIVE: always answer in British English] done", _NoDb(), 1)
    assert saved == 0
    assert "SAVE_DIRECTIVE" not in cleaned
    assert "British English" not in cleaned
    assert DIRECTIVE_WRITE_REQUIRES_APPROVAL_NOTICE in cleaned


def test_the_requested_content_is_never_logged(caplog):
    from app.tools.directive_tool import process_directive_markers
    from app.tools.memory_tool import process_memory_markers

    with caplog.at_level("DEBUG"):
        process_memory_markers("[SAVE_MEMORY: secret-tea-preference]", _NoDb(), 1)
        process_directive_markers("[SAVE_DIRECTIVE: secret-lesson-text]", _NoDb(), 1)
    assert "secret-tea-preference" not in caplog.text
    assert "secret-lesson-text" not in caplog.text


def test_no_flag_combination_re_enables_a_model_write(monkeypatch):
    from app.tools.directive_tool import process_directive_markers
    from app.tools.memory_tool import process_memory_markers

    for flag in (False, True):
        monkeypatch.setattr(settings, "run_engine_sandbox", flag)
        assert process_memory_markers("[SAVE_MEMORY: x]", _NoDb(), 1)[1] == 0
        assert process_directive_markers("[SAVE_DIRECTIVE: y]", _NoDb(), 1)[1] == 0


def test_canonical_proposal_authority_is_run_bound():
    import app.approval.action_preview as ap
    import app.approval.gateway as gw

    gw_src = inspect.getsource(gw)
    assert "run_id" in gw_src and "ctx.run_id" in gw_src
    ap_src = inspect.getsource(ap)
    assert "row.run_id" in ap_src and "row.tool_call_id" in ap_src


def test_explicit_user_saves_are_a_separate_path():
    import app.routes.memories as mem_route
    import app.tools.directive_tool as dt
    import app.tools.memory_tool as mt

    assert "memory_service" in inspect.getsource(mem_route)
    assert "save_memory(" not in inspect.getsource(mt)
    assert "AgentDirective(" not in inspect.getsource(dt)


def test_every_model_marker_call_site_still_routes_through_the_processors():
    import app.routes.messages as msg
    import app.services.langgraph_workflow as lw
    import app.services.streaming_service as ss

    for mod, minimum in ((lw, 3), (ss, 2), (msg, 2)):
        src = inspect.getsource(mod)
        total = src.count("process_memory_markers") + src.count("process_directive_markers")
        assert total >= minimum, (mod.__name__, total)
