"""Members commands and their related validation."""

from dataclasses import replace
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram.filters import CommandObject
from aiogram.types import (
    LinkPreviewOptions,
    Message,
)
from telethon import errors

from tdl_bot.bot.commands import USER_HELP
from tdl_bot.bot.context import CommandGroup
from tdl_bot.bot.formatting import page_number
from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import localized, tr


class MemberCommands(CommandGroup):
    @localized
    async def user(self, message: Message, command: CommandObject) -> None:
        assert message.from_user is not None
        if not self.policy.is_admin(message.from_user.id):
            raise UserError(tr("Only administrators can manage accounts."))
        parts = (command.args or "").split(maxsplit=1)
        if not parts or parts[0] == "help":
            await message.answer(tr(USER_HELP))
            return
        action = parts[0]
        handlers = {
            "list": self.users,
            "add": self.user_add,
            "remove": self.user_remove,
            "ban": self.user_ban,
            "unban": self.user_unban,
            "role": self.user_role,
            "history": self.user_history,
            "follow": self.user_follow,
        }
        handler = handlers.get(action)
        if handler is None:
            raise UserError(tr("Unknown user operation. Use /user for help."))
        await handler(
            message,
            replace(
                command,
                command=f"user {action}",
                args=parts[1] if len(parts) > 1 else None,
            ),
        )

    async def resolve_user(self, value: str) -> int:
        if not value.startswith("@"):
            user_id = self._user_id(value)
            await self.refresh_profile(user_id)
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

    async def refresh_profile(self, user_id: int) -> None:
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
        page = page_number(command.args)
        selected = members[(page - 1) * 10 : page * 10]
        if not selected:
            await message.answer(tr("No members on this page."))
            return
        lines = [
            tr("Members (page {p0}, {p1} total):", p0=page, p1=len(members))
        ]
        counts = {}
        for task in self.store.backups.all():
            if task.kind == "follow" and task.state in {"active", "paused"}:
                counts[task.owner] = counts.get(task.owner, 0) + 1
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
            await self.refresh_profile(member.user_id)
            label = escape(self.store.user_label(member.user_id))
            lines.append(f"{label} · {role} · {state}")
            target_link = await self.app.destinations.target_link(
                self.store.recipient(member.user_id).target, member.user_id
            )
            lines.append(tr("Current destination: {p0}", p0=target_link))
            limit = (
                tr("Unlimited")
                if member.role == "admin"
                else self.store.follow_limit(member.user_id)
            )
            lines.append(
                tr(
                    "Follow slots: {used}/{limit} (active and paused).",
                    used=counts.get(member.user_id, 0),
                    limit=limit,
                )
            )
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
        user_id = await self.resolve_user(args[0])
        self.store.add_member(actor, user_id)
        await self.app.refresh_menu(user_id)
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
        user_id = await self.resolve_user(args[0])
        self.store.remove_member(actor, user_id)
        await self.service.cancel_submitted(actor, user_id)
        await self.app.refresh_menu(user_id)
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
        user_id = await self.resolve_user(args[0])
        self.store.ban_member(actor, user_id, duration)
        await self.service.cancel_submitted(actor, user_id)
        await self.app.refresh_menu(user_id)
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
        user_id = await self.resolve_user(args[0])
        self.store.unban_member(actor, user_id)
        await self.app.refresh_menu(user_id)
        await message.answer(
            tr("{p0}: Account unbanned.", p0=self.store.user_label(user_id))
        )

    @localized
    async def user_role(self, message: Message, command: CommandObject) -> None:
        actor, args = self._admin_args(message, command, 2)
        user_id = await self.resolve_user(args[0])
        self.store.set_member_role(actor, user_id, args[1])
        await self.service.reconcile_permissions()
        await self.app.refresh_menu(user_id)
        await message.answer(
            tr("{p0}: Account role updated.", p0=self.store.user_label(user_id))
        )

    @localized
    async def user_follow(self, message: Message, command: CommandObject):
        actor, args = self._admin_args(message, command, 2)
        if not args[1].isascii() or not args[1].isdigit() or len(args[1]) > 4:
            raise UserError(tr("Follow limits must be between 0 and 1000."))
        user_id = await self.resolve_user(args[0])
        self.store.set_follow_limit(actor, user_id, int(args[1]))
        await message.answer(
            tr(
                "{user}: Follow limit set to {limit}. Existing tasks are "
                "retained; new tasks and resumes must fit the limit. "
                "Administrators are unlimited.",
                user=self.store.user_label(user_id),
                limit=int(args[1]),
            )
        )

    @localized
    async def user_history(
        self, message: Message, command: CommandObject
    ) -> None:
        actor, args = self._admin_args(message, command, 1)
        user_id = await self.resolve_user(args[0])
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
        for _, action, _ in rows:
            if action.startswith("follow_limit:"):
                names[action] = tr(
                    "Set follow limit: {limit}", limit=action.split(":", 1)[1]
                )
        text = "\n".join(
            f"{self._deadline(created)} · {self.store.user_label(admin)} · "
            f"{names.get(action, action)}"
            for admin, action, created in rows
        )
        await message.answer(
            f"{self.store.user_label(user_id)}\n"
            + (text or tr("No account management history."))
        )

    def user_link(self, operator: int) -> str:
        return (
            f'<a href="tg://user?id={operator}">'
            f"{escape(self.store.user_label(operator))}</a>"
        )
