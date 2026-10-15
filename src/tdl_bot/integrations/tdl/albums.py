"""Native tdl byte transfer followed by server-side album assembly."""

import asyncio
import logging
import tempfile
from pathlib import Path
from time import monotonic
from uuid import uuid4

from telethon import types, utils

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr

_LOGGER = logging.getLogger(__name__)


async def upload_album(tdl, client, group, files, target, peer):
    marker = f"tdl-bot-upload:{uuid4().hex}:"
    latest = await client.get_messages(peer, limit=1)
    after = latest[0].id if latest else 0
    staged = []
    try:
        with tempfile.TemporaryDirectory(
            prefix="tdl-upload-", dir=files[0].parent
        ) as temporary:
            directory = Path(temporary)
            linking = asyncio.create_task(
                asyncio.to_thread(_link_files, directory, files)
            )
            try:
                names, byte_count = await asyncio.shield(linking)
            except asyncio.CancelledError:
                await linking
                raise
            visual = all(m.photo or m.video for m in group.messages)
            captions = {name: marker + str(i) for i, name in enumerate(names)}
            previews = await _prepare_previews(client, group, directory, names)
            started = monotonic()
            await tdl.upload(directory, target, captions, photo=visual)
            staged = await _receipts(client, peer, after, marker)
            by_index = {m.message.removeprefix(marker): m for m in staged}
            if len(staged) != len(names) or set(by_index) != {
                str(i) for i in range(len(names))
            }:
                raise UserError(
                    tr(
                        "tdl did not upload every media file. Album delivery "
                        "stopped; temporary messages will be cleaned up."
                    )
                )
            elapsed = max(monotonic() - started, 0.001)
            _LOGGER.info(
                "Native upload verified files=%s bytes=%s "
                "seconds=%.2f MiB_per_second=%.2f",
                len(files),
                byte_count,
                elapsed,
                byte_count / elapsed / 1024**2,
            )
            media = []
            for index, original in enumerate(group.messages):
                message = by_index[str(index)]
                if original.photo and message.photo:
                    media.append(
                        types.InputMediaPhoto(
                            utils.get_input_photo(message.photo), spoiler=False
                        )
                    )
                elif original.document and message.document:
                    if index in previews and not message.document.thumbs:
                        raise UserError(
                            tr(
                                "The uploaded video has no preview thumbnail. "
                                "Album delivery stopped; try again later."
                            )
                        )
                    if original.document.size != message.document.size:
                        raise UserError(
                            tr(
                                "The uploaded file size does not match "
                                "the source."
                            )
                        )
                    media.append(
                        types.InputMediaDocument(
                            utils.get_input_document(message.document),
                            spoiler=False,
                        )
                    )
                else:
                    raise UserError(
                        tr("tdl returned an unexpected media type.")
                    )
            captions = [m.message or "" for m in group.messages]
            entities = [
                [
                    e
                    for e in (m.entities or [])
                    if not isinstance(e, types.MessageEntitySpoiler)
                ]
                for m in group.messages
            ]
            single = len(media) == 1
            # Existing Telegram media references carry no local file payload.
            result = await client.send_file(
                peer,
                media[0] if single else media,
                caption=captions[0] if single else captions,
                formatting_entities=entities[0] if single else entities,
                parse_mode=None,
                reply_to=target.topic_id,
                force_document=not visual,
                supports_streaming=visual,
            )
            sent = result if isinstance(result, list) else [result]
            if len(sent) != len(files) or any(
                not getattr(m, "id", None) for m in sent
            ):
                raise UserError(
                    tr(
                        "Uploading did not return the entire media group. "
                        "Check the destination before retrying."
                    )
                )
            return [m.id for m in sent]
    finally:
        # Cleanup also scans after a failed/zero-exit partial native upload.
        cleanup = asyncio.create_task(
            _cleanup(client, peer, after, marker, staged)
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


def _link_files(directory, files):
    names = []
    for file in files:
        # tdl skips .thumb files; all other names retain the downloaded name.
        name = file.name + (".bin" if file.suffix.lower() == ".thumb" else "")
        (directory / name).hardlink_to(file)
        names.append(name)
    return names, sum(file.stat().st_size for file in files)


async def _prepare_previews(client, group, directory, names):
    previews = set()
    for index, (message, name) in enumerate(
        zip(group.messages, names, strict=True)
    ):
        if not message.video or not message.document.thumbs:
            continue
        path = directory / name
        # Match tdl's walk(): Go TrimRight treats the extension as a cutset,
        # so with_suffix() can produce a sidecar name that tdl never finds.
        thumb = str(path).rstrip(path.suffix) + ".thumb"
        result = await client.download_media(message, file=thumb, thumb=-1)
        if not result:
            raise UserError(
                tr(
                    "Could not retrieve the original video preview. "
                    "Try again later."
                )
            )
        previews.add(index)
    return previews


async def _receipts(client, peer, after, marker):
    return [
        m
        async for m in client.iter_messages(peer, min_id=after)
        if (getattr(m, "message", None) or "").startswith(marker)
    ]


async def _cleanup(client, peer, after, marker, staged):
    try:
        async with asyncio.timeout(30):
            messages = staged or await _receipts(client, peer, after, marker)
            if messages:
                await client.delete_messages(peer, [m.id for m in messages])
    except Exception as exc:
        _LOGGER.warning(
            "Cannot clean tdl upload staging: %s", type(exc).__name__
        )
