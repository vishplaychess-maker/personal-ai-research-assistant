"""MCP server CRUD + discovery route tests. Run inside the backend container:
    docker compose exec -T backend pytest tests/test_mcp_routes.py -v
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.main import app
from app.database import engine, SessionLocal
from app.models.models import MCPServer, User


@pytest.fixture
def client():
    with TestClient(app) as c:      # context manager runs lifespan -> init_db + _migrate_database
        yield c


@pytest.fixture(autouse=True)
def _cleanup(client):
    db = SessionLocal()
    try:
        db.query(MCPServer).delete(synchronize_session=False)
        db.query(User).filter(User.username.like("mcpt_%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    yield
    db = SessionLocal()
    try:
        db.query(MCPServer).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_mcp_servers_table_exists(client):
    cols = {c["name"] for c in inspect(engine).get_columns("mcp_servers")}
    assert {
        "id", "user_id", "name", "command", "args_json", "env_json", "enabled",
        "tool_allowlist_json", "tools_json", "last_discovered_at", "last_error",
        "created_at", "updated_at",
    } <= cols
