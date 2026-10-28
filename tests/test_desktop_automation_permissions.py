"""Desktop automation permission levels (ask / auto / full).

Unit tests drive the service directly with injected OS seams; the HTTP tests
prove the stored ``skill_preferences.config_json.permission_level`` actually
reaches the per-request service. The workspace boundary and the blocked-command
list are invariants that no permission level may bypass.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import desktop_automation as da
from app.services.desktop_automation import DesktopAutomationService, PermissionLevel
from tests.auth_helpers import auth_headers, register_and_login

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "desktop-permissions-hmac-01")
    for flag in (
        "run_messages_stream_durable_enabled",
        "run_engine_enabled",
        "run_worker_enabled",
        "run_chat_stream_enabled",
        "run_public_api_enabled",
        "run_deep_research_enabled",
        "run_multi_agent_enabled",
    ):
        monkeypatch.setattr(settings, flag, False)
    from app.services.rate_limiter import reset_rate_limiter

    reset_rate_limiter()
    yield


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def make_service(workspace, monkeypatch):
    def factory(level):
        monkeypatch.setattr(da.shutil, "which", lambda name: f"/fake/bin/{name}")

        def runner(argv, **kwargs):
            return _Completed(returncode=0, stdout="ok", stderr="")

        return DesktopAutomationService(
            str(workspace),
            timeout_s=30,
            runner=runner,
            screenshotter=lambda: PNG,
            clipboard_reader=lambda: "clipboard text",
            permission_level=level,
        )

    return factory


# ── ASK_EVERY_TIME ───────────────────────────────────────────


@pytest.mark.parametrize(
    "action",
    ["read_file", "write_file", "list_workspace", "run_command", "take_screenshot", "get_clipboard"],
)
def test_ask_every_time_blocks_every_action(make_service, workspace, action):
    service = make_service(PermissionLevel.ASK_EVERY_TIME)
    (workspace / "a.txt").write_text("x", encoding="utf-8")

    calls = {
        "read_file": lambda: service.read_file("a.txt"),
        "write_file": lambda: service.write_file("b.txt", "x"),
        "list_workspace": lambda: service.list_workspace(),
        "run_command": lambda: service.run_command("echo", ["hi"]),
        "take_screenshot": lambda: service.take_screenshot(),
        "get_clipboard": lambda: service.get_clipboard(),
    }

    with pytest.raises(da.PermissionRequired) as exc:
        calls[action]()

    assert str(exc.value) == da.PERMISSION_REQUIRED_MESSAGE


# ── AUTO_APPROVE / FULL_ACCESS ───────────────────────────────


@pytest.mark.parametrize(
    "level", [PermissionLevel.AUTO_APPROVE, PermissionLevel.FULL_ACCESS]
)
def test_non_ask_levels_allow_sandboxed_operations(make_service, level):
    service = make_service(level)

    assert service.write_file("notes.txt", "hi") is True
    assert service.read_file("notes.txt") == "hi"
    assert service.run_command("echo", ["hi"]).stdout == "hi"
    assert service.list_workspace()


@pytest.mark.parametrize(
    "level", [PermissionLevel.AUTO_APPROVE, PermissionLevel.FULL_ACCESS]
)
def test_destructive_and_system_commands_stay_blocked(make_service, level):
    service = make_service(level)

    for command, args in (
        ("rm", ["-rf", "."]),
        ("del", ["a.txt"]),
        ("format", ["C:"]),
        ("shutdown", ["/s"]),
    ):
        with pytest.raises(da.CommandBlockedError):
            service.run_command(command, args)


@pytest.mark.parametrize(
    "level", [PermissionLevel.AUTO_APPROVE, PermissionLevel.FULL_ACCESS]
)
def test_workspace_boundary_is_never_bypassed(make_service, level):
    service = make_service(level)

    with pytest.raises(da.SandboxViolationError):
        service.read_file("../../etc/passwd")
    with pytest.raises(da.SandboxViolationError):
        service.write_file("../escaped.txt", "payload")


def test_unknown_level_falls_back_to_auto(workspace):
    service = DesktopAutomationService(str(workspace), permission_level="not-a-level")
    assert service.permission_level is PermissionLevel.AUTO_APPROVE


# ── HTTP: stored preference reaches the service ─────────────


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    _user_id, token = register_and_login(client, username="perm_" + uuid.uuid4().hex[:8])
    return client, auth_headers(token)


@pytest.fixture
def enabled(monkeypatch, workspace):
    monkeypatch.setattr(settings, "desktop_automation_enabled", True)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    yield


def _set_level(client, headers, level):
    return client.patch(
        "/api/capabilities/desktop_automation",
        headers=headers,
        json={"enabled": True, "config": {"permission_level": level}},
    )


def test_stored_ask_level_blocks_then_auto_allows(signed_in, enabled):
    client, headers = signed_in

    assert _set_level(client, headers, "ask_every_time").status_code == 200

    blocked = client.post(
        "/api/desktop/run", headers=headers, json={"command": "echo", "args": ["hi"]}
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == da.PERMISSION_REQUIRED_MESSAGE

    read = client.post("/api/desktop/read", headers=headers, json={"path": "a.txt"})
    assert read.status_code == 403
    assert read.json()["detail"] == da.PERMISSION_REQUIRED_MESSAGE

    assert _set_level(client, headers, "auto_approve").status_code == 200

    ran = client.post(
        "/api/desktop/run", headers=headers, json={"command": "echo", "args": ["hi"]}
    )
    assert ran.status_code == 200, ran.text[:300]
    assert ran.json()["stdout"] == "hi"

    status = client.get("/api/desktop/status", headers=headers).json()
    assert status["permission_level"] == "auto_approve"


def test_full_access_allows_sandboxed_commands_only(signed_in, enabled):
    client, headers = signed_in

    assert _set_level(client, headers, "full_access").status_code == 200

    ran = client.post(
        "/api/desktop/run", headers=headers, json={"command": "echo", "args": ["test"]}
    )
    assert ran.status_code == 200, ran.text[:300]
    assert ran.json()["stdout"] == "test"

    # The sandbox still wins over the permission level.
    blocked = client.post(
        "/api/desktop/run", headers=headers, json={"command": "rm", "args": ["-rf", "."]}
    )
    assert blocked.status_code == 403

    status = client.get("/api/desktop/status", headers=headers).json()
    assert status["permission_level"] == "full_access"


def test_capabilities_api_defaults_and_round_trips_the_level(signed_in):
    client, headers = signed_in

    listed = {
        item["id"]: item
        for item in client.get("/api/capabilities", headers=headers).json()
    }
    assert listed["desktop_automation"]["config"] == {
        "permission_level": "auto_approve"
    }

    updated = _set_level(client, headers, "full_access")
    assert updated.status_code == 200
    assert updated.json()["config"] == {"permission_level": "full_access"}

    listed = {
        item["id"]: item
        for item in client.get("/api/capabilities", headers=headers).json()
    }
    assert listed["desktop_automation"]["config"] == {
        "permission_level": "full_access"
    }


def test_invalid_permission_level_is_rejected(signed_in):
    client, headers = signed_in

    response = client.patch(
        "/api/capabilities/desktop_automation",
        headers=headers,
        json={"enabled": True, "config": {"permission_level": "yolo"}},
    )

    assert response.status_code == 422
