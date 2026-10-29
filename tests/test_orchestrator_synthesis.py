"""OpenBot synthesis: LLM path, provider/model fidelity, truncation, fallback."""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.database import SessionLocal
from app.models.models import OpenbotTask, User
from app.services import agent_context, agent_events, orchestrator


@pytest.fixture(autouse=True)
def _synthesis_env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "openbot_synthesis_llm_enabled", True)
    monkeypatch.setattr(settings, "openbot_synthesis_timeout_s", 5)
    monkeypatch.setattr(settings, "openbot_synthesis_max_context_chars", 18000)
    monkeypatch.setattr(
        orchestrator.agent_context,
        "collect",
        lambda db, user_id, emit: agent_context.ContextBundle(
            blocks=["Workspace: none."], tools=[], warnings=[]
        ),
    )
    yield


def _make_task() -> tuple[str, int]:
    db = SessionLocal()
    try:
        username = "synth_" + uuid.uuid4().hex[:8]
        user = User(username=username, email=f"{username}@example.com")
        db.add(user)
        db.commit()
        db.refresh(user)
        task_id = uuid.uuid4().hex
        db.add(
            OpenbotTask(
                user_id=user.id,
                task_id=task_id,
                query="Summarize the task",
                status="pending",
            )
        )
        db.commit()
        return task_id, user.id
    finally:
        db.close()


def _read_task(task_id: str) -> tuple[str, str | None]:
    db = SessionLocal()
    try:
        row = db.query(OpenbotTask).filter(OpenbotTask.task_id == task_id).one()
        return row.status, row.result_text
    finally:
        db.close()


def test_synthesis_uses_user_provider_and_model(monkeypatch):
    captured: dict = {}

    class FakeProvider:
        def generate_response(self, messages, system_prompt=None, model_name=None):
            captured["messages"] = messages
            captured["system_prompt"] = system_prompt
            captured["model_name"] = model_name
            return "# Smart Report\n\n## Summary\nEverything checked out."

    def fake_get_provider(config=None):
        captured["config"] = config
        return FakeProvider()

    monkeypatch.setattr(
        orchestrator,
        "get_user_llm_config",
        lambda db, user_id: {
            "provider": "venice",
            "api_key": "test-key",
            "model": "mistral-small-3-2-24b-instruct",
        },
    )
    monkeypatch.setattr(orchestrator, "get_provider", fake_get_provider)

    result = orchestrator._synthesize_with_llm(
        1,
        "Explain the workspace",
        [{"step": "Synthesize", "agent": "synthesizer"}],
        agent_context.ContextBundle(blocks=["Workspace has 3 files."], tools=["workspace"]),
        "Research says X.",
        "REA evidence.",
        ["One warning."],
    )

    assert result.startswith("# Smart Report")
    assert captured["config"]["provider"] == "venice"
    assert captured["model_name"] == "mistral-small-3-2-24b-instruct"
    assert "Expert AI Research & Execution Analyst" in captured["system_prompt"]
    user_message = captured["messages"][0]["content"]
    assert "Explain the workspace" in user_message
    assert "Research says X." in user_message
    assert "REA evidence." in user_message


def test_synthesis_truncates_oldest_sections_first(monkeypatch):
    monkeypatch.setattr(settings, "openbot_synthesis_max_context_chars", 4000)
    context = agent_context.ContextBundle(blocks=["C" * 10000])

    body = orchestrator._compose_synthesis_user_message(
        "Summarize this task",
        [{"step": "Synthesize", "agent": "synthesizer"}],
        context,
        "R" * 10000,
        "A" * 10000,
        ["warning"],
    )

    assert len(body) <= 4000
    assert "## User request" in body
    assert "Summarize this task" in body
    assert "Omitted" in body or "truncated" in body


def test_fallback_runs_when_provider_times_out(monkeypatch):
    import time

    class SlowProvider:
        def generate_response(self, *args, **kwargs):
            time.sleep(3)
            return "too late"

    monkeypatch.setattr(orchestrator, "get_user_llm_config", lambda db, uid: None)
    monkeypatch.setattr(orchestrator, "get_provider", lambda config=None: SlowProvider())
    monkeypatch.setattr(settings, "openbot_synthesis_timeout_s", 1)

    task_id = uuid.uuid4().hex
    started = time.monotonic()
    result = orchestrator._synthesize_or_fallback(
        task_id,
        1,
        "Summarize the task",
        [{"step": "Synthesize", "agent": "synthesizer"}],
        agent_context.ContextBundle(blocks=["Some context."]),
        "",
        "",
        [],
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.5
    assert result.startswith("# OpenBot Task Result")
    events = agent_events.events_since(task_id, 0)
    assert any(
        event["type"] == "warning" and "TimeoutError" in event["message"]
        for event in events
    )
    agent_events.clear(task_id)


def test_fallback_runs_when_provider_raises(monkeypatch):
    class BrokenProvider:
        def generate_response(self, *args, **kwargs):
            raise RuntimeError("provider down")

    monkeypatch.setattr(orchestrator, "get_user_llm_config", lambda db, uid: None)
    monkeypatch.setattr(orchestrator, "get_provider", lambda config=None: BrokenProvider())

    task_id = uuid.uuid4().hex
    result = orchestrator._synthesize_or_fallback(
        task_id,
        1,
        "Summarize the task",
        [{"step": "Synthesize", "agent": "synthesizer"}],
        agent_context.ContextBundle(blocks=["Some context."]),
        "",
        "",
        [],
    )

    assert result.startswith("# OpenBot Task Result")
    assert "**Request:** Summarize the task" in result
    types = [event["type"] for event in agent_events.events_since(task_id, 0)]
    assert types[:2] == ["synthesis", "warning"]
    agent_events.clear(task_id)


def test_run_task_uses_llm_report_and_emits_synthesis_frame(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "_synthesize_with_llm",
        lambda *args, **kwargs: "# Smart Report\n\n## Summary\nDone.",
    )
    task_id, user_id = _make_task()

    orchestrator.run_task(task_id, user_id, "Summarize the task")

    status, result = _read_task(task_id)
    assert status == "completed"
    assert result == "# Smart Report\n\n## Summary\nDone."

    events = agent_events.events_since(task_id, 0)
    types = [event["type"] for event in events]
    assert "synthesis" in types
    synthesis_index = types.index("synthesis")
    completed_index = max(
        index
        for index, event in enumerate(events)
        if event["type"] == "status" and event["message"] == "completed"
    )
    assert synthesis_index < completed_index
    agent_events.clear(task_id)


def test_run_task_falls_back_when_llm_synthesis_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(orchestrator, "_synthesize_with_llm", boom)
    task_id, user_id = _make_task()

    orchestrator.run_task(task_id, user_id, "Summarize the task")

    status, result = _read_task(task_id)
    assert status == "completed"
    assert result is not None and result.startswith("# OpenBot Task Result")
    events = agent_events.events_since(task_id, 0)
    assert any(
        event["type"] == "warning" and "Smart summary unavailable" in event["message"]
        for event in events
    )
    agent_events.clear(task_id)
