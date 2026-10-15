"""Tasks commands and their related validation."""

import logging
from inspect import isawaitable

from aiogram.filters import CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from tdl_bot.bot.context import CommandGroup
from tdl_bot.bot.formatting import page_number
from tdl_bot.core.errors import JoinPending, UserError
from tdl_bot.core.i18n import localized, saved_label, tr
from tdl_bot.core.links import JoinLink
from tdl_bot.storage.models import PendingTransfer

_LOGGER = logging.getLogger(__name__)


class TaskCommands(CommandGroup):
    def __init__(self, app):
        super().__init__(app)
        self._controls = self.store.controls()

    @localized
    async def jobs(self, message: Message, command: CommandObject) -> None:
        """List manageable transfers with buttons usable across accounts."""
        assert message.from_user is not None
        operator = message.from_user.id
        jobs = self.service.jobs(operator)
        page = page_number(command.args)
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
        self._remember_controls(
            operator, reply.message_id, {job.id for job in selected}
        )

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
        page = page_number(command.args)
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
        self._remember_controls(
            message.from_user.id,
            reply.message_id,
            {f"retry:{item.id}" for item in selected},
        )

    def _remember_controls(self, user_id, message_id, tokens):
        self._controls[(user_id, message_id)] = tokens
        # Persist the same bounded button history for both task and retry lists.
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
