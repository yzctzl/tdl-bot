"""Upload windows, shared-session coordination and interrupted writes."""

import asyncio
import hashlib
import io
import threading
from unittest.mock import Mock

import pytest
from telethon import TelegramClient, errors
from telethon.sessions import MemorySession
from telethon.tl import functions, types

from tdl_bot.integrations.telegram.client import (
    AccountLock,
    SharedSessionClient,
)
from tdl_bot.integrations.telegram.uploads import _PART_SIZE, _read_parts


@pytest.mark.parametrize("outcome", ["success", "rejected", "flood", "cancel"])
async def test_upload_batches_preserve_parts_locks_and_failure_semantics(
    tmp_path, monkeypatch, outcome
):
    path = tmp_path / "video.mp4"
    contents = b"".join(bytes([i]) * _PART_SIZE for i in range(21)) + b"last"
    path.write_bytes(contents)
    client = SharedSessionClient(
        MemorySession(),
        1,
        "test",
        account_lock=AccountLock(),
        upload_concurrency=8,
    )
    batches = []
    started = asyncio.Event()
    flood = errors.FloodWaitError(request=None, capture=12)

    async def rpc(self, sender, request, **kwargs):
        assert self.account_lock._owner is asyncio.current_task()
        assert not kwargs["ordered"]
        many = isinstance(request, list)
        wrapped = request if many else [request]
        raw = [item.query.query for item in wrapped]
        batches.append(raw)
        started.set()
        if outcome == "cancel":
            await asyncio.Event().wait()
        if outcome == "flood":
            raise errors.MultiError(
                [flood, *[None] * (len(raw) - 1)], [None] * len(raw), raw
            )
        return [outcome != "rejected"] * len(raw) if many else True

    monkeypatch.setattr(TelegramClient, "_call", rpc)
    progress = Mock()
    task = asyncio.create_task(
        client.upload_file(path, progress_callback=progress)
    )
    if outcome == "cancel":
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "success":
        result = await task
        assert isinstance(result, types.InputFileBig)
        assert result.parts == 22 and result.name == "video.mp4"
        assert [len(batch) for batch in batches] == [8, 8, 6]
        parts = [request for batch in batches for request in batch]
        assert all(
            isinstance(r, functions.upload.SaveBigFilePartRequest)
            for r in parts
        )
        assert {request.file_id for request in parts} == {result.id}
        assert [request.file_part for request in parts] == list(range(22))
        assert b"".join(request.bytes for request in parts) == contents
        assert progress.call_args.args == (len(contents), len(contents))
        # Small files and custom stream options still use Telethon's uploader.
        small = tmp_path / "photo.jpg"
        small.write_bytes(b"photo")
        handle = await client.upload_file(small)
        assert isinstance(handle, types.InputFile)
        assert handle.md5_checksum == hashlib.md5(b"photo").hexdigest()
        stream_handle = await client.upload_file(
            io.BytesIO(b"stream"), file_name="x.bin"
        )
        assert stream_handle.name == "x.bin"
    else:
        with pytest.raises(
            errors.FloodWaitError if outcome == "flood" else RuntimeError
        ) as error:
            await task
        if outcome == "flood":
            assert error.value is flood
    if outcome != "success":
        assert len(batches) == 1  # Never send another window after failure.
    async with asyncio.timeout(1), client.account_lock:
        pass  # Cancellation and every error path must release the account.
    path.unlink()


async def test_cancellation_waits_for_disk_reader_before_cleanup(
    tmp_path, monkeypatch
):
    path = tmp_path / "video.mp4"
    with path.open("wb") as stream:
        stream.truncate(11 * 1024**2)
    client = SharedSessionClient(
        MemorySession(), 1, "test", account_lock=AccountLock()
    )
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def reader(*args):
        entered.set()
        assert release.wait(3)
        try:
            return _read_parts(*args)
        finally:
            closed.set()

    monkeypatch.setattr(
        "tdl_bot.integrations.telegram.uploads._read_parts", reader
    )
    task = asyncio.create_task(client.upload_file(path))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()
    path.unlink()
