"""Desktop automation: sandbox, command policy, and the route surface.

Every OS edge is injected (runner, screenshotter, clipboard reader, ``which``),
so these tests never spawn a process, grab a screen, touch a real clipboard, or
write outside ``tmp_path``. The route tests run against the app with the
isolated test database, plus auth/CSRF.
"""

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routes import desktop as desktop_routes
from app.services import desktop_automation as da
from app.services.desktop_automation import (
    CommandBlockedError,
    DesktopAutomationService,
    SandboxViolationError,
)

from tests.auth_helpers import auth_headers, register_and_login

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def calls():
    return {"argv": [], "cwd": None, "env": None, "timeout": None}


@pytest.fixture
def workspace(tmp_path):
    return tmp_path / "workspace"


@pytest.fixture
def service(workspace, calls, monkeypatch):
    """A service whose every OS edge is a recorder."""
    workspace.mkdir(parents=True, exist_ok=True)

    def runner(argv, **kwargs):
        calls["argv"] = list(argv)
        calls["cwd"] = kwargs.get("cwd")
        calls["env"] = dict(kwargs.get("env") or {})
        calls["timeout"] = kwargs.get("timeout")
        return _Completed(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(
        da.shutil, "which", lambda name: f"/fake/bin/{name}"  # deterministic lookup
    )
    return DesktopAutomationService(
        str(workspace),
        timeout_s=30,
        runner=runner,
        screenshotter=lambda: PNG,
        clipboard_reader=lambda: "clipboard text",
    )


# ── path sandbox ────────────────────────────────────────────


def test_reads_a_relative_path_inside_the_workspace(service, workspace):
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")

    assert service.read_file("notes.txt") == "hello"


def test_reads_an_absolute_path_inside_the_workspace(service, workspace):
    target = workspace / "nested" / "deep.txt"
    target.parent.mkdir()
    target.write_text("deep", encoding="utf-8")

    assert service.read_file(str(target)) == "deep"


def test_relative_escape_is_refused(service):
    with pytest.raises(SandboxViolationError, match="outside workspace"):
        service.read_file("../../etc/passwd")


def test_absolute_path_outside_the_workspace_is_refused(service, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")

    with pytest.raises(SandboxViolationError, match="outside workspace"):
        service.read_file(str(outside))


def test_write_escape_creates_nothing_outside(service, tmp_path):
    outside = tmp_path / "escaped.txt"

    with pytest.raises(SandboxViolationError):
        service.write_file("../escaped.txt", "payload")

    assert not outside.exists()


@pytest.mark.parametrize(
    "target",
    [
        "C:/Windows/win.ini",
        "C:/Users/someone/AppData/Roaming/x.txt",
        "C:/Users/someone/Documents/tax.pdf",
    ],
)
def test_system_and_profile_trees_are_refused_even_via_absolute_path(service, target):
    with pytest.raises(SandboxViolationError, match="outside workspace"):
        service.read_file(target)


def test_symlink_pointing_outside_is_refused(service, workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted for this account")

    with pytest.raises(SandboxViolationError, match="outside workspace"):
        service.read_file("link/anything.txt")


@pytest.mark.parametrize("bad", ["", "a\x00b", "line\nbreak"])
def test_malformed_paths_are_refused(service, bad):
    with pytest.raises(SandboxViolationError):
        service.read_file(bad)


# ── files ───────────────────────────────────────────────────


def test_write_creates_parent_directories(service, workspace):
    assert service.write_file("new/dir/file.txt", "content") is True
    assert (workspace / "new" / "dir" / "file.txt").read_text(encoding="utf-8") == "content"


def test_overwrite_keeps_a_backup_inside_the_workspace(service, workspace):
    service.write_file("data.txt", "first")
    service.write_file("data.txt", "second")

    backups = list((workspace / da.BACKUP_DIR).glob("data.txt.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "first"
    assert (workspace / "data.txt").read_text(encoding="utf-8") == "second"


def test_binary_file_is_refused(service, workspace):
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02binary")

    with pytest.raises(SandboxViolationError, match="[Bb]inary"):
        service.read_file("blob.bin")


def test_read_cap_is_enforced(service, workspace, monkeypatch):
    monkeypatch.setattr(da, "MAX_READ_BYTES", 8)
    (workspace / "big.txt").write_text("x" * 64, encoding="utf-8")

    with pytest.raises(SandboxViolationError, match="larger than"):
        service.read_file("big.txt")


def test_write_cap_is_enforced(service, monkeypatch):
    monkeypatch.setattr(da, "MAX_WRITE_BYTES", 8)

    with pytest.raises(SandboxViolationError, match="larger than"):
        service.write_file("big.txt", "x" * 64)


def test_missing_file_is_a_refusal_not_a_crash(service):
    with pytest.raises(SandboxViolationError, match="not found"):
        service.read_file("absent.txt")


def test_tree_lists_workspace_entries_without_internal_dirs(service, workspace):
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "b.txt").write_text("bb", encoding="utf-8")
    (workspace / ".git").mkdir()

    entries = {e["path"]: e for e in service.list_workspace()}

    assert "a.txt" in entries and entries["a.txt"]["is_dir"] is False
    assert "sub" in entries and entries["sub"]["is_dir"] is True
    assert "sub/b.txt" in entries
    assert not any(path.startswith(".git") for path in entries)


# ── command policy ──────────────────────────────────────────


@pytest.mark.parametrize("blocked", ["rm", "del", "format", "net", "regedit", "shutdown", "taskkill"])
def test_blocked_commands_are_refused(service, blocked):
    with pytest.raises(CommandBlockedError, match="blocked"):
        service.run_command(blocked, ["anything"])


def test_unknown_program_is_refused(service):
    with pytest.raises(CommandBlockedError):
        service.run_command("powershell", ["-c", "whoami"])


def test_allowed_command_runs_with_argv_cwd_and_scrubbed_env(service, calls, workspace, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "must-not-leak")
    monkeypatch.setenv("JWT_SECRET", "must-not-leak-either")

    result = service.run_command("git", ["status"])

    assert result.returncode == 0
    assert calls["argv"] == ["/fake/bin/git", "status"]
    assert Path(calls["cwd"]) == workspace
    assert calls["timeout"] == 30
    assert "ENCRYPTION_KEY" not in calls["env"]
    assert "JWT_SECRET" not in calls["env"]
    assert calls["env"]["HOME"] == str(workspace)
    assert calls["env"]["TEMP"].startswith(str(workspace))


@pytest.mark.parametrize(
    "command,args",
    [
        ("python", ["-c", "print(1)"]),
        ("node", ["-e", "require('fs')"]),
        ("git", ["-C", "C:/", "status"]),
        ("git", ["--exec-path=C:/evil", "status"]),
    ],
)
def test_interpreter_and_directory_escape_flags_are_refused(service, command, args):
    with pytest.raises(CommandBlockedError):
        service.run_command(command, args)


@pytest.mark.parametrize(
    "command,args",
    [
        ("git", ["push", "origin", "main"]),
        ("git", ["credential", "fill"]),
        ("npm", ["exec", "evil-package"]),
        ("pip", ["download", "requests"]),
    ],
)
def test_subcommands_outside_the_allowlist_are_refused(service, command, args):
    if command == "pip":
        (service.workspace / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
        (service.workspace / ".venv" / "Scripts" / "pip.exe").write_text("", encoding="utf-8")
    with pytest.raises(CommandBlockedError):
        service.run_command(command, args)


def test_a_path_argument_outside_the_workspace_is_refused(service):
    with pytest.raises(SandboxViolationError, match="outside workspace"):
        service.run_command("git", ["add", "../../outside.txt"])


def test_pip_without_a_workspace_venv_is_refused(service):
    with pytest.raises(CommandBlockedError, match="workspace virtualenv"):
        service.run_command("pip", ["install", "requests"])


def test_pip_inside_the_workspace_venv_is_allowed(service, calls, workspace):
    pip = workspace / ".venv" / "Scripts" / "pip.exe"
    pip.parent.mkdir(parents=True)
    pip.write_text("", encoding="utf-8")

    service.run_command("pip", ["install", "requests"])

    assert calls["argv"][0] == str(pip)
    assert calls["argv"][1] == "install"


def test_pip_network_and_requirement_flags_are_refused(service, workspace):
    pip = workspace / ".venv" / "Scripts" / "pip.exe"
    pip.parent.mkdir(parents=True)
    pip.write_text("", encoding="utf-8")

    for args in (
        ["install", "--index-url", "http://evil/simple", "pkg"],
        ["install", "-r", "req.txt"],
        ["install", "--target", "C:/Windows/Temp", "pkg"],
    ):
        with pytest.raises(CommandBlockedError):
            service.run_command("pip", args)


def test_timeout_is_reported_not_raised(service, workspace):
    import subprocess

    def explode(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    service._runner = explode

    result = service.run_command("git", ["status"], timeout=5)

    assert result.timed_out is True
    assert result.returncode == -1
    assert "timed out" in result.stderr.lower()


def test_output_is_truncated_to_the_cap(service, monkeypatch):
    monkeypatch.setattr(da, "MAX_OUTPUT_BYTES", 4)
    service._runner = lambda argv, **kwargs: _Completed(stdout="0123456789")

    result = service.run_command("git", ["status"])

    assert result.stdout == "0123"
    assert result.truncated is True


# ── in-process commands (no shell) ──────────────────────────


def test_internal_ls_lists_the_workspace(service, workspace):
    (workspace / "a.txt").write_text("a", encoding="utf-8")

    result = service.run_command("ls", [])

    assert result.returncode == 0
    assert "a.txt" in result.stdout


def test_internal_cat_reads_a_file_and_requires_an_argument(service, workspace):
    (workspace / "a.txt").write_text("contents", encoding="utf-8")

    assert service.run_command("cat", ["a.txt"]).stdout == "contents"
    with pytest.raises(SandboxViolationError):
        service.run_command("cat", [])


@pytest.mark.parametrize("command", ["dir", "type", "echo"])
def test_windows_aliases_never_shell_out(service, calls, workspace, command):
    (workspace / "a.txt").write_text("x", encoding="utf-8")

    result = service.run_command(command, ["a.txt"])

    assert result.returncode == 0
    assert calls["argv"] == []  # no subprocess at all


# ── screenshot and clipboard ────────────────────────────────


def test_screenshot_returns_bytes_from_the_injected_grabber(service):
    assert service.take_screenshot() == PNG


def test_oversize_screenshot_is_refused(service, monkeypatch):
    monkeypatch.setattr(da, "MAX_SCREENSHOT_BYTES", 8)

    with pytest.raises(SandboxViolationError, match="too large"):
        service.take_screenshot()


def test_empty_screenshot_is_refused(service):
    service._screenshotter = lambda: b""

    with pytest.raises(SandboxViolationError, match="no image"):
        service.take_screenshot()


def test_screenshot_failure_is_bounded(service):
    def explode():
        raise RuntimeError("no display")

    service._screenshotter = explode

    with pytest.raises(SandboxViolationError, match="unavailable"):
        service.take_screenshot()


def test_screenshot_never_writes_to_disk(service, workspace):
    before = {p for p in workspace.rglob("*")}
    service.take_screenshot()
    assert {p for p in workspace.rglob("*")} == before


def test_clipboard_returns_text_and_is_capped(service, monkeypatch):
    service._clipboard_reader = lambda: "abc"
    assert service.get_clipboard() == "abc"

    monkeypatch.setattr(da, "MAX_CLIPBOARD_CHARS", 2)
    assert service.get_clipboard() == "ab"


def test_clipboard_failure_is_bounded(service):
    def explode():
        raise RuntimeError("busy")

    service._clipboard_reader = explode

    with pytest.raises(SandboxViolationError, match="unavailable"):
        service.get_clipboard()


def test_there_is_no_clipboard_write_path():
    """Invariant: sensitive data must never be written to the clipboard."""
    assert not any(
        "clipboard" in name and ("write" in name or "set" in name)
        for name in dir(DesktopAutomationService)
    )


# ── logging: metadata only ─────────────────────────────────


def test_content_and_output_are_never_logged(service, workspace, caplog):
    secret = "SECRET_FILE_CONTENT_9f3a"
    (workspace / "secret.txt").write_text(secret, encoding="utf-8")
    service._runner = lambda argv, **kwargs: _Completed(stdout="SECRET_COMMAND_OUTPUT_77b1")

    with caplog.at_level(logging.DEBUG):
        service.read_file("secret.txt")
        service.write_file("written.txt", secret)
        service.run_command("git", ["status"])
        service.get_clipboard()

    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in blob
    assert "SECRET_COMMAND_OUTPUT_77b1" not in blob
    assert "clipboard text" not in blob


# ── routes ─────────────────────────────────────────────────


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    user_id, token = register_and_login(client)
    yield client, auth_headers(token)


@pytest.fixture
def enabled(monkeypatch, service):
    monkeypatch.setattr(settings, "desktop_automation_enabled", True)
    monkeypatch.setattr(desktop_routes, "_service", lambda *args, **kwargs: service)
    return service


def test_status_requires_authentication(client):
    assert client.get("/api/desktop/status").status_code == 401


def test_status_reports_disabled_by_default(signed_in, monkeypatch):
    client, headers = signed_in
    monkeypatch.setattr(settings, "desktop_automation_enabled", False)

    body = client.get("/api/desktop/status", headers=headers).json()

    assert body["enabled"] is False
    assert body["workspace"] == ""
    assert "python" in body["commands"]
    assert "rm" in body["blocked_commands"]


def test_every_action_is_refused_while_disabled(signed_in, monkeypatch):
    client, headers = signed_in
    monkeypatch.setattr(settings, "desktop_automation_enabled", False)

    for method, path, payload in (
        ("post", "/api/desktop/read", {"path": "a.txt"}),
        ("post", "/api/desktop/write", {"path": "a.txt", "content": "x"}),
        ("post", "/api/desktop/run", {"command": "git", "args": ["status"]}),
        ("post", "/api/desktop/screenshot", None),
        ("get", "/api/desktop/tree", None),
    ):
        response = getattr(client, method)(path, headers=headers, **({"json": payload} if payload else {}))
        assert response.status_code == 503, path


def test_read_and_write_round_trip_over_http(signed_in, enabled, workspace):
    client, headers = signed_in

    written = client.post(
        "/api/desktop/write", headers=headers, json={"path": "hello.txt", "content": "hi"}
    )
    assert written.status_code == 200
    assert written.json()["bytes_written"] == 2

    read = client.post("/api/desktop/read", headers=headers, json={"path": "hello.txt"})
    assert read.json()["content"] == "hi"
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "hi"


def test_a_path_escape_is_a_403_with_the_policy_copy(signed_in, enabled):
    client, headers = signed_in

    response = client.post(
        "/api/desktop/read", headers=headers, json={"path": "../../outside.txt"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == da.PATH_DENIED


def test_blocked_command_is_a_403(signed_in, enabled):
    client, headers = signed_in

    response = client.post(
        "/api/desktop/run", headers=headers, json={"command": "rm", "args": ["-rf", "."]}
    )

    assert response.status_code == 403
    assert "blocked" in response.json()["detail"]


def test_run_returns_bounded_output(signed_in, enabled):
    client, headers = signed_in
    enabled._runner = lambda argv, **kwargs: _Completed(returncode=2, stdout="out", stderr="err")

    body = client.post(
        "/api/desktop/run", headers=headers, json={"command": "git", "args": ["status"]}
    ).json()

    assert body == {
        "command": "git",
        "returncode": 2,
        "stdout": "out",
        "stderr": "err",
        "truncated": False,
        "duration_ms": body["duration_ms"],
        "timed_out": False,
    }


def test_tree_lists_the_workspace(signed_in, enabled, workspace):
    client, headers = signed_in
    (workspace / "a.txt").write_text("a", encoding="utf-8")

    body = client.get("/api/desktop/tree", headers=headers).json()

    assert body["root"] == str(workspace)
    assert [e["path"] for e in body["entries"]] == ["a.txt"]


def test_screenshot_goes_to_vision_and_returns_no_image(signed_in, enabled, monkeypatch):
    client, headers = signed_in

    calls = {}

    class _Result:
        status = "ok"
        description = "a browser window"
        notice = ""

    def fake_describe(raw):
        calls["raw"] = raw
        return _Result()

    monkeypatch.setattr(
        "app.services.local_vision_service.describe_image", fake_describe
    )

    body = client.post("/api/desktop/screenshot", headers=headers).json()

    assert body["status"] == "ok"
    assert body["description"] == "a browser window"
    assert body["image_bytes"] == len(PNG)
    assert "image" not in body  # the bytes never come back to the client
    assert calls["raw"] == PNG


def test_mutating_routes_require_csrf(signed_in, enabled):
    client, headers = signed_in

    response = client.post(
        "/api/desktop/read",
        headers={**headers, "Origin": "http://evil.example"},
        json={"path": "a.txt"},
    )

    assert response.status_code == 403
