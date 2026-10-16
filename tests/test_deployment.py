"""Portable workspace configuration and installed CLI behavior, offline."""

import os
import platform

import pytest

from tdl_bot.core.config import Settings, binary_platform
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
    assert settings.tdl_binary == root / "bin" / binary_platform() / (
        "tdl.exe" if platform.system() == "Windows" else "tdl"
    )
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


async def test_container_drain_waits_for_engine_and_coalesces_shutdowns(
    tmp_path,
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from tdl_bot.runtime.cli import _health, instance_lock
    from tdl_bot.runtime.container import drain_engine
    from tdl_bot.runtime.engine import EngineServer

    runtime = tmp_path / "run"
    runtime.mkdir()
    settings = SimpleNamespace(data_dir=tmp_path, runtime_dir=runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()

    backups = SimpleNamespace(
        quiesce=Mock(),
        close=AsyncMock(),
        reconcile_permissions=AsyncMock(),
        busy=False,
    )
    service = SimpleNamespace(
        _jobs={},
        _closing=False,
        backups=backups,
        close=AsyncMock(side_effect=close),
        reconcile_permissions=AsyncMock(),
    )
    server = EngineServer(service, settings)

    async def serve():
        with instance_lock(tmp_path / ".instance.lock"):
            await server.serve()

    running = asyncio.create_task(serve())
    drains = []
    try:
        async with asyncio.timeout(3):
            while not (runtime / "runtime.sock").exists():  # noqa: ASYNC110 - Wait for local RPC listener.
                await asyncio.sleep(0.01)
        await _health(settings)
        drains = [asyncio.create_task(drain_engine(settings)) for _ in range(2)]
        await asyncio.wait_for(entered.wait(), 3)
        assert service._closing and not any(t.done() for t in drains)
        backups.quiesce.assert_called_once()
        release.set()
        await asyncio.wait_for(asyncio.gather(*drains, running), 3)
        service.close.assert_awaited_once()
        backups.close.assert_awaited_once()
        assert not (runtime / "runtime.sock").exists()
        await drain_engine(settings)  # Already stopped is an idempotent no-op.
        with pytest.raises(UserError, match="engine is unavailable"):
            await _health(settings)
    finally:
        release.set()
        for task in [running, *drains]:
            task.cancel()
        await asyncio.gather(running, *drains, return_exceptions=True)


def test_activity_and_member_times_follow_system_timezone():
    import subprocess
    import sys

    script = """
import os
import time
from tdl_bot.bot.members import MemberCommands
from tdl_bot.transfers.activity import activity_line

instants = (1704067200, 1719792000)

def check(expected):
    for timestamp, text in zip(instants, expected, strict=True):
        assert MemberCommands._deadline(timestamp) == text
        assert text in activity_line(
            {'created': timestamp, 'kind': 'media',
             'url': 'https://t.me/example/1'}, '@example'
        )

# Also covers the actual Windows timezone without POSIX-only configuration.
check([time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(t)) for t in instants])
if hasattr(time, 'tzset'):
    for zone, expected in (
        ('UTC0', ('2024-01-01 00:00 UTC', '2024-07-01 00:00 UTC')),
        ('CST-8', ('2024-01-01 08:00 CST', '2024-07-01 08:00 CST')),
        ('EST5EDT,M3.2.0,M11.1.0',
         ('2023-12-31 19:00 EST', '2024-06-30 20:00 EDT')),
    ):
        os.environ['TZ'] = zone
        time.tzset()
        check(expected)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONTZPATH": ""},
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode()
