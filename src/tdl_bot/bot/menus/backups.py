"""Backup creation drafts and state-aware follow/dump controls."""

from html import escape

from tdl_bot.bot.backups import duration
from tdl_bot.bot.menus.base import MenuBase, button
from tdl_bot.bot.menus.copy import tr
from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import saved_label
from tdl_bot.core.time import local_timestamp
from tdl_bot.storage.models import Target


class BackupMenus(MenuBase):
    async def backup_page(self, message, state, path):
        actor = message.from_user.id
        parts = path.split(":")
        kind = parts[0]
        if kind == "dump" and not self.policy.is_admin(actor):
            raise UserError(tr("Only administrators can dump channel history."))
        action = parts[1] if len(parts) > 1 else "home"
        if kind == "follow" and action == "home":
            parts, action = [kind, "list", "1"], "list"
        heading = (
            tr("Scheduled tracking")
            if kind == "follow"
            else tr("History backup")
        )
        back = "root" if kind == "follow" else "admin"
        if action == "home":
            data = await self.service.backup(actor, kind, action="list")
            records = [
                record
                for record in self.store.backups.all()
                if record.kind == kind
                and (record.owner == actor or self.policy.is_admin(actor))
            ]
            text = (
                heading
                + "\n\n"
                + tr(
                    "Running: {running} · Paused: {paused}",
                    running=sum(record.state == "active" for record in records),
                    paused=sum(record.state == "paused" for record in records),
                )
            )
            if kind == "follow" and data["limit"] is not None:
                text += "\n\n" + tr(
                    "Follow slots: {used}/{limit} (active and paused).",
                    used=data["used"],
                    limit=data["limit"],
                )
            await self.show(
                message,
                state,
                escape(text),
                [
                    [
                        button(
                            tr("New tracking")
                            if kind == "follow"
                            else tr("New backup"),
                            kind + ":new",
                        ),
                        button(tr("Tasks"), kind + ":list:1"),
                    ],
                    [button(tr("Back"), back)],
                ],
            )
        elif action == "list":
            page = int(parts[2]) if len(parts) > 2 else 1
            state[kind + "_page"] = page
            data = await self.service.backup(
                actor, kind, action="list", page=page
            )
            rows = [
                [
                    button(
                        r["title"][:28] + " · " + self.backup_state(r),
                        f"{kind}:detail:{r['id']}",
                        wide=True,
                    )
                ]
                for r in data["items"]
            ]
            text = heading + f" · {data['total']}"
            if not data["items"]:
                text += "\n\n" + tr("No backup tasks on this page.")
            rows += self.pages(page, data["total"], 5, kind + ":list")
            if kind == "follow":
                records = [
                    r
                    for r in self.store.backups.all()
                    if r.kind == kind
                    and (r.owner == actor or self.policy.is_admin(actor))
                ]
                text += "\n" + tr(
                    "Running: {running} · Paused: {paused}",
                    running=sum(r.state == "active" for r in records),
                    paused=sum(r.state == "paused" for r in records),
                )
                if data["limit"] is not None:
                    text += "\n" + tr(
                        "Follow slots: {used}/{limit} (active and paused).",
                        used=data["used"],
                        limit=data["limit"],
                    )
                rows += [
                    [
                        button(tr("New tracking"), kind + ":new"),
                        button(tr("Refresh"), "follow:list:" + str(page)),
                    ],
                    [button(tr("Back"), "root")],
                ]
            else:
                rows += [
                    [button(tr("Refresh"), path)],
                    [button(tr("Back"), kind)],
                ]
            await self.show(message, state, escape(text), rows)
        elif action == "new":
            if kind == "follow":
                data = await self.service.backup(actor, kind, action="list")
                if data["limit"] is not None and data["used"] >= data["limit"]:
                    raise UserError(
                        tr(
                            "Follow limit reached ({limit}; active and "
                            "paused tasks both count). Cancel a task or ask "
                            "an administrator.",
                            limit=data["limit"],
                        )
                    )
            state["draft"] = {"kind": kind, "interval": 300}
            await self.prompt(
                message,
                state,
                "backup_source",
                tr("Send the source channel or group: @username, ID or link."),
                kind,
            )
        elif action == "draft":
            draft = state["draft"]
            target = draft.get("target")
            text = (
                heading
                + "\n\n"
                + tr("Source: {source}", source=escape(draft["source_ref"]))
            )
            text += "\n" + tr(
                "Destination: {p0}",
                p0=escape(target["chat"])
                + (
                    " / " + str(target["topic_id"])
                    if target.get("topic_id")
                    else ""
                )
                if target
                else tr("Default"),
            )
            rows = [[button(tr("Destination"), kind + ":target")]]
            if kind == "follow":
                text += "\n" + tr(
                    "Interval: {seconds}s.", seconds=draft["interval"]
                )
                text += "\n\n" + tr(
                    "Only new messages will be tracked. History is not "
                    "included."
                )
                rows[0].append(button(tr("Interval"), kind + ":interval:draft"))
            else:
                text += "\n\n" + tr(
                    "Existing messages will be backed up, including media "
                    "groups and text."
                )
            rows += [
                [
                    button(
                        tr("Start tracking")
                        if kind == "follow"
                        else tr("Start backup"),
                        kind + ":create",
                    )
                ],
                [button(tr("Back"), kind)],
            ]
            await self.show(message, state, text, rows)
        elif action == "target":
            await self.show(
                message,
                state,
                tr("Choose the receiving location."),
                [
                    [button(tr("Default"), kind + ":defaulttarget")],
                    [button(tr("Set group or channel"), kind + ":targetinput")],
                    [button(tr("Back"), kind + ":draft")],
                ],
            )
        elif action == "defaulttarget":
            state["draft"].pop("target", None)
            await self.backup_page(message, state, kind + ":draft")
        elif action == "targetinput":
            await self.prompt(
                message,
                state,
                "backup_target",
                tr(
                    "Send the destination @username or ID, optionally "
                    "followed by a topic ID."
                ),
                kind + ":target",
            )
        elif action == "create":
            draft = state["draft"]
            if draft["kind"] != kind:
                raise UserError(tr("This menu has expired. Open /start again."))
            # Source input is a distinct creation key for each new draft.
            record = await self.service.backup(
                actor,
                kind,
                action="create",
                source_ref=draft["source_ref"],
                target=draft.get("target"),
                interval=draft["interval"],
                source_message_id=draft["request_id"],
            )
            state.pop("draft", None)
            await self.backup_page(
                message, state, f"{kind}:detail:{record['id']}"
            )
        elif action == "detail":
            record = await self.service.backup(
                actor, kind, action="status", task_id=parts[2]
            )
            target = record["target"]
            destination = (
                await self.app.destinations.target_link(Target(**target), actor)
                if target
                else tr("Default")
            )
            text = escape(record["title"]) + "\n\n"
            text += escape(self.backup_state(record)) + "\n"
            text += tr("Destination: {p0}", p0=destination)
            text += "\n" + tr(
                "Processed: {groups} groups · {messages} messages",
                groups=record["processed"],
                messages=record["messages"],
            )
            if kind == "follow":
                text += "\n" + tr(
                    "Interval: {seconds}s.", seconds=record["interval"]
                )
                if (
                    record["state"] == "active"
                    and record["next_run"] > self.store.now()
                ):
                    text += "\n" + tr(
                        "Next check: {time}",
                        time=local_timestamp(record["next_run"]),
                    )
            if self.policy.is_admin(actor):
                text += "\n" + tr(
                    "Account: {p0}",
                    p0=self.app.members.user_link(record["owner"]),
                )
            if record["phase"] == "uncertain":
                text += "\n\n" + tr(
                    "The last send is unconfirmed. Check the destination "
                    "before retrying or skipping."
                )
            if record["error"]:
                text += "\n" + escape(saved_label(record["error"])[:240])
            rows = []
            if record["state"] == "active":
                rows.append(
                    [button(tr("Pause"), f"{kind}:pause:{record['id']}")]
                )
            elif record["state"] == "paused":
                if record["phase"] == "uncertain":
                    rows.append(
                        [
                            button(
                                tr("Retry current item"),
                                f"{kind}:resolve:{record['id']}:retry",
                            ),
                            button(
                                tr("Skip current item"),
                                f"{kind}:resolve:{record['id']}:skip",
                            ),
                        ]
                    )
                else:
                    rows.append(
                        [button(tr("Resume"), f"{kind}:resume:{record['id']}")]
                    )
            if record["state"] in {"active", "paused"}:
                if kind == "follow":
                    rows.append(
                        [
                            button(
                                tr("Change interval"),
                                f"{kind}:interval:{record['id']}",
                            )
                        ]
                    )
                rows.append(
                    [
                        button(
                            tr("Cancel tracking")
                            if kind == "follow"
                            else tr("Cancel backup"),
                            f"{kind}:cancel:{record['id']}",
                        )
                    ]
                )
            rows += [
                [button(tr("Refresh"), path)],
                [
                    button(
                        tr("Back"),
                        f"{kind}:list:{state.get(kind + '_page', 1)}",
                    )
                ],
            ]
            # Omitted targets stay opaque to ordinary operators.
            if not record["target"] and not self.policy.is_admin(actor):
                text = text.replace(
                    escape(tr("Default storage")), escape(tr("Default"))
                )
            await self.show(message, state, text, rows)
        elif action in {"pause", "resume"}:
            await self.service.backup(
                actor, kind, action=action, task_id=parts[2]
            )
            await self.backup_page(message, state, f"{kind}:detail:{parts[2]}")
        elif action in {"cancel", "resolve"}:
            explanation = (
                tr(
                    "Cancel this tracking task? It cannot be resumed. Sent "
                    "messages will remain."
                )
                if kind == "follow"
                else tr(
                    "Cancel this backup? It cannot be resumed. Sent messages "
                    "will remain."
                )
            )
            if action == "resolve":
                explanation = (
                    tr(
                        "Retry the uncertain item? Check the destination "
                        "first to avoid duplicates."
                    )
                    if parts[3] == "retry"
                    else tr(
                        "Skip the uncertain item? Check the destination "
                        "first to avoid missing content."
                    )
                )
            await self.confirm(
                message,
                state,
                path.replace(":" + action + ":", ":apply:" + action + ":", 1),
                explanation,
                f"{kind}:detail:{parts[2]}",
            )
        elif action == "apply":
            options = {
                "action": "cancel" if parts[2] == "cancel" else "resume",
                "task_id": parts[3],
            }
            if parts[2] == "resolve":
                options["resolution"] = parts[4]
            await self.service.backup(actor, kind, **options)
            await self.backup_page(message, state, f"{kind}:detail:{parts[3]}")
        elif action == "interval":
            state["interval_task"] = parts[2]
            state["interval_kind"] = kind
            await self.show(
                message,
                state,
                tr("Choose a tracking interval."),
                [
                    [
                        button(label, f"{kind}:every:{value}")
                        for label, value in (
                            (tr("1 minute"), "60s"),
                            (tr("5 minutes"), "5m"),
                            (tr("15 minutes"), "15m"),
                            (tr("1 hour"), "1h"),
                        )
                    ],
                    [button(tr("Custom interval"), kind + ":custom")],
                    [
                        button(
                            tr("Back"),
                            kind + ":draft"
                            if parts[2] == "draft"
                            else f"{kind}:detail:{parts[2]}",
                        )
                    ],
                ],
            )
        elif action == "custom":
            await self.prompt(
                message,
                state,
                "backup_interval",
                tr(
                    "Send an interval, such as 60s, 5m or 1h (minimum 60 "
                    "seconds)."
                ),
                f"{kind}:interval:{state['interval_task']}",
            )
        elif action == "every":
            await self.set_interval(message, state, duration(parts[2]))

    @staticmethod
    def backup_state(record):
        return {
            "active": tr("Running"),
            "paused": tr("Paused"),
            "done": tr("Completed"),
            "cancelled": tr("Cancelled"),
        }[record["state"]]

    async def set_interval(self, message, state, seconds):
        kind, task = state["interval_kind"], state["interval_task"]
        if task == "draft":
            state["draft"]["interval"] = seconds
            path = kind + ":draft"
        else:
            await self.service.backup(
                message.from_user.id,
                kind,
                action="interval",
                task_id=task,
                interval=seconds,
            )
            path = f"{kind}:detail:{task}"
        await self.backup_page(message, state, path)
