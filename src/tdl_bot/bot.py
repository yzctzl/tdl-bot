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
    TelegramObject,
)
from telethon import errors

from tdl_bot.config import Settings
from tdl_bot.errors import JoinPending, UserError
from tdl_bot.links import JoinLink
from tdl_bot.service import Job, TransferService
from tdl_bot.store import Store, Target
from tdl_bot.tdl import Tdl

_LOGGER = logging.getLogger(__name__)
_HELP = (
    "发送一个 Telegram 消息 url，Bot 会处理整个 media group 和视频文字。\n"
    "/target [shared|private] <chat> [topic_id] 设置共享／个人目标\n"
    "/target shared 或 /target private 切换已保存的目标\n"
    "示例：/target -1001234567890 42；/target me 为收藏夹\n"
    "/jobs 查看可管理的任务；/cancel <任务编号> 取消\n"
    "/pending 查看待重试列表；/retry [编号] 重试\n"
    "/join <群 url> [编号] 使用失败任务的账号入群并重试\n"
    "/status 查看目标、执行账号与工作空间状态\n"
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

    async def __call__(
        self, handler, event: TelegramObject, data: dict[str, Any]
    ):
        """Reject unauthorized messages before running any command."""
        if not isinstance(event, Message):
            return None
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
        tdl: Tdl,
        service: TransferService,
    ) -> None:
        """Wire command handlers to the transfer services."""
        self.bot = bot
        self.settings = settings
        self.store = store
        self.tdl = tdl
        self.service = service
        self._update: asyncio.Task | None = None
        self._controls: dict[tuple[int, int], set[str]] = {}
        router = Router()
        router.message.outer_middleware(Authorization(settings.allowed_users))
        router.message.register(self.help, Command("start", "help"))
        router.message.register(self.whoami, Command("whoami"))
        router.message.register(self.target, Command("target"))
        router.message.register(self.status, Command("status"))
        router.message.register(self.join, Command("join"))
        router.message.register(self.retry, Command("retry"))
        router.message.register(self.jobs, Command("jobs"))
        router.message.register(self.pending, Command("pending"))
        router.message.register(self.cancel_command, Command("cancel"))
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
        """Set/select shared targets or personal overrides in one workspace."""
        assert message.from_user is not None
        operator = message.from_user.id
        args = (command.args or "").split()
        if not args:
            await self.status(message)
            return
        owner = operator
        private = False
        if args[0] == "for":
            if operator not in self.settings.admin_users:
                raise UserError("只有管理员可以设置其他账号的个人目标。")
            if len(args) < 2 or not args[1].isdigit():
                raise UserError(
                    "用法：/target for <操作账号 ID> <chat> [topic_id]"
                )
            owner = int(args[1])
            if owner not in self.settings.allowed_users:
                raise UserError("目标操作账号不在授权名单中。")
            if len(args) == 2:
                target = self.store.target_for(owner)
                await message.answer(
                    f"该账号的个人目标：{target.chat}，"
                    f"话题：{target.topic_id or '默认'}"
                    if target
                    else "该账号尚未设置个人目标。"
                )
                return
            private = True
            args = args[2:]
        elif args[0] in {"shared", "private"}:
            private = args.pop(0) == "private"
            if not args:
                self.store.select_target(operator, private=private)
                await self.status(message)
                return
        if len(args) not in (1, 2):
            raise UserError("用法：/target [shared|private] <chat> [topic_id]")
        topic = None
        if len(args) == 2:
            if not args[1].isdigit() or int(args[1]) <= 0:
                raise UserError("topic_id 必须为正整数。")
            topic = int(args[1])
        target = Target(args[0], topic)
        await self.service.accounts.select(owner).media.validate_target(target)
        self.store.set_target(owner, target, private=private)
        scope = "个人" if private else "共享"
        await message.answer(
            f"{scope}上传目标已保存；已排队任务仍使用原来的目标。"
        )

    async def status(self, message: Message) -> None:
        """Show the selected target and counts visible to this operator."""
        assert message.from_user is not None
        operator = message.from_user.id
        destination = self.store.destination(operator)
        target = (
            f"{destination.target.chat}，话题："
            f"{destination.target.topic_id or '默认'}"
            if destination
            else "尚未设置，请使用 /target"
        )
        scope = "个人" if self.store.private_selected(operator) else "共享"
        executor = self.service.accounts.select(operator)
        account = "自己的执行账号" if executor.owner_id else "共享执行账号"
        await message.answer(
            f"当前目标（{scope}）：{target}\n执行账号：{account}\n"
            f"可管理任务：{len(self.service.jobs(operator))}；"
            f"待重试：{len(self.service.pending_items(operator))}\n"
            "使用 /jobs 管理任务，/pending 查看待重试列表。"
        )

    @staticmethod
    def _page(args: str | None) -> int:
        if args is None or not args.strip():
            return 1
        if not args.strip().isdigit() or int(args) <= 0:
            raise UserError("页码必须为正整数。")
        return int(args)

    async def jobs(self, message: Message, command: CommandObject) -> None:
        """List manageable transfers with buttons usable across accounts."""
        assert message.from_user is not None
        operator = message.from_user.id
        jobs = self.service.jobs(operator)
        page = self._page(command.args)
        selected = jobs[(page - 1) * 10 : page * 10]
        if not selected:
            await message.answer("当前页没有可管理的任务。")
            return
        lines = [f"任务列表（第 {page} 页，共 {len(jobs)} 个）："]
        buttons = []
        for job in selected:
            scope = (
                "共享" if job.owner_id is None else f"个人（{job.owner_id}）"
            )
            lines.append(f"{job.id[:8]} · {scope} · {job.stage}")
            lines.append(
                f"目标：{job.target.chat[:80]}；"
                f"话题：{job.target.topic_id or '默认'}\n{job.url[:120]}"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"取消 {job.id[:8]}",
                        callback_data=f"cancel:{job.id}",
                    )
                ]
            )
        reply = await message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        self._controls[(operator, reply.message_id)] = {
            job.id for job in selected
        }
        # Keep only recent controls; stale task IDs can never cancel new jobs.
        while len(self._controls) > 128:
            del self._controls[next(iter(self._controls))]

    async def pending(self, message: Message, command: CommandObject) -> None:
        """List permission retries without exposing private records to peers."""
        assert message.from_user is not None
        items = self.service.pending_items(message.from_user.id)
        page = self._page(command.args)
        selected = items[(page - 1) * 10 : page * 10]
        if not selected:
            await message.answer("当前页没有待重试任务。")
            return
        lines = [f"待重试列表（第 {page} 页，共 {len(items)} 个）："]
        for item in selected:
            scope = (
                "共享"
                if item.destination.owner_id is None
                else f"个人（{item.destination.owner_id}）"
            )
            account = (
                "共享账号" if item.executor_key == "shared" else "专属账号"
            )
            lines.append(f"{item.id} · {scope} · {account} · {item.url[:180]}")
            lines.append(
                f"目标：{item.destination.target.chat[:80]}；"
                f"话题：{item.destination.target.topic_id or '默认'}"
            )
        lines.append("使用 /retry <编号> 或 /join <群 url> <编号>。")
        await message.answer("\n".join(lines))

    @staticmethod
    def _pending_id(value: str | None) -> int | None:
        if not value:
            return None
        if not value.isdigit() or int(value) <= 0:
            raise UserError("待重试编号必须为正整数，请使用 /pending 查看。")
        return int(value)

    async def join(self, message: Message, command: CommandObject) -> None:
        """Join with the failed task's executor, then retry its snapshot."""
        assert message.from_user is not None
        args = (command.args or "").split()
        if len(args) not in (1, 2):
            raise UserError("用法：/join <群 url> [待重试编号]")
        operator = message.from_user.id
        item = self.service.pending_record(
            operator, self._pending_id(args[1] if len(args) == 2 else None)
        )
        executor = self.service.accounts.get(item.executor_key)
        link = JoinLink.parse(args[0])
        try:
            # Joining cannot race binary replacement on another account.
            async with executor.tdl.gate.job():
                await executor.media.join(link)
        except JoinPending as exc:
            await message.answer(str(exc))
            return
        await self.service.retry(
            operator, item.id, source_message_id=message.message_id
        )

    async def retry(self, message: Message, command: CommandObject) -> None:
        assert message.from_user is not None
        await self.service.retry(
            message.from_user.id,
            self._pending_id((command.args or "").strip()),
            source_message_id=message.message_id,
        )

    async def cancel_command(
        self, message: Message, command: CommandObject
    ) -> None:
        assert message.from_user is not None
        job_id = (command.args or "").strip()
        if not self.service.current_job(message.from_user.id, job_id):
            raise UserError("任务不存在或无权管理，请使用 /jobs 查看任务编号。")
        reply = await message.answer("正在取消并清理……")
        await self.service.cancel(message.from_user.id, job_id)
        await reply.edit_text("取消处理完成；原任务进度消息已更新。")

    async def cancel(self, query: CallbackQuery) -> None:
        """Check management permissions and validate the button source."""
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
            or query.message.chat.id != query.from_user.id
            or not (
                (
                    query.from_user.id == job.user_id
                    and query.message.message_id == job.progress_message_id
                )
                or job.id
                in self._controls.get(
                    (query.from_user.id, query.message.message_id), set()
                )
            )
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
        if self.service.pending_items(user_id):
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
