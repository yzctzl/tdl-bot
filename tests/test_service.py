"""Transfer completeness, queue ownership and cleanup on every exit path."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tdl_bot.core.errors import AccessDenied, UserError
from tdl_bot.integrations.tdl.gate import UpdateGate
from tdl_bot.storage.models import Target
from tdl_bot.storage.store import Store
from tdl_bot.transfers.accounts import AccessPolicy, AccountPool, Executor
from tdl_bot.transfers.jobs import Job
from tdl_bot.transfers.service import TransferService

pytestmark = pytest.mark.usefixtures("chinese")


@pytest.fixture
def setup_service(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    store.set_target(1, Target("me"))
    messages = [
        SimpleNamespace(id=10, document=None),
        SimpleNamespace(id=20, document=SimpleNamespace(size=5)),
    ]
    group = SimpleNamespace(messages=messages, urls=["one", "two"])
    media = SimpleNamespace(
        resolve=AsyncMock(return_value=group), upload=AsyncMock()
    )

    async def download(urls, directory):
        (directory / "10_photo.jpg").write_bytes(b"photo")
        (directory / "20_video.mp4").write_bytes(b"video")

    tdl = SimpleNamespace(
        gate=UpdateGate(), download=AsyncMock(side_effect=download)
    )
    notify = AsyncMock()
    service = TransferService(
        AccountPool(Executor("shared", tdl, media)),
        store,
        tmp_path / "jobs",
        notify,
        policy=AccessPolicy(frozenset({1, 2, 3}), frozenset({3})),
        workers=1,
        queue_size=1,
    )
    service.jobs_dir.mkdir()
    yield service, tdl, media, store, notify
    store.close()


async def test_success_preserves_order_and_deletes_files(setup_service):
    service, _, media, store, notify = setup_service
    url = "https://t.me/channel/20"
    store.set_pending(1, url)
    await service._transfer(Job(1, url, Target("me")))
    paths = media.upload.call_args.args[1]
    assert [path.name for path in paths] == ["10_photo.jpg", "20_video.mp4"]
    assert not any(service.jobs_dir.iterdir())
    assert store.pending_items() == []
    assert "完成：已发送 2 个媒体及文字" in notify.call_args.args[1]
    assert notify.call_args.kwargs["finished"] is True


@pytest.mark.parametrize(
    "failure", ["download", "upload", "incomplete", "size"]
)
async def test_failure_always_cleans_and_incomplete_files_never_upload(
    setup_service, failure
):
    service, tdl, media, _, _ = setup_service
    if failure == "upload":
        media.upload.side_effect = RuntimeError("upload failed")
    else:

        async def fail(urls, directory):
            (directory / "10_photo.jpg").write_bytes(b"photo")
            if failure == "download":
                raise RuntimeError("download failed")
            if failure == "size":
                (directory / "20_video.mp4").write_bytes(b"partial video")

        tdl.download.side_effect = fail
    with pytest.raises((RuntimeError, UserError)):
        await service._transfer(Job(1, "https://t.me/channel/20", Target("me")))
    assert not any(service.jobs_dir.iterdir())
    if failure != "upload":
        media.upload.assert_not_awaited()


async def test_cancellation_cleans_partial_download(setup_service):
    service, tdl, media, _, _ = setup_service
    started = asyncio.Event()

    async def download(urls, directory):
        (directory / "10_partial").write_bytes(b"partial")
        started.set()
        await asyncio.Event().wait()

    tdl.download.side_effect = download
    task = asyncio.create_task(
        service._transfer(Job(1, "https://t.me/channel/20", Target("me")))
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any(service.jobs_dir.iterdir())
    media.upload.assert_not_awaited()


async def test_queue_is_bounded_and_target_is_snapshotted(setup_service):
    service, _, _, store, _ = setup_service
    await service.submit(1, "https://t.me/channel/10")
    with pytest.raises(UserError, match="已在"):
        await service.submit(1, "https://t.me/channel/10")
    store.set_target(1, Target("new"))
    with pytest.raises(UserError, match="已满"):
        await service.submit(2, "https://t.me/channel/20")
    queued = service._queue.get_nowait()
    assert queued.target == Target("me")
    service._queue.task_done()


async def test_access_denied_is_persisted_and_worker_recovers(setup_service):
    service, _, media, store, notify = setup_service
    media.resolve.side_effect = AccessDenied("private")
    service.start()
    await service.submit(1, "https://t.me/channel/10")
    await service.close()
    assert store.pending_items()[0].url == "https://t.me/channel/10"
    assert not service.busy(1)
    assert "/join" in notify.call_args.args[1]


async def test_notification_failure_does_not_abort_transfer(setup_service):
    service, _, media, _, notify = setup_service
    notify.side_effect = RuntimeError("bot blocked")
    await service._transfer(Job(1, "https://t.me/channel/20", Target("me")))
    media.upload.assert_awaited_once()
    assert not any(service.jobs_dir.iterdir())


async def test_start_only_cleans_owned_abandoned_jobs(setup_service):
    service, _, _, _, _ = setup_service
    stale = service.jobs_dir / "job-abandoned"
    stale.mkdir()
    (stale / "partial.mp4").write_bytes(b"partial")
    unrelated = service.jobs_dir / "user-data"
    unrelated.mkdir()
    service.start()
    assert not stale.exists()
    assert unrelated.exists()
    await service.close()


async def test_queued_cancel_releases_slot_and_old_button_cannot_cancel_new_job(
    setup_service,
):
    service, _, media, store, notify = setup_service
    store.set_target(1, Target("me"), private=True)
    first = await service.submit(1, "https://t.me/channel/10")
    assert not await service.cancel(2, first.id)
    assert await service.cancel(1, first.id)
    assert not service.busy(1)
    assert notify.call_args.kwargs["finished"]
    second = await service.submit(1, "https://t.me/channel/20")
    assert not await service.cancel(1, first.id)
    assert service.current_job(1, second.id) is second
    await service.cancel(1, second.id)
    assert service._queue.empty()
    media.resolve.assert_not_awaited()
    await service.close()


@pytest.mark.parametrize(
    "stage", ["resolve", "download", "upload", "update_wait"]
)
async def test_cancel_cleans_at_every_stage_and_worker_accepts_next_job(
    setup_service, stage
):
    service, tdl, media, _, notify = setup_service
    entered = asyncio.Event()
    original_download = tdl.download.side_effect
    original_resolve = media.resolve.return_value

    async def block(*args):
        if stage == "download":
            (args[1] / "10_partial").write_bytes(b"partial")
        entered.set()
        await asyncio.Event().wait()

    if stage == "update_wait":
        gate = tdl.gate.update()
        await gate.__aenter__()
    else:
        getattr(
            media if stage != "download" else tdl, stage
        ).side_effect = block
    service.start()
    job = await service.submit(1, "https://t.me/channel/20")
    if stage == "update_wait":
        # The worker owns the job, but its child is waiting for the update gate.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    else:
        await asyncio.wait_for(entered.wait(), 2)
    async with asyncio.timeout(2):
        assert await service.cancel(1, job.id)
    assert job.done.is_set()
    assert not any(service.jobs_dir.iterdir())
    assert "已取消" in notify.call_args.args[1]
    assert notify.call_args.kwargs["finished"]
    assert not service.busy(1)
    if stage == "update_wait":
        await gate.__aexit__(None, None, None)
    tdl.download.side_effect = original_download
    media.resolve.side_effect = None
    media.resolve.return_value = original_resolve
    media.upload.side_effect = None
    next_job = await service.submit(1, "https://t.me/channel/20")
    await service.close()
    assert next_job.finished
    assert "完成：已发送" in notify.call_args.args[1]


async def test_initial_progress_failure_never_starts_transfer(setup_service):
    service, _, media, _, notify = setup_service
    notify.side_effect = RuntimeError("reply could not be delivered")
    service.start()
    with pytest.raises(RuntimeError):
        await service.submit(1, "https://t.me/channel/20")
    await service.close()
    assert not service.busy(1)
    media.resolve.assert_not_awaited()


async def test_direct_copy_and_cached_repeat_never_download(setup_service):
    service, tdl, media, store, _ = setup_service
    media.copy_group = AsyncMock(return_value=[100, 101])
    media.copy_messages = AsyncMock(return_value=[200, 201])
    url = "https://t.me/channel/20"
    await service._transfer(Job(1, url, Target("me")))
    tdl.download.assert_not_awaited()
    media.upload.assert_not_awaited()
    await service._transfer(Job(2, url, Target("other", 77)))
    assert media.resolve.await_count == 1
    media.copy_messages.assert_awaited_once_with(
        "me", [100, 101], Target("other", 77)
    )
    assert store.saved_results([url], "shared")


async def test_protected_copy_downloads_then_reuses_upload(setup_service):
    service, tdl, media, _, _ = setup_service
    media.copy_group = AsyncMock(return_value=None)
    media.copy_messages = AsyncMock(return_value=[200, 201])
    media.upload.return_value = [100, 101]
    url = "https://t.me/channel/20"
    await service._transfer(Job(1, url, Target("me")))
    await service._transfer(Job(1, url, Target("other")))
    assert tdl.download.await_count == 1
    assert media.upload.await_count == 1


async def test_private_cached_result_is_not_visible_to_other_operator(
    setup_service,
):
    service, _, media, store, _ = setup_service
    url = "https://t.me/channel/20"
    store.save_result([url], "shared", 1, "private", [100, 101])
    media.copy_messages = AsyncMock(return_value=[200, 201])
    await service._transfer(Job(2, url, Target("me")))
    media.copy_messages.assert_not_awaited()
    media.upload.assert_awaited_once()


async def test_deleted_cache_falls_back_to_source(setup_service):
    service, tdl, media, store, _ = setup_service
    url = "https://t.me/channel/20"
    store.save_result([url], "shared", None, "old", [100, 101])
    media.copy_messages = AsyncMock(return_value=None)
    media.copy_group = AsyncMock(return_value=[200, 201])
    await service._transfer(Job(1, url, Target("me")))
    media.resolve.assert_awaited_once()
    tdl.download.assert_not_awaited()


def testmedia_fingerprint_respects_caption_and_media_identity():
    from tdl_bot.transfers.media import media_fingerprint

    message = SimpleNamespace(
        document=SimpleNamespace(id=11),
        photo=None,
        message="caption",
        entities=[],
    )
    first = media_fingerprint([message])
    message.message = "different"
    assert first != media_fingerprint([message])
    message.message = "caption"
    message.document.id = 12
    assert first != media_fingerprint([message])


async def test_different_album_links_reuse_one_result_under_concurrency(
    setup_service,
):
    service, tdl, media, _, _ = setup_service
    media.copy_group = AsyncMock(return_value=[100, 101])
    media.copy_messages = AsyncMock(return_value=[200, 201])
    group = media.resolve.return_value
    group.urls = ["https://t.me/channel/10", "https://t.me/channel/20"]
    await asyncio.gather(
        service._transfer(Job(1, group.urls[0], Target("me"))),
        service._transfer(Job(2, group.urls[1], Target("other"))),
    )
    assert media.copy_group.await_count == 1
    assert media.resolve.await_count == 1
    assert media.copy_messages.await_count == 1
    tdl.download.assert_not_awaited()


async def test_same_media_in_another_source_reuses_by_fingerprint(
    setup_service,
):
    service, tdl, media, _, _ = setup_service
    group = media.resolve.return_value
    for index, message in enumerate(group.messages):
        message.document = SimpleNamespace(id=index + 90, size=5)
        message.photo = None
        message.message = "text"
        message.entities = []
    group.urls = ["https://t.me/channel/10", "https://t.me/channel/20"]
    media.copy_group = AsyncMock(return_value=[100, 101])
    media.copy_messages = AsyncMock(return_value=[200, 201])
    await service._transfer(Job(1, group.urls[0], Target("me")))
    group.urls = ["https://t.me/another/10", "https://t.me/another/20"]
    await service._transfer(Job(1, group.urls[0], Target("other")))
    assert media.resolve.await_count == 2
    assert media.copy_group.await_count == 1
    assert media.copy_messages.await_count == 1
    tdl.download.assert_not_awaited()


async def test_database_schema_failure_does_not_blame_network_or_kill_worker(
    setup_service,
    caplog,
):
    import sqlite3

    service, _, media, _, notify = setup_service
    media.resolve.side_effect = [
        sqlite3.OperationalError("no such column: r.available"),
        media.resolve.return_value,
    ]
    service.start()
    try:
        failed = await service.submit(1, "https://t.me/channel/10")
        await failed.done.wait()
        failure_text = notify.call_args.args[1]
        assert "服务数据异常" in failure_text
        assert "网络" not in failure_text
        assert "r.available" in caplog.text
        succeeding = await service.submit(1, "https://t.me/channel/20")
        await succeeding.done.wait()
        assert "完成" in notify.call_args.args[1]
    finally:
        await service.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_native_clone_owns_scratch_and_skips_folder_upload(
    setup_service, cancel
):
    service, tdl, media, store, notify = setup_service
    entered = asyncio.Event()

    async def clone(group, target, directory):
        assert notify.call_args.args[1] == "3/5 正在下载并上传 2 个媒体。"
        assert not any(
            str(call.args[1]).startswith("4/5")
            for call in notify.call_args_list
        )
        assert group.urls == ["one", "two"] and target == Target("me")
        (directory / "tdl_native").write_bytes(b"native scratch")
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        return [100, 101]

    media.tdl = tdl
    media.clone_group = AsyncMock(side_effect=clone)
    task = asyncio.create_task(
        service._transfer(Job(1, "https://t.me/channel/20", Target("me")))
    )
    await asyncio.wait_for(entered.wait(), 2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
        media.copy_messages = AsyncMock(return_value=[200, 201])
        await service._transfer(Job(1, "https://t.me/channel/20", Target("me")))
        media.copy_messages.assert_awaited_once()
    media.clone_group.assert_awaited_once()
    tdl.download.assert_not_awaited()
    media.upload.assert_not_awaited()
    assert not any(service.jobs_dir.iterdir())


@pytest.mark.parametrize("mode", ["copy", "clone"])
async def test_transfer_and_cached_result_keep_numbered_progress(
    setup_service, mode
):
    service, tdl, media, _, notify = setup_service

    async def transfer(*args):
        expected = (
            "3/5 正在准备 2 个媒体。"
            if mode == "copy"
            else "3/5 正在下载并上传 2 个媒体。"
        )
        assert notify.call_args.args[1] == expected
        assert not any(
            str(call.args[1]).startswith("4/5")
            for call in notify.call_args_list
        )
        return [100, 101]

    async def deliver(job, ids):
        assert notify.call_args.args[1] == "4/5 正在发送 2 个媒体。"
        assert job.delivery_started and ids == [100, 101]
        assert not notify.call_args.kwargs["finished"]
        return [300, 301]

    if mode == "copy":
        media.copy_group = AsyncMock(side_effect=transfer)
    else:
        media.tdl = tdl
        media.clone_group = AsyncMock(side_effect=transfer)
    service.delivery = AsyncMock(side_effect=deliver)
    media.copy_messages = AsyncMock(return_value=[200, 201])
    url = "https://t.me/channel/20"
    for _ in range(2):
        notify.reset_mock()
        await service._transfer(Job(1, url, Target("me")))
        texts = [str(c.args[1]) for c in notify.call_args_list]
        steps = [int(text[0]) for text in texts]
        assert steps == sorted(steps) and set(steps) == {2, 3, 4, 5}
        assert notify.call_args.kwargs["finished"] is True
        assert texts[1] == "3/5 正在准备 2 个媒体。"
        assert texts[-2] == "4/5 正在发送 2 个媒体。"
        assert texts[-1] == "5/5 完成：已发送 2 个媒体及文字。"
        assert not any(
            word in "".join(texts) for word in ("复用", "无需", "tdl", "转存")
        )
    tdl.download.assert_not_awaited()
    media.upload.assert_not_awaited()


async def test_activity_links_precede_media_and_cached_results_only_link_back(
    setup_service,
):
    from tdl_bot.transfers.activity import ActivityRecorder

    service, _, media, store, _ = setup_service
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=50)),
        edit_message_text=AsyncMock(),
    )
    service.activity = ActivityRecorder(bot, store)
    service.delivery = AsyncMock(return_value=[300, 301])
    store.remember_user(1, "example", "Test")

    async def copy(group, target):
        assert bot.send_message.await_count == 1
        assert store.activity.list()[0]["status"] == "recorded"
        assert (
            "https://t.me/channel/20"
            in bot.send_message.call_args.kwargs["text"]
        )
        return [100, 101]

    media.copy_group = AsyncMock(side_effect=copy)
    for user_id in (1, 2):
        job = Job(
            user_id,
            "https://t.me/channel/20",
            Target(str(user_id)),
            storage_target=Target("-10042"),
        )
        await service._transfer(job)
        event = store.activity.get("media:" + job.id)
        assert (
            event["result_ids"] == [100, 101] and event["status"] == "completed"
        )
    assert media.copy_group.await_count == 1
    assert bot.send_message.await_count == 2
    assert (
        "https://t.me/c/42/100"
        in bot.edit_message_text.call_args.kwargs["text"]
    )
    assert service.delivery.await_count == 2
    assert "UID:" not in bot.send_message.call_args_list[0].kwargs["text"]
    # Recovered jobs reuse acknowledged records instead of posting another link.
    event = store.activity.list()[0]
    await service.activity.begin(
        event["id"], "media", 2, event["url"], Target("-10042"), "shared"
    )
    assert bot.send_message.await_count == 2
    # An ambiguous recording failure must not silently replay the write.
    from aiogram.exceptions import TelegramNetworkError

    bot.send_message.side_effect = TelegramNetworkError(
        method=None, message="lost acknowledgement"
    )
    uncertain = Job(
        1,
        "https://t.me/channel/30",
        Target("1"),
        storage_target=Target("-10042"),
    )
    for _ in range(2):
        with pytest.raises(UserError):
            await service._transfer(uncertain)
    assert bot.send_message.await_count == 3
    assert media.copy_group.await_count == 1


@pytest.mark.parametrize("outcome", ["joined", "pending", "failed"])
async def test_join_activity_records_actor_executor_link_and_outcome(
    setup_service, outcome
):
    from tdl_bot.core.errors import JoinPending
    from tdl_bot.transfers.activity import ActivityRecorder

    service, _, media, store, _ = setup_service
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=9)),
        edit_message_text=AsyncMock(),
    )
    service.activity = ActivityRecorder(bot, store)
    store.set_target(1, Target("-10042"))
    media.join = AsyncMock(return_value={"title": "Example", "chat": "-10042"})
    if outcome != "joined":
        media.join.side_effect = (
            JoinPending("pending")
            if outcome == "pending"
            else UserError("failed")
        )
        with pytest.raises(UserError):
            await service.join(1, "https://t.me/example", "shared")
    else:
        await service.join(1, "https://t.me/example", "shared")
    event = store.activity.list()[0]
    assert event["kind"] == "join" and event["user_id"] == 1
    assert (
        event["executor_key"] == "shared"
        and event["url"] == "https://t.me/example"
    )
    assert event["status"] == outcome
    if outcome == "joined":
        assert event["chat"] == "-10042" and event["title"] == "Example"
    with pytest.raises(UserError):
        await service.join(99, "https://t.me/example", "shared")
    assert len(store.activity.list()) == 1
