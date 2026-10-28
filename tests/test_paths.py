"""Portable path resolution (Phase 1 - .exe packaging).

Covers frozen vs dev detection, AppData resolution, and the fail-closed config
reader/writer. Nothing here touches the real %APPDATA% or the repo data dir.
"""

from __future__ import annotations

import json
import sys

import pytest
from sqlalchemy import create_engine, text

from app import paths


@pytest.fixture
def dev_root(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.delenv("THUNDER_AI_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "get_repo_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def frozen_env(monkeypatch, tmp_path):
    meipass = tmp_path / "_meipass"
    meipass.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.delenv("THUNDER_AI_DATA_DIR", raising=False)
    return tmp_path, meipass


def test_dev_mode_is_not_frozen(dev_root):
    assert paths.is_frozen() is False
    assert paths.get_bundle_dir() is None


def test_sys_frozen_flag_is_detected(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert paths.is_frozen() is True


def test_meipass_is_detected_without_frozen_flag(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.is_frozen() is True
    assert paths.get_bundle_dir() == tmp_path


def test_dev_paths_are_repo_relative(dev_root):
    assert paths.get_data_dir() == dev_root / "data"
    assert paths.get_db_path() == dev_root / "data" / "thunder.db"
    assert paths.get_config_path() == dev_root / "data" / "config.json"
    expected_url = "sqlite:///" + (dev_root / "data" / "thunder.db").as_posix()
    assert paths.get_database_url() == expected_url


def test_frozen_paths_use_appdata(frozen_env, monkeypatch, tmp_path):
    appdata = tmp_path / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    expected = appdata / "ThunderAI"
    assert paths.get_data_dir() == expected
    assert expected.is_dir()
    assert paths.get_db_path() == expected / "thunder.db"
    assert paths.get_config_path() == expected / "config.json"


def test_frozen_appdata_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    assert paths._appdata_root() == tmp_path / "AppData" / "Roaming"


def test_data_dir_env_override_wins(dev_root, monkeypatch, tmp_path):
    custom = tmp_path / "custom-data"
    monkeypatch.setenv("THUNDER_AI_DATA_DIR", str(custom))
    assert paths.get_data_dir() == custom
    assert paths.get_db_path() == custom / "thunder.db"


def test_dev_static_dir_is_frontend_dist(dev_root):
    assert paths.get_static_dir() == dev_root / "frontend" / "dist"


def test_frozen_static_dir_prefers_bundled_frontend_dist(frozen_env):
    _tmp, meipass = frozen_env
    dist = meipass / "frontend" / "dist"
    dist.mkdir(parents=True)
    assert paths.get_static_dir() == dist


def test_frozen_static_dir_accepts_flat_dist(frozen_env):
    _tmp, meipass = frozen_env
    dist = meipass / "dist"
    dist.mkdir(parents=True)
    assert paths.get_static_dir() == dist


def test_frozen_static_dir_default_when_absent(frozen_env):
    _tmp, meipass = frozen_env
    assert paths.get_static_dir() == meipass / "frontend" / "dist"
    assert paths.static_dir_available() is False


def test_static_dir_available_requires_index(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    assert paths.static_dir_available(dist) is False
    (dist / "index.html").write_text("x", encoding="utf-8")
    assert paths.static_dir_available(dist) is True


def test_database_url_is_a_usable_sqlite_database(dev_root):
    engine = create_engine(paths.get_database_url())
    with engine.connect() as conn:
        assert conn.execute(text("select 1")).scalar() == 1


def test_config_round_trip(dev_root):
    assert paths.load_config() == {}
    assert paths.save_config({"gemini_api_key": "abc123"}) is True
    assert paths.load_config() == {"gemini_api_key": "abc123"}
    assert paths.save_config({"other": 1}) is True
    assert paths.load_config() == {"gemini_api_key": "abc123", "other": 1}


def test_load_config_corrupt_is_fail_closed(dev_root):
    paths.get_config_path().write_text("this is not json", encoding="utf-8")
    assert paths.load_config() == {}


def test_load_config_non_object_is_fail_closed(dev_root):
    paths.get_config_path().write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert paths.load_config() == {}


def test_save_config_rejects_empty_and_fails_closed(dev_root, monkeypatch, tmp_path):
    assert paths.save_config({}) is False
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(paths, "get_config_path", lambda: blocker / "config.json")
    assert paths.save_config({"gemini_api_key": "x"}) is False
