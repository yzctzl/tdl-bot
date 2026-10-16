"""Private-chat menus sharing authorization and operations with commands."""

import asyncio
from collections import defaultdict
from html import escape

from aiogram.types import CallbackQuery, Message

from tdl_bot.bot.backups import duration
from tdl_bot.bot.menus.backups import BackupMenus
from tdl_bot.bot.menus.base import button
from tdl_bot.bot.menus.copy import tr
from tdl_bot.bot.menus.members import MemberMenus
from tdl_bot.bot.menus.settings import SettingsMenus
from tdl_bot.bot.menus.tasks import TaskMenus
from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import (
    CATALOGS,
    error_message,
    localized,
    render,
    use_language,
)


class Menus(TaskMenus, BackupMenus, MemberMenus, SettingsMenus):
    def __init__(self, app):
        super().__init__(app)
        self.locks = defaultdict(asyncio.Lock)

    @localized
    async def open(self, message):
        async with self.locks[message.from_user.id]:
            reply = await message.answer(
                tr(
                    "Send a Telegram message URL to process its entire media "
                    "group and captions."
                )
            )
            state = {"message": reply.message_id, "version": 0}
            await self.page(message, state, "root")

    @localized
    async def callback(self, query: CallbackQuery):
        actor = query.from_user.id
        if (
            not isinstance(query.message, Message)
            or query.message.chat.type != "private"
            or query.message.chat.id != actor
            or not self.policy.is_allowed(actor)
        ):
            await query.answer(tr("Access denied."), show_alert=True)
            return
        async with self.locks[actor]:
            state = self.store.menu(actor)
            _, version, path = (query.data or "").split(":", 2)
            if (
                not state
                or state["message"] != query.message.message_id
                or str(state["version"]) != version
            ):
                await query.answer(
                    tr("This menu has expired. Open /start again."),
                    show_alert=True,
                )
                return
            await query.answer()
            message = query.message.model_copy(
                update={"from_user": query.from_user}
            )
            state.pop("wait", None)
            # Consume the revision before a write; reconnecting must never
            # execute a duplicate click whose acknowledgement was lost.
            state["version"] += 1
            self.store.save_menu(actor, state)
            try:
                if path.startswith("confirm:"):
                    confirmation = state.pop("confirmation", None)
                    if (
                        not confirmation
                        or confirmation["token"] != path.split(":")[1]
                    ):
                        raise UserError(
                            tr("This menu has expired. Open /start again.")
                        )
                    self.store.save_menu(actor, state)
                    await self.page(message, state, confirmation["action"])
                else:
                    state.pop("confirmation", None)
                    await self.page(message, state, path)
            except UserError as exc:
                await self.show(
                    message,
                    state,
                    escape(render(error_message(exc))),
                    [[button(tr("Back"), state.get("back", "root"))]],
                )

    @localized
    async def text(self, message):
        actor = message.from_user.id
        async with self.locks[actor]:
            state = self.store.menu(actor)
            if not state or not state.get("wait"):
                await self.app.tasks.url(message)
                return
            try:
                await self.input(message, state, (message.text or "").strip())
            except UserError as exc:
                # Keep corrections in the field instead of submitting a URL job.
                self.store.save_menu(actor, state)
                await message.answer(str(exc))

    async def page(self, message, state, path):
        actor = message.from_user.id
        section = path.split(":")[0]
        if section in {
            "jobs",
            "pending",
            "job",
            "stopjob",
            "retry",
            "runretry",
            "delretry",
            "discard",
            "join",
            "joinretry",
            "dojoin",
        }:
            await self.task_page(message, state, path)
        elif section in {"follow", "dump"}:
            await self.backup_page(message, state, path)
        elif section in {
            "accounts",
            "users",
            "member",
            "useradd",
            "quota",
            "role",
            "ban",
            "userop",
            "userapply",
            "history",
        }:
            await self.member_page(message, state, path)
        elif section == "root":
            target = await self.app.destinations.target_link(
                self.store.recipient(actor).target, actor
            )
            count = sum(
                r.kind == "follow"
                and r.owner == actor
                and r.state in {"active", "paused"}
                for r in self.store.backups.all()
            )
            limit = (
                tr("Unlimited")
                if self.policy.is_admin(actor)
                else str(self.store.follow_limit(actor))
            )
            text = (
                tr(
                    "Send a Telegram message URL to process its entire "
                    "media group and captions."
                )
                + "\n\n"
                + self.app.members.user_link(actor)
            )
            text += " · " + (
                tr("Administrator")
                if self.policy.is_admin(actor)
                else tr("Regular account")
            )
            text += "\n" + tr("Destination: {p0}", p0=target)
            text += "\n" + tr(
                "Active tasks: {count}", count=len(self.service.jobs(actor))
            )
            text += "\n" + tr(
                "Pending retries: {count}",
                count=len(self.service.pending_items(actor)),
            )
            text += "\n" + tr(
                "Follow slots: {used}/{limit} (active and paused).",
                used=count,
                limit=limit,
            )
            rows = [
                [
                    button(tr("Task management"), "tasks"),
                    button(tr("Scheduled tracking"), "follow"),
                ]
            ]
            if self.policy.is_admin(actor):
                rows.append(
                    [
                        button(tr("User management"), "users:1"),
                        button(tr("Bot management"), "admin"),
                    ]
                )
            rows.append(
                [button(tr("Settings"), "settings"), button(tr("Help"), "help")]
            )
            await self.show(message, state, text, rows)
        elif section in {"tasks", "status"}:
            values = await self.app.destinations.status_values(actor, actor)
            text = (
                tr("Task management")
                + "\n\n"
                + tr(
                    "Account: {p0}\nDestination: {p1}\nExecution account: "
                    "{p2}\nManageable tasks: {p3}\nPending retries: {p4}\n"
                    "Your pending retry quota: {p5}",
                    **values,
                )
            )
            await self.show(
                message,
                state,
                text,
                [
                    [
                        button(tr("Active tasks"), "jobs"),
                        button(tr("Retry management"), "pending"),
                    ],
                    [
                        button(tr("Join"), "join"),
                    ],
                    [button(tr("Back to main menu"), "root")],
                ],
            )
        elif section == "help":
            await self.show(
                message,
                state,
                escape(self.app._help_text(actor)),
                [[button(tr("Back to main menu"), "root")]],
            )
        elif section == "settings":
            target = await self.app.destinations.target_link(
                self.store.recipient(actor).target, actor
            )
            language = CATALOGS[self.store.language(actor)]["name"]
            text = tr("Settings") + "\n\n" + tr("Destination: {p0}", p0=target)
            text += "\n" + tr("Language: {language}", language=escape(language))
            await self.show(
                message,
                state,
                text,
                [
                    [button(tr("Receiving settings"), "target")],
                    [button(tr("Language settings"), "language")],
                    [
                        button(tr("Back to main menu"), "root"),
                    ],
                ],
            )
        elif section == "language":
            rows = [
                [
                    button(
                        ("✓ " if code == self.store.language(actor) else "")
                        + catalog["name"],
                        "lang:" + code,
                    )
                ]
                for code, catalog in CATALOGS.items()
            ]
            await self.show(
                message,
                state,
                tr(
                    "Language: {language}",
                    language=escape(
                        CATALOGS[self.store.language(actor)]["name"]
                    ),
                )
                + "\n\n"
                + tr("Choose your language."),
                rows + [[button(tr("Back"), "settings")]],
            )
        elif section == "lang":
            code = path.split(":")[1]
            self.store.set_language(actor, code)
            await self.app.refresh_menu(actor)
            with use_language(code):
                await self.page(message, state, "settings")
        elif section == "target":
            owner = int(path.split(":")[1]) if ":" in path else actor
            if not self.policy.can_manage(actor, owner):
                raise UserError(tr("You cannot change this destination."))
            state["target_owner"] = owner
            target = self.store.recipient(owner).target
            link = await self.app.destinations.target_link(target, owner)
            if not self.policy.can_manage(actor, owner):
                raise UserError(tr("You cannot change this destination."))
            text = tr("Destination: {p0}", p0=link)
            if owner != actor:
                text = (
                    tr("Account: {p0}", p0=self.app.members.user_link(owner))
                    + "\n"
                    + text
                )
            rows = []
            if target.chat != str(owner):
                rows.append(
                    [button(tr("Receive in Bot dialogue"), "targetbot")]
                )
            rows += [
                [button(tr("Set group or channel"), "targetinput")],
                [
                    button(
                        tr("Back"),
                        f"member:{owner}" if owner != actor else "settings",
                    )
                ],
            ]
            await self.show(
                message,
                state,
                text + "\n\n" + tr("Changes apply to new tasks only."),
                rows,
            )
        elif section in {"targetbot", "targetinput"}:
            owner = state.get("target_owner", actor)
            back = f"target:{owner}"
            if section == "targetbot":
                await self.command(
                    message,
                    state,
                    "target",
                    f"for {owner} bot" if owner != actor else "bot",
                    back,
                )
            else:
                await self.prompt(
                    message,
                    state,
                    "target",
                    tr(
                        "Send the destination @username or ID, optionally "
                        "followed by a topic ID."
                    ),
                    back,
                )
        elif section == "admin":
            self.require_admin(actor)
            await self.show(
                message,
                state,
                await self.bot_summary(actor),
                [
                    [button(tr("History backup"), "dump")],
                    [
                        button(tr("Change storage"), "storage"),
                        button(tr("Execution account"), "execution"),
                    ],
                    [button(tr("Update tdl"), "update")],
                    [
                        button(tr("Back to main menu"), "root"),
                    ],
                ],
            )
        elif section in {
            "storage",
            "storageinput",
            "execution",
            "selectexec",
            "update",
            "version",
            "updatelatest",
            "updateapply",
        }:
            self.require_admin(actor)
            await self.admin_page(message, state, path)
        else:
            raise UserError(tr("This menu has expired. Open /start again."))

    async def input(self, message, state, text):
        field = state["wait"]
        actor = message.from_user.id
        if field == "join_link":
            await self.join_input(message, state, text)
        elif field in {"target", "storage"}:
            owner = state.get("target_owner", actor)
            args = (
                f"for {owner} {text}"
                if field == "target" and owner != actor
                else text
            )
            await self.command(
                message,
                state,
                field,
                args,
                f"target:{owner}" if field == "target" else "admin",
                present=field != "storage",
            )
            if field == "storage":
                await self.page(message, state, "admin")
        elif field == "backup_source":
            state["draft"]["source_ref"] = text
            state["draft"]["request_id"] = message.message_id
            await self.backup_page(
                message, state, state["draft"]["kind"] + ":draft"
            )
        elif field == "backup_target":
            from tdl_bot.integrations.telegram.delivery import Delivery

            target = self.app.destinations._parse_target(text.split())
            target = await Delivery(self.bot).validate(target, actor)
            state["draft"]["target"] = {
                "chat": target.chat,
                "topic_id": target.topic_id,
            }
            await self.backup_page(
                message, state, state["draft"]["kind"] + ":draft"
            )
        elif field == "backup_interval":
            await self.set_interval(message, state, duration(text))
        elif field in {"user_add", "user_quota"}:
            self.require_admin(actor)
            if field == "user_add":
                owner = await self.app.members.resolve_user(text)
                path = f"userop:add:{owner}"
            else:
                if (
                    not text.isascii()
                    or not text.isdigit()
                    or not 0 <= int(text) <= 1000
                ):
                    raise UserError(
                        tr("Follow limits must be between 0 and 1000.")
                    )
                path = f"userop:follow:{state['member']}:{int(text)}"
            await self.member_page(message, state, path)
        elif field == "version":
            await self.confirm(
                message,
                state,
                "updateapply:" + text,
                tr("Install tdl version {version}?", version=escape(text)),
                "update",
            )
        state.pop("wait", None)
        self.store.save_menu(actor, state)
