"""Enabled custom skills must be discoverable and executable by chat/agent."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.database import SessionLocal
from app.models.models import ResearchSession, SkillPreference, User
from app.services import custom_skill_loader, custom_skill_service, orchestrator


async def _hello_execute(context: dict) -> dict:
    name = (context.get("inputs") or {}).get("name") or "world"
    return {
        "message": f"Hello from custom skill, {name}!",
        "echoed_inputs": context.get("inputs") or {},
        "context_user_id": context.get("user_id"),
    }


@pytest.fixture()
def enabled_skill(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    skill = custom_skill_loader.LoadedCustomSkill(
        id="hello_world",
        name="Hello World",
        description="A friendly example custom skill.",
        icon="sparkles",
        inputs_schema={"name": {"type": "string", "label": "Your name"}},
        directory=None,
        module=SimpleNamespace(execute=_hello_execute),
    )
    monkeypatch.setattr(custom_skill_loader, "_LOADED", {"hello_world": skill})

    db = SessionLocal()
    try:
        username = "custom_" + uuid.uuid4().hex[:8]
        user = User(username=username, email=f"{username}@example.com")
        db.add(user)
        db.commit()
        db.refresh(user)
        db.add(
            SkillPreference(
                user_id=user.id, skill_id="hello_world", enabled=True
            )
        )
        db.commit()
        yield user.id
    finally:
        db.close()


def test_catalog_lists_only_enabled_skills(enabled_skill):
    user_id = enabled_skill
    db = SessionLocal()
    try:
        block = custom_skill_service.catalog_block(db, user_id)
        assert "hello_world" in block
        assert "Hello World" in block
        assert "execute_custom_skill" in block
    finally:
        db.close()


def test_matches_and_executes_an_explicit_request(enabled_skill):
    user_id = enabled_skill
    db = SessionLocal()
    try:
        invocation = custom_skill_service.match_invocation(
            db, user_id, "Run the Hello World custom skill with my name."
        )
    finally:
        db.close()

    assert invocation is not None
    assert invocation.skill.id == "hello_world"
    assert invocation.executable

    result = custom_skill_service.execute(invocation, user_id)
    assert result["message"] == "Hello from custom skill, world!"
    assert custom_skill_service.result_text(result).startswith(
        '{"message": "Hello from custom skill, world!"'
    )


def test_disabled_skill_is_not_visible(enabled_skill):
    user_id = enabled_skill
    db = SessionLocal()
    try:
        pref = db.query(SkillPreference).filter_by(user_id=user_id).one()
        pref.enabled = False
        db.commit()
        assert custom_skill_service.catalog_block(db, user_id) == ""
        assert (
            custom_skill_service.match_invocation(
                db, user_id, "Run the Hello World custom skill."
            )
            is None
        )
    finally:
        db.close()


def test_chat_prompt_includes_catalog_and_execution(enabled_skill, monkeypatch):
    from app.services import streaming_service

    user_id = enabled_skill
    monkeypatch.setattr(settings, "enable_memory", False)
    db = SessionLocal()
    try:
        session = ResearchSession(user_id=user_id, title="Custom skill chat")
        db.add(session)
        db.commit()
        db.refresh(session)
        ctx = streaming_service.prepare_chat_context(
            session_id=session.id,
            user_id=user_id,
            user_input="Run the Hello World custom skill with name: QA.",
            db=db,
        )
    finally:
        db.close()

    assert "Available Custom Skills" in ctx.system_prompt
    assert "hello_world" in ctx.system_prompt
    assert "Hello from custom skill, QA!" in ctx.system_prompt


def test_orchestrator_executes_enabled_skill(enabled_skill):
    user_id = enabled_skill
    task_id = uuid.uuid4().hex

    # Direct helper check (no full task row required): catalog + tool execution.
    block, warnings = orchestrator._custom_skills(
        task_id, user_id, "Run the Hello World custom skill with my name."
    )

    assert warnings == []
    assert "Hello from custom skill, world!" in block
    assert "execute_custom_skill" in block
