"""aiogram command handlers and authorization."""

import asyncio
import logging
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)
from telethon import errors

from tdl_bot.config import Settings
from tdl_bot.errors import JoinPending, UserError
from tdl_bot.links import JoinLink
from tdl_bot.service import Job, TransferService
from tdl_bot.store import Store, Target
from tdl_bot.tdl import Tdl
from tdl_bot.telegram import TelegramMedia

_LOGGER = logging.getLogger(__name__)
_HELP = (
    "发送一个 Telegram 消息 url，Bot 会处理整个 media group 和视频文字。\n"
    "/target <chat> [topic_id] 设置目标聊天／频道和话题\n"
    "示例：/target -1001234567890 42；/target me 为收藏夹\n"
    "/join <群 url> 加入来源群并重试\n"
    "/retry 重试上次无权限的 url\n"
    "/status 查看目标和任务状态\n"
    "/update_tdl [latest|v0.20.4] 更新 tdl（管理员）\n"
    "/whoami 查看自己的用户 ID"
)


class ProgressMessages:
    """Create one reply per job and edit it through all subsequent stages."""

    def __init__(self, bot: Bot) -> None:
        """Use the same bot for initial replies and progress edits."""
        self.bot = bot

    async def __call__(
        self, job: Job, text: str, *, finished: bool = False
    ) -> None:
        """Remove cancellation controls on every terminal state."""
        markup = (
            None
            if finished
            else InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="取消", callback_data=f"cancel:{job.id}"
                        )
                    ]
                ]
            )
        )
        if job.progress_message_id is None:
            reply = (
                ReplyParameters(message_id=job.source_message_id)
                if job.source_message_id is not None
                else None
            )
            message = await self.bot.send_message(
                chat_id=job.user_id,
                text=text,
                reply_parameters=reply,
                reply_markup=markup,
            )
            job.progress_message_id = message.message_id
        else:
            await self.bot.edit_message_text(
                chat_id=job.user_id,
                message_id=job.progress_message_id,
                text=text,
                reply_markup=markup,
            )


class Authorization(BaseMiddleware):
    """Only allowlisted users in private chats may control the user account."""

    def __init__(self, allowed: frozenset[int]) -> None:
        """Store the configured allowlist."""
        self.allowed = allowed

    async def __call__(self, handler, event: Message, data: dict[str, Any]):
        """Reject unauthorized messages before running any command."""
        if event.chat.type != "private" or event.from_user is None:
            return None
        command = (event.text or "").split(maxsplit=1)[0:1]
        if command and command[0].split("@")[0] == "/whoami":
            return await handler(event, data)
        if event.from_user.id not in self.allowed:
            await event.answer("当前用户未获授权。使用 /whoami 获取用户 ID。")
            return None
        return await handler(event, data)


class BotApplication:
    """Own routing and update tasks that must finish during shutdown."""

    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        store: Store,
        media: TelegramMedia,
        tdl: Tdl,
        service: TransferService,
    ) -> None:
        """Wire command handlers to the transfer services."""
        self.bot = bot
        self.settings = settings
        self.store = store
        self.media = media
        self.tdl = tdl
        self.service = service
        self._update: asyncio.Task | None = None
        router = Router()
        router.message.outer_middleware(Authorization(settings.allowed_users))
        router.message.register(self.help, Command("start", "help"))
        router.message.register(self.whoami, Command("whoami"))
        router.message.register(self.target, Command("target"))
        router.message.register(self.status, Command("status"))
        router.message.register(self.join, Command("join"))
        router.message.register(self.retry, Command("retry"))
        router.message.register(self.update, Command("update_tdl"))
        router.message.register(self.url, F.text)
        router.callback_query.register(
            self.cancel, F.data.startswith("cancel:")
        )
        router.errors.register(self.error)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(router)

    async def close(self) -> None:
        """Wait for an already authorized binary update to complete."""
        if self._update:
            await self._update

    async def help(self, message: Message) -> None:
        """Show the command reference."""
        await message.answer(_HELP)

    async def whoami(self, message: Message) -> None:
        """Show a private-chat user's ID without account privileges."""
        assert message.from_user is not None
        await message.answer(f"你的用户 ID：{message.from_user.id}")

    async def target(self, message: Message, command: CommandObject) -> None:
        """Validate and persist a chat/topic destination for this user."""
        assert message.from_user is not None
        args = (command.args or "").split()
        if not args:
            await self.status(message)
            return
        if len(args) > 2:
            raise UserError("用法：/target <chat> [topic_id]")
        topic = None
        if len(args) == 2:
            if not args[1].isdigit() or int(args[1]) <= 0:
                raise UserError("topic_id 必须为正整数。")
            topic = int(args[1])
        target = Target(args[0], topic)
        await self.media.validate_target(target)
        self.store.set_target(message.from_user.id, target)
        await message.answer("上传目标已保存；已排队任务仍使用原来的目标。")

    async def status(self, message: Message) -> None:
        """Show the caller's current target and transfer state."""
        assert message.from_user is not None
        user_id = message.from_user.id
        target = self.store.target(user_id)
        destination = (
            f"{target.chat}，话题：{target.topic_id or '默认'}"
            if target
            else "尚未设置，请使用 /target"
        )
        state = "排队／执行中" if self.service.busy(user_id) else "空闲"
        pending = "；有待权限重试的 url" if self.store.pending(user_id) else ""
        await message.answer(f"目标：{destination}\n任务：{state}{pending}")

    async def join(self, message: Message, command: CommandObject) -> None:
        """Join a provided group url, then enqueue the pending transfer."""
        assert message.from_user is not None
        user_id = message.from_user.id
        if not self.store.pending(user_id):
            raise UserError("请先发送要下载的消息 url；无权限时再提供群 url。")
        if self.service.busy(user_id):
            raise UserError("请等待当前任务结束后再入群重试。")
        link = JoinLink.parse(command.args or "")
        try:
            await self.media.join(link)
        except JoinPending as exc:
            await message.answer(str(exc))
            return
        await self.retry(message)

    async def retry(self, message: Message) -> None:
        """Enqueue the most recent url awaiting permission."""
        assert message.from_user is not None
        user_id = message.from_user.id
        url = self.store.pending(user_id)
        if not url:
            raise UserError("没有待重试的 url，请直接发送消息 url。")
        await self.service.submit(
            user_id, url, source_message_id=message.message_id
        )

    async def cancel(self, query: CallbackQuery) -> None:
        """Only the job's owner may cancel it through its own progress reply."""
        if (
            query.from_user.id not in self.settings.allowed_users
            or query.message is None
            or query.message.chat.type != "private"
        ):
            await query.answer("无权取消此任务。", show_alert=True)
            return
        job_id = (query.data or "").removeprefix("cancel:")
        job = self.service.current_job(query.from_user.id, job_id)
        if (
            job is None
            or query.message.chat.id != job.user_id
            or query.message.message_id != job.progress_message_id
        ):
            await query.answer("任务已结束或按钮已失效。")
            return
        # Acknowledge immediately; tdl process termination/cleanup can take
        # several seconds. The terminal result edits the original reply.
        await query.answer("正在取消……")
        await self.service.cancel(query.from_user.id, job_id)

    async def update(self, message: Message, command: CommandObject) -> None:
        """Schedule an administrator-requested update without blocking input."""
        assert message.from_user is not None
        if message.from_user.id not in self.settings.admin_users:
            raise UserError("只有管理员可以更新 tdl。")
        if self._update and not self._update.done():
            raise UserError("已有 tdl 更新任务，请等待完成。")
        await message.answer("将等待执行中的任务结束后更新，期间新任务排队。")
        self._update = asyncio.create_task(
            self._perform_update(
                message.from_user.id, (command.args or "latest").strip()
            )
        )

    async def _perform_update(self, user_id: int, version: str) -> None:
        """Complete an update and report errors even if notification fails."""
        try:
            result = await self.tdl.update(version)
            text = f"tdl 更新完成：\n{result}"
        except UserError as exc:
            text = str(exc)
        except Exception as exc:
            _LOGGER.error("更新失败：%s", type(exc).__name__)
            text = "tdl 更新失败，请检查服务器配置后重试。"
        try:
            await self.bot.send_message(user_id, text)
        except Exception:
            _LOGGER.warning("无法发送 tdl 更新结果。")

    async def url(self, message: Message) -> None:
        """Treat ordinary text as one url or a pending group invitation."""
        assert message.from_user is not None
        user_id = message.from_user.id
        text = (message.text or "").strip()
        if self.store.pending(user_id):
            try:
                JoinLink.parse(text)
            except UserError:
                pass
            else:
                await self.join(
                    message, CommandObject(command="join", args=text)
                )
                return
        await self.service.submit(
            user_id, text, source_message_id=message.message_id
        )

    async def error(self, event: ErrorEvent) -> bool:
        """Translate known errors without exposing session data to users."""
        message = event.update.message
        if isinstance(event.exception, UserError):
            text = str(event.exception)
        elif isinstance(event.exception, errors.FloodWaitError):
            text = f"Telegram 限流，请 {event.exception.seconds} 秒后重试。"
        else:
            _LOGGER.error("命令失败：%s", type(event.exception).__name__)
            text = "命令失败，请检查网络、群 url 或目标权限后重试。"
        if message:
            await message.answer(text)
        return True
