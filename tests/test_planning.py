"""
F6 Capability 1 �?" plan-then-execute (plan generation) tests.

Covers the planning service and the LangGraph ``generate_plan`` node:

  * ``parse_plan_response`` converts LLM JSON into a list of plan steps
    (each with step / action / target / reason).
  * An empty array means a simple question that needs no plan.
  * Malformed / non-JSON output falls back gracefully to an empty plan
    (never breaks the chat).
  * A ```json ... ``` markdown fence is tolerated.
  * The ``generate_plan`` node stores ``proposed_plan`` on the workflow
    state and degrades gracefully when the provider returns bad output.

Uses FastAPI TestClient (in-process) + LocalProvider where an HTTP round
trip is needed; the parser itself is pure and tested directly. No live
Ollama / external LLM required.
"""

import pytest

from app.services.planning_service import parse_plan_response


# ── parse_plan_response: valid plan ────────────────────────


def test_parse_plan_response_returns_steps():
    text = (
        '[{"step": 1, "action": "retrieve", "target": "documents", '
        '"reason": "need source"}, '
        '{"step": 2, "action": "summarize", "target": "papers", '
        '"reason": "extract findings"}]'
    )
    plan = parse_plan_response(text)
    assert len(plan) == 2
    assert plan[0]["step"] == 1
    assert plan[0]["action"] == "retrieve"
    assert plan[0]["target"] == "documents"
    assert plan[0]["reason"] == "need source"
    assert plan[1]["action"] == "summarize"


def test_parse_plan_response_empty_for_simple_question():
    """A simple question yields an empty plan (no steps needed)."""
    assert parse_plan_response("[]") == []


# ── parse_plan_response: graceful fallback ────────────────


def test_parse_plan_response_none_returns_empty():
    assert parse_plan_response(None) == []


def test_parse_plan_response_empty_string_returns_empty():
    assert parse_plan_response("") == []


def test_parse_plan_response_invalid_json_returns_empty():
    assert parse_plan_response("this is not json") == []


def test_parse_plan_response_not_a_list_returns_empty():
    """Only a JSON list is a valid plan; a dict/object is not."""
    assert parse_plan_response('{"plan": []}') == []


def test_parse_plan_response_missing_fields_skips_step():
    """Steps missing required fields are dropped rather than crashing."""
    text = '[{"step": 1, "action": "ok", "target": "t", "reason": "r"}, {"step": 2}]'
    plan = parse_plan_response(text)
    assert len(plan) == 1
    assert plan[0]["action"] == "ok"


# ── parse_plan_response: markdown fence ────────────────────


def test_parse_plan_response_strips_markdown_fence():
    text = '```json\n[{"step": 1, "action": "search", "target": "web", "reason": "r"}]\n```'
    plan = parse_plan_response(text)
    assert len(plan) == 1
    assert plan[0]["action"] == "search"


# ── generate_plan node: workflow integration ───────────────


def test_generate_plan_node_stores_proposed_plan(monkeypatch):
    """The generate_plan node stores proposed_plan on workflow state."""
    from app.services import planning_service
    from app.services.langgraph_workflow import generate_plan

    def fake_generate(query, **kw):
        return [{"step": 1, "action": "search", "target": "web", "reason": "r"}]

    monkeypatch.setattr(planning_service, "generate_plan_for_query", fake_generate)
    state = {
        "user_input": "Compare A and B",
        "messages": [],
        "db": None,
        "error": None,
        "proposed_plan": [],
        "plan_pending": False,
    }
    result = generate_plan(state)
    assert result["proposed_plan"] == [
        {"step": 1, "action": "search", "target": "web", "reason": "r"}
    ]
    # Simple question path fills these keys so the caller can react
    assert "plan_pending" in result


def test_generate_plan_node_handles_bad_provider_output(monkeypatch):
    """Even with garbage LLM output the node must not raise or error out."""
    from app.services import planning_service
    from app.services.langgraph_workflow import generate_plan

    monkeypatch.setattr(
        planning_service,
        "generate_plan_for_query",
        lambda query, **kw: parse_plan_response("garbage"),
    )
    state = {
        "user_input": "Hello",
        "messages": [],
        "db": None,
        "error": None,
        "proposed_plan": [],
        "plan_pending": False,
    }
    result = generate_plan(state)
    assert result["proposed_plan"] == []
    assert result["error"] is None
