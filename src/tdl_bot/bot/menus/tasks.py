"""Transfer, permission retry and independent join interactions."""

from html import escape
from inspect import isawaitable

from tdl_bot.bot.menus.base import MenuBase, button
from tdl_bot.bot.menus.copy import tr
from tdl_bot.core.errors import JoinPending
from tdl_bot.core.i18n import saved_label
from tdl_bot.core.links import JoinLink


class TaskMenus(MenuBase):
    async def task_page(self, message, state, path):
        actor = message.from_user.id
        parts = path.split(":")
        action = parts[0]
        if action in {"jobs", "pending"}:
            page = int(parts[1]) if len(parts) > 1 else 1
            pending = action == "pending"
            state[action + "_page"] = page
            items = (
                self.service.pending_items(actor)
                if pending
                else self.service.jobs(actor)
            )
            rows = []
            for item in items[(page - 1) * 8 : page * 8]:
                label = (
                    (str(item.id) + " · " + item.url.rsplit("/", 2)[-2])
                    if pending
                    else (item.id[:8] + " · " + saved_label(item.stage))
                )
                rows.append(
                    [
                        button(
                            label[:60],
                            f"{'retry' if pending else 'job'}:{item.id}",
                            wide=True,
                        )
                    ]
                )
            text = tr("Pending retries") if pending else tr("Active tasks")
            text += f" · {len(items)}"
            if not items:
                text += (
                    "\n\n" + tr("No pending retries.")
                    if pending
                    else "\n\n" + tr("No active tasks.")
                )
            if pending and not self.policy.is_admin(actor):
                text += "\n" + tr(
                    "Your pending retries: {used}/3",
                    used=self.store.pending_count(actor),
                )
            rows += self.pages(page, len(items), 8, action)
            rows += [
                [button(tr("Refresh"), path)],
                [button(tr("Back"), "tasks")],
            ]
            await self.show(message, state, escape(text), rows)
        elif action == "job":
            job = self.service.current_job(actor, parts[1])
            if job is None:
                await self.show(
                    message,
                    state,
                    tr(
                        "Task ended. Check its original progress message "
                        "for the result."
                    ),
                    [[button(tr("Back"), "jobs")]],
                )
                return
            target = await self.app.destinations.target_link(job.target, actor)
            text = tr("Task: {id}", id=job.id[:8]) + "\n\n"
            text += (
                f'<a href="{escape(job.url, quote=True)}">{tr("Source")}</a>\n'
            )
            phase = {
                "Queued": 1,
                "Locating media group": 2,
                "Preparing": 3,
                "Transferring": 3,
                "Downloading": 3,
                "Uploading": 3,
                "Sending": 4,
            }.get(job.stage)
            text += tr("Destination: {p0}", p0=target) + "\n\n"
            if phase:
                text += f"{phase}/5 · "
            text += escape(saved_label(job.stage))
            await self.show(
                message,
                state,
                text,
                [
                    [button(tr("Cancel task"), "stopjob:" + job.id)],
                    [
                        button(tr("Refresh"), path),
                        button(tr("Back"), f"jobs:{state.get('jobs_page', 1)}"),
                    ],
                ],
            )
        elif action == "stopjob":
            await self.show(
                message, state, tr("Cancelling and cleaning up…"), []
            )
            cancelled = await self.service.cancel(actor, parts[1])
            await self.show(
                message,
                state,
                tr("Cancellation complete.")
                if cancelled
                else tr(
                    "Task ended. Check its original progress message "
                    "for the result."
                ),
                [[button(tr("Back"), "jobs")]],
            )
        elif action == "retry":
            item = self.service.pending_record(actor, int(parts[1]))
            target = await self.app.destinations.target_link(
                item.destination.target, actor
            )
            text = tr("Pending retry: {id}", id=item.id) + "\n\n"
            text += (
                f'<a href="{escape(item.url, quote=True)}">{tr("Source")}</a>\n'
            )
            text += tr("Destination: {p0}", p0=target)
            await self.show(
                message,
                state,
                text,
                [
                    [button(tr("Retry"), f"runretry:{item.id}")],
                    [button(tr("Join and retry"), f"joinretry:{item.id}")],
                    [button(tr("Remove retry"), f"delretry:{item.id}")],
                    [
                        button(
                            tr("Back"),
                            f"pending:{state.get('pending_page', 1)}",
                        )
                    ],
                ],
            )
        elif action == "runretry":
            job = await self.service.retry(
                actor, int(parts[1]), source_message_id=None
            )
            await self.show(
                message,
                state,
                tr("Task queued."),
                [
                    [button(tr("View task"), "job:" + job.id)],
                    [button(tr("Back"), "pending")],
                ],
            )
        elif action == "delretry":
            self.service.pending_record(actor, int(parts[1]))
            await self.confirm(
                message,
                state,
                path.replace("delretry:", "discard:"),
                tr("Remove this retry? Sent media will not be deleted."),
                "retry:" + parts[1],
            )
        elif action == "discard":
            result = self.service.discard_pending(actor, int(parts[1]))
            if isawaitable(result):
                await result
            await self.task_page(message, state, "pending")
        elif action in {"join", "joinretry"}:
            retry_id = int(parts[1]) if action == "joinretry" else None
            if retry_id is not None:
                self.service.pending_record(actor, retry_id)
            state["join_retry"] = retry_id
            await self.prompt(
                message,
                state,
                "join_link",
                tr("Send the group or channel link to join."),
                "retry:" + str(retry_id) if retry_id else "tasks",
            )
        elif action == "dojoin":
            retry_id = state.get("join_retry")
            if retry_id:
                self.service.pending_record(actor, retry_id)
            key = state["join_executor"]
            try:
                await self.service.join(actor, state["join_link"], key)
            except JoinPending:
                await self.show(
                    message,
                    state,
                    tr(
                        "Waiting for approval. "
                        "The pending retry has been retained."
                    )
                    if retry_id
                    else tr("Join request submitted. Waiting for approval."),
                    [[button(tr("Back"), "pending" if retry_id else "tasks")]],
                )
                return
            if retry_id:
                await self.task_page(message, state, f"runretry:{retry_id}")
            else:
                await self.show(
                    message,
                    state,
                    tr("Joined successfully."),
                    [[button(tr("Back"), "tasks")]],
                )

    async def join_input(self, message, state, text):
        JoinLink.parse(text)
        state["join_link"] = text
        retry_id = state.get("join_retry")
        key = (
            self.service.pending_record(
                message.from_user.id, retry_id
            ).executor_key
            if retry_id
            else self.service.execution_account(message.from_user.id).key
        )
        state["join_executor"] = key
        label = (
            tr("Shared account") if key == "shared" else tr("Personal account")
        )
        await self.confirm(
            message,
            state,
            "dojoin",
            tr(
                "Join using execution account {account}?\n{link}",
                account=escape(label),
                link=escape(text),
            ),
            "retry:" + str(retry_id) if retry_id else "tasks",
        )
