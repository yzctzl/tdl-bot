"""Dump and follow commands with bounded status pages."""

import re
from html import escape

from aiogram.filters import CommandObject
from aiogram.types import LinkPreviewOptions, Message

from tdl_bot.bot.context import CommandGroup
from tdl_bot.bot.formatting import page_number
from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import localized, saved_label, tr
from tdl_bot.storage.models import Target


def duration(value):
    match = re.fullmatch(r"([0-9]{1,9})([smhd]?)", value.lower())
    seconds = (
        int(match[1])
        * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
        if match
        else 0
    )
    if seconds < 60:
        raise UserError(
            tr("Use an interval of at least 60 seconds, such as 60s, 5m or 1h.")
        )
    return seconds


def arguments(kind, value):
    args = value.split()
    if not args:
        return {"action": "list"}
    action = args.pop(0)
    if action == "list":
        return {"action": "list", "page": page_number(" ".join(args))}
    if action in {"status", "pause", "cancel", "remove", "resume", "interval"}:
        if not args:
            raise UserError(
                tr("Provide a backup ID. Use /dump or /follow to list tasks.")
            )
        result = {
            "action": "cancel" if action == "remove" else action,
            "task_id": args.pop(0),
        }
        if action == "resume" and args:
            result["resolution"] = args.pop(0)
            if result["resolution"] not in {"retry", "skip"}:
                raise UserError(tr("Resume resolution must be retry or skip."))
        if action == "interval" and kind == "follow" and args:
            result["interval"] = duration(args.pop(0))
        elif action == "interval":
            raise UserError(tr("Usage: /follow interval <id> <60s|5m|1h>"))
        if args:
            raise UserError(tr("Unexpected backup arguments. See /help."))
        return result
    if action == "add":
        if not args:
            raise UserError(tr("Provide a channel or group to back up."))
        action = args.pop(0)
    result = {"action": "create", "source_ref": action}
    while args:
        option = args.pop(0)
        if option == "to" and args and "target" not in result:
            chat = args.pop(0)
            topic = int(args.pop(0)) if args and args[0].isdigit() else None
            if topic is not None and topic <= 0:
                raise UserError(tr("topic_id must be a positive integer."))
            result["target"] = {"chat": chat, "topic_id": topic}
        elif (
            option == "every"
            and kind == "follow"
            and args
            and "interval" not in result
        ):
            result["interval"] = duration(args.pop(0))
        else:
            raise UserError(
                tr(
                    "Usage: /dump <source> [to <chat> [topic_id]] or "
                    "/follow <source> [to <chat> [topic_id]] [every 5m]"
                )
            )
    return result


class BackupCommands(CommandGroup):
    @localized
    async def dump(self, message: Message, command: CommandObject):
        await self._handle(message, command, "dump")

    @localized
    async def follow(self, message: Message, command: CommandObject):
        await self._handle(message, command, "follow")

    async def _handle(self, message, command, kind):
        assert message.from_user is not None
        actor = message.from_user.id
        if kind == "dump" and not self.policy.is_admin(actor):
            raise UserError(tr("Only administrators can dump channel history."))
        options = arguments(kind, command.args or "")
        if options["action"] == "create":
            options["source_message_id"] = getattr(message, "message_id", None)
        result = await self.service.backup(actor, kind, **options)
        if options["action"] == "list":
            if not result["items"]:
                await message.answer(tr("No backup tasks on this page."))
                return
            text = tr(
                "Backups: {total} tasks. Page {page}.",
                total=result["total"],
                page=options.get("page", 1),
            )
            rows = [escape(text)]
            for record in result["items"]:
                rows.append(await self._render(record, actor))
            rows.append(
                escape(
                    tr(
                        "Use /{kind} status <id>, pause <id>, resume <id>, "
                        "or cancel <id>.",
                        kind=kind,
                    )
                )
            )
        else:
            rows = [await self._render(result, actor)]
            rows.append(
                escape(
                    tr(
                        "Use /{kind} to list tasks and /{kind} status {id} "
                        "to check progress.",
                        kind=kind,
                        id=result["id"],
                    )
                )
            )
        foreign = any(
            item["owner"] != actor
            for item in (
                result["items"] if options["action"] == "list" else [result]
            )
        )
        if not self.policy.is_allowed(actor) or (
            (foreign or kind == "dump") and not self.policy.is_admin(actor)
        ):
            raise UserError(tr("Backup not found or access denied."))
        await message.answer(
            "\n\n".join(rows),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def _render(self, record, actor):
        states = {
            "active": tr("Active"),
            "paused": tr("Paused"),
            "done": tr("Completed"),
            "cancelled": tr("Cancelled"),
        }
        target = record["target"]
        link = (
            await self.app.destinations.target_link(Target(**target), actor)
            if target
            else escape(tr("Default storage"))
        )
        lines = [
            f"<code>{record['id']}</code> · {escape(states[record['state']])}",
            escape(record["title"][:80]),
            tr("Destination: {p0}", p0=link),
        ]
        lines.append(
            escape(
                tr(
                    "Processed {groups} groups / {messages} messages. "
                    "Cursor: {cursor}.",
                    groups=record["processed"],
                    messages=record["messages"],
                    cursor=record["cursor"],
                )
            )
        )
        if record["kind"] == "follow":
            lines.append(
                escape(tr("Interval: {seconds}s.", seconds=record["interval"]))
            )
        if self.policy.is_admin(actor):
            lines.append(
                tr(
                    "Account: {p0}",
                    p0=self.app.members.user_link(record["owner"]),
                )
            )
        if record["phase"] == "uncertain":
            lines.append(
                escape(
                    tr(
                        "Last write is uncertain. Check the destination, "
                        "then resume <id> retry or resume <id> skip."
                    )
                )
            )
        if record["error"]:
            lines.append(escape(saved_label(record["error"])[:240]))
        return "\n".join(lines)
