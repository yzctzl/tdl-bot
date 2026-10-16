"""Portable workspace configuration and installed CLI behavior, offline."""

import os

import pytest

from tdl_bot.core.config import Settings
from tdl_bot.core.errors import UserError
from tdl_bot.runtime.setup import initialize


def test_workspace_paths_reload_without_exporting_configuration(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    initialize(root)
    config = root / ".env"
    monkeypatch.chdir(tmp_path)
    for name in (
        "TDL_BOT_ENV_FILE",
        "BOT_TOKEN",
        "ADMIN_USER_IDS",
        "DATA_DIR",
        "BIN_DIR",
        "TDL_BINARY",
        "TDL_DIR",
        "TDL_PROXY",
        "RUNTIME_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="Configuration file not found"):
        Settings.from_env()
    monkeypatch.setenv("TDL_BOT_ENV_FILE", str(config))
    with pytest.raises(ValueError, match="ADMIN_USER_IDS"):
        Settings.from_env()
    settings = Settings.from_env(login_only=True)
    assert settings.data_dir == root / "data"
    assert settings.tdl_binary == root / "bin/tdl"
    assert settings.tdl_dir == root / "data/tdl"
    assert "DATA_DIR" not in os.environ
    original = config.read_text()
    config.write_text(original.replace("WORKERS=2", "WORKERS=4"))
    assert Settings.from_env(login_only=True).workers == 4
    monkeypatch.setenv("WORKERS", "5")
    assert Settings.from_env(login_only=True).workers == 5
    with pytest.raises(UserError, match="already exists"):
        initialize(root)
    assert "WORKERS=4" in config.read_text()
    config.write_text(
        "DATA_DIR=../state\nTDL_BINARY=../executables/tdl\nTDL_DIR=../sessions\nRUNTIME_DIR=./run\n"
    )
    settings = Settings.from_env(login_only=True)
    assert settings.data_dir == tmp_path / "state"
    assert settings.tdl_binary == tmp_path / "executables/tdl"
    assert settings.tdl_dir == tmp_path / "sessions"
    assert settings.runtime_dir == root / "run"
    for text, error in [
        ("BIN_DIR=bin", "DATA_DIR"),
        ("DATA_DIR=data", "BIN_DIR"),
    ]:
        config.write_text(text)
        with pytest.raises(ValueError, match=error):
            Settings.from_env(login_only=True)
    assert not list(tmp_path.glob("*.sqlite3"))
