"""Focused model-selection validation tests with no live-service dependency."""

import asyncio

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.models import ResearchSession, User
from app.routes.models import list_models
from app.routes.sessions import _get_session_or_404, create_session, update_session_model
from app.schemas.sessions import ModelUpdate, SessionCreate
import app.routes.models as models_route
import app.routes.sessions as sessions_route


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_list_models_excludes_embedding_only_models(monkeypatch):
    """The chat-model discovery endpoint never offers embedding models."""
    response = httpx.Response(
        200,
        json={"models": [
            {"name": "llama3.2:3b", "size": 10, "modified_at": "2026-01-01"},
            {"name": "nomic-embed-text:latest", "size": 10, "modified_at": "2026-01-01"},
        ]},
        request=httpx.Request("GET", "http://ollama/api/tags"),
    )

    async def fake_get(*_args, **_kwargs):
        return response

    monkeypatch.setattr(models_route.httpx.AsyncClient, "get", fake_get)
    result = asyncio.run(list_models(provider="ollama"))

    assert [model.name for model in result.models] == ["llama3.2:3b"]


def test_new_session_explicitly_starts_with_default_model(db):
    user = User(username="new-session-owner", email="new-session-owner@example.com")
    db.add(user)
    db.commit()

    result = create_session(SessionCreate(title="Default Model"), db, user, None)

    assert result.model is None


def test_session_lookup_hides_other_users_sessions(db):
    owner = User(username="owner", email="owner@example.com")
    other = User(username="other", email="other@example.com")
    db.add_all([owner, other])
    db.flush()
    research_session = ResearchSession(title="Private", user_id=owner.id)
    db.add(research_session)
    db.commit()

    assert _get_session_or_404(db, research_session.id, owner.id).id == research_session.id
    with pytest.raises(HTTPException) as exc:
        _get_session_or_404(db, research_session.id, other.id)

    assert exc.value.status_code == 404


def test_model_selection_fails_closed_when_discovery_is_unavailable(db, monkeypatch):
    """An explicit model is not persisted when Ollama cannot verify it."""
    user = User(username="model-owner", email="model-owner@example.com")
    db.add(user)
    db.flush()
    research_session = ResearchSession(title="Discovery Down", user_id=user.id)
    db.add(research_session)
    db.commit()
    monkeypatch.setattr(sessions_route, "fetch_available_chat_models", lambda db, user_id: None)

    with pytest.raises(HTTPException) as exc:
        update_session_model(
            research_session.id,
            ModelUpdate(model="mistral:7b"),
            db,
            user,
            None,
        )

    assert exc.value.status_code == 503
    assert "verify" in exc.value.detail.lower()
    db.refresh(research_session)
    assert research_session.model is None
