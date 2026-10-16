"""Native lifecycle and cross-process controls without Telegram access."""

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tdl_bot.core.config import Settings
from tdl_bot.core.errors import UserError
from tdl_bot.integrations.tdl.runner import Tdl
from tdl_bot.runtime import cli, locks, windows


def test_native_imports_and_child_processes_without_unix_modules(tmp_path):
    script = """
import builtins
original = builtins.__import__
def portable(name, *args, **kwargs):
    if name == 'fcntl':
        raise ImportError('Unix module unavailable')
    return original(name, *args, **kwargs)
builtins.__import__ = portable
from tdl_bot.runtime.cli import parse_arguments
parse_arguments(['help'])
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=20
    )
    assert result.returncode == 0, result.stderr.decode()
    assert b"setup" in result.stdout


async def test_native_tdl_subprocess_can_run_and_cancel(tmp_path):
    tdl = Tdl(Path(sys.executable), "default")
    assert await tdl._run(["-c", "print('ready')"]) == "ready"
    task = asyncio.create_task(tdl._run(["-c", "import time; time.sleep(60)"]))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    # Cancellation must release both coordination locks.
    assert await tdl._run(["-c", "print('released')"]) == "released"


def test_cross_process_native_health_stop_and_stale_recovery(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path)
    script = """
import sys, time
from pathlib import Path
from tdl_bot.runtime.cli import instance_lock
from tdl_bot.runtime.windows import lifetime
root = Path(sys.argv[1])
with instance_lock(root / '.supervisor.lock'), lifetime(root):
    (root / 'polling.heartbeat').touch()
    while not (root / 'stop.request').exists():
        time.sleep(0.01)
    time.sleep(0.1)
    (root / 'drained').touch()
"""
    # A previous crash must not stop the new instance immediately.
    (tmp_path / "stop.request").touch()
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)])
    try:
        import time

        deadline = time.monotonic() + 15
        while not (tmp_path / "polling.heartbeat").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert locks.busy(tmp_path / ".supervisor.lock")
        with pytest.raises(UserError, match="Another process"):
            with cli.instance_lock(tmp_path / ".supervisor.lock"):
                pytest.fail("An active workspace must remain locked")
        windows.control(settings, "health")
        os.utime(tmp_path / "polling.heartbeat", (0, 0))
        with pytest.raises(UserError, match="unavailable"):
            windows.control(settings, "health")
        windows.control(settings, "stop")
        assert (tmp_path / "drained").exists()
        assert process.wait(timeout=10) == 0
        assert not (tmp_path / "stop.request").exists()
        assert not (tmp_path / "polling.heartbeat").exists()
        with pytest.raises(UserError, match="No Bot"):
            windows.control(settings, "health")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


@pytest.mark.parametrize("end", ["stop", "interrupt", "failure"])
async def test_native_polling_stops_before_shared_services_close(tmp_path, end):
    entered = asyncio.Event()
    application = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        bot=object(),
        register_commands=AsyncMock(),
        service=SimpleNamespace(
            reconcile_permissions=AsyncMock(),
            backups=SimpleNamespace(
                quiesce=Mock(), reconcile_permissions=AsyncMock()
            ),
        ),
        close=AsyncMock(),
    )

    async def polling(bot, **options):
        assert bot is application.bot
        assert options["handle_signals"] is False
        assert options["handle_as_tasks"] is True
        assert options["close_bot_session"] is False
        entered.set()
        try:
            if end == "failure":
                raise RuntimeError("Polling failed")
            await release.wait()
        finally:
            application.dispatcher.closed = True

    async def stop_polling():
        release.set()

    release = asyncio.Event()
    application.dispatcher = SimpleNamespace(
        start_polling=polling,
        stop_polling=stop_polling,
        closed=False,
        update=SimpleNamespace(outer_middleware=Mock()),
    )
    original = signal.getsignal(signal.SIGINT)
    task = asyncio.create_task(windows.poll(application))
    await entered.wait()
    if end == "interrupt":
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
    elif end == "stop":
        (tmp_path / "stop.request").touch()
    if end == "failure":
        with pytest.raises(RuntimeError, match="Polling failed"):
            await asyncio.wait_for(task, 5)
    else:
        await asyncio.wait_for(task, 5)
    assert application.dispatcher.closed
    application.service.backups.quiesce.assert_called_once()
    application.close.assert_awaited_once()
    assert signal.getsignal(signal.SIGINT) == original
    assert not (tmp_path / "polling.heartbeat").exists()


@pytest.mark.parametrize(
    "command", ["run", "login", "reload", "health", "stop"]
)
def test_native_cli_routes_without_unix_process_control(
    tmp_path, monkeypatch, command
):
    settings = Settings(
        "99:TEST",
        0,
        "",
        frozenset({1}),
        frozenset({1}),
        tmp_path / "data",
        tmp_path / "bin/tdl.exe",
        tdl_dir=tmp_path / "sessions",
    )
    (settings.tdl_dir / "data").mkdir(parents=True)
    (settings.tdl_dir / "data/default").touch()
    monkeypatch.setattr(cli, "WINDOWS", True)
    monkeypatch.setattr(cli.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr("sys.argv", ["tdl-bot", command])
    run = AsyncMock()
    control = Mock()
    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr(windows, "control", control)
    monkeypatch.setattr(
        cli, "signal_supervisor", Mock(side_effect=AssertionError)
    )
    monkeypatch.setattr(cli, "ensure_engine", Mock(side_effect=AssertionError))
    if command == "reload":
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 1
        run.assert_not_awaited()
    else:
        cli.main()
        if command in {"health", "stop"}:
            control.assert_called_once_with(settings, command)
            run.assert_not_awaited()
        elif command == "login":
            run.assert_awaited_once_with(settings, command, login_method="qr")
        else:
            run.assert_awaited_once_with(settings, command)
        assert not locks.busy(settings.data_dir / ".supervisor.lock")


def test_windows_managed_executable_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tdl_bot.core.config.platform.system", lambda: "Windows"
    )
    monkeypatch.setattr("tdl_bot.core.config.platform.machine", lambda: "AMD64")
    for name in ("TDL_BINARY", "BIN_DIR", "DATA_DIR", "TDL_DIR"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / ".env"
    config.write_text("DATA_DIR=./data\nBIN_DIR=./bin\n")
    settings = Settings.from_env(login_only=True, env_file=config)
    assert settings.tdl_binary == tmp_path / "bin/windows-amd64/tdl.exe"


async def test_native_commands_overlap_and_drain_with_permission_checks(
    tmp_path, monkeypatch
):
    from aiogram import Bot, Dispatcher
    from aiogram.types import Chat, Message, Update, User

    dispatcher = Dispatcher()
    slow_started = asyncio.Event()
    fast_finished = asyncio.Event()
    release = asyncio.Event()
    exited = asyncio.Event()
    permissions_checked = asyncio.Event()
    maintenance_entered = asyncio.Event()
    maintenance_release = asyncio.Event()
    updates = []

    async def command(message):
        if message.text == "slow":
            slow_started.set()
            await release.wait()
        else:
            fast_finished.set()

    async def reconcile_permissions():
        maintenance_entered.set()
        await maintenance_release.wait()

    dispatcher.message.register(command)

    async def receive(bot, **options):
        assert options["handle_as_tasks"] is True
        for number, text in enumerate(("slow", "fast"), 1):
            update = Update(
                update_id=number,
                message=Message(
                    message_id=number,
                    date=0,
                    text=text,
                    chat=Chat(id=1, type="private"),
                    from_user=User(id=1, is_bot=False, first_name="Tester"),
                ),
            )
            updates.append(
                asyncio.create_task(dispatcher.feed_update(bot, update))
            )
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    monkeypatch.setattr(dispatcher, "_polling", receive)
    bot = Bot("99:TEST")
    application = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        bot=bot,
        dispatcher=dispatcher,
        register_commands=AsyncMock(),
        service=SimpleNamespace(
            reconcile_permissions=AsyncMock(side_effect=reconcile_permissions),
            backups=SimpleNamespace(
                quiesce=Mock(),
                reconcile_permissions=AsyncMock(
                    side_effect=permissions_checked.set
                ),
            ),
        ),
        close=AsyncMock(),
    )
    task = asyncio.create_task(windows.poll(application))
    try:
        await asyncio.wait_for(slow_started.wait(), 5)
        await asyncio.wait_for(fast_finished.wait(), 5)
        assert not release.is_set()
        await asyncio.wait_for(maintenance_entered.wait(), 5)
        (tmp_path / "stop.request").touch()
        await asyncio.wait_for(exited.wait(), 5)
        # Stop polling immediately, but let accepted commands finish first.
        assert not task.done()
        application.close.assert_not_awaited()
        maintenance_release.set()
        await asyncio.wait_for(permissions_checked.wait(), 5)
        application.service.reconcile_permissions.assert_awaited()
        application.service.backups.reconcile_permissions.assert_awaited()
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 5)
        assert all(update.done() for update in updates)
        assert not dispatcher._running_lock.locked()
        application.close.assert_awaited_once()
    finally:
        maintenance_release.set()
        release.set()
        task.cancel()
        await asyncio.gather(task, *updates, return_exceptions=True)
        await bot.session.close()


async def test_native_stop_cancels_pending_command_registration(tmp_path):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def register():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    application = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        bot=object(),
        register_commands=register,
        dispatcher=SimpleNamespace(
            update=SimpleNamespace(outer_middleware=Mock()),
            start_polling=AsyncMock(),
            stop_polling=AsyncMock(),
        ),
        service=SimpleNamespace(
            backups=None, reconcile_permissions=AsyncMock()
        ),
        close=AsyncMock(),
    )
    task = asyncio.create_task(windows.poll(application))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not (tmp_path / "polling.heartbeat").exists()
        (tmp_path / "stop.request").touch()
        await asyncio.wait_for(task, 3)
        assert cancelled.is_set()
        application.dispatcher.start_polling.assert_not_awaited()
        application.dispatcher.stop_polling.assert_not_awaited()
        application.close.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
