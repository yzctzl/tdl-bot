"""Read complete media groups and upload them using a user account."""

from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient, errors, utils
from telethon.tl import functions, types
from telethon.tl.custom.message import Message

from tdl_bot.errors import AccessDenied, JoinPending, UserError
from tdl_bot.links import JoinLink, MessageLink
from tdl_bot.store import Target

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

    def __init__(self, client: TelegramClient) -> None:
        """Use an already authorized user-account client."""
        self.client = client

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
                "下载账号尚未加入该群，或无法解析该聊天。"
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
                    raise UserError("无法定位该频道的讨论群。")
                chat_id = utils.get_peer_id(candidates[0].peer_id)
                link = MessageLink(chat_id, link.comment_id)
                peer = await self._peer(chat_id)
            anchor = await self.client.get_messages(peer, ids=link.message_id)
            if not anchor or isinstance(anchor, types.MessageEmpty):
                raise UserError("消息不存在、已删除，或入群前的历史不可见。")
            if not anchor.photo and not anchor.document:
                raise UserError("该消息不包含可下载的图片、视频或文件。")
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
            raise AccessDenied("下载账号没有访问该群的权限。") from exc

    async def join(self, link: JoinLink) -> None:
        """Join an explicitly supplied chat; report approval requirements."""
        try:
            if link.invite_hash:
                await self.client(
                    functions.messages.ImportChatInviteRequest(link.invite_hash)
                )
            else:
                await self.client(
                    functions.channels.JoinChannelRequest(link.username)
                )
        except errors.UserAlreadyParticipantError:
            return
        except errors.InviteRequestSentError as exc:
            raise JoinPending("已申请入群，获批准后使用 /retry 重试。") from exc
        except (
            errors.InviteHashExpiredError,
            errors.InviteHashInvalidError,
        ) as exc:
            raise UserError(
                "群邀请 url 无效或已过期，请提供新的邀请。"
            ) from exc

    async def validate_target(self, target: Target) -> None:
        """Resolve a destination before persisting it."""
        try:
            await self.target_peer(target)
        except (AccessDenied, *_ACCESS_ERRORS) as exc:
            raise UserError(
                "上传账号无法访问该目标，请确认聊天 ID 和权限。"
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

    async def upload(
        self, group: MediaGroup, files: list[Path], target: Target
    ) -> None:
        """Upload an ordered album with each original caption and entities.

        Uploads use the user account so large videos do not encounter the
        hosted Bot API's file-size limit. Audio/document albums are sent as
        documents; visual albums retain photo/video presentation.
        """
        await self.validate_target(target)
        peer = await self.target_peer(target)
        captions = [message.message or "" for message in group.messages]
        entities = [message.entities or [] for message in group.messages]
        visual = all(
            message.photo or message.video for message in group.messages
        )
        uploaded = []
        for message, file in zip(group.messages, files, strict=True):
            handle = await self.client.upload_file(str(file))
            if message.photo:
                uploaded.append(types.InputMediaUploadedPhoto(handle))
            else:
                uploaded.append(
                    types.InputMediaUploadedDocument(
                        handle,
                        mime_type=message.document.mime_type,
                        attributes=message.document.attributes,
                        force_file=not visual,
                    )
                )
        single = len(uploaded) == 1
        result = await self.client.send_file(
            peer,
            uploaded[0] if single else uploaded,
            caption=captions[0] if single else captions,
            formatting_entities=entities[0] if single else entities,
            parse_mode=None,
            reply_to=target.topic_id,
            force_document=not visual,
            supports_streaming=visual,
        )
        count = (
            len(result) if isinstance(result, list) else int(result is not None)
        )
        if count != len(files):
            raise UserError("上传未返回完整媒体组，请检查目标后重试。")


def _target_peer(chat: str) -> str | int:
    """Translate Telegram's numeric peer IDs for Telethon."""
    return int(chat) if chat.lstrip("-").isdigit() else chat
