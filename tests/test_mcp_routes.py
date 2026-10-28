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


from tests.auth_helpers import register_and_login, auth_headers


def _auth(client):
    uid, token = register_and_login(client, username=None)  # helper makes a unique user
    return auth_headers(token)


def _csrf(client):
    # CSRF cookie is set on login; mirror how other route tests read it.
    tok = client.cookies.get("research_assistant_csrf_token")
    return {"X-CSRF-Token": tok} if tok else {}


def test_crud_flow(client):
    h = {**_auth(client), **_csrf(client)}
    # create
    r = client.post("/api/mcp/servers", headers=h, json={
        "name": "local_fetch", "command": "python", "args": ["-m", "mcp_server_fetch"],
        "env": None, "enabled": True, "tool_allowlist": None,
    })
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    # list
    assert any(s["id"] == sid for s in client.get("/api/mcp/servers", headers=h).json())
    # update
    r = client.put(f"/api/mcp/servers/{sid}", headers=h, json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    # delete
    assert client.delete(f"/api/mcp/servers/{sid}", headers=h).status_code == 204


def test_name_validation_rejects_double_underscore(client):
    h = {**_auth(client), **_csrf(client)}
    r = client.post("/api/mcp/servers", headers=h, json={
        "name": "bad__name", "command": "python", "args": [],
    })
    # The plan's Step-1 text asserted 400, but McpServerIn's field_validator
    # raises ValueError -> FastAPI returns 422 for body-schema failures. 422 is
    # the real, stable contract; the plan text is the erroneous half of a
    # plan-internal contradiction (ledger DEV-2).
    assert r.status_code == 422, r.text


def test_requires_auth(client):
    assert client.get("/api/mcp/servers").status_code in (401, 403)


def test_discover_against_stub(client):
    import os
    stub = os.path.abspath(os.path.join(os.path.dirname(__file__), "fixtures", "stub_mcp_server.py"))
    h = {**_auth(client), **_csrf(client)}
    r = client.post("/api/mcp/servers", headers=h, json={
        "name": "stub", "command": "python", "args": [stub],
    })
    sid = r.json()["id"]
    r = client.post(f"/api/mcp/servers/{sid}/discover", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"] is None
    assert any(t["name"] == "echo" for t in body["tools"])


def test_update_rejects_duplicate_name(client):
    h = {**_auth(client), **_csrf(client)}
    a = client.post("/api/mcp/servers", headers=h, json={
        "name": "srv_a", "command": "python", "args": [],
    })
    assert a.status_code == 201, a.text
    b = client.post("/api/mcp/servers", headers=h, json={
        "name": "srv_b", "command": "python", "args": [],
    })
    assert b.status_code == 201, b.text
    b_id = b.json()["id"]
    r = client.put(f"/api/mcp/servers/{b_id}", headers=h, json={"name": "srv_a"})
    assert r.status_code == 400, r.text


def test_discover_bad_command_reports_error(client):
    h = {**_auth(client), **_csrf(client)}
    r = client.post("/api/mcp/servers", headers=h, json={
        "name": "broken", "command": "definitely-not-a-real-binary-xyz", "args": [],
    })
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = client.post(f"/api/mcp/servers/{sid}/discover", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["error"], str) and body["error"], body
    assert body["tools"] == []
    srv = next(s for s in client.get("/api/mcp/servers", headers=h).json() if s["id"] == sid)
    assert srv["last_error"], srv


def test_foreign_server_is_404(client):
    ha = {**_auth(client), **_csrf(client)}
    r = client.post("/api/mcp/servers", headers=ha, json={
        "name": "owned", "command": "python", "args": [],
    })
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    # a different user must not see or touch it
    hb = {**_auth(client), **_csrf(client)}
    assert all(s["id"] != sid for s in client.get("/api/mcp/servers", headers=hb).json())
    assert client.put(f"/api/mcp/servers/{sid}", headers=hb, json={"enabled": False}).status_code == 404
    assert client.post(f"/api/mcp/servers/{sid}/discover", headers=hb).status_code == 404
    assert client.delete(f"/api/mcp/servers/{sid}", headers=hb).status_code == 404
