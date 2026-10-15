"""Account binding verification and cleanup without network access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession

from tdl_bot.core.config import AccountBinding, Settings
from tdl_bot.core.errors import UserError
from tdl_bot.integrations.tdl.runner import UpdateGate
from tdl_bot.integrations.telegram.client import AccountLock
from tdl_bot.runtime.cli import run

pytestmark = pytest.mark.usefixtures("chinese")


@pytest.mark.parametrize(
    "failure", [None, "identity", "expired", "duplicate", "connect"]
)
async def test_account_startup_validates_and_disconnects_all_clients(
    tmp_path, monkeypatch, failure
):
    def imported(key):
        session = MemorySession()
        session.auth_key = AuthKey(bytes([key]) * 256)
        return SimpleNamespace(session=session, api_id=1, api_hash="test")

    shared = imported(1)
    personal = imported(1 if failure == "duplicate" else 2)
    imports = AsyncMock(side_effect=[shared, personal])
    monkeypatch.setattr("tdl_bot.runtime.cli.load_tdl_session", imports)
    clients = []

    def make_client(session, *args, **kwargs):
        own = bool(clients)
        client = SimpleNamespace(
            session=session,
            connect=AsyncMock(),
            disconnect=AsyncMock(),
            is_user_authorized=AsyncMock(
                return_value=not (own and failure == "expired")
            ),
            get_me=AsyncMock(
                return_value=SimpleNamespace(
                    bot=False,
                    id=3
                    if own and failure == "identity"
                    else (2 if own else 1),
                )
            ),
        )
        if own and failure == "connect":
            client.connect.side_effect = OSError("simulated network failure")
        clients.append(client)
        return client

    monkeypatch.setattr("tdl_bot.runtime.cli.SharedSessionClient", make_client)
    tdls = []

    def make_tdl(binary, namespace, *args, **kwargs):
        tdl = SimpleNamespace(
            gate=UpdateGate(),
            account_lock=AccountLock(),
            version=AsyncMock(),
            namespace=namespace,
            proxy=kwargs.get("proxy"),
        )
        tdls.append(tdl)
        return tdl

    monkeypatch.setattr("tdl_bot.runtime.cli.Tdl", make_tdl)
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    constructor = Mock(return_value=bot)
    monkeypatch.setattr("tdl_bot.runtime.cli.Bot", constructor)
    monkeypatch.setattr(
        "tdl_bot.runtime.cli.ProgressMessages", lambda bot: AsyncMock()
    )
    engine = SimpleNamespace(serve=AsyncMock())
    engine_constructor = Mock(return_value=engine)
    monkeypatch.setattr("tdl_bot.runtime.cli.EngineServer", engine_constructor)
    settings = Settings(
        "configured",
        0,
        "",
        frozenset({1, 2, 3}),
        frozenset({1}),
        tmp_path,
        tmp_path / "tdl",
        tdl_dir=tmp_path / "shared",
        account_bindings=(AccountBinding(2, tmp_path / "own", "personal"),),
    )
    if failure:
        with pytest.raises(OSError if failure == "connect" else UserError):
            await run(settings, "run")
        assert not (tmp_path / "state.sqlite3").exists()
        constructor.assert_not_called()
    else:
        await run(settings, "run")
        service = engine_constructor.call_args.args[0]
        assert service.accounts.select(2).media.client is clients[1]
        assert service.accounts.select(3).media.client is clients[0]
        assert tdls[0].gate is tdls[1].gate
        bot.session.close.assert_awaited_once()
        engine.serve.assert_awaited_once()
    for client in clients:
        client.disconnect.assert_awaited_once()
