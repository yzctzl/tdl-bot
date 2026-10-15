"""Bounded history scans preserving album boundaries and durable cursors."""

from dataclasses import dataclass
from time import time
from typing import cast
from urllib.parse import urlsplit

from telethon import types, utils
from telethon.tl import functions
from telethon.tl.custom.message import Message

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr
from tdl_bot.core.links import JoinLink, MessageLink
from tdl_bot.integrations.telegram.media import MediaGroup


@dataclass(frozen=True)
class HistoryUnit(MediaGroup):
    end_id: int = 0
    native_peer: str | int | None = None

    @property
    def native_urls(self):
        link = MessageLink(
            self.native_peer or self.link.peer, self.link.message_id
        )
        return [link.url_for(message.id) for message in self.messages]


@dataclass
class HistoryPage:
    units: list[HistoryUnit]
    through: int
    complete: bool


async def source(media, value):
    """Resolve homepages, message links and IDs without implicitly joining."""
    peer = value
    if value.startswith(("https://", "http://")):
        try:
            peer = MessageLink.parse(value).peer
        except UserError:
            parts = urlsplit(value)
            path = parts.path.strip("/").split("/")
            if (
                parts.hostname in {"t.me", "telegram.me", "telegram.dog"}
                and len(path) == 2
                and path[0] == "c"
                and path[1].isdigit()
            ):
                peer = -(1_000_000_000_000 + int(path[1]))
            else:
                link = JoinLink.parse(value)
                if link.invite_hash:
                    invite = await media.client(
                        functions.messages.CheckChatInviteRequest(
                            link.invite_hash
                        )
                    )
                    if not isinstance(invite, types.ChatInviteAlready):
                        raise UserError(
                            tr(
                                "Join the source with the execution account "
                                "before creating a backup."
                            )
                        ) from None
                    peer = invite.chat
                else:
                    peer = link.username
    if isinstance(peer, str) and peer.lstrip("-").isdigit():
        peer = int(peer)
        if peer > 0:
            peer = -(1_000_000_000_000 + peer)
    if isinstance(peer, int):
        peer = await media._peer(peer)
    entity = await media.client.get_entity(peer)
    if not isinstance(entity, (types.Channel, types.Chat)):
        raise UserError(tr("Choose a channel or group as the backup source."))
    chat = utils.get_peer_id(entity)
    latest = await media.client.get_messages(entity, limit=1)
    return chat, entity.title, latest[0].id if latest else 0


async def read_page(media, chat, cursor, upper, *, settle=False, limit=100):
    """Read a small page, extending it until the last album is complete."""
    peer = await media._peer(chat)
    entity = await media.client.get_entity(peer)
    native_peer = getattr(entity, "username", None)
    units, pending = [], []
    key = None
    through = cursor
    seen = 0

    def flush():
        nonlocal pending, through
        if not pending:
            return
        through = pending[-1].id
        messages = [
            m
            for m in pending
            if isinstance(m, (Message, types.Message))
            and not getattr(m, "action", None)
        ]
        if messages:
            units.append(
                HistoryUnit(
                    MessageLink(chat, messages[0].id),
                    cast(list[Message], messages),
                    through,
                    native_peer,
                )
            )
        pending = []

    async for message in media.client.iter_messages(
        peer, min_id=cursor, max_id=upper + 1, reverse=True, limit=None
    ):
        group = getattr(message, "grouped_id", None)
        current = ("album", group) if group else ("message", message.id)
        if key != current:
            flush()
            if seen >= limit:
                return HistoryPage(units, through, False)
        # Do not commit a tail album while Telegram may still be publishing it.
        if settle and message.date.timestamp() > time() - 10:
            return HistoryPage(units, through, False)
        pending.append(message)
        key = current
        seen += 1
    flush()
    return HistoryPage(units, upper, True)
