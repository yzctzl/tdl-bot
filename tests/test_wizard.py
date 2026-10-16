"""Offline onboarding flows; no real tokens, logins or Telegram writes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from dotenv import dotenv_values

from tdl_bot.core.config import Settings
from tdl_bot.core.errors import UserError
from tdl_bot.runtime import wizard
from tdl_bot.runtime.setup import initialize
from tdl_bot.storage.models import Target
from tdl_bot.storage.store import Store


@pytest.mark.parametrize("existing_members", [False, True])
async def test_wizard_retries_and_saves_verified_configuration(
    tmp_path, monkeypatch, capsys, existing_members
):
    initialize(tmp_path, announce=False)
    config = tmp_path / ".env"
    original = config.read_text().replace("WORKERS=2", "WORKERS=4")
    config.write_text(original)
    settings = Settings.from_env(login_only=True, env_file=config)
    if existing_members:
        store = Store(settings.data_dir / "state.sqlite3")
        store.seed_members(frozenset({7, 8}), frozenset({7}))
        store.set_target(7, Target("-10042", 9))
        store.close()
    tokens = iter(["bad", "9:FAKE", "9:FAKE"])
    monkeypatch.setattr(wizard.getpass, "getpass", lambda _: next(tokens))
    answers = iter(["", "1"] if existing_members else ["0", "7,8", "1", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    bots = []

    def make_bot(token):
        bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="example")),
            session=SimpleNamespace(close=AsyncMock()),
        )
        if not bots:
            from aiogram.exceptions import TelegramNetworkError

            bot.get_me.side_effect = TelegramNetworkError(
                method=Mock(), message="offline"
            )
        bots.append(bot)
        return bot

    monkeypatch.setattr(wizard, "Bot", make_bot)
    login = AsyncMock()
    monkeypatch.setattr(wizard, "native_login", login)
    monkeypatch.setattr(
        wizard, "Tdl", Mock(return_value=SimpleNamespace(account_lock=None))
    )
    monkeypatch.setattr(
        wizard,
        "load_tdl_session",
        AsyncMock(
            return_value=SimpleNamespace(
                session=Mock(), api_id=1, api_hash="fake"
            )
        ),
    )
    iter_dialogs = Mock(
        side_effect=AssertionError("Do not list chats by default")
    )

    client = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        get_me=AsyncMock(
            return_value=SimpleNamespace(id=7, username="example", bot=False)
        ),
        iter_dialogs=iter_dialogs,
    )
    monkeypatch.setattr(
        wizard, "SharedSessionClient", Mock(return_value=client)
    )
    target = Target("-10042", 9 if existing_members else None)
    validate = AsyncMock(side_effect=[UserError("Missing permission"), target])
    monkeypatch.setattr(
        wizard,
        "Delivery",
        Mock(return_value=SimpleNamespace(validate=validate)),
    )
    media = SimpleNamespace(validate_target=AsyncMock(return_value=Mock()))
    monkeypatch.setattr(wizard, "TelegramMedia", Mock(return_value=media))
    monkeypatch.setattr(wizard, "_check_write_access", AsyncMock())
    await wizard._configure(settings, config)
    saved = dotenv_values(config)
    assert saved["BOT_TOKEN"] == "9:FAKE" and saved["WORKERS"] == "4"
    assert saved["ADMIN_USER_IDS"] == ("" if existing_members else "7,8")
    assert config.stat().st_mode & 0o777 == 0o600
    store = Store(settings.data_dir / "state.sqlite3")
    assert store.internal_target() == target
    store.close()
    iter_dialogs.assert_not_called()
    login.assert_awaited_once()
    client.disconnect.assert_awaited_once()
    assert all(bot.session.close.await_count == 1 for bot in bots)
    output = capsys.readouterr().out
    assert "9:FAKE" not in output and "Invalid token" in output


def test_interactive_setup_requires_terminal_and_respects_live_locks(
    tmp_path, monkeypatch
):
    from tdl_bot.runtime.cli import instance_lock, main

    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: False)
    with pytest.raises(UserError, match="terminal"):
        wizard.configure(tmp_path)
    assert not (tmp_path / ".env").exists()
    monkeypatch.setattr(wizard.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(wizard.sys.stdout, "isatty", lambda: True)
    initialize(tmp_path, announce=False)
    original = (tmp_path / ".env").read_bytes()
    with instance_lock(tmp_path / "data/.supervisor.lock"):
        with pytest.raises(UserError, match="Another process"):
            wizard.configure(tmp_path)
    assert (tmp_path / ".env").read_bytes() == original
    configure = Mock()
    monkeypatch.setattr(wizard, "configure", configure)
    monkeypatch.setattr(
        "sys.argv", ["tdl-bot", "setup", "-i", "-d", str(tmp_path)]
    )
    main()
    configure.assert_called_once_with(tmp_path)


async def test_storage_checks_execution_account_permissions():
    permissions = SimpleNamespace(
        post_messages=False,
        has_left=False,
        is_admin=False,
        participant=SimpleNamespace(banned_rights=None),
    )
    entity = SimpleNamespace(broadcast=True)
    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=entity),
        get_permissions=AsyncMock(return_value=permissions),
    )
    with pytest.raises(UserError, match="permission"):
        await wizard._check_write_access(client, Mock())
    permissions.post_messages = True
    await wizard._check_write_access(client, Mock())
    entity.broadcast = False
    entity.default_banned_rights = SimpleNamespace(send_videos=True)
    with pytest.raises(UserError, match="permission"):
        await wizard._check_write_access(client, Mock())
    entity.default_banned_rights = None
    await wizard._check_write_access(client, Mock())


@pytest.mark.parametrize("owned", [False, True])
async def test_storage_lists_only_owned_chats_on_request(
    tmp_path, monkeypatch, capsys, owned
):
    initialize(tmp_path, announce=False)
    settings = Settings.from_env(login_only=True, env_file=tmp_path / ".env")
    answers = iter(["list", "42"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    async def dialogs():
        for owner, name in ((False, "Joined chat"), (owned, "Owned chat")):
            yield SimpleNamespace(
                is_channel=True,
                is_group=False,
                id=-10042,
                name=name,
                entity=SimpleNamespace(creator=owner, username="example"),
            )

    client = SimpleNamespace(iter_dialogs=dialogs)
    validate = AsyncMock(return_value=Target("-10042"))
    monkeypatch.setattr(
        wizard,
        "Delivery",
        Mock(return_value=SimpleNamespace(validate=validate)),
    )
    monkeypatch.setattr(
        wizard,
        "TelegramMedia",
        Mock(return_value=SimpleNamespace(validate_target=AsyncMock())),
    )
    monkeypatch.setattr(wizard, "_check_write_access", AsyncMock())
    await wizard._storage(Mock(), client, settings, 7)
    # Bare numeric input always remains a UID, never a list index.
    validate.assert_awaited_once_with(Target("42"), 7, storage=True)
    output = capsys.readouterr().out
    assert "Joined chat" not in output
    assert ("Owned chat · -10042 · @example" in output) == owned
    assert ("No channels or groups owned" in output) != owned


@pytest.mark.parametrize("detected", ["Asia/Taipei", "unknown"])
def test_timezone_setup_detects_confirms_and_validates(
    tmp_path, monkeypatch, capsys, detected
):
    from tdl_bot.runtime import setup

    monkeypatch.setattr(setup, "get_localzone_name", lambda: detected)
    initialize(tmp_path, announce=False)
    config = tmp_path / ".env"
    values = dotenv_values(config)
    assert values["TZ"] == ("Asia/Taipei" if detected != "unknown" else "")
    original = config.read_text()
    answers = iter(["invalid/timezone", "Europe/London"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    wizard._timezone(config)
    assert dotenv_values(config)["TZ"] == "Europe/London"
    assert "Enter a timezone" in capsys.readouterr().out
    assert "DATA_DIR=./data" in config.read_text()
    assert "BOT_TOKEN=" in config.read_text()
    monkeypatch.setattr("builtins.input", lambda _: "")
    wizard._timezone(config)
    assert dotenv_values(config)["TZ"] == "Europe/London"
    with pytest.raises(UserError, match="already exists"):
        initialize(tmp_path)
    assert config.read_text() != original
    assert dotenv_values(config)["TZ"] == "Europe/London"
