"""CLI lifecycle tests without starting production processes or sessions."""

import asyncio
import os
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from tdl_bot.core.config import Settings
from tdl_bot.core.errors import UserError
from tdl_bot.runtime import cli
from tdl_bot.storage.store import Store


@pytest.fixture
def settings(tmp_path, monkeypatch):
    settings = Settings(
        "99:TEST",
        0,
        "",
        frozenset({1, 2}),
        frozenset({1}),
        tmp_path / "data",
        tmp_path / "tdl",
        tdl_dir=tmp_path / "sessions",
    )
    settings.data_dir.mkdir()
    (settings.tdl_dir / "data").mkdir(parents=True)
    (settings.tdl_dir / "data/default").touch()
    monkeypatch.setattr(cli.Settings, "from_env", Mock(return_value=settings))
    previous_mask = os.umask(0o077)
    try:
        yield settings
    finally:
        os.umask(previous_mask)


def test_reload_and_stop_require_live_supervisor_and_release_locks(
    settings, monkeypatch
):
    pid_file = settings.data_dir / "frontend.pid"
    pid_file.write_text("123")
    (settings.data_dir / "runtime.sock").touch()
    signals = []

    def kill(pid, sig):
        signals.append((pid, sig))
        if sig == signal.SIGTERM:
            pid_file.unlink()

    monkeypatch.setattr(cli.os, "kill", kill)
    exchange = AsyncMock()
    monkeypatch.setattr(cli, "exchange", exchange)
    drain = AsyncMock()
    monkeypatch.setattr("tdl_bot.runtime.container.drain_engine", drain)
    lock = settings.data_dir / ".supervisor.lock"
    with cli.instance_lock(lock):
        with pytest.raises(UserError, match="Another process"):
            with cli.instance_lock(lock):
                pytest.fail("Duplicate supervisor acquired the lock")
        monkeypatch.setattr("sys.argv", ["tdl-bot", "reload"])
        cli.main()
        monkeypatch.setattr("sys.argv", ["tdl-bot", "stop"])
        cli.main()
    assert signals == [(123, signal.SIGHUP), (123, signal.SIGTERM)]
    exchange.assert_has_awaits(
        [
            call(
                settings.data_dir / "runtime.sock",
                {"method": "ping", "args": {"reload": True}},
            ),
        ]
    )
    assert exchange.await_count == 1
    drain.assert_awaited_once_with(settings)
    # Never signal a stale PID after the supervisor releases its lock.
    monkeypatch.setattr("sys.argv", ["tdl-bot", "reload"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert len(signals) == 2


@pytest.mark.parametrize("command", ["run", "frontend", "engine"])
def test_cli_routes_to_correct_process_with_isolated_locks(
    settings, monkeypatch, command
):
    operations = {
        name: AsyncMock() for name in ("run", "frontend", "supervise")
    }
    monkeypatch.setattr(cli, "run", operations["run"])
    monkeypatch.setattr(cli, "frontend", operations["frontend"])
    monkeypatch.setattr(
        "tdl_bot.runtime.supervisor.supervise", operations["supervise"]
    )
    monkeypatch.setattr("sys.argv", ["tdl-bot", command])
    cli.main()
    if command == "engine":
        operations["run"].assert_awaited_once_with(settings, "engine")
        assert (settings.tdl_dir / ".bot-default.lock").exists()
        assert settings.tdl_binary.with_suffix(".lock").exists()
    elif command == "run":
        operations["supervise"].assert_awaited_once_with(settings.data_dir)
        operations["run"].assert_not_awaited()
    else:
        operations["frontend"].assert_awaited_once_with(settings)
        operations["run"].assert_not_awaited()


async def test_frontend_reconnects_and_closes_resources_without_stopping_engine(
    settings, monkeypatch
):
    Store(settings.data_dir / "state.sqlite3").close()
    ensure = AsyncMock(return_value=settings.data_dir / "runtime.sock")
    exchange = AsyncMock()
    monkeypatch.setattr(cli, "ensure_engine", ensure)
    monkeypatch.setattr(cli, "exchange", exchange)
    bot = SimpleNamespace(
        set_my_commands=AsyncMock(), session=SimpleNamespace(close=AsyncMock())
    )
    monkeypatch.setattr(cli, "Bot", Mock(return_value=bot))
    applications = []
    application_class = cli.BotApplication

    def application(*args):
        result = application_class(*args)
        applications.append(result)
        return result

    async def polling(self, received_bot, **kwargs):
        assert received_bot is bot
        assert kwargs["allowed_updates"] == ["message", "callback_query"]
        applications[0]._update = asyncio.create_task(asyncio.Event().wait())

    monkeypatch.setattr(cli, "BotApplication", application)
    monkeypatch.setattr("aiogram.Dispatcher.start_polling", polling)
    await cli.frontend(settings)
    assert applications[0]._update.cancelled()
    bot.session.close.assert_awaited_once()
    assert bot.set_my_commands.await_count >= 2
    exchange.assert_awaited_once()
    assert exchange.call_args.args[1]["method"] == "ping"


@pytest.mark.parametrize("namespace", ["personal", "../outside"])
def test_login_uses_project_storage_without_requiring_an_existing_namespace(
    settings, monkeypatch, namespace
):
    run = AsyncMock()
    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "tdl-bot",
            "login",
            "--namespace",
            namespace,
            "--login-method",
            "code",
        ],
    )
    if namespace.startswith(".."):
        with pytest.raises(SystemExit):
            cli.main()
        run.assert_not_awaited()
    else:
        cli.main()
        configured, command = run.call_args.args
        assert command == "login" and configured.tdl_namespace == namespace
        assert configured.tdl_dir == settings.tdl_dir
        assert settings.tdl_namespace == "default"
        assert run.call_args.kwargs == {"login_method": "code"}
