"""Phase 1B.2R MP-4C-R2A-1 — MarkerScanner opt-in Python capture.

One atomic capability: the EXISTING scanner may confidentially capture a
complete [PYTHON_CODE: ...] marker when explicitly opted in. Nothing is
validated, executed or persisted here, and the default constructor is
state-for-state unchanged.

Security assertions made by this suite:
* default scanner: Python stays `unsupported`, nothing captured;
* opt-in: only Python becomes capturable — shell and browser stay `unsupported`;
* MCP capture is byte-for-byte unchanged;
* deferred markers unchanged;
* the canonical Python body is recoverable through the EXISTING strict parser;
* no marker prefix or code ever reaches visible output;
* overflow / incomplete-at-EOF fail closed with NO partial body in `captured`.
"""

from __future__ import annotations

import pytest

from app.run_engine.chat_stream import ACTION_MARKERS, DEFERRED_MARKERS, MarkerScanner
from app.run_engine.sandbox_policy import strict_extract_python_code

PY = "[PYTHON_CODE: print(1)]"
MCP = '[MCP_CALL: mcp__srv__echo {"a": 1}]'
SHELL = "[PROPOSED_COMMAND: rm -rf /]"
BROWSER = "[BROWSER_ACTION: click]"
DEFERRED = "[SAVE_MEMORY: the user likes tea]"

CAP = 4096


def _optin(**kw):
    return MarkerScanner(capture=True, max_capture_bytes=kw.pop("cap", CAP),
                         capture_python=True, **kw)


# 1 — default constructor: Python remains unsupported, nothing captured
def test_default_scanner_leaves_python_unsupported():
    sc = MarkerScanner(capture=True, max_capture_bytes=CAP)
    out = sc.feed("hello " + PY + "\n")
    assert sc.capture_status == "unsupported"
    assert sc.captured is None
    assert sc.captured_kind is None
    assert out == "hello "
    assert "PYTHON_CODE" not in out and "print(1)" not in out
    assert sc.capture_done is True


# 2 — opt-in: complete capture, split across many chunks
def test_optin_captures_complete_python_marker_across_chunks():
    sc = _optin()
    visible = "".join(sc.feed(c) for c in
                      ["ok ", "[PYTH", "ON_CODE: pri", "nt(1)]", "\n", "tail"])
    assert sc.capture_status == "complete"
    assert sc.captured_kind == "python"
    code, reason = strict_extract_python_code(sc.captured)
    assert (code, reason) == ("print(1)", None)
    assert visible == "ok "
    assert "PYTHON_CODE" not in visible and "print" not in visible


def test_optin_capture_completes_at_eof_without_newline():
    sc = _optin()
    sc.feed(PY)
    assert sc.finish() == ""
    assert sc.capture_status == "complete"
    assert sc.captured_kind == "python"
    assert strict_extract_python_code(sc.captured) == ("print(1)", None)


# 3 — opt-in must not change the MCP contract
def test_optin_mcp_capture_is_unchanged():
    sc = _optin()
    sc.feed(MCP + "\n")
    assert sc.capture_status == "complete"
    assert sc.captured_kind == "mcp"
    assert sc.captured == MCP


def test_default_and_optin_mcp_captures_are_identical():
    a = MarkerScanner(capture=True, max_capture_bytes=CAP)
    b = _optin()
    a.feed(MCP + "\n")
    b.feed(MCP + "\n")
    assert (a.capture_status, a.captured) == (b.capture_status, b.captured) == ("complete", MCP)


# 4 / 5 — shell and browser stay unsupported even when opted in
@pytest.mark.parametrize("marker", [SHELL, BROWSER])
def test_optin_shell_and_browser_stay_unsupported(marker):
    sc = _optin()
    out = sc.feed("pre " + marker + "\n")
    assert sc.capture_status == "unsupported"
    assert sc.captured is None and sc.captured_kind is None
    assert out == "pre "
    assert marker.split(":")[0] not in out


# 6 — deferred markers unchanged
def test_deferred_marker_behavior_unchanged():
    markers = ACTION_MARKERS + DEFERRED_MARKERS
    a = MarkerScanner(markers, capture=True, max_capture_bytes=CAP)
    b = MarkerScanner(markers, capture=True, max_capture_bytes=CAP, capture_python=True)
    assert a.feed("x " + DEFERRED + "\n") == b.feed("x " + DEFERRED + "\n") == "x "
    assert a.capture_status == b.capture_status == "deferred"
    assert a.captured is None and b.captured is None


# 7 — oversized: bounded failure, NO partial body exposed
def test_oversized_python_capture_fails_bounded_without_partial_body():
    sc = _optin(cap=16)
    out = sc.feed("[PYTHON_CODE: " + "x" * 400 + "]\n")
    assert sc.capture_status == "overflow"
    assert sc.captured is None and sc.captured_kind == "python"
    assert out == ""
    assert "xxxx" not in out


# 8 — incomplete at EOF: fail closed, no partial body, no leak
def test_incomplete_python_capture_at_eof_fails_closed():
    sc = _optin()
    sc.feed("[PYTHON_CODE: print(1)")
    visible = sc.finish()
    assert sc.capture_status == "incomplete"
    assert sc.captured is None
    assert visible == ""
    assert "print(1)" not in visible


# 9 — strict failure behavior preserved through the existing parser
def test_multiple_python_markers_still_fail_closed():
    sc = _optin()
    sc.feed("[PYTHON_CODE: a] [PYTHON_CODE: b]\n")
    assert sc.capture_status == "complete"
    assert strict_extract_python_code(sc.captured) == (None, "sandbox_marker_multiple")


def test_mixed_python_and_mcp_still_fail_closed():
    sc = _optin()
    sc.feed("[PYTHON_CODE: a] " + MCP + "\n")
    assert sc.capture_status == "complete"
    assert strict_extract_python_code(sc.captured) == (None, "sandbox_marker_mixed")


def test_nested_python_marker_still_fails_closed():
    sc = _optin()
    sc.feed("[PYTHON_CODE: [PYTHON_CODE: x]]\n")
    assert sc.capture_status == "complete"
    assert strict_extract_python_code(sc.captured) == (None, "sandbox_marker_nested")


def test_prose_without_markers_is_untouched():
    a = MarkerScanner(capture=True, max_capture_bytes=CAP)
    b = _optin()
    assert a.feed("plain text") == b.feed("plain text") == "plain text"
    assert a.capture_status == b.capture_status == "none"
    assert b.captured is None


# 10 — default constructor regression: signature-compatible construction
def test_default_constructor_signature_unchanged():
    sc = MarkerScanner()
    assert sc.capture_status == "none" and sc.captured is None
    assert sc.captured_kind is None
    assert sc.feed("[PYTHON_CODE: print(1)]") == ""
    assert sc.capture_status == "none"
