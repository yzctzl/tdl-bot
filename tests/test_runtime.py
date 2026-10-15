"""Frontend reconnection, actual tdl subprocess survival and hidden delivery."""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import ChatMemberMember, MessageId, User

from tdl_bot.core.errors import UserError
from tdl_bot.integrations.tdl.gate import UpdateGate
from tdl_bot.integrations.tdl.runner import Tdl
from tdl_bot.integrations.telegram.delivery import Delivery, MissingResult
from tdl_bot.runtime.engine import EngineServer, RemoteService, exchange
from tdl_bot.storage.models import Destination, Target
from tdl_bot.storage.store import Store
from tdl_bot.transfers.accounts import AccessPolicy, AccountPool, Executor
from tdl_bot.transfers.jobs import Job
from tdl_bot.transfers.service import TransferService

pytestmark = pytest.mark.usefixtures("chinese")


def delivery_bot(**kwargs):
    return SimpleNamespace(
        get_chat=AsyncMock(
            side_effect=lambda peer: SimpleNamespace(
                id=peer,
                has_protected_content=False,
            )
        ),
        get_me=AsyncMock(return_value=SimpleNamespace(id=99)),
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status="member")
        ),
        **kwargs,
    )


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():  # noqa: ASYNC110 - Poll external process/files.
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("cancel", [False, True])
async def test_frontend_reconnect_keeps_real_tdl_pid_and_can_cancel(
    tmp_path,
    monkeypatch,
    cancel,
):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    policy = AccessPolicy(frozenset({1, 2}), frozenset({1}), store=store)
    store.set_target(1, Target("-1001234567890"))
    release = tmp_path / "release"
    pid_file = tmp_path / "tdl.pid"
    binary = tmp_path / "tdl"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\nfrom pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "directory = Path(sys.argv[sys.argv.index('--dir') + 1])\n"
        "(directory / '10_photo.jpg').write_bytes(b'photo')\n"
    )
    binary.chmod(0o700)
    tdl = Tdl(binary, "default")
    media = SimpleNamespace(
        resolve=AsyncMock(
            return_value=SimpleNamespace(
                messages=[SimpleNamespace(id=10, document=None)],
                urls=["https://t.me/channel/10"],
            )
        ),
        upload=AsyncMock(return_value=[100]),
    )
    bot = delivery_bot(
        copy_messages=AsyncMock(return_value=[MessageId(message_id=200)])
    )

    async def notify(job, text, **kwargs):
        job.progress_message_id = 99

    service = TransferService(
        AccountPool(Executor("shared", tdl, media)),
        store,
        tmp_path / "jobs",
        notify,
        policy=policy,
        workers=1,
        delivery=Delivery(bot),
        durable=True,
    )
    settings = SimpleNamespace(
        data_dir=tmp_path,
        allowed_users=frozenset({1, 2}),
        admin_users=frozenset({1}),
    )
    service.start()
    server = EngineServer(service, settings)
    serving = asyncio.create_task(server.serve())
    opened_stores = []
    try:
        socket = tmp_path / "runtime.sock"
        await until(socket.exists)

        async def ensure(settings):
            return socket

        monkeypatch.setattr("tdl_bot.runtime.engine.ensure_engine", ensure)
        first_store = Store(path)
        opened_stores.append(first_store)
        first = RemoteService(settings, first_store)
        job = await first.submit(
            2, "https://t.me/channel/10", source_message_id=77
        )
        await until(pid_file.exists)
        pid = int(pid_file.read_text())
        first_store.close()  # The old frontend is gone.
        second_store = Store(path)
        opened_stores.append(second_store)
        second = RemoteService(settings, second_store)
        restored = second.current_job(2, job.id[:8])
        assert restored is not None
        assert restored.progress_message_id == 99
        assert restored.target == Target("2")
        assert os.kill(pid, 0) is None  # The same download process is alive.
        assert socket.stat().st_mode & 0o777 == 0o600
        # A code generation change waits for active tasks, without killing tdl.
        await exchange(
            socket, {"method": "ping", "args": {"version": "new-code"}}
        )
        assert not serving.done()
        assert os.kill(pid, 0) is None
        if cancel:
            assert await second.cancel(2, job.id)
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            bot.copy_messages.assert_not_awaited()
        else:
            release.touch()
            await until(lambda: not second.jobs(2))
            media.upload.assert_awaited_once()
            assert media.upload.call_args.args[-1] == Target("-1001234567890")
            bot.copy_messages.assert_awaited_once()
            assert bot.copy_messages.call_args.kwargs["chat_id"] == 2
            # Redelivered Telegram updates are idempotent even after completion.
            await second.submit(
                2, "https://t.me/channel/10", source_message_id=77
            )
            assert media.upload.await_count == 1
        await asyncio.wait_for(serving, 3)
        assert not list(service.jobs_dir.iterdir())
    finally:
        release.touch()
        await service.close()
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        for opened in opened_stores:
            opened.close()
        store.close()


async def test_bot_delivery_copies_album_and_topic_without_origin():
    bot = delivery_bot(
        copy_messages=AsyncMock(
            return_value=[
                MessageId(message_id=20),
                MessageId(message_id=21),
            ]
        )
    )
    job = Job(
        2,
        "url",
        Target("-1001234567890", 42),
        storage_target=Target("-1009876543210"),
    )
    await Delivery(bot)(job, [11, 10])
    assert bot.copy_messages.call_args.kwargs == {
        "chat_id": -1001234567890,
        "from_chat_id": -1009876543210,
        "message_ids": [10, 11],
        "message_thread_id": 42,
        "remove_caption": False,
    }
    bot.copy_messages.return_value = []
    with pytest.raises(MissingResult):
        await Delivery(bot)(job, [10])
    bot.copy_messages.return_value = [MessageId(message_id=20)]
    with pytest.raises(UserError, match="部分"):
        await Delivery(bot)(job, [10, 11])


async def test_bot_target_requires_membership_and_rejects_other_private_user():
    bot = SimpleNamespace(
        get_chat=AsyncMock(return_value=SimpleNamespace(type="private", id=3)),
        get_me=AsyncMock(return_value=SimpleNamespace(id=10)),
        get_chat_member=AsyncMock(),
    )
    with pytest.raises(UserError, match="只能选择"):
        await Delivery(bot).validate(Target("3"), 2)
    bot.get_chat.return_value = SimpleNamespace(
        type="supergroup", id=-123, permissions=None
    )
    member = ChatMemberMember(user=User(id=10, is_bot=True, first_name="Bot"))
    bot.get_chat_member.side_effect = [member, SimpleNamespace(status="left")]
    with pytest.raises(UserError, match="访问权限"):
        await Delivery(bot).validate(Target("@group"), 2)
    bot.get_chat.return_value = SimpleNamespace(type="channel", id=-123)
    bot.get_chat_member.side_effect = None
    bot.get_chat_member.return_value = member
    with pytest.raises(UserError, match="发布消息权限"):
        await Delivery(bot).validate(Target("@channel"), 2)


def test_retries_migrate_to_private_recipients_and_controls_persist(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    store.set_target(1, Target("-1001234567890"))
    store.set_target(2, Target("@private"), private=True)
    store.set_pending(
        2,
        "https://t.me/channel/10",
        destination=Destination(
            Target("-1001234567890"),
        ),
    )
    store.convert_retries_to_recipients()
    assert store.pending_items()[0].destination == Destination(
        Target("@private"), 2
    )
    store.convert_retries_to_recipients()
    controls = {(2, 99): {"retry:1", "job-id"}}
    store.save_controls(controls)
    store.close()
    reopened = Store(tmp_path / "state.sqlite3")
    try:
        assert reopened.controls() == controls
        assert reopened.recipient(3) == Destination(Target("3"), 3)
    finally:
        reopened.close()


async def test_supervisor_reloads_only_on_explicit_request(
    tmp_path,
    monkeypatch,
):
    import signal
    import subprocess

    from tdl_bot.runtime.supervisor import supervise

    loop = asyncio.get_running_loop()
    handlers = {}
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, fn: handlers.update({sig: fn})
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: None)
    from unittest.mock import Mock

    version = Mock(return_value="new")
    monkeypatch.setattr("tdl_bot.runtime.engine.code_version", version)
    children = []

    def spawn(command, **kwargs):
        assert command == "frontend"
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs
        )
        children.append(process)
        return process

    monkeypatch.setattr("tdl_bot.runtime.supervisor.spawn_process", spawn)
    unrelated = await asyncio.to_thread(
        subprocess.Popen, [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    task = asyncio.create_task(supervise(tmp_path))
    try:
        await until(lambda: len(children) == 1)
        first = children[0]
        (tmp_path / "changed.py").write_text("changed = True\n")
        await asyncio.sleep(0.7)
        assert len(children) == 1
        version.assert_not_called()
        handlers[signal.SIGHUP]()
        await until(lambda: len(children) == 2)
        assert first.poll() is not None
        assert children[1].poll() is None
        assert unrelated.poll() is None
        handlers[signal.SIGTERM]()
        await asyncio.wait_for(task, 3)
        assert not (tmp_path / "frontend.pid").exists()
        assert children[1].poll() is not None
        assert unrelated.poll() is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for process in [*children, unrelated]:
            if process.poll() is None:
                process.kill()
            await asyncio.to_thread(process.wait)


async def test_shared_storage_cache_reused_across_executors_without_source_read(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite3")
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    policy = AccessPolicy(frozenset({1, 2}), frozenset({1}), store=store)
    storage = Target("-1001234567890")
    store.set_target(1, storage)
    media = SimpleNamespace(resolve=AsyncMock(), copy_messages=AsyncMock())
    tdl = SimpleNamespace(gate=UpdateGate())
    bot = delivery_bot(
        copy_messages=AsyncMock(return_value=[MessageId(message_id=200)])
    )
    service = TransferService(
        AccountPool(Executor("shared", tdl, media)),
        store,
        tmp_path / "jobs",
        AsyncMock(),
        policy=policy,
        delivery=Delivery(bot),
    )
    service.jobs_dir.mkdir()
    url = "https://t.me/channel/10"
    store.save_result([url], "operator:1", None, storage.chat, [100])
    try:
        await service._transfer(
            Job(2, url, Target("2"), owner_id=2, storage_target=storage)
        )
        media.resolve.assert_not_awaited()
        media.copy_messages.assert_not_awaited()
        bot.copy_messages.assert_awaited_once()
    finally:
        store.close()


@pytest.mark.parametrize("unsafe_write", [False, True])
async def test_engine_recovers_queued_jobs_but_never_repeats_uncertain_writes(
    tmp_path,
    unsafe_write,
):
    store = Store(tmp_path / "state.sqlite3")
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    policy = AccessPolicy(frozenset({1, 2}), frozenset({1}), store=store)
    storage = Target("-1001234567890")
    store.set_target(1, storage)
    job = Job(
        2,
        "https://t.me/channel/10",
        Target("2"),
        storage_target=storage,
        owner_id=2,
        source_message_id=77,
        upload_started=unsafe_write,
    )
    store.save_job(job.payload())
    media = SimpleNamespace(
        resolve=AsyncMock(
            return_value=SimpleNamespace(
                messages=[SimpleNamespace(id=10, document=None)],
                urls=[job.url],
            )
        ),
        copy_group=AsyncMock(return_value=[100]),
    )
    tdl = SimpleNamespace(gate=UpdateGate())
    notify = AsyncMock()
    bot = delivery_bot(
        copy_messages=AsyncMock(return_value=[MessageId(message_id=200)])
    )
    service = TransferService(
        AccountPool(Executor("shared", tdl, media)),
        store,
        tmp_path / "jobs",
        notify,
        policy=policy,
        delivery=Delivery(bot),
        durable=True,
    )
    try:
        service.start()
        await until(lambda: notify.await_count > 0)
        await service.close()
        if unsafe_write:
            media.resolve.assert_not_awaited()
            bot.copy_messages.assert_not_awaited()
            assert "检查接收位置" in notify.call_args.args[1]
        else:
            media.copy_group.assert_awaited_once()
            bot.copy_messages.assert_awaited_once()
        assert not store.active_job_payloads()
    finally:
        store.close()


async def test_failed_delivery_reuses_internal_copy_without_download(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    policy = AccessPolicy(frozenset({1, 2}), frozenset({1}), store=store)
    storage = Target("-1001234567890")
    store.set_target(1, storage)
    media = SimpleNamespace(
        resolve=AsyncMock(
            return_value=SimpleNamespace(
                messages=[SimpleNamespace(id=10, document=None)],
                urls=["https://t.me/channel/10"],
            )
        ),
        copy_group=AsyncMock(return_value=[100]),
    )
    delivery = AsyncMock(side_effect=[UserError("发送失败"), None])
    service = TransferService(
        AccountPool(
            Executor("shared", SimpleNamespace(gate=UpdateGate()), media)
        ),
        store,
        tmp_path / "jobs",
        AsyncMock(),
        policy=policy,
        delivery=delivery,
    )
    service.jobs_dir.mkdir()
    try:
        with pytest.raises(UserError, match="发送失败"):
            await service._transfer(
                Job(
                    2,
                    "https://t.me/channel/10",
                    Target("2"),
                    owner_id=2,
                    storage_target=storage,
                )
            )
        await service._transfer(
            Job(
                2,
                "https://t.me/channel/10",
                Target("2"),
                owner_id=2,
                storage_target=storage,
            )
        )
        assert media.resolve.await_count == 1
        assert media.copy_group.await_count == 1
        assert delivery.await_count == 2
    finally:
        store.close()


async def test_storage_inaccessible_is_actionable_for_admin_and_hidden_for_user(
    caplog,
):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import CopyMessages

    from tdl_bot.integrations.telegram.delivery import api_reason

    bot = delivery_bot(copy_messages=AsyncMock())
    method = CopyMessages(
        chat_id=2, from_chat_id=-1001234567890, message_ids=[10]
    )
    error = TelegramBadRequest(
        method=method, message="Bad Request: chat not found"
    )
    bot.get_chat.side_effect = error
    policy = AccessPolicy(frozenset({1, 2}), frozenset({1}))
    delivery = Delivery(bot, policy=policy)
    job = Job(1, "url", Target("1"), storage_target=Target("-1001234567890"))
    with pytest.raises(UserError, match="/storage"):
        await delivery(job, [10])
    job.user_id = 2
    with pytest.raises(UserError) as raised:
        await delivery(job, [10])
    assert "管理员" in str(raised.value)
    assert "存储" not in str(raised.value)
    bot.copy_messages.assert_not_awaited()
    assert "stage=source" in caplog.text
    assert "chat not found" in caplog.text
    error.message = "chat -1001234567890 @example https://example.com/private"
    assert api_reason(error) == "chat [redacted] [redacted] [redacted]"


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("Bad Request: chat not found", "接收位置"),
        ("Bad Request: message thread not found", "话题"),
        ("Bad Request: not enough rights to send", "发送权限"),
    ],
)
async def test_destination_api_errors_are_not_misreported_as_storage_errors(
    reason,
    expected,
):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import CopyMessages

    bot = delivery_bot(
        copy_messages=AsyncMock(
            side_effect=TelegramBadRequest(
                method=CopyMessages(
                    chat_id=2, from_chat_id=-1001234567890, message_ids=[10]
                ),
                message=reason,
            )
        )
    )
    job = Job(2, "url", Target("2"), storage_target=Target("-1001234567890"))
    with pytest.raises(UserError, match=expected):
        await Delivery(bot)(job, [10])


async def test_storage_preflight_failure_does_not_read_or_transfer_source(
    tmp_path,
):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import GetChat

    store = Store(tmp_path / "state.sqlite3")
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    policy = AccessPolicy(frozenset({1, 2}), frozenset({1}), store=store)
    storage = Target("-1001234567890")
    bot = delivery_bot(copy_messages=AsyncMock())
    bot.get_chat.side_effect = TelegramBadRequest(
        method=GetChat(chat_id=-1001234567890),
        message="Bad Request: chat not found",
    )
    media = SimpleNamespace(resolve=AsyncMock(), copy_group=AsyncMock())
    tdl = SimpleNamespace(gate=UpdateGate(), download=AsyncMock())
    service = TransferService(
        AccountPool(Executor("shared", tdl, media)),
        store,
        tmp_path / "jobs",
        AsyncMock(),
        policy=policy,
        delivery=Delivery(bot, policy=policy),
    )
    service.jobs_dir.mkdir()
    try:
        with pytest.raises(UserError, match="管理员"):
            await service._transfer(
                Job(
                    2,
                    "https://t.me/channel/10",
                    Target("2"),
                    owner_id=2,
                    storage_target=storage,
                )
            )
        media.resolve.assert_not_awaited()
        media.copy_group.assert_not_awaited()
        tdl.download.assert_not_awaited()
    finally:
        store.close()
