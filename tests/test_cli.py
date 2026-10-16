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
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("")
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace/.env").write_text("")
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
    assert signals == [(123, 0), (123, signal.SIGHUP), (123, signal.SIGTERM)]
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
    assert len(signals) == 3


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


def test_subcommands_scope_options_and_preserve_default_run(tmp_path, capsys):
    config = tmp_path / ".env"
    assert cli.parse_arguments(["setup"])[1].directory == cli.Path(".")
    for option in ("-d", "--directory"):
        args = cli.parse_arguments(["setup", option, str(tmp_path)])[1]
        assert args.command == "setup" and args.directory == tmp_path
    args = cli.parse_arguments(["login", "-n", "personal", "-m", "code"])[1]
    assert args.namespace == "personal" and args.login_method == "code"
    for argv in (
        [],
        ["run"],
        ["--no-reload"],
        ["run", "--no-reload"],
    ):
        assert cli.parse_arguments(argv)[1].command == "run"
    for argv in (
        ["--env-file", str(config)],
        ["--env-file", str(config), "login"],
        ["login", "--env-file", str(config)],
        ["-e", str(config)],
        ["-e", str(config), "login"],
        ["login", "-e", str(config)],
    ):
        assert cli.parse_arguments(argv)[1].env_file == config
    for argv in (
        ["run", "--namespace", "personal"],
        ["run", "--login-method", "code"],
        ["--namespace", "personal", "login"],
        ["login", "--directory", str(tmp_path)],
        ["init"],
        ["help", "unknown"],
    ):
        with pytest.raises(SystemExit) as error:
            cli.parse_arguments(argv)
        assert error.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_help_exits_without_loading_configuration(monkeypatch, capsys):
    configure = Mock(side_effect=AssertionError("Help must work before setup"))
    monkeypatch.setattr(cli.Settings, "from_env", configure)
    for argv, expected in (
        (["help"], "commands:"),
        (["--help"], "commands:"),
        (["help", "login"], "--namespace"),
        (["login", "--help"], "--login-method"),
        (["help", "setup"], "--directory"),
    ):
        monkeypatch.setattr("sys.argv", ["tdl-bot", *argv])
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 0
        assert expected in capsys.readouterr().out
    for command in (
        "run",
        "login",
        "reload",
        "stop",
        "setup",
        "health",
        "container",
        "engine",
        "frontend",
    ):
        outputs = []
        for argv in (
            ["help", command],
            [command, "help"],
            [command, "--help"],
            [command, "-h"],
        ):
            monkeypatch.setattr("sys.argv", ["tdl-bot", *argv])
            with pytest.raises(SystemExit) as error:
                cli.main()
            assert error.value.code == 0
            outputs.append(capsys.readouterr().out)
        assert all(output == outputs[0] for output in outputs)
    # An option value named 'help' must not become a help request.
    args = cli.parse_arguments(["login", "-n", "help"])[1]
    assert args.namespace == "help" and args.command_help is None
    configure.assert_not_called()


def test_reload_discovers_instances_and_requires_an_explicit_choice(
    tmp_path, monkeypatch, capsys
):
    from pathlib import Path

    from tdl_bot.runtime.instances import (
        discover,
        registered,
        registry_directory,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    settings = []
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        (root / "frontend.pid").write_text(str(os.getpid()))
        settings.append(
            SimpleNamespace(env_file=root / ".env", runtime_dir=root)
        )
    signal_process = Mock(return_value=True)
    monkeypatch.setattr(cli, "signal_supervisor", signal_process)
    with registered(settings[0]):
        # No .env is needed to reload a registered live process from any cwd.
        cli.reload_instances()
        assert "Reload requested:" in capsys.readouterr().out
        assert signal_process.call_args.args == (
            settings[0].runtime_dir,
            signal.SIGHUP,
        )
        signal_process.reset_mock()
        with registered(settings[1]):
            with pytest.raises(UserError, match="Multiple instances"):
                cli.reload_instances()
            assert all(
                call.args[1] == 0 for call in signal_process.call_args_list
            )
            signal_process.reset_mock()
            cli.reload_instances(all_instances=True)
            assert [
                call.args[0]
                for call in signal_process.call_args_list
                if call.args[1] == signal.SIGHUP
            ] == [s.runtime_dir for s in settings]
            signal_process.reset_mock()
            cli.reload_instances(settings[1].env_file)
            assert signal_process.call_args.args == (
                settings[1].runtime_dir,
                signal.SIGHUP,
            )
            # A failure in one instance must not prevent another reload.
            signal_process.side_effect = lambda path, sig: (
                not (sig == signal.SIGHUP and path == settings[0].runtime_dir)
            )
            with pytest.raises(UserError, match="1 instance"):
                cli.reload_instances(all_instances=True)
            assert signal_process.call_args.args == (
                settings[1].runtime_dir,
                signal.SIGHUP,
            )
        # Stale PIDs and malformed records never become reload targets.
        (settings[0].runtime_dir / "frontend.pid").write_text("-1")
        (registry_directory() / "broken.json").write_text("{invalid")
        assert discover(lambda _: True) == []
    assert len(list(registry_directory().glob("*.json"))) == 1
    args = cli.parse_arguments(["reload", "-a"])[1]
    assert args.all_instances
    with pytest.raises(SystemExit) as error:
        cli.parse_arguments(["reload", "-a", "-e", str(Path(".env"))])
    assert error.value.code == 2
