"""Backup ordering, durable checkpoints, permissions and uncertain writes."""

import asyncio
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject
from telethon import types, utils
from telethon.tl.custom.message import Message

from tdl_bot.bot.application import BotApplication
from tdl_bot.bot.backups import arguments
from tdl_bot.core.errors import UserError
from tdl_bot.integrations.tdl.gate import UpdateGate
from tdl_bot.integrations.telegram.history import read_page, source
from tdl_bot.integrations.telegram.media import TelegramMedia
from tdl_bot.runtime.engine import EngineServer, RemoteService, exchange
from tdl_bot.storage.models import Target
from tdl_bot.storage.store import Store
from tdl_bot.transfers.accounts import AccessPolicy, AccountPool, Executor
from tdl_bot.transfers.backups.service import BackupService
from tdl_bot.transfers.service import TransferService

STAMP = datetime(2020, 1, 1, tzinfo=UTC)


def message(number, *, album=None, photo=False, action=None, recent=False):
    media = (
        types.MessageMediaPhoto(
            photo=types.Photo(number, 1, b"ref", STAMP, [], 2)
        )
        if photo
        else None
    )
    return Message(
        id=number,
        peer_id=types.PeerChannel(123456),
        date=datetime.now(UTC) if recent else STAMP,
        message=f"Message {number}",
        media=media,
        grouped_id=album,
        action=action,
    )


class Delivery:
    def __init__(self):
        self.prepare = AsyncMock()
        self.validate = AsyncMock(side_effect=lambda target, owner: target)
        self.sent = AsyncMock(return_value=[900])

    async def __call__(self, job, ids):
        return await self.sent(job, ids)


@pytest.fixture
async def workspace(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    policy = AccessPolicy(frozenset({1, 2, 3}), frozenset({1}), store=store)
    storage = Target("-1000000999999")
    store.set_target(1, storage)
    channels = {
        f"@source{i}": types.Channel(
            id=123456 + i,
            title=f"Source {i}",
            photo=types.ChatPhotoEmpty(),
            date=STAMP,
            megagroup=True,
        )
        for i in range(12)
    }
    data = {utils.get_peer_id(ch): [] for ch in channels.values()}
    first = utils.get_peer_id(channels["@source0"])
    data[first] = [
        message(1, photo=True),
        message(2, album=7, photo=True),
        message(3, album=7, photo=True),
        message(4, action=types.MessageActionPinMessage()),
        message(5),
    ]

    async def entity(value):
        if value in channels:
            return channels[value]
        if isinstance(value, str) and "@" + value in channels:
            return channels["@" + value]
        return next(
            ch for ch in channels.values() if utils.get_peer_id(ch) == value
        )

    async def latest(peer, *, limit):
        chat = (
            utils.get_peer_id(peer)
            if isinstance(peer, (types.Channel, types.Chat))
            else peer
        )
        return data[chat][-limit:][::-1]

    async def history(peer, *, min_id, max_id, reverse, limit):
        assert reverse and limit is None
        for item in data[peer]:
            if min_id < item.id < max_id:
                yield item

    async def copy(group, target):
        return [1000 + m.id for m in group.messages]

    def executor(key, owner=None):
        client = SimpleNamespace(
            get_entity=AsyncMock(side_effect=entity),
            get_messages=AsyncMock(side_effect=latest),
            get_input_entity=AsyncMock(side_effect=lambda value: value),
            iter_messages=history,
            send_message=AsyncMock(return_value=SimpleNamespace(id=888)),
        )
        media = TelegramMedia(client)
        monkeypatch.setattr(media, "copy_group", AsyncMock(side_effect=copy))
        monkeypatch.setattr(
            media, "reveal_messages", AsyncMock(return_value=True)
        )
        return Executor(key, SimpleNamespace(gate=UpdateGate()), media, owner)

    shared, own = executor("shared"), executor("operator:2", 2)
    delivery = Delivery()
    transfers = TransferService(
        AccountPool(shared, (own,)),
        store,
        tmp_path / "jobs",
        AsyncMock(),
        policy=policy,
        delivery=delivery,
    )
    transfers.jobs_dir.mkdir()
    manager = BackupService(transfers, notify=AsyncMock(), workers=1)
    monkeypatch.setattr(manager.pacer, "wait", AsyncMock(return_value=True))
    monkeypatch.setattr(manager.pacer, "remaining", lambda key: 0)
    transfers.backups = manager
    yield SimpleNamespace(
        manager=manager,
        transfers=transfers,
        store=store,
        policy=policy,
        shared=shared,
        own=own,
        data=data,
        first=first,
        channels=channels,
        delivery=delivery,
    )
    await manager.close()
    await transfers.close()
    store.close()


async def test_dump_freezes_history_and_follow_starts_at_latest(workspace):
    w = workspace
    dump = await w.manager.execute(
        1,
        "dump",
        "create",
        source_ref="@source0",
        target={"chat": "@destination", "topic_id": 77},
        source_message_id=91,
    )
    follow = await w.manager.execute(
        2, "follow", "create", source_ref="@source0"
    )
    assert dump["cursor"] == 0 and follow["cursor"] == 5
    assert "storage" not in follow and follow["executor"] == "operator:2"
    w.data[w.first].extend([message(6, photo=True), message(7)])
    await w.manager._cycle(w.manager.records[dump["id"]])
    assert w.manager.records[dump["id"]].state == "done"
    assert [
        [m.id for m in c.args[0].messages]
        for c in w.shared.media.copy_group.call_args_list
    ] == [[1], [2, 3], [5]]
    assert w.delivery.sent.await_count == 3
    duplicate = await w.manager.execute(
        1, "dump", "create", source_ref="@source0", source_message_id=91
    )
    assert duplicate["id"] == dump["id"] and duplicate["state"] == "done"
    assert len(w.manager.records) == 2
    assert all(
        c.args[0].target == Target("@destination", 77)
        for c in w.delivery.sent.call_args_list
    )
    await w.manager._cycle(w.manager.records[follow["id"]])
    assert [
        [m.id for m in c.args[0].messages]
        for c in w.own.media.copy_group.call_args_list
    ] == [[6], [7]]
    assert (
        w.delivery.sent.await_count == 3
    )  # Default storage has no second copy.
    stored = w.manager.records[follow["id"]]
    assert (
        stored.cursor == 7 and stored.messages == 2 and stored.state == "active"
    )
    await w.manager._cycle(stored)
    assert w.own.media.copy_group.await_count == 2
    stored.error, stored.attempts = "Temporary network error", 3
    await w.manager._cycle(stored)
    assert stored.error == "" and stored.attempts == 0
    reopened = Store(
        w.transfers.jobs_dir.parent / "state.sqlite3", migrate=False
    )
    try:
        assert (
            next(r for r in reopened.backups.all() if r.id == stored.id).cursor
            == 7
        )
    finally:
        reopened.close()


async def test_history_page_keeps_album_tail_and_ignores_service_actions(
    workspace,
):
    w = workspace
    w.channels["@source0"].username = "source0"
    page = await read_page(w.shared.media, w.first, 0, 5, limit=2)
    assert [[m.id for m in unit.messages] for unit in page.units] == [
        [1],
        [2, 3],
    ]
    assert page.through == 3 and not page.complete
    assert page.units[0].native_urls == ["https://t.me/source0/1"]
    assert page.units[0].urls == ["https://t.me/c/123456/1"]
    tail = await read_page(w.shared.media, w.first, 3, 5)
    assert [[m.id for m in unit.messages] for unit in tail.units] == [[5]]
    w.data[w.first].extend(
        [
            message(6, album=9, photo=True),
            message(7, album=9, photo=True, recent=True),
        ]
    )
    settled = await read_page(w.shared.media, w.first, 5, 7, settle=True)
    assert not settled.units and settled.through == 5 and not settled.complete
    w.data[w.first][-1].date = STAMP
    ready = await read_page(w.shared.media, w.first, 5, 7, settle=True)
    assert [m.id for m in ready.units[0].messages] == [6, 7]


async def test_backup_permissions_quota_cycles_and_account_snapshot(workspace):
    w = workspace
    with pytest.raises(UserError):
        await w.manager.execute(2, "dump", "create", source_ref="@source0")
    assert w.store.follow_limit(2) == 9
    w.store.set_follow_limit(1, 2, 3)
    ids = []
    for index in range(3):
        ids.append(
            (
                await w.manager.execute(
                    2, "follow", "create", source_ref=f"@source{index}"
                )
            )["id"]
        )
    with pytest.raises(UserError, match=r"limit reached \(3"):
        await w.manager.execute(2, "follow", "create", source_ref="@source3")
    assert (await w.manager.execute(3, "follow"))["items"] == []
    assert len((await w.manager.execute(1, "follow"))["items"]) == 3
    with pytest.raises(UserError):
        await w.manager.execute(3, "follow", "pause", task_id=ids[0])
    await w.manager.execute(2, "follow", "pause", task_id=ids[0])
    with pytest.raises(UserError, match=r"limit reached \(3"):
        await w.manager.execute(2, "follow", "create", source_ref="@source3")
    w.store.set_follow_limit(1, 2, 2)
    with pytest.raises(UserError, match=r"limit reached \(2"):
        await w.manager.execute(2, "follow", "resume", task_id=ids[0])
    await w.manager.execute(1, "follow", "cancel", task_id=ids[1])
    w.store.set_follow_limit(1, 2, 3)
    await w.manager.execute(2, "follow", "create", source_ref="@source3")
    await w.manager.execute(2, "follow", "resume", task_id=ids[0])
    await w.manager.execute(
        2, "follow", "interval", task_id=ids[0], interval=60
    )
    assert w.manager.records[ids[0]].interval == 60
    await w.transfers.execution_accounts(1, "2")
    admin = await w.manager.execute(
        1, "follow", "create", source_ref="@source4"
    )
    await w.transfers.execution_accounts(1, "shared")
    assert admin["executor"] == "operator:2"
    # A reverse edge would cause two follow tasks to feed one another forever.
    a = await w.manager.execute(
        1,
        "follow",
        "create",
        source_ref="@source0",
        target={"chat": str(utils.get_peer_id(w.channels["@source1"]))},
    )
    with pytest.raises(UserError, match="loop"):
        await w.manager.execute(
            1,
            "follow",
            "create",
            source_ref="@source1",
            target={"chat": str(w.first)},
        )
    assert a["state"] == "active"


@pytest.mark.parametrize("failure", ["network", "write", "rate", "denied"])
async def test_failures_never_advance_cursor_and_uncertain_resume_is_explicit(
    workspace, monkeypatch, failure
):
    from telethon.errors import FloodWaitError

    w = workspace
    created = await w.manager.execute(
        1, "dump", "create", source_ref="@source0"
    )
    record = w.manager.records[created["id"]]
    if failure == "network":
        w.shared.media.client.get_entity.side_effect = OSError("offline")
    elif failure == "denied":
        w.shared.media.client.get_entity.side_effect = UserError("No access")
    elif failure == "rate":
        w.shared.media.copy_group.side_effect = FloodWaitError(None, capture=12)
    else:
        w.shared.media.copy_group.side_effect = OSError("acknowledgement lost")
    await w.manager._cycle(record)
    assert record.cursor == 0
    if failure == "rate":
        assert (
            w.manager.pacer.deadlines[("account", "shared")]
            > w.manager.pacer.clock()
        )
    if failure in {"network", "rate"}:
        assert record.state == "active" and record.next_run > w.store.now()
    else:
        assert record.state == "paused"
        if failure == "write":
            assert record.phase == "uncertain"
            with pytest.raises(UserError, match="Check the destination"):
                await w.manager.execute(1, "dump", "resume", task_id=record.id)
            await w.manager.execute(
                1, "dump", "resume", task_id=record.id, resolution="skip"
            )
            assert record.cursor == 1
            await w.manager.execute(1, "dump", "pause", task_id=record.id)
            record.phase = "uncertain"
            await w.manager.execute(
                1, "dump", "resume", task_id=record.id, resolution="retry"
            )
            assert record.cursor == 1


async def test_scheduler_cancellation_recovery_and_graceful_reload(workspace):
    if sys.platform == "win32":
        pytest.skip("Unix engine handoff requires local sockets")
    w = workspace
    created = await w.manager.execute(
        1, "dump", "create", source_ref="@source0"
    )
    record = w.manager.records[created["id"]]
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(group, target):
        entered.set()
        await release.wait()
        return [1000 + m.id for m in group.messages]

    w.shared.media.copy_group.side_effect = blocked
    w.manager.start()
    await asyncio.wait_for(entered.wait(), 3)
    settings = SimpleNamespace(data_dir=w.transfers.jobs_dir.parent)
    server = EngineServer(w.transfers, settings)
    serving = asyncio.create_task(server.serve())
    socket = settings.data_dir / "runtime.sock"
    try:
        async with asyncio.timeout(3):
            while not socket.exists():  # noqa: ASYNC110 - local RPC startup
                await asyncio.sleep(0.01)
        await exchange(socket, {"method": "ping", "args": {"reload": True}})
        async with asyncio.timeout(3):
            while not w.manager.draining:  # noqa: ASYNC110 - engine handoff
                await asyncio.sleep(0.01)
        assert w.manager.busy and not serving.done()
        release.set()
        await asyncio.wait_for(serving, 3)
    finally:
        # Release the fake writer even when server startup or assertions fail.
        release.set()
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
    assert record.cursor == 1 and record.state == "active"
    await w.manager.close()
    record.phase, record.inflight = "writing", 3
    w.store.backups.save(record)
    restarted = BackupService(w.transfers)
    restarted.start()
    try:
        recovered = restarted.records[record.id]
        assert recovered.state == "paused" and recovered.phase == "uncertain"
        await restarted.execute(
            1, "dump", "resume", task_id=record.id, resolution="skip"
        )
        entered.clear()
        release.clear()
        await asyncio.wait_for(entered.wait(), 3)
        await restarted.execute(1, "dump", "cancel", task_id=record.id)
        assert recovered.state == "cancelled" and recovered.phase == "uncertain"
    finally:
        release.set()
        await restarted.close()


async def test_protected_text_uses_storage_pipeline_without_spoilers(workspace):
    w = workspace
    created = await w.manager.execute(
        1, "dump", "create", source_ref="@source0"
    )
    record = w.manager.records[created["id"]]
    record.cursor = 4
    w.data[w.first][-1].entities = [
        types.MessageEntityBold(0, 7),
        types.MessageEntitySpoiler(0, 7),
    ]
    w.shared.media.copy_group.return_value = None
    w.shared.media.copy_group.side_effect = None
    await w.manager._cycle(record)
    call = w.shared.media.client.send_message.call_args
    assert call.args[1] == "Message 5"
    assert [type(e) for e in call.kwargs["formatting_entities"]] == [
        types.MessageEntityBold
    ]
    assert record.state == "done" and record.cursor == 5
    w.transfers.notify.assert_not_awaited()


async def test_commands_and_rpc_hide_storage_and_register_role_menus(
    workspace, monkeypatch
):
    w = workspace
    settings = SimpleNamespace(
        allowed_users=frozenset({1, 2, 3}), admin_users=frozenset({1})
    )
    server = EngineServer(w.transfers, settings)
    remote = RemoteService(settings, w.store)

    async def rpc(method, **args):
        return await server.dispatch(method, args)

    monkeypatch.setattr(remote, "call", rpc)
    app = BotApplication(
        SimpleNamespace(), settings, w.store, w.shared.tdl, remote
    )
    event = SimpleNamespace(from_user=SimpleNamespace(id=2), answer=AsyncMock())
    await app.backups.follow(
        event, CommandObject(command="follow", args="@source0 every 60s")
    )
    assert "Default storage" in event.answer.call_args.args[0]
    assert w.store.internal_target().chat not in event.answer.call_args.args[0]
    task_id = next(iter(w.manager.records))
    await app.backups.follow(
        event, CommandObject(command="follow", args=f"pause {task_id}")
    )
    assert w.manager.records[task_id].state == "paused"
    await app.backups.follow(event, CommandObject(command="follow"))
    assert task_id in event.answer.call_args.args[0]
    with pytest.raises(UserError):
        await app.backups.dump(event, CommandObject(command="dump"))
    ordinary = {item.command for item in app.command_menu()}
    assert "follow" in ordinary and "dump" not in ordinary
    assert "user" not in ordinary
    assert "user" in {item.command for item in app.command_menu(admin=True)}
    resolve_user = AsyncMock(return_value=2)
    monkeypatch.setattr(app.members, "resolve_user", resolve_user)
    monkeypatch.setattr(app.members, "refresh_profile", AsyncMock())
    admin = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())
    command = CommandObject(command="user_follow_limit", args="@operator 5")
    with pytest.raises(UserError):
        await app.members.user_follow(event, command)
    await app.members.user_follow(admin, command)
    resolve_user.assert_awaited_with("@operator")
    assert w.store.follow_limit(2) == 5
    with pytest.raises(UserError):
        await app.members.user_follow(
            admin,
            CommandObject(
                command="user_follow_limit", args="@operator invalid"
            ),
        )
    await app.members.users(admin, CommandObject(command="users"))
    assert "Follow slots: 1/5" in admin.answer.call_args.args[0]
    await app.members.user_history(
        admin, CommandObject(command="user_history", args="@operator")
    )
    tabs = admin.answer.call_args.kwargs["reply_markup"].inline_keyboard[0]
    assert tabs[1].callback_data == "history:1:2:management:1"
    text, _ = app.members._history_page(1, 2, 1, "management")
    assert "Set follow limit: 5" in text
    await app.backups.follow(event, CommandObject(command="follow"))
    assert "Follow slots: 1/5" in event.answer.call_args.args[0]
    assert "dump" in {item.command for item in app.command_menu(admin=True)}
    assert arguments("follow", "@source0 to @destination 77 every 1h") == {
        "action": "create",
        "source_ref": "@source0",
        "target": {"chat": "@destination", "topic_id": 77},
        "interval": 3600,
    }
    for command in (
        "@source0 every 5s",
        "resume",
        "resume id invalid",
        "@source0 to",
        "interval id",
    ):
        with pytest.raises(UserError):
            arguments("follow", command)


async def test_source_references_and_basic_group_fallback(
    workspace, monkeypatch
):
    w = workspace
    for ref in (
        "@source0",
        "https://t.me/source0",
        "https://t.me/source0/5",
        str(w.first),
        "123456",
        "https://t.me/c/123456",
    ):
        assert (await source(w.shared.media, ref))[0] == w.first
    basic = types.Chat(
        id=23456,
        title="Basic group",
        photo=types.ChatPhotoEmpty(),
        participants_count=3,
        date=STAMP,
        version=1,
    )
    w.channels["@basic"] = basic
    w.data[-23456] = [message(1, photo=True)]
    created = await w.manager.execute(1, "dump", "create", source_ref="@basic")
    w.shared.media.copy_group.side_effect = None
    w.shared.media.copy_group.return_value = None
    native = SimpleNamespace(clone=AsyncMock())
    w.shared.media.tdl = native

    async def download(original, *, file):
        from pathlib import Path

        await asyncio.to_thread(Path(file).write_bytes, b"photo")

    w.shared.media.client.upload_file = AsyncMock(
        return_value=types.InputFile(1, 1, "photo", "digest")
    )
    w.shared.media.client.download_media = AsyncMock(side_effect=download)
    w.shared.media.client.send_file = AsyncMock(
        return_value=SimpleNamespace(id=777)
    )
    record = w.manager.records[created["id"]]
    await w.manager._cycle(record)
    assert record.state == "done", record.error
    native.clone.assert_not_awaited()
    assert w.shared.media.client.download_media.await_count == 1
    assert not any(w.transfers.jobs_dir.iterdir())
    assert w.store.saved_results(
        ["telegram:chat:23456:1"], None, internal_only=True
    )
    basic.migrated_to = types.InputChannel(123456, 1)
    with pytest.raises(UserError, match="upgraded"):
        await source(w.shared.media, "@basic")


async def test_live_permissions_cancel_backups_and_rename_never_changes_source(
    workspace,
):
    w = workspace
    created = await w.manager.execute(
        2, "follow", "create", source_ref="@source0"
    )
    record = w.manager.records[created["id"]]
    entered = asyncio.Event()
    w.data[w.first].append(message(6))

    async def write(group, target):
        entered.set()
        await asyncio.Event().wait()

    w.own.media.copy_group.side_effect = write
    task = asyncio.create_task(w.manager._cycle(record))
    w.manager.running[record.id] = task
    await asyncio.wait_for(entered.wait(), 3)
    w.store.ban_member(1, 2, 86400)
    await w.manager.reconcile_permissions()
    assert (
        task.cancelled()
        and record.phase == "uncertain"
        and record.state == "paused"
    )
    with pytest.raises(UserError):
        await w.manager.execute(2, "follow", "resume", task_id=record.id)
    other = await w.manager.execute(1, "dump", "create", source_ref="@source0")
    w.shared.media.client.get_entity.side_effect = None
    w.shared.media.client.get_entity.return_value = w.channels["@source1"]
    await w.manager._cycle(w.manager.records[other["id"]])
    assert w.manager.records[other["id"]].state == "paused"
    w.shared.media.copy_group.assert_not_awaited()


async def test_idle_follow_restarts_after_reload_without_a_user_request(
    workspace, monkeypatch
):
    if sys.platform == "win32":
        pytest.skip("Unix engine handoff requires local sockets")
    from tdl_bot.runtime.engine import maintain_engine

    w = workspace
    created = await w.manager.execute(
        2, "follow", "create", source_ref="@source0"
    )
    settings = SimpleNamespace(data_dir=w.transfers.jobs_dir.parent)
    w.manager.start()
    first = EngineServer(w.transfers, settings)
    first.reload_requested = True
    old = asyncio.create_task(first.serve())
    socket = settings.data_dir / "runtime.sock"
    new = None
    replacement = None
    check = asyncio.Event()

    async def ensure(received):
        nonlocal new, replacement
        assert received is settings
        if old.done() and new is None:
            replacement = BackupService(w.transfers, workers=1)
            w.transfers.backups = replacement
            replacement.start()
            server = EngineServer(w.transfers, settings)
            new = asyncio.create_task(server.serve())
            w.data[w.first].append(message(6))
            replacement.records[created["id"]].next_run = 0
            replacement.wake.set()
            check.set()
        return socket

    monkeypatch.setattr("tdl_bot.runtime.engine.ensure_engine", ensure)
    monitor = asyncio.create_task(maintain_engine(settings, interval=0.01))
    try:
        await asyncio.wait_for(check.wait(), 3)
        assert replacement is not None
        async with asyncio.timeout(3):
            while replacement.records[created["id"]].cursor != 6:  # noqa: ASYNC110 - durable engine handoff
                await asyncio.sleep(0.01)
        assert w.own.media.copy_group.await_count == 1
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        for task in (old, new):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if replacement is not None:
            await replacement.close()


async def test_backup_pacing_shares_chats_cooldowns_and_stops_before_writes():
    from tdl_bot.transfers.backups.pacing import BackupPacer

    now = 100.0
    waits = []
    stop = False

    async def sleep(delay):
        nonlocal now
        waits.append(delay)
        now += delay

    pacer = BackupPacer(clock=lambda: now, sleep=sleep)
    storage = Target("-1000000999999")
    first = pacer.keys("shared", storage, Target("@destination", 1))
    other = pacer.keys("own", storage, Target("@destination", 2))
    assert await pacer.reserve(first, 10, lambda: stop)
    assert pacer.remaining("shared") == 10
    assert await pacer.wait(other, lambda: stop)
    assert (
        sum(waits) == 30
    )  # Album members count individually; topics share limits.
    pacer.cooldown("shared", 60)
    pacer.cooldown("shared", 1)
    assert pacer.remaining("shared") == 60
    assert pacer.remaining("own") == 0
    pacer.cooldown("shared", 90, bot=True)
    started = now
    assert await pacer.wait(other, lambda: stop)
    assert now - started == 90
    stop = True
    assert not await pacer.wait(first, lambda: stop)
    assert all(delay <= 1 for delay in waits)


async def test_backup_cycles_overlap_without_losing_individual_checkpoints(
    workspace,
):
    w = workspace
    w.manager.workers = 8
    assert BackupService(w.transfers, workers=8).workers == 8
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_copy(group, target):
        started.set()
        await release.wait()
        return [2000 + m.id for m in group.messages]

    w.shared.media.copy_group.side_effect = slow_copy
    first = await w.manager.execute(1, "dump", "create", source_ref="@source0")
    # A different executor and source must progress during the slow transfer.
    w.data[utils.get_peer_id(w.channels["@source1"])] = [
        message(20, photo=True)
    ]
    second = await w.manager.execute(1, "dump", "create", source_ref="@source1")
    other = w.manager.records[second["id"]]
    other.executor = "operator:2"
    slow = w.manager.records[first["id"]]
    task = asyncio.create_task(w.manager._cycle(slow))
    try:
        async with asyncio.timeout(2):
            await started.wait()
            await w.manager._cycle(other)
        assert not task.done()
        assert other.state == "done" and other.cursor == 20
        assert slow.cursor == 0 and slow.phase == "writing"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert slow.state == "paused" and slow.phase == "uncertain"
        assert other.state == "done" and other.cursor == 20
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_follow_default_capacity_cancelled_retention_and_durable_limits(
    workspace, monkeypatch
):
    w = workspace
    for index in range(9):
        await w.manager.execute(
            3, "follow", "create", source_ref=f"@source{index}"
        )
    with pytest.raises(UserError, match="limit reached"):
        await w.manager.execute(3, "follow", "create", source_ref="@source9")
    result = await w.manager.execute(3, "follow")
    assert result["used"] == result["limit"] == result["total"] == 9
    for actor, user, limit in [(2, 3, 1), (1, 99, 1), (1, 3, -1), (1, 3, 1001)]:
        with pytest.raises(UserError):
            w.store.set_follow_limit(actor, user, limit)
    w.store.set_follow_limit(1, 3, 0)
    first = next(r for r in w.manager.records.values() if r.owner == 3)
    await w.manager.execute(3, "follow", "pause", task_id=first.id)
    with pytest.raises(UserError, match="limit reached"):
        await w.manager.execute(3, "follow", "resume", task_id=first.id)
    path = w.transfers.jobs_dir.parent / "state.sqlite3"
    reopened = Store(path, migrate=False)
    assert reopened.follow_limit(3) == 0
    assert reopened.member_history(1, 3)[0][1] == "follow_limit:0"
    reopened.close()
    dump = await w.manager.execute(1, "dump", "create", source_ref="@source0")
    now = w.store.now()
    monkeypatch.setattr(w.store, "now", lambda: now)
    for actor, kind, task_id in [
        (3, "follow", first.id),
        (1, "dump", dump["id"]),
    ]:
        await w.manager.execute(actor, kind, "cancel", task_id=task_id)
        assert w.manager.records[task_id].cancelled_at == now
        monkeypatch.setattr(w.store, "now", lambda: now + 7 * 86400)
        page = await w.manager.execute(
            actor, kind, page=2 if kind == "follow" else 1
        )
        assert task_id in {r["id"] for r in page["items"]}
        monkeypatch.setattr(w.store, "now", lambda: now + 7 * 86400 + 1)
        pages = [await w.manager.execute(actor, kind, page=i) for i in (1, 2)]
        assert task_id not in {r["id"] for page in pages for r in page["items"]}
        assert (
            await w.manager.execute(actor, kind, "status", task_id=task_id)
        )["state"] == "cancelled"
        monkeypatch.setattr(w.store, "now", lambda: now)
    # Legacy cancellations start their seven-day display window at upgrade.
    payload = first.payload()
    payload.pop("cancelled_at")
    from tdl_bot.storage.backups import Backup

    w.store.backups.save(Backup.restore(payload))
    restored = BackupService(w.transfers)
    assert restored.records[first.id].cancelled_at == now
    assert (await restored.execute(3, "follow"))["total"] == 9


async def test_rate_waits_preserve_checkpoints_on_cancel_and_reload(workspace):
    from tdl_bot.transfers.backups.pacing import BackupPacer

    w = workspace
    entered, release = asyncio.Event(), asyncio.Event()

    async def sleep(delay):
        entered.set()
        await release.wait()

    w.manager.pacer = BackupPacer(sleep=sleep)
    created = await w.manager.execute(
        1, "dump", "create", source_ref="@source0"
    )
    record = w.manager.records[created["id"]]
    w.manager.pacer.cooldown("shared", 60)
    await w.manager._cycle(record)
    assert record.next_run > w.store.now() + 50 and record.cursor == 0
    w.shared.media.copy_group.assert_not_awaited()
    w.manager.pacer.deadlines.clear()
    w.manager.pacer.cooldown("shared", 60, bot=True)
    task = asyncio.create_task(w.manager._cycle(record))
    w.manager.running[record.id] = task
    await asyncio.wait_for(entered.wait(), 1)
    await w.manager.execute(1, "dump", "cancel", task_id=record.id)
    assert record.cursor == 0 and record.phase == "idle"
    created = await w.manager.execute(
        1, "dump", "create", source_ref="@source0"
    )
    record = w.manager.records[created["id"]]
    entered.clear()
    task = asyncio.create_task(w.manager._cycle(record))
    w.manager.running[record.id] = task
    await asyncio.wait_for(entered.wait(), 1)
    w.manager.quiesce()
    release.set()
    await asyncio.wait_for(task, 1)
    assert record.state == "active" and record.phase == "idle"
    assert record.cursor == 0
    w.shared.media.copy_group.assert_not_awaited()


def test_backup_subcommand_prefixes_preserve_sources_and_reject_ambiguity():
    for short, full in (
        ("l 2", "list 2"),
        ("s id", "status id"),
        ("p id", "pause id"),
        ("c id", "cancel id"),
        ("res id r", "resume id retry"),
        ("res id s", "resume id skip"),
        ("i id 5m", "interval id 5m"),
        (
            "ad @source0 t @destination e 5m",
            "add @source0 to @destination every 5m",
        ),
    ):
        assert arguments("follow", short) == arguments("follow", full)
    with pytest.raises(UserError, match="remove, resume"):
        arguments("follow", "r id")
    for source_ref in ("@resume", "https://t.me/example", "-10042"):
        assert arguments("follow", source_ref)["source_ref"] == source_ref
