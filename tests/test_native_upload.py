"""Native folder uploads and receipt-verified server-side albums."""

import asyncio
import csv
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
        [types.DocumentAttributeVideo(1, 16, 16)],
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
async def test_native_upload_verifies_receipts_assembles_and_cleans(
    tmp_path, outcome
):
    original = [
        SimpleNamespace(
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
            photo=None,
            document=document(20),
            video=True,
            message="Video",
            entities=[],
        ),
    ]
    files = [tmp_path / "10_photo,<name>.jpg", tmp_path / "20_video.mp4"]
    for file in files:
        file.write_bytes(b"media")
    if outcome == "single":
        original, files = original[:1], files[:1]
    target = Target("-1000000123456", 77)
    staged = []
    started = asyncio.Event()
    folders = []

    async def upload(directory, actual_target, captions, *, photo):
        assert actual_target == target and photo
        folders.append(directory)
        assert {p.name for p in directory.iterdir()} == {p.name for p in files}
        for file in files:
            assert (directory / file.name).stat().st_ino == file.stat().st_ino
        for index, file in enumerate(files):
            staged.append(
                SimpleNamespace(
                    id=200 + index,
                    message=captions[file.name],
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
            raise UserError("Native upload failed")
        if outcome == "cancel":
            await asyncio.Event().wait()

    async def history(peer, *, min_id):
        assert peer == "peer" and min_id == 100
        # Neither other tasks nor unrelated storage posts may be deleted.
        yield SimpleNamespace(id=999, message="tdl-bot-upload:another-task:0")
        for message in reversed(staged):
            yield message

    sent = [SimpleNamespace(id=500 + i) for i in range(len(files))]
    client = SimpleNamespace(
        get_input_entity=AsyncMock(return_value="peer"),
        get_messages=AsyncMock(return_value=[SimpleNamespace(id=100)]),
        iter_messages=history,
        delete_messages=AsyncMock(),
        send_file=AsyncMock(return_value=sent[0] if len(sent) == 1 else sent),
        upload_file=AsyncMock(),
    )
    tdl = SimpleNamespace(upload=AsyncMock(side_effect=upload))
    media = TelegramMedia(client, tdl=tdl)
    task = asyncio.create_task(
        media.upload(SimpleNamespace(messages=original), files, target)
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
    assert all(not directory.exists() for directory in folders)
    assert all(file.exists() for file in files)


async def test_native_upload_command_preserves_session_topic_and_safe_captions(
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
    directory = tmp_path / "folder,with comma"
    captions = {'video_\N{GRINNING FACE},<name>".mp4': "unique-marker:0"}
    await tdl.upload(
        directory, Target("-1000000123456", 77), captions, photo=True
    )
    args = run.call_args.args[0]
    assert args[args.index("--ns") + 1] == "own"
    assert args[args.index("--threads") + 1] == "4"
    assert args[args.index("--chat") + 1] == "123456"
    assert args[args.index("--topic") + 1] == "77"
    assert next(csv.reader([args[args.index("--path") + 1]])) == [
        str(directory)
    ]
    expression = args[args.index("--caption") + 1]
    assert "\N{GRINNING FACE}" in expression
    mapping = json.loads(expression.removesuffix("[FileName + FileExt]"))
    assert mapping == captions
    for filename, receipt in captions.items():
        # Match the real tdl exprEnv: FileName excludes the last extension.
        name = Path(filename)
        file_name, file_ext = name.stem, name.suffix
        assert mapping[file_name + file_ext] == receipt
    assert "--rm" not in args and "--photo" in args
    assert run.call_args.kwargs == {"timeout": 44, "operation": "upload"}
