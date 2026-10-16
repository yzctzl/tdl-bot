"""Deliver stored media through the Bot without revealing internal storage."""

import logging
import re

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberMember,
    ChatMemberOwner,
)

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr
from tdl_bot.storage.models import Target

_LOGGER = logging.getLogger(__name__)


def api_reason(exc: TelegramAPIError) -> str:
    """Keep useful API diagnostics without logging private identifiers."""
    return re.sub(
        r"https?://\S+|@\w+|\d{8,12}:[A-Za-z0-9_-]{30,}|-?\d{5,}",
        "[redacted]",
        exc.message,
    )


class MissingResult(UserError):
    """A stored album was deleted before it could be copied."""


class PartialDelivery(UserError):
    def __init__(self, message_ids):
        super().__init__(
            tr(
                "Some media could not be delivered. Check the destination "
                "before retrying."
            )
        )
        self.message_ids = message_ids


class Delivery:
    def __init__(self, bot: Bot, *, policy=None):
        self.bot = bot
        self.policy = policy

    async def validate(self, target: Target, operator: int, *, storage=False):
        peer = (
            int(target.chat)
            if target.chat.lstrip("-").isdigit()
            else target.chat
        )
        if isinstance(peer, int) and peer > 0 and (storage or peer != operator):
            peer = -(1_000_000_000_000 + peer)
        try:
            chat = await self.bot.get_chat(peer)
            if chat.type == "private":
                if storage or chat.id != operator:
                    raise UserError(
                        tr(
                            "Choose your own Bot dialogue or a group or "
                            "channel you can access."
                        )
                    )
            else:
                me = await self.bot.get_me()
                member = await self.bot.get_chat_member(chat.id, me.id)
                if chat.type == "channel":
                    if (
                        not isinstance(member, ChatMemberAdministrator)
                        or not member.can_post_messages
                    ):
                        raise UserError(
                            tr(
                                "Add the Bot to the destination channel "
                                "and grant permission to post messages."
                            )
                        )
                elif not isinstance(
                    member,
                    (
                        ChatMemberAdministrator,
                        ChatMemberOwner,
                        ChatMemberMember,
                    ),
                ):
                    raise UserError(
                        tr(
                            "Add the Bot to the destination group and "
                            "grant permission to send media."
                        )
                    )
                if isinstance(member, ChatMemberMember) and chat.permissions:
                    permissions = chat.permissions
                    if any(
                        getattr(permissions, name) is False
                        for name in (
                            "can_send_messages",
                            "can_send_photos",
                            "can_send_videos",
                            "can_send_documents",
                            "can_send_audios",
                        )
                    ):
                        raise UserError(
                            tr(
                                "The Bot does not have permission to send "
                                "all media types in this group."
                            )
                        )
                if not storage:
                    actor = await self.bot.get_chat_member(chat.id, operator)
                    if not isinstance(
                        actor,
                        (
                            ChatMemberAdministrator,
                            ChatMemberOwner,
                            ChatMemberMember,
                        ),
                    ):
                        raise UserError(
                            tr("You do not have access to this destination.")
                        )
            if storage and chat.has_protected_content:
                raise UserError(
                    tr("Internal storage must allow saving and forwarding.")
                )
            return Target(str(chat.id), target.topic_id)
        except TelegramAPIError as exc:
            raise UserError(
                tr(
                    "Cannot use this destination. Check that the Bot has "
                    "joined and has permission."
                )
            ) from exc

    def _storage_error(self, job, detail: str) -> UserError:
        if self.policy is not None and self.policy.is_admin(job.user_id):
            return UserError(
                tr(
                    (
                        "Internal storage is unavailable: {p0}. Add this "
                        "Bot to the storage group or channel and check "
                        "/storage. After fixing it, send the link again to "
                        "reuse saved media."
                    ),
                    p0=detail,
                )
            )
        return UserError(
            tr(
                "Service unavailable. Ask an administrator to check the "
                "configuration, then send the link again."
            )
        )

    async def prepare(self, job):
        """Check Bot source access before copying or doing new transfer work."""
        try:
            storage = job.storage_target
            chat = await self.bot.get_chat(
                int(storage.chat)
                if storage.chat.lstrip("-").isdigit()
                else storage.chat
            )
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(chat.id, me.id)
        except TelegramRetryAfter as exc:
            raise UserError(
                tr(
                    "Telegram rate limit. Retry in {p0} seconds.",
                    p0=exc.retry_after,
                )
            ) from exc
        except TelegramAPIError as exc:
            _LOGGER.warning(
                "Media delivery failed stage=source error=%s reason=%s",
                type(exc).__name__,
                api_reason(exc),
            )
            raise self._storage_error(
                job,
                tr(
                    "The Bot cannot access it, or the destination ID is "
                    "incorrect"
                ),
            ) from exc
        if member.status in {"left", "kicked"}:
            raise self._storage_error(
                job, tr("The Bot has not joined or has been removed")
            )
        if chat.has_protected_content:
            raise self._storage_error(
                job, tr("Saving or forwarding is restricted")
            )
        return chat.id

    async def __call__(self, job, ids):
        source = await self.prepare(job)
        try:
            result = await self.bot.copy_messages(
                chat_id=int(job.target.chat)
                if job.target.chat.lstrip("-").isdigit()
                else job.target.chat,
                from_chat_id=source,
                message_ids=sorted(ids),
                message_thread_id=job.target.topic_id,
                remove_caption=False,
            )
        except TelegramAPIError as exc:
            _LOGGER.warning(
                "Media delivery failed stage=copy error=%s reason=%s",
                type(exc).__name__,
                api_reason(exc),
            )
            if isinstance(exc, TelegramRetryAfter):
                text = tr(
                    "Telegram rate limit. Retry in {p0} seconds.",
                    p0=exc.retry_after,
                )
            elif isinstance(exc, TelegramForbiddenError):
                text = tr(
                    "The Bot cannot send to this destination. Unblock it "
                    "and send /start, or check its group or channel "
                    "permissions."
                )
            elif isinstance(exc, TelegramBadRequest):
                reason = exc.message.lower()
                if "message to copy not found" in reason:
                    raise MissingResult(
                        tr("The media is unavailable. Send the link again.")
                    ) from exc
                if "thread" in reason or "topic" in reason:
                    text = tr(
                        "The destination topic does not exist or is "
                        "closed. Set another topic with /target."
                    )
                elif "chat not found" in reason:
                    text = tr(
                        "The Bot cannot access this destination. Use "
                        "/target bot or choose another group or channel."
                    )
                elif (
                    "not enough rights" in reason or "write_forbidden" in reason
                ):
                    text = tr(
                        "No permission to send to this destination. Add "
                        "the Bot to the destination group or channel and "
                        "retry."
                    )
                else:
                    text = tr(
                        "Telegram refused to copy the media. Ask an "
                        "administrator to check the service logs."
                    )
            else:
                text = tr(
                    "Delivery failed. Ask an administrator to check the "
                    "service logs before retrying."
                )
            raise UserError(text) from exc
        if not result:
            raise MissingResult(
                tr("The media is unavailable. Send the link again.")
            )
        if len(result) != len(ids):
            raise PartialDelivery([m.message_id for m in result])
        return [m.message_id for m in result]
