"""aiogram command handlers and authorization."""

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
    TelegramObject,
)
from telethon import errors

from tdl_bot.accounts import AccessPolicy
from tdl_bot.config import Settings
from tdl_bot.errors import JoinPending, UserError
from tdl_bot.links import JoinLink
from tdl_bot.service import Job, TransferService
from tdl_bot.store import PendingTransfer, Store, Target
from tdl_bot.tdl import Tdl

_LOGGER = logging.getLogger(__name__)
# A single registry drives both routing and Telegram's command hints.
_COMMANDS = (
    ("start", "help", "开始使用", False),
    ("help", "help", "查看用法", False),
    ("whoami", "whoami", "查看自己的账号 ID", False),
    ("target", "target", "设置个人目标或切换目标", False),
    ("status", "status", "查看目标与任务状态", False),
    ("jobs", "jobs", "查看可管理的任务", False),
    ("cancel", "cancel_command", "取消活动任务：任务编号", False),
    ("pending", "pending", "查看待重试列表", False),
    ("retry", "retry", "重试：待重试编号", False),
    ("retry_cancel", "retry_cancel", "移除待重试记录：编号", False),
    ("join", "join", "加入来源群：群链接 待重试编号", False),
    ("update_tdl", "update", "检查并更新 tdl", True),
    ("users", "users", "查看成员与封禁状态", True),
    ("user_add", "user_add", "添加普通账号：UID/@用户名", True),
    ("user_remove", "user_remove", "删除账号：UID/@用户名", True),
    ("user_ban", "user_ban", "封禁账号：UID/@用户名 1d/1w/1m", True),
    ("user_unban", "user_unban", "解除封禁：UID/@用户名", True),
    ("user_role", "user_role", "设置角色：UID/@用户名 user/admin", True),
    ("user_history", "user_history", "查看账号管理记录：UID/@用户名", True),
)
_HELP = (
    "发送 Telegram 消息 url，Bot 会处理整个媒体组和视频文字\n"
    "/target private <chat> [topic_id] 设置个人目标\n"
    "/target shared 或 /target private 切换已保存目标\n"
    "/status 查看所有已保存目标与当前目标\n"
    "/jobs 查看活动任务，/cancel <编号> 取消\n"
    "/pending 查看待重试记录，/retry [编号] 重试\n"
    "/retry_cancel <编号> 删除待重试记录\n"
    "/join <群 url> [编号] 入群后重试\n"
    "普通账号最多保存 3 条待重试记录。\n"
    "/status 查看状态，/whoami 查看自己的账号 ID"
)
_ADMIN_HELP = (
    "\n\n管理员：\n"
    "/target shared <chat> [topic_id] 设置共享目标\n"
    "/target for <UID或@用户名> [chat] [topic_id] 管理个人目标\n"
    "/users 查看成员\n"
    "/user_add <UID或@用户名> 添加普通账号\n"
    "/user_remove <UID或@用户名> 删除账号\n"
    "/user_ban <UID或@用户名> <1d|1w|1m> 封禁一天／周／30天\n"
    "/user_unban <UID或@用户名> 解封\n"
    "/user_role <UID或@用户名> <user|admin> 设置角色\n"
    "/user_history <UID或@用户名> 查看管理记录\n"
    "/status <UID或@用户名> 查看其他账号的所有目标\n"
    "/update_tdl [latest|v0.20.4] 检查并更新 tdl"
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

    def __init__(self, policy: AccessPolicy, refresh_menu=None) -> None:
        """Check current membership instead of caching startup identities."""
        self.policy = policy
        self.refresh_menu = refresh_menu

    async def __call__(
        self, handler, event: TelegramObject, data: dict[str, Any]
    ):
        """Reject unauthorized messages before running any command."""
        if not isinstance(event, Message):
            return None
        if event.chat.type != "private" or event.from_user is None:
            return None
        if self.policy.store is not None:
            self.policy.store.remember_user(
                event.from_user.id,
                getattr(event.from_user, "username", None),
                getattr(event.from_user, "full_name", ""),
            )
        command = (event.text or "").split(maxsplit=1)[0:1]
        if command and command[0].split("@")[0] == "/whoami":
            return await handler(event, data)
        if not self.policy.is_allowed(event.from_user.id):
            await event.answer(
                "当前账号未获授权或处于封禁中。使用 /whoami 获取账号 ID。"
            )
            return None
        if self.refresh_menu:
            await self.refresh_menu(event.from_user.id)
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
        self.policy = service.policy
        self._menu_roles: dict[int, str] = {}
        self._menu_lock = asyncio.Lock()
        self._update: asyncio.Task | None = None
        self._controls: dict[tuple[int, int], set[str]] = {}
        router = Router()
        router.message.outer_middleware(
            Authorization(self.policy, self.refresh_menu)
        )
        for name, handler, _, _ in _COMMANDS:
            router.message.register(getattr(self, handler), Command(name))
        router.message.register(self.url, F.text)
        router.callback_query.register(
            self.cancel, F.data.startswith("cancel:")
        )
        router.callback_query.register(
            self.cancel_retry, F.data.startswith("retry_cancel:")
        )
        router.errors.register(self.error)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(router)

    @staticmethod
    def command_menu(*, admin: bool = False, allowed: bool = True):
        return [
            BotCommand(command=name, description=description)
            for name, _, description, admin_only in _COMMANDS
            if (allowed and (admin or not admin_only)) or name == "whoami"
        ]

    async def register_commands(self) -> None:
        """Install ordinary hints and separate administrator menus."""
        await self.bot.set_my_commands(
            self.command_menu(allowed=False), scope=BotCommandScopeDefault()
        )
        await self.bot.set_my_commands(
            self.command_menu(), scope=BotCommandScopeAllPrivateChats()
        )
        for operator in self.policy.menu_ids:
            await self.refresh_menu(operator)

    async def refresh_menu(self, operator_id: int) -> None:
        async with self._menu_lock:
            role = (
                "admin"
                if self.policy.is_admin(operator_id)
                else "user"
                if self.policy.is_allowed(operator_id)
                else "denied"
            )
            if self._menu_roles.get(operator_id) == role:
                return
            try:
                await self.bot.set_my_commands(
                    self.command_menu(
                        admin=role == "admin", allowed=role != "denied"
                    ),
                    scope=BotCommandScopeChat(chat_id=operator_id),
                )
            except Exception:
                # The member may not have opened a private bot chat yet.
                # Retry on their next message; permissions stay current.
                _LOGGER.warning("无法同步账号命令菜单。")
            else:
                self._menu_roles[operator_id] = role

    async def close(self) -> None:
        """Wait for an already authorized binary update to complete."""
        if self._update:
            await self._update

    async def help(self, message: Message) -> None:
        """Show the command reference."""
        assert message.from_user is not None
        text = _HELP
        if self.policy.is_admin(message.from_user.id):
            text += _ADMIN_HELP
        await message.answer(text)

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
        private = not self.policy.is_admin(operator)
        if args[0] == "for":
            if not self.policy.is_admin(operator):
                raise UserError("只有管理员可以设置其他账号的个人目标。")
            if len(args) < 2:
                raise UserError(
                    "用法：/target for <UID或@用户名> <chat> [topic_id]"
                )
            owner = await self._resolve_user(args[1])
            if not self.policy.is_admin(operator):
                raise UserError("只有管理员可以查看其他账号的目标。")
            if (
                not self.policy.is_known(owner)
                and self.store.target_for(owner) is None
            ):
                raise UserError("目标账号没有成员或历史个人目标记录。")
            if len(args) == 2:
                target = self.store.target_for(owner)
                await message.answer(
                    f"{self.store.user_label(owner)} 的个人目标："
                    f"{target.chat}，"
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
        if not private and not self.policy.is_admin(operator):
            raise UserError("只有管理员可以设置或修改共享目标。")
        target = Target(args[0], topic)
        await self.service.accounts.select(owner).media.validate_target(target)
        # Re-check after the network await, in case membership was revoked.
        if not self.policy.can_manage(operator, owner if private else None):
            raise UserError("当前账号无权修改目标。")
        if not private and not self.policy.is_admin(operator):
            raise UserError("只有管理员可以修改共享目标。")
        self.store.set_target(owner, target, private=private)
        scope = "个人" if private else "共享"
        await message.answer(
            f"{scope}上传目标已保存；已排队任务仍使用原来的目标。"
        )

    async def status(
        self, message: Message, command: CommandObject | None = None
    ) -> None:
        """Show the selected target and counts visible to this operator."""
        assert message.from_user is not None
        actor = message.from_user.id
        operator = actor
        if command and command.args:
            if not self.policy.is_admin(actor):
                raise UserError("只有管理员可以查看其他账号的目标。")
            operator = await self._resolve_user(command.args.strip())
            if not self.policy.is_admin(actor):
                raise UserError("只有管理员可以查看其他账号的目标。")
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
        quota = (
            "不限"
            if self.policy.is_admin(operator)
            else f"{self.store.pending_count(operator)}/3"
        )
        await message.answer(
            f"账号：{self.store.user_label(operator)}\n"
            f"共享目标：{self._target_label(self.store.target_for(None))}\n"
            f"个人目标：{self._target_label(self.store.target_for(operator))}\n"
            f"当前目标（{scope}）：{target}\n执行账号：{account}\n"
            f"可管理任务：{len(self.service.jobs(operator))}；"
            f"待重试：{len(self.service.pending_items(operator))}\n"
            f"自己的待重试名额：{quota}\n"
            "使用 /jobs 管理任务，/pending 查看待重试列表。"
        )

    @staticmethod
    def _target_label(target: Target | None) -> str:
        if target is None:
            return "尚未设置"
        return f"{target.chat}；话题：{target.topic_id or '默认'}"

    async def _resolve_user(self, value: str) -> int:
        if not value.startswith("@"):
            user_id = self._user_id(value)
            await self._refresh_profile(user_id)
            return user_id
        # Resolve live: old usernames must never grant privileges to a new UID.
        media = self.service.accounts.shared.media
        async with self.tdl.gate.job():
            entity = await media.resolve_user(value)
        self.store.remember_user(
            entity.id,
            entity.username,
            " ".join(filter(None, [entity.first_name, entity.last_name])),
        )
        return entity.id

    async def _refresh_profile(self, user_id: int) -> None:
        media = self.service.accounts.shared.media
        if not hasattr(media, "resolve_user"):
            return
        try:
            async with self.tdl.gate.job():
                entity = await media.resolve_user(user_id)
        except (UserError, errors.RPCError, ValueError):
            return
        if entity.id == user_id:
            self.store.remember_user(
                entity.id,
                entity.username,
                " ".join(filter(None, [entity.first_name, entity.last_name])),
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

    @staticmethod
    def _pending_lines(item: PendingTransfer) -> list[str]:
        lines = []
        scope = (
            "共享"
            if item.destination.owner_id is None
            else f"个人（{item.destination.owner_id}）"
        )
        account = "共享账号" if item.executor_key == "shared" else "专属账号"
        archive = " · 归档（仅管理员）" if item.archived else ""
        lines.append(
            f"{item.id} · {scope} · {account}{archive} · {item.url[:180]}"
        )
        lines.append(
            f"目标：{item.destination.target.chat[:80]}；"
            f"话题：{item.destination.target.topic_id or '默认'}"
        )
        return lines

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
        buttons = []
        for item in selected:
            lines.extend(self._pending_lines(item))
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"移除待重试 {item.id}",
                        callback_data=f"retry_cancel:{item.id}",
                    )
                ]
            )
        lines.append("使用 /retry <编号> 或 /join <群 url> <编号>。")
        reply = await message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        self._controls[(message.from_user.id, reply.message_id)] = {
            f"retry:{item.id}" for item in selected
        }
        while len(self._controls) > 128:
            del self._controls[next(iter(self._controls))]

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
            not self.policy.is_allowed(query.from_user.id)
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

    @staticmethod
    def _user_id(value: str) -> int:
        if not value.isdigit() or int(value) <= 0:
            raise UserError("账号 ID 必须为正整数。")
        return int(value)

    def _admin_args(self, message: Message, command: CommandObject, count: int):
        assert message.from_user is not None
        if not self.policy.is_admin(message.from_user.id):
            raise UserError("只有管理员可以管理账号。")
        args = (command.args or "").split()
        if len(args) != count:
            raise UserError(
                f"/{command.command} 需要 {count} 个参数，请查看 /help。"
            )
        return message.from_user.id, args

    async def retry_cancel(
        self, message: Message, command: CommandObject
    ) -> None:
        assert message.from_user is not None
        pending_id = self._pending_id((command.args or "").strip())
        if pending_id is None:
            raise UserError("用法：/retry_cancel <待重试编号>")
        self.service.discard_pending(message.from_user.id, pending_id)
        await message.answer("待重试记录已移除；不会执行入群、下载或上传。")

    async def cancel_retry(self, query: CallbackQuery) -> None:
        if (
            not self.policy.is_allowed(query.from_user.id)
            or query.message is None
            or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
        ):
            await query.answer("无权管理此记录。", show_alert=True)
            return
        try:
            pending_id = self._pending_id(
                (query.data or "").removeprefix("retry_cancel:")
            )
            controls = self._controls.get(
                (query.from_user.id, query.message.message_id), set()
            )
            if pending_id is None or f"retry:{pending_id}" not in controls:
                raise UserError("按钮已失效，请重新使用 /pending。")
            self.service.discard_pending(query.from_user.id, pending_id)
        except UserError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        controls.discard(f"retry:{pending_id}")
        await query.answer("待重试记录已移除。")
        remaining = [
            item
            for item in self.service.pending_items(query.from_user.id)
            if f"retry:{item.id}" in controls
        ]
        controls.intersection_update(f"retry:{item.id}" for item in remaining)
        markup = (
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"移除待重试 {item.id}",
                            callback_data=f"retry_cancel:{item.id}",
                        )
                    ]
                    for item in remaining
                ]
            )
            if remaining
            else None
        )
        lines = [f"已移除待重试记录 {pending_id}。"]
        for item in remaining:
            lines.extend(self._pending_lines(item))
        if not remaining:
            lines.append("此列表已没有待重试记录。")
        try:
            await self.bot.edit_message_text(
                chat_id=query.from_user.id,
                message_id=query.message.message_id,
                text="\n".join(lines),
                reply_markup=markup,
            )
        except Exception:
            _LOGGER.warning("记录已移除，但无法刷新列表。")

    @staticmethod
    def _deadline(timestamp: float) -> str:
        return datetime.fromtimestamp(
            timestamp, ZoneInfo("Asia/Tokyo")
        ).strftime("%Y-%m-%d %H:%M JST")

    async def users(self, message: Message, command: CommandObject) -> None:
        assert message.from_user is not None
        if not self.policy.is_admin(message.from_user.id):
            raise UserError("只有管理员可以查看成员列表。")
        members = self.store.members()
        page = self._page(command.args)
        selected = members[(page - 1) * 10 : page * 10]
        if not selected:
            await message.answer("当前页没有成员。")
            return
        lines = [f"成员列表（第 {page} 页，共 {len(members)} 个）："]
        for member in selected:
            role = "管理员" if member.role == "admin" else "普通账号"
            state = (
                "正常"
                if member.active(self.store.now())
                else (f"封禁至 {self._deadline(member.banned_until)}")
            )
            await self._refresh_profile(member.user_id)
            lines.append(
                f"{self.store.user_label(member.user_id)} · {role} · {state}"
            )
            lines.append(
                f"当前目标：{self._target_label(self.store.target(member.user_id))}"
            )
        if not self.policy.is_admin(message.from_user.id):
            raise UserError("只有管理员可以查看成员列表。")
        await message.answer("\n".join(lines))

    async def user_add(self, message: Message, command: CommandObject) -> None:
        actor, args = self._admin_args(message, command, 1)
        user_id = await self._resolve_user(args[0])
        self.store.add_member(actor, user_id)
        await self.refresh_menu(user_id)
        await message.answer(
            f"{self.store.user_label(user_id)}：普通账号已添加，权限已生效。"
        )

    async def user_remove(
        self, message: Message, command: CommandObject
    ) -> None:
        actor, args = self._admin_args(message, command, 1)
        user_id = await self._resolve_user(args[0])
        self.store.remove_member(actor, user_id)
        await self.service.cancel_submitted(actor, user_id)
        await self.refresh_menu(user_id)
        await message.answer(
            f"{self.store.user_label(user_id)}：账号已删除，活动任务已取消并清理。"
            "历史目标和重试记录保留给管理员。"
        )

    async def user_ban(self, message: Message, command: CommandObject) -> None:
        actor, args = self._admin_args(message, command, 2)
        durations = {"1d": 86400, "1w": 7 * 86400, "1m": 30 * 86400}
        duration = durations.get(args[1])
        if duration is None:
            raise UserError("封禁时长请使用 1d、1w 或 1m（30 天）。")
        user_id = await self._resolve_user(args[0])
        self.store.ban_member(actor, user_id, duration)
        await self.service.cancel_submitted(actor, user_id)
        await self.refresh_menu(user_id)
        await message.answer(
            f"{self.store.user_label(user_id)}：封禁已生效，活动任务已取消并清理；到期自动恢复权限。"
        )

    async def user_unban(
        self, message: Message, command: CommandObject
    ) -> None:
        actor, args = self._admin_args(message, command, 1)
        user_id = await self._resolve_user(args[0])
        self.store.unban_member(actor, user_id)
        await self.refresh_menu(user_id)
        await message.answer(f"{self.store.user_label(user_id)}：封禁已解除。")

    async def user_role(self, message: Message, command: CommandObject) -> None:
        actor, args = self._admin_args(message, command, 2)
        user_id = await self._resolve_user(args[0])
        self.store.set_member_role(actor, user_id, args[1])
        await self.service.reconcile_permissions()
        await self.refresh_menu(user_id)
        await message.answer(
            f"{self.store.user_label(user_id)}：账号角色已更新。"
        )

    async def user_history(
        self, message: Message, command: CommandObject
    ) -> None:
        actor, args = self._admin_args(message, command, 1)
        user_id = await self._resolve_user(args[0])
        rows = self.store.member_history(actor, user_id)
        names = {
            "add": "添加普通账号",
            "remove": "删除账号",
            "unban": "解除封禁",
            "role:user": "设为普通账号",
            "role:admin": "设为管理员",
            "ban:86400": "封禁一天",
            "ban:604800": "封禁一周",
            "ban:2592000": "封禁 30 天",
        }
        text = "\n".join(
            f"{self._deadline(created)} · {self.store.user_label(admin)} · "
            f"{names.get(action, action)}"
            for admin, action, created in rows
        )
        await message.answer(
            f"{self.store.user_label(user_id)}\n"
            + (text or "没有账号管理记录。")
        )

    async def update(self, message: Message, command: CommandObject) -> None:
        """Schedule an administrator-requested update without blocking input."""
        assert message.from_user is not None
        if not self.policy.is_admin(message.from_user.id):
            raise UserError("只有管理员可以更新 tdl。")
        if self._update and not self._update.done():
            raise UserError("已有 tdl 更新任务，请等待完成。")
        await message.answer(
            "正在检查 tdl 版本；需要更新时将等待活动任务结束。"
        )
        self._update = asyncio.create_task(
            self._perform_update(
                message.from_user.id, (command.args or "latest").strip()
            )
        )

    async def _perform_update(self, user_id: int, version: str) -> None:
        """Complete an update and report errors even if notification fails."""
        try:
            result = await self.tdl.update(version)
            text = (
                result
                if result.startswith("无需更新")
                else f"tdl 更新完成：\n{result}"
            )
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
