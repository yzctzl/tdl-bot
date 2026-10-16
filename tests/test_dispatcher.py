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
    tdl = SimpleNamespace(
        gate=UpdateGate(), version=AsyncMock(return_value="Version: 0.20.4")
    )

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
            if callback.startswith("menu:"):
                menu = store.menu(user)
                assert menu is not None
                message["message_id"] = menu["message"]
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
        "Send a Telegram message URL"
        if language == "en"
        else "发送 Telegram 消息链接"
    ) in help_message.text
    assert "/storage" not in help_message.text
    assert len(help_message.reply_markup.inline_keyboard) == 2
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
    assert (
        "Send a Telegram message URL"
        in j.transport.last("EditMessageText").text
    )
    assert j.transport.last("EditMessageText").reply_markup is not None


async def test_authorization_changes_take_effect_through_dispatcher(journey):
    j = journey
    await j.send("/start", user=3)
    assert "unauthorized" in j.transport.last().text
    await j.send("/whoami@test_bot", user=3)
    assert "Your user ID: 3" == j.transport.last().text
    count = len(j.transport.calls)
    await j.send("/start", chat_type="group")
    assert len(j.transport.calls) == count
    await j.send("/user", user=1)
    assert "/user add" in j.transport.last().text
    assert "/user follow" in j.transport.last().text
    await j.send("/user unknown", user=1)
    assert "Unknown user operation" in j.transport.last().text
    await j.send("/user add", user=1)
    assert (
        "Incorrect arguments for /user add (expected: 1)"
        in j.transport.last().text
    )
    await j.send("/user@test_bot add 3", user=1)
    assert j.service.policy.is_allowed(3)
    await j.send("/user ban 3 1d")
    assert "Only administrators" in j.transport.last().text
    await j.send("/user ban 3 1d", user=1)
    await j.send("/help", user=3)
    assert "unauthorized" in j.transport.last().text
    await j.send("/user unban 3", user=1)
    await j.send("/help", user=3)
    assert "Send a Telegram message URL" in j.transport.last().text
    await j.send("/status 1", user=3)
    assert "Only administrators" in j.transport.last().text
    await j.send("/user role 3 admin", user=1)
    assert j.service.policy.is_admin(3)
    await j.send("/user role 3 user", user=1)
    assert not j.service.policy.is_admin(3)
    await j.send("/user follow 3 7", user=1)
    assert j.service.store.follow_limit(3) == 7
    await j.send("/user list", user=1)
    assert "Follow slots: 0/7" in j.transport.last().text
    await j.send("/user hi 3", user=1)
    assert "History" in j.transport.last().text
    await j.send("", user=1, callback="history:1:3:management:1")
    assert "Set follow limit: 7" in j.transport.last("EditMessageText").text
    await j.send("/user remove 3", user=1)
    assert not j.service.policy.is_allowed(3)


async def test_url_progress_and_queued_cancellation_through_dispatcher(journey):
    j = journey
    await j.send("/target bot")
    await j.send("https://t.me/example/10")
    queued = j.transport.last()
    assert queued.text == "1/5 Task queued."
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
    assert [call.text[:3] for call in updates] == [
        "2/5",
        "3/5",
        "3/5",
        "4/5",
        "5/5",
    ]
    assert updates[0].text == "2/5 Locating media group."
    assert updates[-1].text == "5/5 Complete: media and captions sent (1)."
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
        await remote.join(2, "https://t.me/example", "shared")
        j.media.join.assert_awaited_once_with(JoinLink(username="example"))
        assert await remote.tdl.version() == "Version: 0.20.4"
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


async def test_member_buttons_page_live_data_and_recheck_admin_access(journey):
    j = journey
    for user in range(3, 23):
        j.store.add_member(1, user)
    await j.send("/user list", user=1)
    first = j.transport.last()
    assert "page 1/3" in first.text
    assert [b.callback_data for b in first.reply_markup.inline_keyboard[0]] == [
        "users:1:2"
    ]
    await j.send("", user=1, callback="users:1:2")
    second = j.transport.last("EditMessageText")
    assert "page 2/3" in second.text
    assert [
        b.callback_data for b in second.reply_markup.inline_keyboard[0]
    ] == ["users:1:1", "users:1:3"]
    await j.send("", user=1, callback="users:1:3")
    assert "page 3/3" in j.transport.last("EditMessageText").text
    for actor, payload, chat in [
        (2, "users:2:1", "private"),
        (1, "users:2:1", "private"),
        (1, "users:1:0", "private"),
        (1, "users:1:bad", "private"),
        (1, "users:1:1:extra", "private"),
        (1, "users:1:1", "group"),
    ]:
        edits = sum(
            type(c).__name__ == "EditMessageText" for c in j.transport.calls
        )
        await j.send("", user=actor, callback=payload, chat_type=chat)
        assert j.transport.last("AnswerCallbackQuery").show_alert
        assert (
            sum(
                type(c).__name__ == "EditMessageText" for c in j.transport.calls
            )
            == edits
        )
    for user in range(3, 23):
        j.store.remove_member(1, user)
    await j.send("", user=1, callback="users:1:3")
    refreshed = j.transport.last("EditMessageText")
    assert "page 1/1" in refreshed.text and refreshed.reply_markup is None
    j.store.set_member_role(1, 2, "admin")
    j.store.set_member_role(2, 1, "user")
    await j.send("", user=1, callback="users:1:1")
    assert j.transport.last("AnswerCallbackQuery").show_alert


async def menu_click(j, path, *, user=2):
    """Click the real rendered button; fail if the action is not offered."""
    current = j.transport.last("EditMessageText")
    payload = next(
        b.callback_data
        for row in current.reply_markup.inline_keyboard
        for b in row
        if b.callback_data.split(":", 2)[2] == path
    )
    assert len(payload.encode()) <= 64
    assert all(len(row) <= 2 for row in current.reply_markup.inline_keyboard)
    assert all(
        "full_width" not in b.model_dump(exclude_none=True)
        for row in current.reply_markup.inline_keyboard
        for b in row
    )
    await j.send("", user=user, callback=payload)
    return j.transport.last("EditMessageText")


async def menu_start(j, user=2):
    j.store.set_language(user, "en")
    await j.send("/start", user=user)


async def test_menu_navigation_inputs_permissions_and_restart(journey):
    j = journey
    await menu_start(j)
    root = j.transport.last("EditMessageText")
    assert [
        b.text for row in root.reply_markup.inline_keyboard for b in row
    ] == [
        "Task management",
        "Scheduled tracking",
        "Settings",
        "Help",
    ]
    assert root.text.startswith("Send a Telegram message URL")
    assert "Main menu" not in root.text
    assert [
        [b.text for b in row] for row in root.reply_markup.inline_keyboard
    ] == [
        ["Task management", "Scheduled tracking"],
        ["Settings", "Help"],
    ]
    stale = root.reply_markup.inline_keyboard[0][0].callback_data
    await menu_click(j, "settings")
    await j.send("", callback=stale)
    assert j.transport.last("AnswerCallbackQuery").show_alert
    await menu_click(j, "target")
    await menu_click(j, "targetinput")
    await j.send("bad 0")
    assert j.store.menu(2)["wait"] == "target"
    assert j.store.recipient(2).target == Target("2")
    # A new frontend uses the same draft and continues the input step.
    replacement = BotApplication(
        j.app.bot, SimpleNamespace(), j.store, j.app.tdl, j.service
    )
    restored = replacement.store.menu(2)
    assert restored is not None and restored["wait"] == "target"
    await replacement.menus.text(
        Message(
            message_id=300,
            date=0,
            chat=Chat(id=2, type="private"),
            from_user=User(id=2, is_bot=False, first_name="Tester"),
            text="bot",
        ).as_(j.app.bot)
    )
    assert "wait" not in j.store.menu(2)
    await menu_click(j, "target:2")
    await menu_click(j, "settings")
    await menu_click(j, "language")
    await menu_click(j, "lang:zh")
    assert j.store.language(2) == "zh"
    assert j.transport.last("EditMessageText").text.startswith("设置\n")
    assert "语言：简体中文" in j.transport.last("EditMessageText").text
    assert "接收位置：" in j.transport.last("EditMessageText").text
    await menu_click(j, "root")
    await menu_click(j, "tasks")
    assert "执行账号" in j.transport.last("EditMessageText").text
    await menu_click(j, "jobs")
    assert "暂无活动任务" in j.transport.last("EditMessageText").text
    await menu_click(j, "tasks")
    await menu_click(j, "pending")
    await menu_click(j, "tasks")
    await menu_click(j, "root")
    await menu_click(j, "help")
    assert "/retry" in j.transport.last("EditMessageText").text
    assert "/storage" not in j.transport.last("EditMessageText").text
    # Forging an administrator path still goes through live authorization.
    version = j.store.menu(2)["version"]
    await j.send("", callback=f"menu:{version}:admin")
    assert "管理员" in j.transport.last("EditMessageText").text
    j.store.ban_member(1, 2, 86400)
    await j.send("", callback=f"menu:{j.store.menu(2)['version']}:root")
    assert j.transport.last("AnswerCallbackQuery").show_alert


async def test_menu_transfer_retry_join_and_command_escape(journey):
    from tdl_bot.core.errors import JoinPending

    j = journey
    j.media.join = AsyncMock()
    await j.send("/target bot")
    await menu_start(j)
    await menu_click(j, "tasks")
    await menu_click(j, "join")
    await j.send("invalid")
    assert j.store.menu(2)["wait"] == "join_link"
    await j.send("https://t.me/example")
    confirmation = j.store.menu(2)["confirmation"]
    revision = j.store.menu(2)["version"]
    await menu_click(j, "confirm:" + confirmation["token"])
    await j.send(
        "", callback=f"menu:{revision}:confirm:{confirmation['token']}"
    )
    assert j.transport.last("AnswerCallbackQuery").show_alert
    assert "Joined successfully" in j.transport.last("EditMessageText").text
    j.media.join.assert_awaited_once()
    await menu_click(j, "tasks")
    await menu_click(j, "join")
    await j.send("/help")
    assert "wait" not in j.store.menu(2)
    await j.send("https://t.me/example/10")
    job = j.service.jobs(2)[0]
    await menu_start(j)
    await menu_click(j, "tasks")
    await menu_click(j, "jobs")
    await menu_click(j, "job:" + job.id)
    assert "Queued" in j.transport.last("EditMessageText").text
    await menu_click(j, "stopjob:" + job.id)
    assert not j.service.jobs(2)
    version = j.store.menu(2)["version"]
    await j.send("", callback=f"menu:{version}:job:{job.id}")
    assert "Task ended" in j.transport.last("EditMessageText").text
    await menu_click(j, "jobs")
    await menu_click(j, "tasks")
    j.store.set_pending(
        2, "https://t.me/example/20", destination=j.store.recipient(2)
    )
    item = j.service.pending_items(2)[0]
    await menu_click(j, "pending")
    await menu_click(j, f"retry:{item.id}")
    await menu_click(j, f"joinretry:{item.id}")
    j.media.join.side_effect = JoinPending("Waiting for approval")
    await j.send("https://t.me/+invite")
    await menu_click(j, "confirm:" + j.store.menu(2)["confirmation"]["token"])
    assert "Waiting for approval" in j.transport.last("EditMessageText").text
    assert not j.service.jobs(2)
    await menu_click(j, "pending")
    await menu_click(j, f"retry:{item.id}")
    await menu_click(j, f"joinretry:{item.id}")
    j.media.join.side_effect = None
    await j.send("https://t.me/+invite")
    await menu_click(j, "confirm:" + j.store.menu(2)["confirmation"]["token"])
    retry_job = j.service.jobs(2)[0]
    assert retry_job.retry_id == item.id
    await j.service.cancel(2, retry_job.id)
    await menu_start(j)
    await menu_click(j, "tasks")
    await menu_click(j, "pending")
    await menu_click(j, f"retry:{item.id}")
    await menu_click(j, f"delretry:{item.id}")
    await menu_click(j, "confirm:" + j.store.menu(2)["confirmation"]["token"])
    assert not j.service.pending_items(2)
    j.store.set_pending(
        2, "https://t.me/example/30", destination=j.store.recipient(2)
    )
    item = j.service.pending_items(2)[0]
    await menu_click(j, "pending")
    await menu_click(j, f"retry:{item.id}")
    await menu_click(j, f"runretry:{item.id}")
    assert j.service.jobs(2)[0].retry_id == item.id
    await j.send("/cancel " + j.service.jobs(2)[0].id)
    # Independent command joins work without any retry record.
    j.store.delete_pending(item.id)
    j.media.join.side_effect = None
    await j.send("/join https://t.me/example")
    assert "Joined successfully" in j.transport.last().text


async def test_menu_follow_and_dump_use_real_durable_backup_controls(
    journey, monkeypatch
):
    from datetime import UTC, datetime

    from telethon import types

    from tdl_bot.transfers.backups.service import BackupService

    j = journey
    j.store.set_target(1, Target("-1000000999999"))
    channel = types.Channel(
        id=123456,
        title="Source",
        photo=types.ChatPhotoEmpty(),
        date=datetime.now(UTC),
        megagroup=True,
    )
    j.media.client = SimpleNamespace(
        get_entity=AsyncMock(return_value=channel),
        get_messages=AsyncMock(return_value=[SimpleNamespace(id=10)]),
    )
    j.service.delivery = SimpleNamespace(
        validate=AsyncMock(side_effect=lambda target, owner: target)
    )
    manager = BackupService(j.service, notify=AsyncMock())
    j.service.backups = manager
    try:
        await menu_start(j)
        await menu_click(j, "follow")
        text = j.transport.last("EditMessageText").text
        assert "Running: 0 · Paused: 0" in text
        assert "No backup tasks" in text
        await menu_click(j, "follow:new")
        await j.send("@source")
        assert "Only new messages" in j.transport.last("EditMessageText").text
        from tdl_bot.integrations.telegram.delivery import Delivery

        monkeypatch.setattr(
            Delivery,
            "validate",
            AsyncMock(side_effect=lambda target, owner, **kwargs: target),
        )
        await menu_click(j, "follow:target")
        await menu_click(j, "follow:targetinput")
        await j.send("-1000000123456 17")
        assert j.store.menu(2)["draft"]["target"]["topic_id"] == 17
        await menu_click(j, "follow:target")
        await menu_click(j, "follow:defaulttarget")
        assert "target" not in j.store.menu(2)["draft"]
        await menu_click(j, "follow:interval:draft")
        await menu_click(j, "follow:custom")
        await j.send("10s")
        assert j.store.menu(2)["wait"] == "backup_interval"
        await j.send("10m")
        assert j.store.menu(2)["draft"]["interval"] == 600
        await menu_click(j, "follow:create")
        record = next(iter(manager.records.values()))
        assert record.cursor == 10 and record.interval == 600
        assert "Cursor" not in j.transport.last("EditMessageText").text
        assert "Default storage" not in j.transport.last("EditMessageText").text
        await menu_click(j, f"follow:pause:{record.id}")
        assert record.state == "paused"
        await menu_click(j, f"follow:resume:{record.id}")
        assert record.state == "active"
        await menu_click(j, f"follow:interval:{record.id}")
        await menu_click(j, "follow:every:5m")
        assert record.interval == 300
        await menu_click(j, f"follow:pause:{record.id}")
        record.phase = "uncertain"
        record.inflight = 15
        record.error = "Send not confirmed"
        manager.store.backups.save(record)
        await menu_click(j, f"follow:detail:{record.id}")
        assert (
            "last send is unconfirmed"
            in j.transport.last("EditMessageText").text
        )
        await menu_click(j, f"follow:resolve:{record.id}:skip")
        await menu_click(
            j, "confirm:" + j.store.menu(2)["confirmation"]["token"]
        )
        assert record.cursor == 15 and record.state == "active"
        await menu_click(j, f"follow:pause:{record.id}")
        record.phase, record.inflight = "uncertain", 20
        manager.store.backups.save(record)
        await menu_click(j, f"follow:detail:{record.id}")
        await menu_click(j, f"follow:resolve:{record.id}:retry")
        await menu_click(
            j, "confirm:" + j.store.menu(2)["confirmation"]["token"]
        )
        assert record.cursor == 15 and record.state == "active"
        await menu_click(j, f"follow:cancel:{record.id}")
        await menu_click(
            j, "confirm:" + j.store.menu(2)["confirmation"]["token"]
        )
        assert record.state == "cancelled"
        await menu_click(j, "follow:list:1")
        j.store.set_follow_limit(1, 2, 0)
        await menu_click(j, "follow:new")
        assert (
            "Follow limit reached" in j.transport.last("EditMessageText").text
        )
        await menu_start(j, user=1)
        await menu_click(j, "admin", user=1)
        await menu_click(j, "dump", user=1)
        await menu_click(j, "dump:new", user=1)
        await j.send("@source", user=1)
        await menu_click(j, "dump:create", user=1)
        dump = next(r for r in manager.records.values() if r.kind == "dump")
        assert dump.cursor == 0 and dump.upper == 10
        await menu_click(j, f"dump:cancel:{dump.id}", user=1)
        await menu_click(
            j, "confirm:" + j.store.menu(1)["confirmation"]["token"], user=1
        )
        assert dump.state == "cancelled"
    finally:
        await manager.close()


async def test_menu_member_management_and_system_operations(
    journey, monkeypatch
):
    from tdl_bot.integrations.telegram.delivery import Delivery

    j = journey
    j.media.validate_target = AsyncMock()
    monkeypatch.setattr(
        Delivery,
        "validate",
        AsyncMock(side_effect=lambda target, owner, **kwargs: target),
    )
    j.app.tdl.update = AsyncMock(return_value="No update needed")
    await menu_start(j, user=1)
    root = j.transport.last("EditMessageText")
    assert [
        [b.text for b in row] for row in root.reply_markup.inline_keyboard
    ] == [
        ["Task management", "Scheduled tracking"],
        ["User management", "Bot management"],
        ["Settings", "Help"],
    ]
    await menu_click(j, "users:1", user=1)
    rows = j.transport.last("EditMessageText").reply_markup.inline_keyboard
    assert all(len(row) == 1 for row in rows[:2])
    await menu_click(j, "useradd", user=1)
    await j.send("3", user=1)
    await menu_click(
        j, "confirm:" + j.store.menu(1)["confirmation"]["token"], user=1
    )
    assert j.service.policy.is_allowed(3)
    await menu_click(j, "quota:3", user=1)
    await j.send("1001", user=1)
    assert j.store.menu(1)["wait"] == "user_quota"
    await j.send("7", user=1)
    await menu_click(
        j, "confirm:" + j.store.menu(1)["confirmation"]["token"], user=1
    )
    assert j.store.follow_limit(3) == 7
    await menu_click(j, "ban:3", user=1)
    await menu_click(j, "userop:ban:3:1d", user=1)
    await menu_click(
        j, "confirm:" + j.store.menu(1)["confirmation"]["token"], user=1
    )
    assert not j.service.policy.is_allowed(3)
    await menu_click(j, "userop:unban:3", user=1)
    await menu_click(
        j, "confirm:" + j.store.menu(1)["confirmation"]["token"], user=1
    )
    assert j.service.policy.is_allowed(3)
    await menu_click(j, "role:3", user=1)
    await menu_click(j, "userop:role:3:admin", user=1)
    await menu_click(
        j, "confirm:" + j.store.menu(1)["confirmation"]["token"], user=1
    )
    assert j.service.policy.is_admin(3)
    await menu_click(j, "history:3:activity:1", user=1)
    await menu_click(j, "history:3:management:1", user=1)
    assert "Set administrator" in j.transport.last("EditMessageText").text
    await menu_click(j, "member:3", user=1)
    await menu_click(j, "target:3", user=1)
    await menu_click(j, "targetinput", user=1)
    await j.send("-1000000123456 17", user=1)
    assert j.store.recipient(3).target.topic_id == 17
    await menu_click(j, "target:3", user=1)
    await menu_click(j, "targetbot", user=1)
    assert j.store.recipient(3).target == Target("3")
    await menu_click(j, "target:3", user=1)
    await menu_click(j, "member:3", user=1)
    await menu_click(j, "userop:remove:3", user=1)
    await menu_click(
        j, "confirm:" + j.store.menu(1)["confirmation"]["token"], user=1
    )
    assert not j.service.policy.is_allowed(3)
    await menu_start(j, user=1)
    await menu_click(j, "admin", user=1)
    summary = j.transport.last("EditMessageText").text
    assert "Not configured" in summary
    assert "Automatic" in summary
    assert "tdl: v0.20.4" in summary
    assert "tdl update" not in summary
    assert "Refresh" not in [
        b.text
        for row in j.transport.last(
            "EditMessageText"
        ).reply_markup.inline_keyboard
        for b in row
    ]
    await menu_click(j, "storage", user=1)
    assert j.store.menu(1)["wait"] == "storage"
    await j.send("-1000000999999", user=1)
    assert j.store.internal_target() == Target("-1000000999999")
    assert "wait" not in j.store.menu(1)
    assert (
        'href="https://t.me/c/999999"'
        in j.transport.last("EditMessageText").text
    )
    await menu_click(j, "execution", user=1)
    await menu_click(j, "selectexec:shared", user=1)
    assert "Selection: Manual" in j.transport.last("EditMessageText").text
    assert j.store.executor_choice(1) == "shared"
    await menu_click(j, "selectexec:auto", user=1)
    assert j.store.executor_choice(1) is None
    await menu_click(j, "admin", user=1)
    await menu_click(j, "update", user=1)
    await menu_click(j, "version", user=1)
    await j.send("v0.20.4", user=1)
    await menu_click(
        j, "confirm:" + j.store.menu(1)["confirmation"]["token"], user=1
    )
    await j.app.close()
    j.app.tdl.update.assert_awaited_once_with("v0.20.4")
    await menu_click(j, "admin", user=1)
    await menu_click(j, "update", user=1)
    await menu_click(j, "updatelatest", user=1)
    await j.app.close()
    assert j.app.tdl.update.call_args.args == ("latest",)


@pytest.mark.parametrize("page", ["member", "target"])
async def test_menu_rechecks_admin_after_resolving_private_destination(
    journey, monkeypatch, page
):
    j = journey
    j.store.add_member(1, 3)
    j.store.set_member_role(1, 3, "admin")
    await menu_start(j, user=1)
    await menu_click(j, "users:1", user=1)
    if page == "target":
        await menu_click(j, "member:2", user=1)

    async def resolve(target, owner):
        j.store.set_member_role(3, 1, "user")
        return "Private destination"

    monkeypatch.setattr(j.app.destinations, "target_link", resolve)
    await menu_click(j, f"{page}:2", user=1)
    text = j.transport.last("EditMessageText").text
    assert "Private destination" not in text
    assert "Only administrators" in text or "You cannot change" in text
