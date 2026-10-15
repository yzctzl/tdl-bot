"""Read complete media groups and upload them using a user account."""

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from telethon import TelegramClient, errors, utils
from telethon.tl import functions, types
from telethon.tl.custom.message import Message

from tdl_bot.core.errors import AccessDenied, JoinPending, UserError
from tdl_bot.core.i18n import tr
from tdl_bot.core.links import JoinLink, MessageLink
from tdl_bot.integrations.tdl.albums import clone_album
from tdl_bot.storage.models import Target

_ACCESS_ERRORS = (
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.ChatIdInvalidError,
    errors.PeerIdInvalidError,
    errors.UserNotParticipantError,
)


@dataclass(frozen=True)
class MediaGroup:
    """Ordered messages and their resolved download location."""

    link: MessageLink
    messages: list[Message]

    @property
    def urls(self) -> list[str]:
        """Return one canonical url for each downloadable member."""
        return [self.link.url_for(message.id) for message in self.messages]


class TelegramMedia:
    """MTProto operations unavailable through aiogram's Bot API."""

    def __init__(self, client: TelegramClient, *, tdl=None) -> None:
        """Use an already authorized user-account client."""
        self.client = client
        self.tdl = tdl

    async def _peer(self, peer: str | int):
        """Refresh dialog entities when resolving private chat IDs."""
        try:
            return await self.client.get_input_entity(peer)
        except ValueError:
            if isinstance(peer, int):
                async for dialog in self.client.iter_dialogs():
                    if dialog.id == peer:
                        return dialog.input_entity
            raise AccessDenied(
                tr(
                    "The download account has not joined this group or "
                    "cannot resolve the chat."
                )
            ) from None

    async def resolve(self, link: MessageLink) -> MediaGroup:
        """Find an entire album, even if the supplied member has no caption.

        Walk history in both directions rather than assuming message IDs
        are consecutive. Stop at the first different grouped_id; Telegram
        albums contain at most ten messages.

        Args:
            link: A parsed message url.

        Returns:
            The ordered media group with each member's video text/entities.

        Raises:
            AccessDenied: The account cannot read the source chat.
            UserError: The source is missing or is not downloadable media.
        """
        try:
            peer = await self._peer(link.peer)
            if link.comment_id:
                discussion = await self.client(
                    functions.messages.GetDiscussionMessageRequest(
                        peer, link.message_id
                    )
                )
                # The linked discussion copy belongs to a different channel.
                candidates = [
                    message
                    for message in discussion.messages
                    if utils.get_peer_id(message.peer_id)
                    != utils.get_peer_id(peer)
                ]
                if not candidates:
                    raise UserError(
                        tr("Cannot find this channel's discussion group.")
                    )
                chat_id = utils.get_peer_id(candidates[0].peer_id)
                link = MessageLink(chat_id, link.comment_id)
                peer = await self._peer(chat_id)
            # A scalar ids argument returns one message, not TotalList.
            anchor = cast(
                Message | types.MessageEmpty | None,
                await self.client.get_messages(peer, ids=link.message_id),
            )
            if not anchor or isinstance(anchor, types.MessageEmpty):
                raise UserError(
                    tr(
                        "The message does not exist, was deleted, or its "
                        "history is hidden from new members."
                    )
                )
            if not anchor.photo and not anchor.document:
                raise UserError(
                    tr(
                        "This message has no downloadable photo, video, or "
                        "file."
                    )
                )
            messages = [anchor]
            if anchor.grouped_id is not None:
                for reverse in (False, True):
                    async for message in self.client.iter_messages(
                        peer, offset_id=anchor.id, reverse=reverse, limit=10
                    ):
                        if message.grouped_id != anchor.grouped_id:
                            break
                        if message.photo or message.document:
                            messages.append(message)
            return MediaGroup(link, sorted(messages, key=lambda item: item.id))
        except _ACCESS_ERRORS as exc:
            raise AccessDenied(
                tr("The download account cannot access this group.")
            ) from exc

    async def join(self, link: JoinLink) -> None:
        """Join an explicitly supplied chat; report approval requirements."""
        try:
            if link.invite_hash:
                await self.client(
                    functions.messages.ImportChatInviteRequest(link.invite_hash)
                )
            else:
                if not link.username:
                    raise UserError(
                        tr("Provide a group homepage or invite URL.")
                    )
                # Telethon resolves usernames before serializing the request.
                await self.client(
                    functions.channels.JoinChannelRequest(link.username)  # ty: ignore[invalid-argument-type]
                )
        except errors.UserAlreadyParticipantError:
            return
        except errors.InviteRequestSentError as exc:
            raise JoinPending(
                tr("Join request submitted. Use /retry after approval.")
            ) from exc
        except (
            errors.InviteHashExpiredError,
            errors.InviteHashInvalidError,
        ) as exc:
            raise UserError(
                tr(
                    "The group invite URL is invalid or expired. Provide a "
                    "new invite."
                )
            ) from exc

    async def validate_target(self, target: Target):
        """Validate and return the resolved peer for subsequent operations."""
        try:
            return await self.target_peer(target)
        except (AccessDenied, *_ACCESS_ERRORS) as exc:
            raise UserError(
                tr(
                    "The upload account cannot access this destination. "
                    "Check the chat ID and permissions."
                )
            ) from exc

    async def target_peer(self, target: Target):
        """Interpret positive numeric targets as raw channel IDs.

        A bare positive integer normally means PeerUser in Telethon. tdl
        users commonly supply a raw channel ID instead, so make that type
        explicit. Negative Bot API IDs and usernames retain their meaning.
        """
        peer = _target_peer(target.chat)
        if isinstance(peer, int) and peer > 0:
            return await self._peer(-(1_000_000_000_000 + peer))
        return await self._peer(peer)

    async def resolve_user(self, value: str | int):
        """Resolve usernames live, rejecting channels and bot identities."""
        try:
            entity = await self.client.get_entity(value)
        except (
            ValueError,
            *_ACCESS_ERRORS,
            errors.UsernameNotOccupiedError,
            errors.UsernameInvalidError,
        ) as exc:
            raise UserError(
                tr("Cannot resolve the user. Check @username or use a UID.")
            ) from exc
        if not isinstance(entity, types.User) or entity.bot or entity.deleted:
            raise UserError(
                tr("Specify a real user, not a channel, group, or Bot.")
            )
        return entity

    async def copy_messages(self, chat: str, ids: list[int], target: Target):
        """Copy an entire album server-side, retaining captions and topics."""
        source = await self._peer(_target_peer(chat))
        try:
            destination = await self.target_peer(target)
        except (AccessDenied, *_ACCESS_ERRORS) as exc:
            raise UserError(
                tr(
                    "Cannot write to the destination. Ask an administrator "
                    "to check service permissions."
                )
            ) from exc
        existing = cast(
            list[Message | types.MessageEmpty | None],
            await self.client.get_messages(source, ids=ids),
        )
        if len(existing) != len(ids) or any(
            not message or isinstance(message, types.MessageEmpty)
            for message in existing
        ):
            return None  # A deleted cached result is safe to regenerate.
        request = functions.messages.ForwardMessagesRequest(
            from_peer=source,
            id=ids,
            to_peer=destination,
            drop_author=True,
            top_msg_id=target.topic_id,
        )
        result = await self.client(request)
        # Only newly created messages belong to this copy operation.
        messages = [
            update.message
            for update in result.updates
            if isinstance(
                update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)
            )
        ]
        if len(messages) != len(ids):
            raise UserError(
                tr(
                    "Forwarding did not return the entire media group. "
                    "Check the destination before retrying."
                )
            )
        return [message.id for message in messages]

    async def copy_group(self, group: MediaGroup, target: Target):
        try:
            return await self.copy_messages(
                str(group.link.peer), [m.id for m in group.messages], target
            )
        except errors.ChatForwardsRestrictedError:
            return None

    async def copy_text(self, group, target):
        """Copy protected text without a forward header or spoiler entities."""
        peer = await self.validate_target(target)
        ids = []
        for message in group.messages:
            media = getattr(message, "media", None)
            if media is not None and not isinstance(
                media, types.MessageMediaWebPage
            ):
                raise UserError(
                    tr("This protected message type cannot be backed up.")
                )
            sent = await self.client.send_message(
                peer,
                message.message or "",
                parse_mode=None,
                formatting_entities=_visible_entities(message.entities or []),
                reply_to=target.topic_id,
                link_preview=False,
            )
            ids.append(sent.id)
        return ids

    async def download_group(self, group, directory):
        """Basic groups have no tdl message URL; retain a bounded fallback."""
        for message in group.messages:
            await self.client.download_media(
                message, file=str(directory / f"{message.id}_media")
            )

    async def reveal_messages(self, chat: str, ids: list[int]) -> bool:
        """Remove spoilers only on stored copies, never source messages."""
        peer = await self._peer(_target_peer(chat))
        messages = cast(
            list[Message | types.MessageEmpty | None],
            await self.client.get_messages(peer, ids=ids),
        )
        if len(messages) != len(ids) or any(
            not m or isinstance(m, types.MessageEmpty) for m in messages
        ):
            return False
        for message in messages:
            if message is None or isinstance(message, types.MessageEmpty):
                return False
            entities = _visible_entities(message.entities or [])
            media = None
            hidden = bool(getattr(message.media, "spoiler", False))
            if hidden and message.photo:
                media = types.InputMediaPhoto(
                    utils.get_input_photo(message.photo), spoiler=False
                )
            elif hidden and message.document:
                media = types.InputMediaDocument(
                    utils.get_input_document(message.document), spoiler=False
                )
            if media is None and len(entities) == len(message.entities or []):
                continue
            try:
                await self.client(
                    functions.messages.EditMessageRequest(
                        peer=peer,
                        id=message.id,
                        message=message.message or "",
                        entities=entities,
                        media=media,
                    )
                )
            except errors.MessageNotModifiedError:
                pass
        return True

    async def clone_group(
        self, group: MediaGroup, target: Target, directory: Path
    ):
        """Preserve original attributes and previews through native tdl."""
        peer = await self.validate_target(target)
        return await clone_album(
            self.tdl, self.client, group, target, peer, directory
        )

    async def upload(
        self, group: MediaGroup, files: list[Path], target: Target
    ):
        """Upload an ordered album with each original caption and entities.

        Standalone login mode uses Telethon for file bytes. Existing native
        sessions use clone_group instead, preserving original media metadata.
        """
        peer = await self.validate_target(target)
        captions = [message.message or "" for message in group.messages]
        entities = [
            _visible_entities(message.entities or [])
            for message in group.messages
        ]
        visual = all(
            message.photo or message.video for message in group.messages
        )
        uploaded = []
        for message, file in zip(group.messages, files, strict=True):
            handle = await self.client.upload_file(str(file))
            if message.photo:
                uploaded.append(
                    types.InputMediaUploadedPhoto(handle, spoiler=False)
                )
            else:
                uploaded.append(
                    types.InputMediaUploadedDocument(
                        handle,
                        mime_type=message.document.mime_type,
                        attributes=message.document.attributes,
                        force_file=not visual,
                        spoiler=False,
                    )
                )
        single = len(uploaded) == 1
        result = await self.client.send_file(
            peer,
            uploaded[0] if single else uploaded,
            caption=captions[0] if single else captions,
            formatting_entities=entities[0] if single else entities,
            parse_mode=None,
            # None is supported by Telethon but missing from its annotation.
            reply_to=target.topic_id,  # ty: ignore[invalid-argument-type]
            force_document=not visual,
            supports_streaming=visual,
        )
        count = (
            len(result) if isinstance(result, list) else int(result is not None)
        )
        if count != len(files):
            raise UserError(
                tr(
                    "Uploading did not return the entire media group. "
                    "Check the destination before retrying."
                )
            )

        messages = result if isinstance(result, list) else [result]
        return [message.id for message in messages if hasattr(message, "id")]


def _target_peer(chat: str) -> str | int:
    """Translate Telegram's numeric peer IDs for Telethon."""
    return int(chat) if chat.lstrip("-").isdigit() else chat


def _visible_entities(entities):
    return [
        entity
        for entity in entities
        if not isinstance(entity, types.MessageEntitySpoiler)
    ]
