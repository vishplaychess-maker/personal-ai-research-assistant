"""Swarm handoff, tool grounding, and orchestrator fallback.

The roster is fixed (Planner → Researcher → Synthesizer); these tests pin the
sequential handoff through :class:`SwarmState`, the real ``agent_events``
frames per role, the researcher's tool fallback, and the exact orchestrator
fallback behavior for every swarm failure mode.
"""

from __future__ import annotations

import logging

import pytest

from app.config import settings
from app.services import agent_events, orchestrator, swarm_manager
from app.services.skill_executors import WebSearchNotConfigured

TASK = "swarm-unit-test"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "swarm_timeout_s", 30)
    monkeypatch.setattr(settings, "swarm_agent_timeout_s", 5)
    monkeypatch.setattr(settings, "swarm_max_evidence_chars", 4000)
    agent_events.clear(TASK)


def _fake_llm(monkeypatch, calls):
    def fake(user_id, system_prompt, user_prompt):
        if "Planner" in system_prompt:
            calls.append("planner")
            return "1. Inspect the workspace\n2. Summarize the services\n3. List gaps"
        if "Researcher" in system_prompt:
            calls.append("researcher")
            return "- Three services exist\n- One has no tests"
        calls.append("synthesizer")
        return "# Services report\n\n## Summary\nAll three services are healthy."

    monkeypatch.setattr(swarm_manager, "_agent_completion", fake)


def test_default_roster():
    names = [agent.name for agent in swarm_manager.DEFAULT_SWARM]
    assert names == ["Planner", "Researcher", "Synthesizer"]
    researcher = swarm_manager.DEFAULT_SWARM[1]
    assert researcher.allowed_tools == ["web_search", "search_workspace_files"]
    assert all(agent.goal and agent.backstory for agent in swarm_manager.DEFAULT_SWARM)


def test_sequential_handoff_and_events(monkeypatch):
    calls: list[str] = []
    _fake_llm(monkeypatch, calls)
    monkeypatch.setattr(
        swarm_manager,
        "_gather_evidence",
        lambda task_id, query: (
            "### Workspace files\n- backend/app/services/x.py",
            ["search_workspace_files"],
            [],
        ),
    )

    result = swarm_manager.run_swarm_task(TASK, "summarize the python services", 7)

    assert calls == ["planner", "researcher", "synthesizer"]
    assert result.startswith("# Services report")

    events = agent_events.events_since(TASK)
    by_agent = {}
    for event in events:
        by_agent.setdefault(event["agent"], []).append(event)

    assert set(by_agent) >= {"planner", "researcher", "synthesizer"}
    # Planner first, then Researcher, then Synthesizer (strict handoff order).
    assert (
        by_agent["planner"][0]["seq"]
        < by_agent["researcher"][0]["seq"]
        < by_agent["synthesizer"][0]["seq"]
    )
    assert any(e["type"] == "plan" for e in by_agent["planner"])
    assert any(e["type"] == "output" for e in by_agent["researcher"])
    assert any(e["type"] == "synthesis" for e in by_agent["synthesizer"])
    # The final result reaches SSE as a real output frame.
    assert any(
        e["type"] == "output" and e["agent"] == "synthesizer" for e in events
    )


def test_planner_output_is_parsed_and_passed_downstream(monkeypatch):
    prompts = {}

    def fake(user_id, system_prompt, user_prompt):
        if "Planner" in system_prompt:
            return "1. alpha\n2. beta\n- step three"
        if "Researcher" in system_prompt:
            prompts["researcher"] = user_prompt
            return "- fact one"
        prompts["synthesizer"] = user_prompt
        return "# final"

    monkeypatch.setattr(swarm_manager, "_agent_completion", fake)
    monkeypatch.setattr(swarm_manager, "_gather_evidence", lambda *a, **k: ("", [], []))

    swarm_manager.run_swarm_task(TASK, "q", 1)

    assert "1. alpha" in prompts["researcher"]
    assert "3. step three" in prompts["researcher"]  # prefix "1. " stripped
    assert "- fact one" in prompts["synthesizer"]  # findings reach the last agent


def test_planner_empty_plan_raises(monkeypatch):
    monkeypatch.setattr(swarm_manager, "_agent_completion", lambda *a, **k: "")
    with pytest.raises(swarm_manager.SwarmError):
        swarm_manager.run_swarm_task(TASK, "q", 1)


def test_researcher_falls_back_to_workspace_when_web_unavailable(monkeypatch):
    def no_web(query, limit=3):
        raise WebSearchNotConfigured("no key")

    monkeypatch.setattr(swarm_manager, "execute_web_search", no_web)
    monkeypatch.setattr(
        swarm_manager.agent_context,
        "search_workspace_files",
        lambda directory, pattern: [
            {"path": "backend/app/services/x.py", "size": 42, "description": "Does x"}
        ],
    )

    text, tools, warnings = swarm_manager._gather_evidence(
        TASK, "search the latest news about the service files"
    )
    assert tools == ["search_workspace_files"]
    assert "x.py" in text
    assert any("not configured" in warning for warning in warnings)


def test_run_swarm_success_marks_completed(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        swarm_manager, "run_swarm_task", lambda tid, query, uid: "# Done"
    )
    monkeypatch.setattr(
        orchestrator,
        "_set_status",
        lambda tid, status, **kwargs: captured.update(
            status=status, result=kwargs.get("result_text")
        ),
    )
    assert orchestrator._run_swarm(TASK, 1, "q") is True
    assert captured == {"status": "completed", "result": "# Done"}
    assert any(
        e["type"] == "status" and e["message"] == "completed"
        for e in agent_events.events_since(TASK)
    )


def test_research_route_forwards_use_swarm(monkeypatch):
    """The dashboard's Swarm toggle must reach the service layer."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import openbot_service
    from tests.auth_helpers import auth_headers, register_and_login

    captured = {}

    def fake_start(uid, query, use_swarm=False):
        captured["use_swarm"] = use_swarm
        return "task-123"

    monkeypatch.setattr(openbot_service, "start_research_task", fake_start)
    with TestClient(app) as client:
        user_id, token = register_and_login(client, username="swarm_route_user")
        headers = auth_headers(token)
        enabled = client.patch(
            "/api/capabilities/openbot_research",
            json={"enabled": True},
            headers=headers,
        )
        assert enabled.status_code == 200, enabled.text[:200]
        response = client.post(
            "/api/agents/openbot/research",
            json={"query": "summarize the services", "use_swarm": True},
            headers=headers,
        )
    assert response.status_code == 200, response.text[:300]
    assert response.json() == {"task_id": "task-123"}
    assert captured == {"use_swarm": True}


@pytest.mark.parametrize(
    "failure",    [
        lambda *a, **k: (_ for _ in ()).throw(swarm_manager.SwarmError("boom")),
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("slow")),
        lambda *a, **k: "   ",
    ],
    ids=["swarm-error", "timeout", "empty-result"],
)
def test_orchestrator_falls_back_on_any_swarm_failure(monkeypatch, caplog, failure):
    monkeypatch.setattr(swarm_manager, "run_swarm_task", failure)
    monkeypatch.setattr(
        orchestrator, "_set_status", lambda *a, **k: pytest.fail("must not finalize")
    )
    with caplog.at_level(logging.WARNING):
        assert orchestrator._run_swarm(TASK, 1, "q") is False
    assert "Swarm execution failed, falling back to single agent." in caplog.text
    warning_events = [
        e
        for e in agent_events.events_since(TASK)
        if e["type"] == "warning" and e["agent"] == "swarm"
    ]
    assert warning_events, "fallback must be visible in the SSE stream"
