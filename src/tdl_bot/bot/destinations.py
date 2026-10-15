"""Destinations commands and their related validation."""

import logging
from html import escape

from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject
from aiogram.types import (
    LinkPreviewOptions,
    Message,
)

from tdl_bot.bot.context import CommandGroup
from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import localized, tr
from tdl_bot.integrations.telegram.delivery import Delivery
from tdl_bot.storage.models import Target

_LOGGER = logging.getLogger(__name__)


class DestinationCommands(CommandGroup):
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
            await self.target_link(target, actor)
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
            owner = await self.app.members.resolve_user(args[1])
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
        link = await self.target_link(target, owner)
        if not self.policy.can_manage(actor, owner):
            raise UserError(tr("You cannot view this destination."))
        text = tr("Destination: {p0}", p0=link)
        if owner != actor:
            text = (
                tr("Account: {p0}\n", p0=self.app.members.user_link(owner))
                + text
            )
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
            operator = await self.app.members.resolve_user(command.args.strip())
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
        target_link = await self.target_link(target, operator)
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
                p0=self.app.members.user_link(operator),
                p1=target_link,
                p2=account,
                p3=len(self.service.jobs(operator)),
                p4=len(self.service.pending_items(operator)),
                p5=quota,
            ),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def target_link(self, target: Target, operator: int) -> str:
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
