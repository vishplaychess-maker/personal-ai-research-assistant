"""agent-desktop wrapper: safety checks, permission ladder, timeout handling.

The real CLI is NEVER executed here: every subprocess call goes through an
injected fake runner. These tests pin the guardrails that make OS-level
computer use acceptable at all.
"""

from __future__ import annotations

import json
import subprocess

import pytest

import app.services.desktop_agent_service as das
from app.config import settings
from app.services.desktop_automation import PermissionLevel, PermissionRequired


class _Proc:
    def __init__(self, returncode=0, payload=None):
        self.returncode = returncode
        self.stdout = json.dumps(payload) if payload is not None else ""
        self.stderr = ""


def _ok_snapshot(snapshot_id="s111aaaa"):
    return _Proc(payload={
        "version": "2.6", "ok": True, "command": "snapshot",
        "data": {"app": "Notepad", "snapshot_id": snapshot_id, "ref_count": 2,
                 "complete": True,
                 "tree": {"role": "window", "children": [
                     {"role": "button", "name": "OK", "ref_id": f"@{snapshot_id}:e8"}]}},
    })


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setattr(settings, "agent_desktop_enabled", True)
    monkeypatch.setattr(settings, "agent_desktop_allowed_apps", "Notepad,Calculator")
    monkeypatch.setattr(settings, "agent_desktop_command", "agent-desktop")
    monkeypatch.setattr(settings, "agent_desktop_timeout_s", 30)
    monkeypatch.setattr(settings, "agent_desktop_max_output_chars", 6000)
    das.reset_snapshot_ids()
    yield
    das.reset_snapshot_ids()


def _calls():
    seen = []
    return seen


def make_runner(seen, proc=None, exc=None):
    def runner(argv, **kwargs):
        seen.append((argv, kwargs))
        if exc is not None:
            raise exc
        return proc if proc is not None else _ok_snapshot()
    return runner


# ── fail-closed master gates ───────────────────────────────────────────────


def test_disabled_master_flag_never_executes(monkeypatch):
    monkeypatch.setattr(settings, "agent_desktop_enabled", False)
    seen = _calls()
    with pytest.raises(das.DesktopAgentDisabled):
        das.run("snapshot", ["--app", "Notepad"], permission_level="auto_approve",
                runner=make_runner(seen))
    assert seen == []  # no subprocess spawned


def test_empty_app_allowlist_blocks_everything(monkeypatch):
    monkeypatch.setattr(settings, "agent_desktop_allowed_apps", "")
    assert das.is_enabled() is False
    seen = _calls()
    with pytest.raises(das.DesktopAgentDisabled):
        das.read_ui("Notepad", permission_level="full_access", runner=make_runner(seen))
    assert seen == []


# ── permission ladder ──────────────────────────────────────────────────────


def test_ask_level_blocks_reads_too():
    seen = _calls()
    with pytest.raises(PermissionRequired):
        das.read_ui("Notepad", permission_level="ask_every_time", runner=make_runner(seen))
    assert seen == []


def test_unknown_level_falls_back_to_strictest():
    assert das._coerce_level("nonsense") is PermissionLevel.ASK_EVERY_TIME
    assert das._coerce_level(None) is PermissionLevel.ASK_EVERY_TIME
    assert das._coerce_level("auto_approve") is PermissionLevel.AUTO_APPROVE


def test_auto_level_performs_snapshot_with_scrubbed_env_and_argv():
    seen = _calls()
    result = das.read_ui("Notepad", permission_level="auto_approve",
                         runner=make_runner(seen))
    argv, kwargs = seen[0]
    assert argv[:2] == ["agent-desktop", "snapshot"]
    assert "--app" in argv and "Notepad" in argv
    assert kwargs["shell"] if "shell" in kwargs else True  # never used shell
    assert kwargs["timeout"] == 30.0
    assert "shell" not in kwargs  # subprocess never sees shell=True at all
    env = kwargs["env"]
    assert "ENCRYPTION_KEY" not in env and "JWT_SECRET" not in env
    assert set(env) <= {"PATH", "SystemRoot", "WINDIR", "COMSPEC"}
    assert result.data["snapshot_id"] == "s111aaaa"


# ── command/arg guards ─────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["clipboard-set", "session-start", "mcp", "screenshot"])
def test_command_allowlist_refuses(bad):
    seen = _calls()
    with pytest.raises(das.DesktopAgentError):
        das.run(bad, ["--app", "Notepad"], permission_level="auto_approve",
                runner=make_runner(seen))
    assert seen == []


def test_headed_flag_refused_at_every_level():
    seen = _calls()
    with pytest.raises(das.DesktopAgentError, match="Headed"):
        das.run("click", ["--app", "Notepad", "@s111aaaa:e8", "--headed"],
                permission_level="full_access", runner=make_runner(seen))


def test_unlisted_app_refused():
    seen = _calls()
    with pytest.raises(das.DesktopAgentError, match="not in the allowed apps"):
        das.read_ui("Explorer", permission_level="auto_approve", runner=make_runner(seen))


def test_injection_style_args_refused():
    seen = _calls()
    with pytest.raises(das.DesktopAgentError):
        das.read_ui("Notepad; rm -rf /", permission_level="auto_approve",
                    runner=make_runner(seen))
    assert seen == []


def test_stale_or_foreign_ref_refused():
    seen = _calls()
    # Never-snapshotted ref id.
    with pytest.raises(das.DesktopAgentError, match="STALE_REF"):
        das.click("@zzzzzzzz:e1", "Notepad", permission_level="auto_approve",
                  runner=make_runner(seen))
    # Bare/malformed ref.
    with pytest.raises(das.DesktopAgentError):
        das.click("@e8", "Notepad", permission_level="auto_approve",
                  runner=make_runner(seen))
    assert seen == []


def test_ref_from_a_live_snapshot_is_accepted():
    seen = _calls()
    das.read_ui("Notepad", permission_level="auto_approve",
                runner=make_runner(seen, _ok_snapshot("abc12345")))
    das.click("@abc12345:e8", "Notepad", permission_level="auto_approve",
              runner=make_runner(seen, _Proc(payload={
                  "ok": True, "command": "click", "data": {"applied": True}})))
    assert seen[-1][0][:2] == ["agent-desktop", "click"]


def test_typed_text_goes_through_stdin_not_argv():
    seen = _calls()
    das.read_ui("Notepad", permission_level="auto_approve",
                runner=make_runner(seen, _ok_snapshot("abcd1234")))
    secret = "hunter2-secret-line"
    das.type_text("@abcd1234:e2", secret, "Notepad",
                  permission_level="auto_approve",
                  runner=make_runner(seen, _Proc(payload={
                      "ok": True, "command": "type", "data": {}})))
    argv, kwargs = seen[-1]
    assert secret not in " ".join(argv)
    assert kwargs["input"] == secret
    assert "--stdin" in argv


# ── failure envelopes ──────────────────────────────────────────────────────


def test_timeout_raises_bounded_error_and_is_not_retried():
    seen = _calls()

    def timeout_runner(argv, **kwargs):
        seen.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, timeout=30)

    with pytest.raises(das.DesktopAgentTimeout):
        das.read_ui("Notepad", permission_level="auto_approve", runner=timeout_runner)
    assert len(seen) == 1


def test_server_error_envelope_surfaces_code():
    seen = _calls()
    proc = _Proc(payload={"ok": False, "command": "snapshot",
                          "error": {"code": "PERMISSION_MISSING"}})
    with pytest.raises(das.DesktopAgentError, match="PERMISSION_MISSING"):
        das.read_ui("Notepad", permission_level="auto_approve",
                    runner=make_runner(seen, proc))


def test_non_json_output_refused():
    seen = _calls()
    with pytest.raises(das.DesktopAgentError, match="non-JSON"):
        das.run("snapshot", ["--app", "Notepad"], permission_level="auto_approve",
                runner=make_runner(seen, _Proc(returncode=0, payload=None)))
