"""OpenBot orchestrator + REA: capability gate, confinement, and events.

REA itself is monkeypatched; these tests prove the orchestrator only calls it
when the user enabled ``rea_tools``, that refused paths never reach the server,
and that the analysis is rendered into the task result with live events.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import app.services.rea_service as rea_module
from app.config import settings
from tests.auth_helpers import auth_headers, register_and_login


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    base = "D:/" if os.name == "nt" else None
    scratch = Path(tempfile.mkdtemp(prefix="thunder-rea-orch-", dir=base))
    workspace = scratch / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "rea_mcp_enabled", True)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    monkeypatch.setattr(settings, "desktop_automation_enabled", False)
    yield workspace
    shutil.rmtree(scratch, ignore_errors=True)


def _headers(client, username):
    user_id, token = register_and_login(client, username=username)
    return user_id, auth_headers(token)


def _enable(client, headers, capability_id):
    response = client.patch(
        f"/api/capabilities/{capability_id}",
        json={"enabled": True},
        headers=headers,
    )
    assert response.status_code == 200, response.text[:300]


def _run_task(client, headers, query):
    response = client.post(
        "/api/agents/openbot/research",
        json={"query": query},
        headers=headers,
    )
    assert response.status_code in (200, 201), response.text[:300]
    task_id = response.json()["task_id"]

    deadline = time.time() + 15
    while time.time() < deadline:
        body = client.get(
            f"/api/agents/openbot/tasks/{task_id}", headers=headers
        ).json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return task_id, body
        time.sleep(0.05)
    raise AssertionError("task did not finish")


def test_enabled_capability_runs_rea_and_emits_events(monkeypatch, _env):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import openbot_service

    sample = _env / "sample.exe"
    sample.write_bytes(b"MZ")
    calls = []

    def fake_call(tool, arguments, timeout_s=None):
        calls.append((tool, arguments))
        return ("Imports: KERNEL32.dll", False)

    monkeypatch.setattr(rea_module, "call_tool", fake_call)

    with TestClient(app) as client:
        user_id, headers = _headers(client, "rea_enabled")
        _enable(client, headers, "openbot_research")
        _enable(client, headers, "rea_tools")

        task_id, body = _run_task(
            client, headers, "Please reverse engineer sample.exe"
        )

        assert body["status"] == "completed", body
        assert "## REA analysis" in body["result_text"]
        assert "Imports: KERNEL32.dll" in body["result_text"]
        assert calls == [
            ("inspect_pe_imports", {"path": str(sample.resolve())})
        ]

        events = openbot_service.get_task_events(task_id, user_id) or []
        assert any(
            event.get("type") == "tool"
            and event.get("tool") == "inspect_pe_imports"
            for event in events
        ), events


def test_capability_gate_skips_rea(monkeypatch, _env):
    from fastapi.testclient import TestClient

    from app.main import app

    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        return ("unexpected", False)

    monkeypatch.setattr(rea_module, "call_tool", forbidden)

    with TestClient(app) as client:
        _user_id, headers = _headers(client, "rea_disabled")
        _enable(client, headers, "openbot_research")

        _task_id, body = _run_task(
            client, headers, "Please reverse engineer sample.exe"
        )

        assert body["status"] == "completed"
        assert "REA tools are not enabled" in body["result_text"]
        assert calls == []


def test_outside_workspace_path_never_reaches_rea(monkeypatch, _env):
    from fastapi.testclient import TestClient

    from app.main import app

    outside = _env.parent / "outside.exe"
    outside.write_bytes(b"MZ")

    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        return ("unexpected", False)

    monkeypatch.setattr(rea_module, "call_tool", forbidden)

    with TestClient(app) as client:
        _user_id, headers = _headers(client, "rea_outside")
        _enable(client, headers, "openbot_research")
        _enable(client, headers, "rea_tools")

        _task_id, body = _run_task(
            client, headers, f"Please reverse engineer {outside}"
        )

        assert body["status"] == "completed"
        assert "not inside the workspace" in body["result_text"]
        assert calls == []


def test_rea_failure_is_a_warning_not_a_failed_task(monkeypatch, _env):
    from fastapi.testclient import TestClient

    from app.main import app

    sample = _env / "broken.bin"
    sample.write_bytes(b"\x00")

    def boom(*args, **kwargs):
        raise rea_module.ReaUnavailable("REA executable not found")

    monkeypatch.setattr(rea_module, "call_tool", boom)

    with TestClient(app) as client:
        _user_id, headers = _headers(client, "rea_failure")
        _enable(client, headers, "openbot_research")
        _enable(client, headers, "rea_tools")

        _task_id, body = _run_task(
            client, headers, "Analyze broken.bin as a binary"
        )

        assert body["status"] == "completed"
        assert "REA refused broken.bin" in body["result_text"]
        assert "REA executable not found" in body["result_text"]
