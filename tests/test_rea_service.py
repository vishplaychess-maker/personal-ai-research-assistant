"""REA MCP integration — allowlist, workspace confinement, and gating.

The real REA server is never spawned here: ``mcp_service`` is monkeypatched so
these tests stay fast and assert exactly what would be sent to the server.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from app.config import settings
from app.services import mcp_service, rea_service


def _scratch_root() -> Path:
    # The app refuses user-profile workspaces (on Windows pytest's tmp_path
    # lives under AppData), so tests use a D: scratch directory when possible.
    base = "D:/" if os.name == "nt" else None
    return Path(tempfile.mkdtemp(prefix="thunder-rea-", dir=base))


@pytest.fixture(autouse=True)
def _rea_workspace(monkeypatch):
    scratch = _scratch_root()
    workspace = scratch / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "rea_mcp_enabled", True)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    monkeypatch.setattr(settings, "rea_mcp_timeout_s", 10)
    monkeypatch.setattr(settings, "rea_max_output_chars", 1000)
    yield workspace
    shutil.rmtree(scratch, ignore_errors=True)


def _capture(monkeypatch):
    calls = []

    def fake_call(cfg, tool, arguments, timeout_s=None):
        calls.append((tool, arguments, timeout_s))
        return ("analysis output", False)

    monkeypatch.setattr(mcp_service, "call_tool", fake_call)
    return calls


def test_allowlisted_tool_resolves_path_inside_workspace(monkeypatch, _rea_workspace):
    sample = _rea_workspace / "sample.exe"
    sample.write_bytes(b"MZ")
    calls = _capture(monkeypatch)

    text, is_error = rea_service.call_tool(
        "inspect_pe_imports", {"path": "sample.exe"}
    )

    assert text == "analysis output"
    assert is_error is False
    tool, arguments, timeout_s = calls[0]
    assert tool == "inspect_pe_imports"
    assert arguments == {"path": str(sample.resolve())}
    assert timeout_s == 10


def test_path_outside_workspace_is_rejected(monkeypatch, _rea_workspace):
    outside = _rea_workspace.parent / "outside.bin"
    outside.write_bytes(b"secret")
    calls = _capture(monkeypatch)

    with pytest.raises(rea_service.ReaConfinementError):
        rea_service.call_tool("inspect_binary_layout", {"path": str(outside)})

    with pytest.raises(rea_service.ReaConfinementError):
        rea_service.call_tool("inspect_binary_layout", {"path": "../outside.bin"})

    assert calls == []


def test_nested_path_arguments_are_confined(monkeypatch, _rea_workspace):
    (_rea_workspace / "module.wasm").write_bytes(b"\x00asm")
    glue = _rea_workspace / "glue.js"
    glue.write_text("x")
    calls = _capture(monkeypatch)

    rea_service.call_tool(
        "inspect_wasm_artifact",
        {"path": "module.wasm", "glue_paths": ["glue.js"]},
    )

    _, arguments, _ = calls[0]
    assert arguments["glue_paths"] == [str(glue.resolve())]


def test_non_allowlisted_tool_is_rejected(monkeypatch):
    calls = _capture(monkeypatch)

    with pytest.raises(rea_service.ReaToolNotAllowed):
        rea_service.call_tool("gdb_console", {"command": "run"})

    with pytest.raises(rea_service.ReaToolNotAllowed):
        rea_service.call_tool("push_adb_file", {"path": "sample.bin"})

    assert calls == []


def test_disabled_capability_never_spawns_the_server(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setattr(settings, "rea_mcp_enabled", False)

    with pytest.raises(rea_service.ReaUnavailable):
        rea_service.call_tool("inspect_binary_layout", {"path": "sample.bin"})

    assert calls == []


def test_system_workspace_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", r"C:\Windows")

    with pytest.raises(rea_service.ReaUnavailable):
        rea_service.call_tool("inspect_binary_layout", {"path": "notepad.exe"})


def test_explicit_query_file_is_preferred(_rea_workspace):
    target = _rea_workspace / "target.apk"
    target.write_bytes(b"PK")
    other = _rea_workspace / "other.exe"
    other.write_bytes(b"MZ")

    candidates, warnings = rea_service.find_workspace_candidates(
        "please reverse engineer target.apk"
    )

    assert candidates == [target.resolve()]
    assert warnings == []


def test_explicit_outside_query_is_skipped(_rea_workspace):
    outside = _rea_workspace.parent / "outside.exe"
    outside.write_bytes(b"MZ")

    candidates, warnings = rea_service.find_workspace_candidates(
        f"analyze {outside}"
    )

    assert candidates == []
    assert any("not inside the workspace" in warning for warning in warnings)


def test_walk_finds_artifacts_without_an_explicit_path(_rea_workspace):
    nested = _rea_workspace / "samples"
    nested.mkdir()
    (nested / "one.sqlite").write_bytes(b"SQLite format 3\x00")
    (nested / "notes.txt").write_text("ignored")

    candidates, _ = rea_service.find_workspace_candidates("inspect the artifacts")

    assert [path.name for path in candidates] == ["one.sqlite"]


def test_tool_selection_by_suffix():
    assert rea_service.tool_for_path("a.exe") == "inspect_pe_imports"
    assert rea_service.tool_for_path("a.so") == "inspect_elf_notes"
    assert rea_service.tool_for_path("a.pyc") == "inspect_python_pyc"
    assert rea_service.tool_for_path("a.sqlite") == "inspect_sqlite_database"
    assert rea_service.tool_for_path("a.unknown") == "inspect_binary_layout"


def test_discovery_returns_only_allowlisted_tools(monkeypatch):
    monkeypatch.setattr(
        mcp_service,
        "discover_tools",
        lambda cfg: [
            {"name": "inspect_binary_layout", "description": "", "inputSchema": {}},
            {"name": "gdb_console", "description": "", "inputSchema": {}},
            {"name": "push_adb_file", "description": "", "inputSchema": {}},
        ],
    )

    tools = rea_service.discover_tools()

    assert [tool["name"] for tool in tools] == ["inspect_binary_layout"]
