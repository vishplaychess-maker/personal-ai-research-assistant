"""
F6 Capability 2 �?" self-evaluation / confidence score tests.

Covers the evaluation service and the LangGraph ``self_evaluate`` node:

  * ``parse_evaluation_response`` turns LLM JSON into a confidence score
    (0�?"100) plus a reason.
  * The confidence is clamped to the valid range.
  * Malformed / non-JSON output falls back gracefully to a null confidence
    (never breaks the chat).
  * The ``self_evaluate`` node stores confidence / confidence_reason on the
    workflow state.
  * ``save_output`` persists confidence onto the assistant Message row.

Uses FastAPI TestClient (in-process) + LocalProvider where an HTTP round
trip is needed; the parser is pure and tested directly.
"""

import pytest

from app.services.evaluation_service import parse_evaluation_response


# ── parse_evaluation_response: valid output ───────────────


def test_parse_evaluation_response_returns_confidence_and_reason():
    text = '{"confidence": 85, "reason": "Confident in sources"}'
    result = parse_evaluation_response(text)
    assert result["confidence"] == 85
    assert result["reason"] == "Confident in sources"


def test_parse_evaluation_response_zero_confidence():
    text = '{"confidence": 0, "reason": "unsure"}'
    result = parse_evaluation_response(text)
    assert result["confidence"] == 0


# ── parse_evaluation_response: clamping ──────────────────


def test_parse_evaluation_response_clamps_high_confidence():
    text = '{"confidence": 150, "reason": "overconfident"}'
    result = parse_evaluation_response(text)
    assert result["confidence"] == 100


def test_parse_evaluation_response_clamps_low_confidence():
    text = '{"confidence": -10, "reason": "negative"}'
    result = parse_evaluation_response(text)
    assert result["confidence"] == 0


# ── parse_evaluation_response: graceful fallback ─────────


def test_parse_evaluation_response_none_returns_null():
    result = parse_evaluation_response(None)
    assert result["confidence"] is None
    assert result["reason"] == ""


def test_parse_evaluation_response_invalid_json_returns_null():
    result = parse_evaluation_response("this is not json")
    assert result["confidence"] is None
    assert result["reason"] == ""


def test_parse_evaluation_response_non_int_confidence_returns_null():
    result = parse_evaluation_response('{"confidence": "high", "reason": "x"}')
    assert result["confidence"] is None


# ── self_evaluate node: workflow integration ──────────────


def test_self_evaluate_node_stores_confidence(monkeypatch):
    from app.services import evaluation_service
    from app.services.langgraph_workflow import self_evaluate

    monkeypatch.setattr(
        evaluation_service,
        "evaluate_response",
        lambda **kw: {"confidence": 92, "reason": "solid"},
    )
    state = {
        "user_input": "Q",
        "response": "A",
        "messages": [],
        "db": None,
        "error": None,
        "confidence": None,
        "confidence_reason": None,
    }
    result = self_evaluate(state)
    assert result["confidence"] == 92
    assert result["confidence_reason"] == "solid"


def test_self_evaluate_node_bad_output_sets_none(monkeypatch):
    from app.services import evaluation_service
    from app.services.langgraph_workflow import self_evaluate

    monkeypatch.setattr(
        evaluation_service,
        "evaluate_response",
        lambda **kw: parse_evaluation_response("garbage"),
    )
    state = {
        "user_input": "Q",
        "response": "A",
        "messages": [],
        "db": None,
        "error": None,
        "confidence": None,
        "confidence_reason": None,
    }
    result = self_evaluate(state)
    assert result["confidence"] is None
    assert result["error"] is None


# ── save_output persists confidence on Message ────────────


def test_save_output_persists_confidence(monkeypatch):
    """save_output must write confidence + confidence_reason to the Message."""
    from app.services.langgraph_workflow import save_output

    class _FakeMsg:
        def __init__(self, **kw):
            self.session_id = kw["session_id"]
            self.role = kw["role"]
            self.content = kw["content"]
            self.citations = kw.get("citations")
            self.confidence = kw.get("confidence")
            self.confidence_reason = kw.get("confidence_reason")
            self.id = 1

    class _FakeDB:
        def __init__(self):
            self.added = None

        def query(self, model):
            class _Q:
                def filter(self, *a, **k):
                    return self

                def first(self):
                    return None
            return _Q()

        def add(self, obj):
            self.added = obj

        def commit(self):
            pass

        def refresh(self, obj):
            pass

    state = {
        "error": None,
        "response": "answer",
        "assistant_message_id": None,
        "session_id": 1,
        "citations": [],
        "db": _FakeDB(),
        "confidence": 90,
        "confidence_reason": "high",
    }
    result = save_output(state)
    assert result["confidence"] == 90
    saved = result["db"].added
    assert saved.confidence == 90
    assert saved.confidence_reason == "high"