"""Native tdl byte transfer followed by server-side album assembly."""

import asyncio
import logging
from time import monotonic
from uuid import uuid4

from telethon import types, utils

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr

_LOGGER = logging.getLogger(__name__)


async def clone_album(tdl, client, group, target, peer, directory):
    marker = f"tdl-bot-upload:{uuid4().hex}:"
    latest = await client.get_messages(peer, limit=1)
    after = latest[0].id if latest else 0
    staged = []
    try:
        markers = {
            str(m.id): marker + str(i) for i, m in enumerate(group.messages)
        }
        started = monotonic()
        await tdl.clone(
            getattr(group, "native_urls", group.urls),
            target,
            markers,
            directory,
        )
        staged = await _receipts(client, peer, after, marker)
        by_index = {m.message.removeprefix(marker): m for m in staged}
        if len(staged) != len(group.messages) or set(by_index) != {
            str(i) for i in range(len(group.messages))
        }:
            raise UserError(
                tr(
                    "tdl did not transfer every media file. Album delivery "
                    "stopped; temporary messages will be cleaned up."
                )
            )
        _LOGGER.info(
            "Native clone verified media=%s seconds=%.2f",
            len(group.messages),
            monotonic() - started,
        )
        visual = all(m.photo or m.video for m in group.messages)
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
                if original.document.size != message.document.size:
                    raise UserError(
                        tr("The uploaded file size does not match the source.")
                    )
                media.append(
                    types.InputMediaDocument(
                        utils.get_input_document(message.document),
                        spoiler=False,
                    )
                )
            else:
                raise UserError(tr("tdl returned an unexpected media type."))
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
        if len(sent) != len(group.messages) or any(
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
        # Cleanup also scans after a failed/zero-exit partial native clone.
        cleanup = asyncio.create_task(
            _cleanup(client, peer, after, marker, staged)
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


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
            "Cannot clean tdl clone staging: %s", type(exc).__name__
        )
