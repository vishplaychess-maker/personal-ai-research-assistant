"""Phase 1B.2R MP-4C — focused runtime + AST evidence for the security invariant:

    No public model-controlled path may execute Python, invoke a Python
    subprocess, mutate durable memory, or mutate the knowledge graph before a
    valid server-owned approval is consumed — under EVERY flag combination.

These tests are executable evidence, not documentation. They assert exact
values (never "any error"), and the AST guard fails closed on any surviving
legacy direct-execution call site.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.config import Settings, settings
from app.run_engine import sandbox_policy as sp

REPO = pathlib.Path(__file__).resolve().parents[1]

# The two legacy modules that used to auto-execute model-emitted Python in
# plain violation of the invariant (streaming_service x2, langgraph_workflow x2).
LEGACY_EXEC_FILES = (
    "backend/app/services/streaming_service.py",
    "backend/app/services/langgraph_workflow.py",
)
FORBIDDEN_CALLS = frozenset({
    "run_python_code", "run_python_code_async", "execute_python_code",
})


# ── Stage 1: the flag ────────────────────────────────────────────────

def test_run_engine_sandbox_defaults_false():
    assert Settings().run_engine_sandbox is False


def test_sandbox_bounds_are_positive():
    s = Settings()
    assert s.run_engine_sandbox_max_code_bytes >= 1
    assert s.run_engine_sandbox_timeout_s >= 1
    assert s.run_engine_sandbox_max_output_bytes >= 1


@pytest.mark.parametrize("flag", [False, True])
def test_legacy_never_may_execute_python_under_either_flag(monkeypatch, flag):
    """run_engine_sandbox=True authorizes a PROPOSAL, never execution."""
    monkeypatch.setattr(settings, "run_engine_sandbox", flag)
    assert sp.legacy_may_execute_python() is False
    assert sp.sandbox_enabled() is flag


# ── Stage 2: the strict marker contract ──────────────────────────────

def test_valid_single_marker_parses():
    code, reason = sp.strict_extract_python_code("hello [PYTHON_CODE: print(1)] bye")
    assert code == "print(1)"
    assert reason is None


def test_marker_split_across_chunks_still_parses_once_assembled():
    parts = ["[PYTHON", "_CODE: pri", "nt(1)]"]
    code, reason = sp.strict_extract_python_code("".join(parts))
    assert code == "print(1)"
    assert reason is None


@pytest.mark.parametrize("text,expected", [
    ("[PYTHON_CODE: a] [PYTHON_CODE: b]", "sandbox_marker_multiple"),
    ("[PYTHON_CODE: print(1)", "sandbox_marker_incomplete"),
    ("[PYTHON_CODE: ]", "sandbox_marker_malformed"),
    ("[PYTHON_CODE: [PYTHON_CODE: x]]", "sandbox_marker_nested"),
    ("[PYTHON_CODE: a] [MCP_CALL: mcp__s__t]", "sandbox_marker_mixed"),
    ("[PYTHON_CODE: a] [PROPOSED_COMMAND: rm -rf /]", "sandbox_marker_mixed"),
])
def test_adversarial_markers_fail_closed_with_exact_reason(text, expected):
    code, reason = sp.strict_extract_python_code(text)
    assert code is None
    assert reason == expected


def test_oversized_marker_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox_max_code_bytes", 16)
    code, reason = sp.strict_extract_python_code("[PYTHON_CODE: " + "x" * 64 + "]")
    assert code is None
    assert reason == "sandbox_marker_oversized"


def test_no_marker_is_not_an_error():
    assert sp.strict_extract_python_code("just prose") == (None, None)
    assert sp.strict_extract_python_code("") == (None, None)
    assert sp.strict_extract_python_code(None) == (None, None)


def test_every_reason_is_a_bounded_slug():
    for reason in sp.SANDBOX_STATUS_REASONS:
        assert reason.startswith("sandbox_")
        assert reason.isascii() and len(reason) <= 64


# ── Stage 2: marker suppression ──────────────────────────────────────

def test_marker_is_stripped_from_visible_text():
    out = sp.strip_python_marker("before [PYTHON_CODE: print(1)] after")
    assert "PYTHON_CODE" not in out
    assert "print(1)" not in out
    assert "before" in out and "after" in out


def test_fail_closed_notice_never_echoes_code_or_marker():
    notice = sp.legacy_python_fail_closed("x [PYTHON_CODE: SECRET_TOKEN] y")
    assert notice == sp.LEGACY_PYTHON_BLOCK_NOTICE
    assert "SECRET_TOKEN" not in notice
    assert "PYTHON_CODE" not in notice


def test_fail_closed_notice_is_empty_without_a_marker():
    assert sp.legacy_python_fail_closed("plain answer") == ""


# ── Stage 5: legacy call sites are gone (AST guard) ──────────────────

def test_no_legacy_direct_python_execution_call_sites():
    offenders = []
    for rel in LEGACY_EXEC_FILES:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in FORBIDDEN_CALLS):
                offenders.append("%s:%d %s" % (rel, node.lineno, node.func.id))
    assert offenders == [], "legacy direct Python execution survives: " + repr(offenders)


def test_legacy_modules_call_the_policy_authority():
    for rel in LEGACY_EXEC_FILES:
        src = (REPO / rel).read_text(encoding="utf-8")
        assert "legacy_python_fail_closed" in src, rel + " does not use the MP-4C authority"


# ── Stage 6: model-emitted memory writes are proposals only ──────────

class _NoDb:
    """Any attribute access is a write/read attempt — proof of zero DB use."""

    def __getattr__(self, name):  # pragma: no cover - only on violation
        raise AssertionError("process_memory_markers touched the DB: " + str(name))


def test_memory_marker_writes_nothing_and_never_touches_the_db():
    from app.tools.memory_tool import process_memory_markers

    cleaned, saved = process_memory_markers(
        "keep me [SAVE_MEMORY: the user likes tea] please", _NoDb(), 1,
    )
    assert saved == 0
    assert "SAVE_MEMORY" not in cleaned
    assert "likes tea" not in cleaned


def test_memory_write_requires_approval_is_true():
    from app.tools.memory_tool import memory_write_requires_approval

    assert memory_write_requires_approval() is True
