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

from tdl_bot.errors import UserError
from tdl_bot.store import Target

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
        super().__init__("部分媒体未能发送，请检查接收位置后再重试。")
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
                        "只能选择你与 Bot 的私聊，或有权限的群／频道。"
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
                            "请将 Bot 加入目标频道并授予发布消息权限。"
                        )
                elif not isinstance(
                    member,
                    (
                        ChatMemberAdministrator,
                        ChatMemberOwner,
                        ChatMemberMember,
                    ),
                ):
                    raise UserError("请将 Bot 加入目标群并授予发送媒体权限。")
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
                        raise UserError("Bot 在该群没有完整的媒体发送权限。")
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
                        raise UserError("你没有该接收位置的访问权限。")
            if storage and chat.has_protected_content:
                raise UserError("内部存储不能开启禁止保存／转发。")
            return Target(str(chat.id), target.topic_id)
        except TelegramAPIError as exc:
            raise UserError(
                "无法使用该接收位置，请检查 Bot 是否已加入及其权限。"
            ) from exc

    def _storage_error(self, job, detail: str) -> UserError:
        if self.policy is not None and self.policy.is_admin(job.user_id):
            return UserError(
                f"内部存储不可用：{detail}。"
                "请将当前 Bot 加入存储群／频道，并用 /storage 确认目标配置；"
                "修正后重新发送链接，可复用已保存媒体。"
            )
        return UserError("服务暂不可用，请联系管理员检查配置后重新发送链接。")

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
                f"Telegram 限流，请 {exc.retry_after} 秒后重试。"
            ) from exc
        except TelegramAPIError as exc:
            _LOGGER.warning(
                "媒体发送失败 stage=source error=%s reason=%s",
                type(exc).__name__,
                api_reason(exc),
            )
            raise self._storage_error(
                job, "Bot 无法访问，或目标 ID 不正确"
            ) from exc
        if member.status in {"left", "kicked"}:
            raise self._storage_error(job, "Bot 尚未加入或已被移除")
        if chat.has_protected_content:
            raise self._storage_error(job, "已开启禁止保存／转发")
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
                "媒体发送失败 stage=copy error=%s reason=%s",
                type(exc).__name__,
                api_reason(exc),
            )
            if isinstance(exc, TelegramRetryAfter):
                text = f"Telegram 限流，请 {exc.retry_after} 秒后重试。"
            elif isinstance(exc, TelegramForbiddenError):
                text = (
                    "Bot 无权发送到接收位置。请解除屏蔽并发送 /start，"
                    "或检查目标群／频道中的 Bot 权限。"
                )
            elif isinstance(exc, TelegramBadRequest):
                reason = exc.message.lower()
                if "message to copy not found" in reason:
                    raise MissingResult(
                        "结果已失效，需要重新处理来源。"
                    ) from exc
                if "thread" in reason or "topic" in reason:
                    text = "接收话题不存在或已关闭，请用 /target 重新设置话题。"
                elif "chat not found" in reason:
                    text = (
                        "Bot 无法访问接收位置，"
                        "请用 /target bot 或重新设置群／频道。"
                    )
                elif (
                    "not enough rights" in reason or "write_forbidden" in reason
                ):
                    text = (
                        "没有接收位置的发送权限，"
                        "将 Bot 加入目标群／频道后重试。"
                    )
                else:
                    text = "Telegram 拒绝复制媒体，请联系管理员查看服务日志。"
            else:
                text = "发送失败，请联系管理员查看服务日志后重试。"
            raise UserError(text) from exc
        if not result:
            raise MissingResult("结果已失效，需要重新处理来源。")
        if len(result) != len(ids):
            raise PartialDelivery([m.message_id for m in result])
        return [m.message_id for m in result]
