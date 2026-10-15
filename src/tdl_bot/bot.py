"""aiogram command handlers and authorization."""

import asyncio
import logging
from datetime import datetime
from html import escape
from inspect import isawaitable
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
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
    LinkPreviewOptions,
    Message,
    ReplyParameters,
    TelegramObject,
)
from telethon import errors

from tdl_bot.accounts import AccessPolicy
from tdl_bot.config import Settings
from tdl_bot.delivery import Delivery
from tdl_bot.errors import JoinPending, UserError
from tdl_bot.i18n import CATALOGS, localized, saved_label, tr, use_language
from tdl_bot.links import JoinLink
from tdl_bot.service import Job
from tdl_bot.store import PendingTransfer, Store, Target

_LOGGER = logging.getLogger(__name__)
# A single registry drives both routing and Telegram's command hints.
_COMMANDS = (
    ("language", "language", "Choose your language", False),
    ("start", "start", "Get started", False),
    ("help", "help", "Show help", False),
    ("whoami", "whoami", "Show your account ID", False),
    (
        "target",
        "target",
        "Set destination: Bot dialogue, group, or channel",
        False,
    ),
    ("status", "status", "View destination and task status", False),
    ("jobs", "jobs", "List tasks you can manage", False),
    ("cancel", "cancel_command", "Cancel an active task: task ID", False),
    ("pending", "pending", "List pending retries", False),
    ("retry", "retry", "Retry a task: retry ID", False),
    ("retry_cancel", "retry_cancel", "Remove a pending retry: ID", False),
    ("join", "join", "Join source group: group link and retry ID", False),
    ("storage", "storage", "Configure internal storage (admin)", True),
    ("update_tdl", "update", "Check and update tdl", True),
    ("users", "users", "List members and ban status", True),
    ("user_add", "user_add", "Add a regular account: UID/@username", True),
    ("user_remove", "user_remove", "Remove an account: UID/@username", True),
    ("user_ban", "user_ban", "Ban an account: UID/@username 1d/1w/1m", True),
    ("user_unban", "user_unban", "Unban an account: UID/@username", True),
    ("user_role", "user_role", "Set role: UID/@username user/admin", True),
    (
        "user_history",
        "user_history",
        "View account history: UID/@username",
        True,
    ),
)
_HELP = (
    "Send a Telegram message URL to process its entire media group and "
    "captions.\n/target <chat> [topic_id] Set a destination group or "
    "channel\n/target bot Receive in this dialogue (default)\n/status View "
    "destination and task status\n/jobs List active tasks; /cancel <id> "
    "Cancel a task\n/pending List retries; /retry [id] Retry a "
    "task\n/retry_cancel <id> Remove a retry\n/join <group_url> [id] Join "
    "and retry\nRegular accounts can keep up to 3 pending "
    "retries.\n/status View status; /whoami View your account "
    "ID\n/language [en|zh] Choose your language"
)
_ADMIN_HELP = (
    "\n\nAdministrators:\n/storage <chat> [topic_id] Configure internal "
    "storage\n/target for <UID|@username> [chat] [topic_id] Manage a "
    "personal destination\n/users List members\n/user_add <UID|@username> "
    "Add a regular account\n/user_remove <UID|@username> Remove an "
    "account\n/user_ban <UID|@username> <1d|1w|1m> Ban for a day, week, or "
    "30 days\n/user_unban <UID|@username> Unban an account\n/user_role "
    "<UID|@username> <user|admin> Set a role\n/user_history "
    "<UID|@username> View account management history\n/status "
    "<UID|@username> View another account's destination\n/update_tdl "
    "[latest|v0.20.4] Check and update tdl"
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
                            text=tr("Cancel"), callback_data=f"cancel:{job.id}"
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
            language = (
                self.policy.store.language(event.from_user.id)
                if self.policy.store is not None
                else "en"
            )
            with use_language(language):
                await event.answer(
                    tr(
                        "This account is unauthorized or banned. Use "
                        "/whoami to get your account ID."
                    )
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
        tdl,
        service,
    ) -> None:
        """Wire command handlers to the transfer services."""
        self.bot = bot
        self.settings = settings
        self.store = store
        self.tdl = tdl
        self.service = service
        self.policy = service.policy
        self._menu_roles: dict[int, tuple[str, str]] = {}
        self._menu_lock = asyncio.Lock()
        self._update: asyncio.Task | None = None
        self._controls: dict[tuple[int, int], set[str]] = store.controls()
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
        router.callback_query.register(
            self.select_language, F.data.startswith("language:")
        )
        router.errors.register(self.error)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(router)

    @staticmethod
    def command_menu(*, admin: bool = False, allowed: bool = True):
        return [
            BotCommand(command=name, description=tr(description))
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
            language = self.store.language(operator_id)
            menu_key = (role, language)
            if self._menu_roles.get(operator_id) == menu_key:
                return
            try:
                with use_language(language):
                    await self.bot.set_my_commands(
                        self.command_menu(
                            admin=role == "admin", allowed=role != "denied"
                        ),
                        scope=BotCommandScopeChat(chat_id=operator_id),
                    )
            except Exception:
                # The member may not have opened a private bot chat yet.
                # Retry on their next message; permissions stay current.
                _LOGGER.warning("Cannot synchronize account command menu.")
            else:
                self._menu_roles[operator_id] = menu_key

    async def close(self) -> None:
        """Wait for an already authorized binary update to complete."""
        if self._update:
            await self._update

    def _help_text(self, user_id: int) -> str:
        text = tr(_HELP)
        if self.policy.is_admin(user_id):
            text += tr(_ADMIN_HELP)
        return text

    @localized
    async def start(self, message: Message) -> None:
        """Ask first-time operators to choose a language explicitly."""
        assert message.from_user is not None
        if self.store.has_language(message.from_user.id):
            await self.help(message)
            return
        with use_language("en"):
            english = tr("Welcome! Please choose your language to get started.")
        with use_language("zh"):
            chinese = tr("Welcome! Please choose your language to get started.")
        await message.answer(
            f"{english}\n\n{chinese}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=catalog["name"],
                            callback_data=f"language:{code}",
                        )
                    ]
                    for code, catalog in CATALOGS.items()
                ]
            ),
        )

    @localized
    async def select_language(self, query: CallbackQuery) -> None:
        """Only a current member can change their own private-chat language."""
        user_id = query.from_user.id
        code = (query.data or "").removeprefix("language:")
        if (
            not self.policy.is_allowed(user_id)
            or query.message is None
            or query.message.chat.type != "private"
            or query.message.chat.id != user_id
            or code not in CATALOGS
        ):
            await query.answer(
                tr("Language selection is unavailable."), show_alert=True
            )
            return
        self.store.set_language(user_id, code)
        with use_language(code):
            await query.answer(tr("Language selected."))
            await self.refresh_menu(user_id)
            # Replace the chooser so its buttons cannot be clicked again.
            await self.bot.edit_message_text(
                chat_id=user_id,
                message_id=query.message.message_id,
                text=self._help_text(user_id),
                reply_markup=None,
            )

    @localized
    async def help(self, message: Message) -> None:
        """Show the command reference."""
        assert message.from_user is not None
        await message.answer(self._help_text(message.from_user.id))

    @localized
    async def language(self, message: Message, command: CommandObject) -> None:
        """Persist a language choice and refresh this chat's command hints."""
        assert message.from_user is not None
        user_id = message.from_user.id
        code = (command.args or "").strip().lower()
        if code:
            if code not in CATALOGS:
                raise UserError(
                    tr(
                        "Unsupported language. Use /language to see "
                        "available languages."
                    )
                )
            self.store.set_language(user_id, code)
            await self.refresh_menu(user_id)
        with use_language(self.store.language(user_id)):
            name = CATALOGS[self.store.language(user_id)]["name"]
            options = "\n".join(
                f"/language {key} — {catalog['name']}"
                for key, catalog in CATALOGS.items()
            )
            await message.answer(
                tr("Language: {name}\n{options}", name=name, options=options)
            )

    @localized
    async def whoami(self, message: Message) -> None:
        """Show a private-chat user's ID without account privileges."""
        assert message.from_user is not None
        await message.answer(tr("Your user ID: {p0}", p0=message.from_user.id))

    @localized
    async def storage(self, message: Message, command: CommandObject) -> None:
        assert message.from_user is not None
        actor = message.from_user.id
        if not self.policy.is_admin(actor):
            raise UserError(
                tr("Only administrators can configure internal storage.")
            )
        args = (command.args or "").split()
        if args:
            target = self._parse_target(args)
            target = await Delivery(self.bot).validate(
                target, actor, storage=True
            )
            await self.service.accounts.shared.media.validate_target(target)
            if not self.policy.is_admin(actor):
                raise UserError(
                    tr("Only administrators can configure internal storage.")
                )
            self.store.set_target(actor, target)
        else:
            target = self.store.internal_target()
        link = (
            await self._target_link(target, actor)
            if target
            else tr("Not configured")
        )
        if not self.policy.is_admin(actor):
            raise UserError(
                tr("Only administrators can view internal storage.")
            )
        text = tr("Internal storage: {p0}", p0=link)
        if args:
            text += tr(
                "\nSaved. Already accepted tasks will keep their original "
                "location."
            )
        await message.answer(
            text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    @staticmethod
    def _parse_target(args):
        if len(args) not in (1, 2):
            raise UserError(
                tr("Provide a chat or channel and an optional topic ID.")
            )
        topic = None
        if len(args) == 2:
            if not args[1].isdigit() or int(args[1]) <= 0:
                raise UserError(tr("topic_id must be a positive integer."))
            topic = int(args[1])
        return Target(args[0], topic)

    @localized
    async def target(self, message: Message, command: CommandObject) -> None:
        """Configure only the final recipient, never the internal storage."""
        assert message.from_user is not None
        actor = message.from_user.id
        owner = actor
        args = (command.args or "").split()
        if not args:
            await self._show_target(message, owner)
            return
        if args[0] == "get":
            raise UserError(
                tr(
                    "Use /target to view your destination or /target "
                    "<group_or_channel> to set one."
                )
            )
        if args[0] == "shared":
            raise UserError(
                tr(
                    "Use /target bot or /target <group_or_channel> to set "
                    "your destination."
                )
            )
        if args[0] == "for":
            if not self.policy.is_admin(actor):
                raise UserError(
                    tr(
                        "Only administrators can set another account's "
                        "destination."
                    )
                )
            if len(args) < 2:
                raise UserError(
                    tr("Usage: /target for <UID|@username> [chat] [topic_id]")
                )
            owner = await self._resolve_user(args[1])
            if not self.policy.is_admin(actor):
                raise UserError(
                    tr(
                        "Only administrators can view another account's "
                        "receiving location."
                    )
                )
            if not self.policy.is_known(owner):
                raise UserError(tr("The target account is not a member."))
            args = args[2:]
            if not args:
                await self._show_target(message, owner)
                return
        if args[0] == "private":
            args = args[1:]
        if not args or args == ["bot"]:
            target = Target(str(owner))
        else:
            target = await Delivery(self.bot).validate(
                self._parse_target(args), owner
            )
        if not self.policy.can_manage(actor, owner):
            raise UserError(tr("You cannot change this destination."))
        self.store.set_target(owner, target, private=True)
        await self._show_target(message, owner, saved=True)

    async def _show_target(self, message: Message, owner: int, *, saved=False):
        assert message.from_user is not None
        actor = message.from_user.id
        if not self.policy.can_manage(actor, owner):
            raise UserError(tr("You cannot view this destination."))
        target = self.store.recipient(owner).target
        link = await self._target_link(target, owner)
        if not self.policy.can_manage(actor, owner):
            raise UserError(tr("You cannot view this destination."))
        text = tr("Destination: {p0}", p0=link)
        if owner != actor:
            text = tr("Account: {p0}\n", p0=self._user_link(owner)) + text
        if saved:
            text += tr(
                "\nSaved. Queued tasks will keep their original destination."
            )
        await message.answer(
            text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    @localized
    async def status(
        self, message: Message, command: CommandObject | None = None
    ) -> None:
        """Show the selected target and counts visible to this operator."""
        assert message.from_user is not None
        actor = message.from_user.id
        operator = actor
        if command and command.args:
            if not self.policy.is_admin(actor):
                raise UserError(
                    tr(
                        "Only administrators can view another account's "
                        "destination."
                    )
                )
            operator = await self._resolve_user(command.args.strip())
            if not self.policy.is_admin(actor):
                raise UserError(
                    tr(
                        "Only administrators can view another account's "
                        "destination."
                    )
                )
        target = self.store.recipient(operator).target
        executor = self.service.accounts.select(operator)
        account = (
            tr("Personal execution account")
            if executor.owner_id
            else tr("Default execution account")
        )
        quota = (
            tr("Unlimited")
            if self.policy.is_admin(operator)
            else f"{self.store.pending_count(operator)}/3"
        )
        target_link = await self._target_link(target, operator)
        # Re-check after network rendering before exposing another account.
        if operator != actor and not self.policy.is_admin(actor):
            raise UserError(
                tr(
                    "Only administrators can view another account's "
                    "destination."
                )
            )
        await message.answer(
            tr(
                (
                    "Account: {p0}\nDestination: {p1}\nExecution account: "
                    "{p2}\nManageable tasks: {p3}; pending retries: "
                    "{p4}\nYour pending retry quota: {p5}\nUse /target bot "
                    "to receive here, or /target &lt;group_or_channel&gt; "
                    "to choose another destination."
                ),
                p0=self._user_link(operator),
                p1=target_link,
                p2=account,
                p3=len(self.service.jobs(operator)),
                p4=len(self.service.pending_items(operator)),
                p5=quota,
            ),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    @staticmethod
    def _target_label(target: Target | None) -> str:
        if target is None:
            return tr("Not configured")
        return target.chat + (
            tr("; topic: {p0}", p0=target.topic_id) if target.topic_id else ""
        )

    def _user_link(self, operator: int) -> str:
        return (
            f'<a href="tg://user?id={operator}">'
            f"{escape(self.store.user_label(operator))}</a>"
        )

    async def _target_link(self, target: Target, operator: int) -> str:
        if target.chat.isdigit() and int(target.chat) != operator:
            target = Target(
                str(-(1_000_000_000_000 + int(target.chat))), target.topic_id
            )
        chat = None
        get_chat = getattr(self.bot, "get_chat", None)
        if get_chat is not None:
            try:
                peer = (
                    int(target.chat)
                    if target.chat.lstrip("-").isdigit()
                    else target.chat
                )
                chat = await get_chat(peer)
            except TelegramAPIError:
                pass
        label = getattr(chat, "title", None) or target.chat
        url = None
        username = getattr(chat, "username", None)
        if target.chat == str(operator):
            label = tr("Bot dialogue")
            username = None
            get_me = getattr(self.bot, "get_me", None)
            if get_me is not None:
                try:
                    username = (await get_me()).username
                except TelegramAPIError:
                    pass
        if username or target.chat.startswith("@"):
            url = "https://t.me/" + (username or target.chat.lstrip("@"))
            if target.topic_id:
                url += f"/{target.topic_id}"
        elif target.chat.startswith("-100"):
            channel = -int(target.chat) - 1_000_000_000_000
            url = f"https://t.me/c/{channel}"
            if target.topic_id:
                url += f"/{target.topic_id}"
        elif chat:
            url = getattr(chat, "invite_link", None)
        if target.topic_id:
            label += tr(" / Topic {p0}", p0=target.topic_id)
        return (
            f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'
            if url
            else escape(label)
        )

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
            raise UserError(tr("Page number must be a positive integer."))
        return int(args)

    @localized
    async def jobs(self, message: Message, command: CommandObject) -> None:
        """List manageable transfers with buttons usable across accounts."""
        assert message.from_user is not None
        operator = message.from_user.id
        jobs = self.service.jobs(operator)
        page = self._page(command.args)
        selected = jobs[(page - 1) * 10 : page * 10]
        if not selected:
            await message.answer(tr("No manageable tasks on this page."))
            return
        lines = [tr("Tasks (page {p0}, {p1} total):", p0=page, p1=len(jobs))]
        buttons = []
        for job in selected:
            scope = (
                tr("Shared")
                if job.owner_id is None
                else tr("Personal ({p0})", p0=job.owner_id)
            )
            lines.append(f"{job.id[:8]} · {scope} · {saved_label(job.stage)}")
            lines.append(
                tr(
                    "Destination: {p0}; topic: {p1}\n{p2}",
                    p0=job.target.chat[:80],
                    p1=job.target.topic_id or tr("Default"),
                    p2=job.url[:120],
                )
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=tr("Cancel {p0}", p0=job.id[:8]),
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
        self.store.save_controls(self._controls)

    @staticmethod
    def _pending_lines(item: PendingTransfer) -> list[str]:
        lines = []
        scope = (
            tr("Shared")
            if item.destination.owner_id is None
            else tr("Personal ({p0})", p0=item.destination.owner_id)
        )
        account = (
            tr("Shared account")
            if item.executor_key == "shared"
            else tr("Personal account")
        )
        archive = (
            tr(" · Archived (administrators only)") if item.archived else ""
        )
        lines.append(
            f"{item.id} · {scope} · {account}{archive} · {item.url[:180]}"
        )
        lines.append(
            tr(
                "Destination: {p0}; topic: {p1}",
                p0=item.destination.target.chat[:80],
                p1=item.destination.target.topic_id or tr("Default"),
            )
        )
        return lines

    @localized
    async def pending(self, message: Message, command: CommandObject) -> None:
        """List permission retries without exposing private records to peers."""
        assert message.from_user is not None
        items = self.service.pending_items(message.from_user.id)
        page = self._page(command.args)
        selected = items[(page - 1) * 10 : page * 10]
        if not selected:
            await message.answer(tr("No pending retries on this page."))
            return
        lines = [
            tr(
                "Pending retries (page {p0}, {p1} total):",
                p0=page,
                p1=len(items),
            )
        ]
        buttons = []
        for item in selected:
            lines.extend(self._pending_lines(item))
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=tr("Remove retry {p0}", p0=item.id),
                        callback_data=f"retry_cancel:{item.id}",
                    )
                ]
            )
        lines.append(tr("Use /retry <id> or /join <group_url> <id>."))
        reply = await message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        self._controls[(message.from_user.id, reply.message_id)] = {
            f"retry:{item.id}" for item in selected
        }
        while len(self._controls) > 128:
            del self._controls[next(iter(self._controls))]
        self.store.save_controls(self._controls)

    @staticmethod
    def _pending_id(value: str | None) -> int | None:
        if not value:
            return None
        if not value.isdigit() or int(value) <= 0:
            raise UserError(
                tr(
                    "Retry ID must be a positive integer. Use /pending to "
                    "view retries."
                )
            )
        return int(value)

    @localized
    async def join(self, message: Message, command: CommandObject) -> None:
        """Join with the failed task's executor, then retry its snapshot."""
        assert message.from_user is not None
        args = (command.args or "").split()
        if len(args) not in (1, 2):
            raise UserError(tr("Usage: /join <group_url> [retry_id]"))
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

    @localized
    async def retry(self, message: Message, command: CommandObject) -> None:
        assert message.from_user is not None
        await self.service.retry(
            message.from_user.id,
            self._pending_id((command.args or "").strip()),
            source_message_id=message.message_id,
        )

    @localized
    async def cancel_command(
        self, message: Message, command: CommandObject
    ) -> None:
        assert message.from_user is not None
        job_id = (command.args or "").strip()
        if not self.service.current_job(message.from_user.id, job_id):
            raise UserError(
                tr(
                    "Task not found or access denied. Use /jobs to find "
                    "the task ID."
                )
            )
        reply = await message.answer(tr("Cancelling and cleaning up…"))
        await self.service.cancel(message.from_user.id, job_id)
        await reply.edit_text(
            tr(
                "Cancellation complete. The original task progress message "
                "has been updated."
            )
        )

    @localized
    async def cancel(self, query: CallbackQuery) -> None:
        """Check management permissions and validate the button source."""
        if (
            not self.policy.is_allowed(query.from_user.id)
            or query.message is None
            or query.message.chat.type != "private"
        ):
            await query.answer(
                tr("You cannot cancel this task."), show_alert=True
            )
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
            await query.answer(tr("Task ended or button expired."))
            return
        # Acknowledge immediately; tdl process termination/cleanup can take
        # several seconds. The terminal result edits the original reply.
        await query.answer(tr("Cancelling…"))
        await self.service.cancel(query.from_user.id, job_id)

    @staticmethod
    def _user_id(value: str) -> int:
        if not value.isdigit() or int(value) <= 0:
            raise UserError(tr("Account ID must be a positive integer."))
        return int(value)

    def _admin_args(self, message: Message, command: CommandObject, count: int):
        assert message.from_user is not None
        if not self.policy.is_admin(message.from_user.id):
            raise UserError(tr("Only administrators can manage accounts."))
        args = (command.args or "").split()
        if len(args) != count:
            raise UserError(
                tr(
                    "/{p0} requires {p1} arguments. See /help.",
                    p0=command.command,
                    p1=count,
                )
            )
        return message.from_user.id, args

    @localized
    async def retry_cancel(
        self, message: Message, command: CommandObject
    ) -> None:
        assert message.from_user is not None
        pending_id = self._pending_id((command.args or "").strip())
        if pending_id is None:
            raise UserError(tr("Usage: /retry_cancel <retry_id>"))
        result = self.service.discard_pending(message.from_user.id, pending_id)
        if isawaitable(result):
            await result
        await message.answer(
            tr(
                "Pending retry removed. No joining, downloading, or "
                "uploading will occur."
            )
        )

    @localized
    async def cancel_retry(self, query: CallbackQuery) -> None:
        if (
            not self.policy.is_allowed(query.from_user.id)
            or query.message is None
            or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
        ):
            await query.answer(
                tr("You cannot manage this record."), show_alert=True
            )
            return
        try:
            pending_id = self._pending_id(
                (query.data or "").removeprefix("retry_cancel:")
            )
            controls = self._controls.get(
                (query.from_user.id, query.message.message_id), set()
            )
            if pending_id is None or f"retry:{pending_id}" not in controls:
                raise UserError(tr("Button expired. Use /pending again."))
            result = self.service.discard_pending(
                query.from_user.id, pending_id
            )
            if isawaitable(result):
                await result
        except UserError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        controls.discard(f"retry:{pending_id}")
        await query.answer(tr("Pending retry removed."))
        remaining = [
            item
            for item in self.service.pending_items(query.from_user.id)
            if f"retry:{item.id}" in controls
        ]
        controls.intersection_update(f"retry:{item.id}" for item in remaining)
        self.store.save_controls(self._controls)
        markup = (
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=tr("Remove retry {p0}", p0=item.id),
                            callback_data=f"retry_cancel:{item.id}",
                        )
                    ]
                    for item in remaining
                ]
            )
            if remaining
            else None
        )
        lines = [tr("Removed pending retry {p0}.", p0=pending_id)]
        for item in remaining:
            lines.extend(self._pending_lines(item))
        if not remaining:
            lines.append(tr("No pending retries remain in this list."))
        try:
            await self.bot.edit_message_text(
                chat_id=query.from_user.id,
                message_id=query.message.message_id,
                text="\n".join(lines),
                reply_markup=markup,
            )
        except Exception:
            _LOGGER.warning(
                "Record removed, but the list could not be refreshed."
            )

    @staticmethod
    def _deadline(timestamp: float) -> str:
        return datetime.fromtimestamp(
            timestamp, ZoneInfo("Asia/Tokyo")
        ).strftime("%Y-%m-%d %H:%M JST")

    @localized
    async def users(self, message: Message, command: CommandObject) -> None:
        assert message.from_user is not None
        if not self.policy.is_admin(message.from_user.id):
            raise UserError(tr("Only administrators can view the member list."))
        members = self.store.members()
        page = self._page(command.args)
        selected = members[(page - 1) * 10 : page * 10]
        if not selected:
            await message.answer(tr("No members on this page."))
            return
        lines = [
            tr("Members (page {p0}, {p1} total):", p0=page, p1=len(members))
        ]
        for member in selected:
            role = (
                tr("Administrator")
                if member.role == "admin"
                else tr("Regular account")
            )
            state = (
                tr("Active")
                if member.active(self.store.now())
                else (
                    tr(
                        "Banned until {p0}",
                        p0=self._deadline(member.banned_until),
                    )
                )
            )
            await self._refresh_profile(member.user_id)
            label = escape(self.store.user_label(member.user_id))
            lines.append(f"{label} · {role} · {state}")
            target_link = await self._target_link(
                self.store.recipient(member.user_id).target, member.user_id
            )
            lines.append(tr("Current destination: {p0}", p0=target_link))
        if not self.policy.is_admin(message.from_user.id):
            raise UserError(tr("Only administrators can view the member list."))
        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    @localized
    async def user_add(self, message: Message, command: CommandObject) -> None:
        actor, args = self._admin_args(message, command, 1)
        user_id = await self._resolve_user(args[0])
        self.store.add_member(actor, user_id)
        await self.refresh_menu(user_id)
        await message.answer(
            tr(
                "{p0}: Regular account added. Access is now active.",
                p0=self.store.user_label(user_id),
            )
        )

    @localized
    async def user_remove(
        self, message: Message, command: CommandObject
    ) -> None:
        actor, args = self._admin_args(message, command, 1)
        user_id = await self._resolve_user(args[0])
        self.store.remove_member(actor, user_id)
        await self.service.cancel_submitted(actor, user_id)
        await self.refresh_menu(user_id)
        await message.answer(
            tr(
                (
                    "{p0}: Account removed; active tasks cancelled and "
                    "cleaned up. Previous destinations and retries remain "
                    "available to administrators."
                ),
                p0=self.store.user_label(user_id),
            )
        )

    @localized
    async def user_ban(self, message: Message, command: CommandObject) -> None:
        actor, args = self._admin_args(message, command, 2)
        durations = {"1d": 86400, "1w": 7 * 86400, "1m": 30 * 86400}
        duration = durations.get(args[1])
        if duration is None:
            raise UserError(
                tr("Use 1d, 1w, or 1m (30 days) for the ban duration.")
            )
        user_id = await self._resolve_user(args[0])
        self.store.ban_member(actor, user_id, duration)
        await self.service.cancel_submitted(actor, user_id)
        await self.refresh_menu(user_id)
        await message.answer(
            tr(
                (
                    "{p0}: Account banned; active tasks cancelled and "
                    "cleaned up. Access will be restored automatically "
                    "when the ban expires."
                ),
                p0=self.store.user_label(user_id),
            )
        )

    @localized
    async def user_unban(
        self, message: Message, command: CommandObject
    ) -> None:
        actor, args = self._admin_args(message, command, 1)
        user_id = await self._resolve_user(args[0])
        self.store.unban_member(actor, user_id)
        await self.refresh_menu(user_id)
        await message.answer(
            tr("{p0}: Account unbanned.", p0=self.store.user_label(user_id))
        )

    @localized
    async def user_role(self, message: Message, command: CommandObject) -> None:
        actor, args = self._admin_args(message, command, 2)
        user_id = await self._resolve_user(args[0])
        self.store.set_member_role(actor, user_id, args[1])
        await self.service.reconcile_permissions()
        await self.refresh_menu(user_id)
        await message.answer(
            tr("{p0}: Account role updated.", p0=self.store.user_label(user_id))
        )

    @localized
    async def user_history(
        self, message: Message, command: CommandObject
    ) -> None:
        actor, args = self._admin_args(message, command, 1)
        user_id = await self._resolve_user(args[0])
        rows = self.store.member_history(actor, user_id)
        names = {
            "add": tr("Add regular account"),
            "remove": tr("Remove account"),
            "unban": tr("Unban account"),
            "role:user": tr("Set regular account role"),
            "role:admin": tr("Set administrator role"),
            "ban:86400": tr("Ban for one day"),
            "ban:604800": tr("Ban for one week"),
            "ban:2592000": tr("Ban for 30 days"),
        }
        text = "\n".join(
            f"{self._deadline(created)} · {self.store.user_label(admin)} · "
            f"{names.get(action, action)}"
            for admin, action, created in rows
        )
        await message.answer(
            f"{self.store.user_label(user_id)}\n"
            + (text or tr("No account management history."))
        )

    @localized
    async def update(self, message: Message, command: CommandObject) -> None:
        """Schedule an administrator-requested update without blocking input."""
        assert message.from_user is not None
        if not self.policy.is_admin(message.from_user.id):
            raise UserError(tr("Only administrators can update tdl."))
        if self._update and not self._update.done():
            raise UserError(
                tr("A tdl update is already running. Wait for it to finish.")
            )
        await message.answer(
            tr(
                "Checking the tdl version. If an update is needed, it will "
                "wait for active tasks to finish."
            )
        )
        self._update = asyncio.create_task(
            self._perform_update(
                message.from_user.id, (command.args or "latest").strip()
            )
        )

    @localized
    async def _perform_update(self, user_id: int, version: str) -> None:
        """Complete an update and report errors even if notification fails."""
        try:
            result = await self.tdl.update(version)
            text = (
                result
                if result.startswith(tr("No update needed"))
                else tr("tdl update completed:\n{p0}", p0=result)
            )
        except UserError as exc:
            text = str(exc)
        except Exception as exc:
            _LOGGER.error("Update failed: %s", type(exc).__name__)
            text = tr(
                "tdl update failed. Check the server configuration before "
                "retrying."
            )
        try:
            await self.bot.send_message(user_id, text)
        except Exception:
            _LOGGER.warning("Cannot send tdl update result.")

    @localized
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

    @localized
    async def error(self, event: ErrorEvent) -> bool:
        """Translate known errors without exposing session data to users."""
        message = event.update.message
        if isinstance(event.exception, UserError):
            text = str(event.exception)
        elif isinstance(event.exception, errors.FloodWaitError):
            text = tr(
                "Telegram rate limit. Retry in {p0} seconds.",
                p0=event.exception.seconds,
            )
        else:
            _LOGGER.error("Command failed: %s", type(event.exception).__name__)
            text = tr(
                "Command failed. Check the network, group URL, and "
                "destination permissions before retrying."
            )
        if message:
            await message.answer(text)
        return True
