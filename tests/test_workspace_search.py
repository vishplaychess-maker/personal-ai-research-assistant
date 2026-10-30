"""Recursive workspace search must stay inside the workspace and bounded."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from app.config import settings
from app.services import agent_context, orchestrator


@pytest.fixture()
def workspace(monkeypatch):
    base = "D:/" if os.name == "nt" else None
    root = Path(tempfile.mkdtemp(prefix="thunder-ws-", dir=base))
    (root / "pkg").mkdir()
    (root / "pkg" / "alpha.py").write_text(
        '"""Alpha module summary."""\n\nVALUE = 1\n', encoding="utf-8"
    )
    (root / "pkg" / "notes.txt").write_text("notes", encoding="utf-8")
    (root / "pkg" / "sub").mkdir()
    (root / "pkg" / "sub" / "beta.py").write_text(
        '"""Beta module."""\n', encoding="utf-8"
    )
    (root / "secrets").mkdir()
    (root / "secrets" / "hidden.py").write_text("HIDDEN = 1", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.py").write_text("DEP = 1", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    outside = Path(tempfile.mkdtemp(prefix="thunder-outside-", dir=base))
    (outside / "evil.py").write_text("EVIL = 1", encoding="utf-8")

    monkeypatch.setattr(settings, "workspace_dir", str(root))
    yield root, outside
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(outside, ignore_errors=True)


def test_searches_python_files_and_reads_docstrings(workspace):
    root, _outside = workspace

    results = agent_context.search_workspace_files("pkg", "*.py")

    assert {item["path"] for item in results} == {
        "pkg/alpha.py",
        "pkg/sub/beta.py",
    }
    assert results[0]["description"] == "Alpha module summary."
    assert (root / "pkg" / "alpha.py").is_file()


def test_skips_sensitive_and_dependency_directories(workspace):
    _root, _outside = workspace

    paths = {item["path"] for item in agent_context.search_workspace_files(".", "*.py")}

    assert "secrets/hidden.py" not in paths
    assert "node_modules/dep.py" not in paths
    assert "pkg/alpha.py" in paths


def test_rejects_parent_traversal(workspace):
    with pytest.raises(ValueError):
        agent_context.search_workspace_files("..", "*.py")
    with pytest.raises(ValueError):
        agent_context.search_workspace_files(".", "../*.py")


def test_rejects_absolute_directory_outside_workspace(workspace):
    _root, outside = workspace

    with pytest.raises(ValueError):
        agent_context.search_workspace_files(str(outside), "*.py")


def test_missing_directory_is_an_error(workspace):
    with pytest.raises(OSError):
        agent_context.search_workspace_files("does-not-exist", "*.py")


def test_orchestrator_search_uses_the_confined_tool(workspace):
    block, warnings = orchestrator._workspace_search(
        "task-" + "a" * 24, "List all Python files in pkg/sub"
    )

    assert warnings == []
    assert "pkg/sub/beta.py" in block
    assert "Beta module." in block
