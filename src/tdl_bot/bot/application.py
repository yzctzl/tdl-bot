"""Bot routing, onboarding, language menus and update lifecycle."""

import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
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
)
from telethon import errors

from tdl_bot.bot.commands import ADMIN_HELP, COMMANDS, HELP
from tdl_bot.bot.destinations import DestinationCommands
from tdl_bot.bot.members import MemberCommands
from tdl_bot.bot.middleware import Authorization
from tdl_bot.bot.tasks import TaskCommands
from tdl_bot.core.config import Settings
from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import CATALOGS, localized, tr, use_language
from tdl_bot.storage.store import Store

_LOGGER = logging.getLogger(__name__)


class BotApplication:
    """Compose command groups and own frontend-only routing and updates."""

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
        self.destinations = DestinationCommands(self)
        self.members = MemberCommands(self)
        self.tasks = TaskCommands(self)
        router = Router()
        router.message.outer_middleware(
            Authorization(self.policy, self.refresh_menu)
        )
        for name, handler, _, _ in COMMANDS:
            router.message.register(self.handler(handler), Command(name))
        router.message.register(self.tasks.url, F.text)
        router.callback_query.register(
            self.tasks.cancel, F.data.startswith("cancel:")
        )
        router.callback_query.register(
            self.tasks.cancel_retry, F.data.startswith("retry_cancel:")
        )
        router.callback_query.register(
            self.select_language, F.data.startswith("language:")
        )
        router.errors.register(self.error)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(router)

    def handler(self, path):
        """Bind a registry path to its owning command group."""
        group, separator, method = path.partition(".")
        return (
            getattr(getattr(self, group), method)
            if separator
            else getattr(self, group)
        )

    @staticmethod
    def command_menu(*, admin: bool = False, allowed: bool = True):
        return [
            BotCommand(command=name, description=tr(description))
            for name, _, description, admin_only in COMMANDS
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
        text = tr(HELP)
        if self.policy.is_admin(user_id):
            text += tr(ADMIN_HELP)
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
