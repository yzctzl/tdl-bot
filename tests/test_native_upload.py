"""Native clones and receipt-verified server-side albums."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types

from tdl_bot.core.errors import UserError
from tdl_bot.integrations.tdl.runner import Tdl
from tdl_bot.integrations.telegram.media import TelegramMedia
from tdl_bot.storage.models import Target


def make_photo(media_id):
    return types.Photo(media_id, 11, b"reference", None, [], 2)


def document(media_id, size=5):
    return types.Document(
        media_id,
        12,
        b"reference",
        None,
        "video/mp4",
        size,
        2,
        [types.DocumentAttributeVideo(1, 720, 1280)],
    )


@pytest.mark.parametrize(
    "outcome",
    [
        "album",
        "single",
        "partial",
        "duplicate",
        "size",
        "native_error",
        "cancel",
    ],
)
async def test_native_clone_verifies_receipts_assembles_and_cleans(
    tmp_path, outcome
):
    original = [
        SimpleNamespace(
            id=101,
            photo=make_photo(10),
            document=None,
            video=False,
            message="Caption <one>",
            entities=[
                types.MessageEntityBold(0, 7),
                types.MessageEntitySpoiler(0, 7),
            ],
        ),
        SimpleNamespace(
            id=102,
            photo=None,
            document=document(20),
            video=True,
            message="Video",
            entities=[],
        ),
    ]
    if outcome == "single":
        original = original[:1]
    urls = [f"https://t.me/channel/{m.id}" for m in original]
    target = Target("-1000000123456", 77)
    staged = []
    started = asyncio.Event()

    async def clone(actual_urls, actual_target, markers, directory):
        assert actual_target == target and actual_urls == urls
        assert directory == tmp_path
        assert set(markers) == {str(m.id) for m in original}
        for index, message in enumerate(original):
            staged.append(
                SimpleNamespace(
                    id=200 + index,
                    message=markers[str(message.id)],
                    photo=make_photo(100 + index) if index == 0 else None,
                    document=document(
                        200 + index, 6 if outcome == "size" else 5
                    )
                    if index
                    else None,
                )
            )
        if outcome in {"partial", "native_error", "cancel"}:
            del staged[1:]
        elif outcome == "duplicate":
            staged[1].message = staged[0].message
        started.set()
        if outcome == "native_error":
            raise UserError("Native clone failed")
        if outcome == "cancel":
            await asyncio.Event().wait()

    async def history(peer, *, min_id):
        assert peer == "peer" and min_id == 100
        # Neither other tasks nor unrelated storage posts may be deleted.
        yield SimpleNamespace(id=999, message="tdl-bot-upload:another-task:0")
        for message in reversed(staged):
            yield message

    sent = [SimpleNamespace(id=500 + i) for i in range(len(original))]
    client = SimpleNamespace(
        get_input_entity=AsyncMock(return_value="peer"),
        get_messages=AsyncMock(return_value=[SimpleNamespace(id=100)]),
        iter_messages=history,
        delete_messages=AsyncMock(),
        send_file=AsyncMock(return_value=sent[0] if len(sent) == 1 else sent),
        upload_file=AsyncMock(),
        download_media=AsyncMock(),
    )
    tdl = SimpleNamespace(clone=AsyncMock(side_effect=clone))
    media = TelegramMedia(client, tdl=tdl)
    task = asyncio.create_task(
        media.clone_group(
            SimpleNamespace(messages=original, urls=urls), target, tmp_path
        )
    )
    if outcome == "cancel":
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome in {"album", "single"}:
        assert await task == [m.id for m in sent]
        args, kwargs = client.send_file.call_args
        refs = args[1] if isinstance(args[1], list) else [args[1]]
        assert isinstance(refs[0], types.InputMediaPhoto)
        assert refs[0].id.id == 100 and refs[0].spoiler is False
        if outcome == "album":
            assert isinstance(refs[1], types.InputMediaDocument)
            assert refs[1].id.id == 201 and refs[1].spoiler is False
            assert kwargs["caption"] == ["Caption <one>", "Video"]
            assert kwargs["formatting_entities"] == [
                [original[0].entities[0]],
                [],
            ]
        else:
            assert kwargs["caption"] == "Caption <one>"
        assert kwargs["reply_to"] == 77 and kwargs["parse_mode"] is None
    else:
        with pytest.raises(UserError):
            await task
    if outcome not in {"album", "single"}:
        client.send_file.assert_not_awaited()
    client.upload_file.assert_not_awaited()  # No second file-byte upload.
    assert set(client.delete_messages.call_args.args[1]) == {
        m.id for m in staged
    }
    client.download_media.assert_not_awaited()  # tdl owns media and previews.


async def test_native_clone_command_preserves_session_topic_and_receipts(
    tmp_path, monkeypatch
):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/own").touch()
    tdl = Tdl(
        Path("tdl"),
        "own",
        data_dir=tmp_path,
        upload_threads=4,
        upload_timeout=44,
    )
    run = AsyncMock()
    monkeypatch.setattr(tdl, "_run", run)
    directory = tmp_path / "native scratch"
    urls = ["https://t.me/channel/101", "https://t.me/channel/102"]
    markers = {"101": "unique-marker:0", "102": "unique-marker:1"}
    await tdl.clone(urls, Target("-1000000123456", 77), markers, directory)
    args = run.call_args.args[0]
    assert args[args.index("--ns") + 1] == "own"
    assert args[args.index("--threads") + 1] == "4"
    assert args[args.index("--mode") + 1] == "clone"
    assert "forward" in args and "--single" in args
    assert json.loads(args[args.index("--to") + 1]) == {
        "Peer": "123456",
        "Thread": 77,
    }
    expression = args[args.index("--edit") + 1]
    assert (
        json.loads(expression.removesuffix("[string(Message.ID)]")) == markers
    )
    assert args[-4:] == ["--from", urls[0], "--from", urls[1]]
    assert run.call_args.kwargs == {
        "timeout": 44,
        "operation": "clone",
        "temporary_dir": directory,
    }
