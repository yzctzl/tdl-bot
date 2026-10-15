"""Offline user journeys through real routing, middleware, and persistence."""

import asyncio
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, ChatFullInfo, ChatMemberMember, Message, User

from tdl_bot.bot.application import BotApplication
from tdl_bot.bot.progress import ProgressMessages
from tdl_bot.core.links import JoinLink
from tdl_bot.integrations.tdl.gate import UpdateGate
from tdl_bot.runtime.engine import EngineServer, RemoteService
from tdl_bot.storage.models import Target
from tdl_bot.storage.store import Store
from tdl_bot.transfers.accounts import AccessPolicy, AccountPool, Executor
from tdl_bot.transfers.service import TransferService


class TelegramTransport(BaseSession):
    """Replace Telegram transport and reject unexpected API requests."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.ids = itertools.count(100)
        self.identity = User(
            id=99, is_bot=True, first_name="Bot", username="test_bot"
        )

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("Unexpected file download")
        yield b""  # pragma: no cover - required async generator interface

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return self.identity
        if name in {"SetMyCommands", "AnswerCallbackQuery"}:
            return True
        if name == "GetChat":
            return ChatFullInfo.model_construct(
                id=method.chat_id, type="private", title=None, username=None
            )
        if name == "GetChatMember":
            return ChatMemberMember(user=self.identity)
        if name in {"SendMessage", "EditMessageText"}:
            return Message(
                message_id=getattr(method, "message_id", None)
                or next(self.ids),
                date=0,
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=self.identity,
                text=method.text,
                reply_markup=method.reply_markup,
            )
        raise AssertionError(f"Unexpected Telegram API method: {name}")

    def last(self, name="SendMessage"):
        return next(
            call for call in reversed(self.calls) if type(call).__name__ == name
        )


@pytest.fixture
async def journey(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    policy = AccessPolicy(frozenset({1, 2}), frozenset({1}), store=store)
    transport = TelegramTransport()
    bot = Bot("99:TEST", session=transport)
    tdl = SimpleNamespace(gate=UpdateGate())

    async def download(urls, directory):
        (directory / "10_photo.jpg").write_bytes(b"photo")

    tdl.download = AsyncMock(side_effect=download)
    media = SimpleNamespace(
        resolve=AsyncMock(
            return_value=SimpleNamespace(
                messages=[SimpleNamespace(id=10, document=None)],
                urls=["https://t.me/example/10"],
            )
        ),
        upload=AsyncMock(return_value=[1000]),
    )
    service = TransferService(
        AccountPool(Executor("shared", tdl, media)),
        store,
        tmp_path / "jobs",
        ProgressMessages(bot),
        policy=policy,
        workers=1,
    )
    service.jobs_dir.mkdir()
    app = BotApplication(bot, SimpleNamespace(), store, tdl, service)
    ids = itertools.count(1)

    async def send(text, *, user=2, callback=None, chat_type="private"):
        message = {
            "message_id": next(ids),
            "date": 0,
            "chat": {"id": user, "type": chat_type},
            "from": {"id": user, "is_bot": False, "first_name": "Tester"},
            "text": text,
        }
        update = {"update_id": next(ids), "message": message}
        if callback is not None:
            update = {
                "update_id": next(ids),
                "callback_query": {
                    "id": str(next(ids)),
                    "chat_instance": "test",
                    "from": message["from"],
                    "data": callback,
                    "message": {
                        **message,
                        "from": transport.identity.model_dump(),
                    },
                },
            }
        await app.dispatcher.feed_raw_update(bot, update)

    try:
        yield SimpleNamespace(
            send=send,
            app=app,
            store=store,
            service=service,
            transport=transport,
            media=media,
        )
    finally:
        await service.close()
        await bot.session.close()
        store.close()


@pytest.mark.parametrize("language", ["en", "zh"])
async def test_onboarding_language_target_and_restart(journey, language):
    j = journey
    await j.send("/start")
    welcome = j.transport.last()
    assert "Please choose your language" in welcome.text
    assert "请先选择语言" in welcome.text
    assert {
        b.callback_data
        for row in welcome.reply_markup.inline_keyboard
        for b in row
    } == {"language:en", "language:zh"}
    assert not j.store.has_language(2)
    await j.send("", callback=f"language:{language}")
    help_message = j.transport.last("EditMessageText")
    assert (
        "/target" in help_message.text and "/storage" not in help_message.text
    )
    assert help_message.reply_markup is None
    assert j.store.language(2) == language
    await j.send("/language")
    assert (
        "Language:" if language == "en" else "当前语言："
    ) in j.transport.last().text
    await j.send("/language invalid")
    assert (
        "Unsupported language" if language == "en" else "不支持该语言"
    ) in j.transport.last().text
    await j.send("/target bot")
    await j.send("/target")
    assert 'href="https://t.me/test_bot"' in j.transport.last().text
    await j.send("/status")
    assert (
        "Execution account:" if language == "en" else "执行账号："
    ) in j.transport.last().text
    await j.send("/language en")
    assert "Language: English" in j.transport.last().text
    assert any(
        item.command == "language"
        for item in j.transport.last("SetMyCommands").commands
    )
    reopened = Store(j.service.jobs_dir.parent / "state.sqlite3", migrate=False)
    try:
        assert reopened.has_language(2) and reopened.language(2) == "en"
    finally:
        reopened.close()
    await j.send("/start")
    assert "Send a Telegram message URL" in j.transport.last().text
    assert j.transport.last().reply_markup is None


async def test_authorization_changes_take_effect_through_dispatcher(journey):
    j = journey
    await j.send("/start", user=3)
    assert "unauthorized" in j.transport.last().text
    await j.send("/whoami@test_bot", user=3)
    assert "Your user ID: 3" == j.transport.last().text
    count = len(j.transport.calls)
    await j.send("/start", chat_type="group")
    assert len(j.transport.calls) == count
    await j.send("/user_add 3", user=1)
    assert j.service.policy.is_allowed(3)
    await j.send("/user_ban 3 1d")
    assert "Only administrators" in j.transport.last().text
    await j.send("/user_ban 3 1d", user=1)
    await j.send("/help", user=3)
    assert "unauthorized" in j.transport.last().text
    await j.send("/user_unban 3", user=1)
    await j.send("/help", user=3)
    assert "Send a Telegram message URL" in j.transport.last().text
    await j.send("/status 1", user=3)
    assert "Only administrators" in j.transport.last().text
    await j.send("/user_remove 3", user=1)
    assert not j.service.policy.is_allowed(3)


async def test_url_progress_and_queued_cancellation_through_dispatcher(journey):
    j = journey
    await j.send("/target bot")
    await j.send("https://t.me/example/10")
    queued = j.transport.last()
    assert queued.text == "1/5 Download task queued."
    assert queued.reply_parameters.message_id is not None
    job = j.service.jobs(2)[0]
    await j.send("/jobs")
    assert job.id[:8] in j.transport.last().text
    await j.send("/cancel " + job.id)
    assert job.done.is_set() and job.cancel_requested
    assert not list(j.service.jobs_dir.iterdir())
    j.media.resolve.assert_not_awaited()
    await j.send("https://t.me/example/20")
    next_job = j.service.jobs(2)[0]
    j.service.start()
    await j.service.close()
    updates = [
        call
        for call in j.transport.calls
        if type(call).__name__ == "EditMessageText"
        and call.message_id == next_job.progress_message_id
    ]
    assert len(updates) == 4
    assert updates[0].text == "2/5 Locating media group."
    assert (
        updates[-1].text
        == "5/5 Complete: uploaded 1 media files with captions."
    )
    assert updates[-1].reply_markup is None
    assert j.store.saved_results([next_job.url], "shared")
    assert not list(j.service.jobs_dir.iterdir())


async def test_remote_engine_bootstrap_retry_and_shutdown_lifecycle(
    journey, monkeypatch
):
    """Exercise Unix RPC, durable jobs, retries, and account operations."""
    j = journey
    await j.send("/target bot")
    j.service.durable = True
    settings = SimpleNamespace(
        data_dir=j.service.jobs_dir.parent,
        allowed_users=frozenset({1, 2}),
        admin_users=frozenset({1}),
    )
    j.media.resolve_user = AsyncMock(
        return_value=SimpleNamespace(
            id=2,
            username="tester",
            first_name="Test",
            last_name=None,
        )
    )
    j.media.validate_target = AsyncMock()
    j.media.join = AsyncMock()
    j.service.accounts.shared.tdl.update = AsyncMock(
        return_value="Version: 1.2.3"
    )
    engine = EngineServer(j.service, settings)
    engines = []

    def spawn(command, **kwargs):
        assert command == "engine"
        engines.append(asyncio.create_task(engine.serve()))
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr("tdl_bot.runtime.engine.spawn_process", spawn)
    remote = RemoteService(settings, j.store)
    try:
        media = remote.accounts.shared.media
        assert (await media.resolve_user("@tester")).id == 2
        assert len(engines) == 1
        assert (
            settings.data_dir / "engine.log"
        ).stat().st_mode & 0o777 == 0o600
        await media.validate_target(Target("2"))
        await media.join(JoinLink(username="example"))
        assert await remote.tdl.update() == "Version: 1.2.3"
        j.store.set_pending(
            2, "https://t.me/example/10", destination=j.store.recipient(2)
        )
        pending = remote.pending_record(2)
        job = await remote.retry(2, pending.id, source_message_id=70)
        assert remote.current_job(2, job.id).id == job.id
        assert await remote.cancel(2, job.id)
        await remote.discard_pending(2, pending.id)
        assert not remote.pending_items(2)
        submitted = await remote.submit(2, "https://t.me/example/20")
        await remote.cancel_submitted(1, 2)
        assert remote.current_job(2, submitted.id) is None
        await remote.reconcile_permissions()
        await remote.call("shutdown")
        await asyncio.wait_for(engines[0], 3)
        assert not (settings.data_dir / "runtime.sock").exists()
        assert len(engines) == 1
    finally:
        for task in engines:
            task.cancel()
        await asyncio.gather(*engines, return_exceptions=True)
