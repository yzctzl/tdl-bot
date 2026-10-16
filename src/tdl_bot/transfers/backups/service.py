"""Bounded backup scheduling with explicit recovery for uncertain writes."""

import asyncio
import logging

from aiogram.exceptions import TelegramRetryAfter
from aiohttp import ClientError
from telethon.errors import FloodWaitError

from tdl_bot.core.errors import AccessDenied, UserError
from tdl_bot.core.i18n import error_message, saved_label, tr, use_language
from tdl_bot.integrations.telegram.history import read_page, source
from tdl_bot.storage.backups import Backup
from tdl_bot.storage.models import Target
from tdl_bot.transfers.backups.pacing import BackupPacer
from tdl_bot.transfers.jobs import Job

_LOG = logging.getLogger(__name__)


class BackupService:
    def __init__(self, transfers, *, notify=None, workers=2):
        self.transfers = transfers
        self.store = transfers.store
        self.policy = transfers.policy
        self.notify = notify
        self.workers = max(1, min(workers, 2))
        self.records = {task.id: task for task in self.store.backups.all()}
        for record in self.records.values():
            if record.state == "cancelled" and not record.cancelled_at:
                # Retain legacy cancellations for seven more days.
                record.cancelled_at = self.store.now()
                self.store.backups.save(record)
        self.running = {}
        self.draining = False
        self.closing = False
        self.wake = asyncio.Event()
        self.runner = None
        self.pacer = BackupPacer()
        self.write_lock = asyncio.Lock()

    @property
    def busy(self):
        return any(not task.done() for task in self.running.values())

    def start(self):
        for record in self.records.values():
            if record.phase == "writing":
                record.phase = "uncertain"
                if record.state == "active":
                    record.state = "paused"
                record.error = tr(
                    "A write was interrupted. Check the destination, then "
                    "resume with retry or skip."
                )
                self.store.backups.save(record)
        self.runner = asyncio.create_task(self._loop())

    def quiesce(self):
        self.draining = True
        self.wake.set()

    async def close(self):
        self.quiesce()
        self.closing = True
        self.wake.set()
        if self.runner is not None:
            await self.runner
        await asyncio.gather(*self.running.values(), return_exceptions=True)

    async def reconcile_permissions(self):
        for record in self.records.values():
            if record.state == "active" and not self._allowed(record):
                record.state = "paused"
                record.error = tr(
                    "The backup owner cannot use its original "
                    "execution account."
                )
                self.store.backups.save(record)
                task = self.running.get(record.id)
                if task and not task.done():
                    if not task.cancelling():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await self._notify(record)

    def _allowed(self, record):
        return (
            self.policy.is_allowed(record.owner)
            and (record.kind != "dump" or self.policy.is_admin(record.owner))
            and self.transfers.can_use_executor(record.owner, record.executor)
        )

    def _quota(self, owner, kind, excluding=None):
        if kind == "follow" and not self.policy.is_admin(owner):
            count = self._follow_count(owner, excluding)
            limit = self.store.follow_limit(owner)
            if count >= limit:
                raise UserError(
                    tr(
                        "Follow limit reached ({limit}; active and paused "
                        "tasks both count). Cancel a task or ask "
                        "an administrator.",
                        limit=limit,
                    )
                )

    def _follow_count(self, owner, excluding=None):
        return sum(
            r.owner == owner
            and r.kind == "follow"
            and r.state in {"active", "paused"}
            and r.id != excluding
            for r in self.records.values()
        )

    def _cycle_check(self, source_id, destination):
        edges = {}
        for record in self.records.values():
            if record.kind == "follow" and record.state in {"active", "paused"}:
                edges.setdefault(str(record.source), set()).add(
                    (record.target or record.storage).chat
                )
        pending, visited = [destination], set()
        while pending:
            node = pending.pop()
            if node == str(source_id):
                raise UserError(
                    tr("This backup would create a forwarding loop.")
                )
            if node not in visited:
                visited.add(node)
                pending.extend(edges.get(node, ()))

    def _view(self, record, actor):
        values = record.payload()
        values.pop("source_ref")
        if not self.policy.is_admin(actor):
            values.pop("storage")
        return values

    async def execute(
        self,
        user_id,
        kind,
        action="list",
        task_id=None,
        source_ref=None,
        target=None,
        interval=300,
        resolution=None,
        page=1,
        source_message_id=None,
    ):
        if kind not in {"dump", "follow"}:
            raise UserError(tr("Unknown backup type."))
        if not self.policy.is_allowed(user_id):
            raise UserError(tr("This account is unauthorized or banned."))
        if kind == "dump" and not self.policy.is_admin(user_id):
            raise UserError(tr("Only administrators can dump channel history."))
        if action == "create":
            if self.draining:
                raise UserError(
                    tr("The task engine is reloading. Try again shortly.")
                )
            return self._view(
                await self._create(
                    user_id,
                    kind,
                    source_ref,
                    target,
                    interval,
                    source_message_id,
                ),
                user_id,
            )
        records = [
            r
            for r in self.records.values()
            if r.kind == kind
            and (r.owner == user_id or self.policy.is_admin(user_id))
        ]
        if action == "list":
            if type(page) is not int or page < 1:
                raise UserError(tr("Page must be a positive integer."))
            records = [
                r
                for r in records
                if r.state != "cancelled"
                or r.cancelled_at >= self.store.now() - 7 * 86400
            ]
            selected = list(reversed(records))[(page - 1) * 5 : page * 5]
            return {
                "items": [self._view(r, user_id) for r in selected],
                "total": len(records),
                "used": self._follow_count(user_id),
                "limit": None
                if self.policy.is_admin(user_id)
                else self.store.follow_limit(user_id),
            }
        record = next((r for r in records if r.id == task_id), None)
        if record is None:
            raise UserError(tr("Backup not found or access denied."))
        if action == "status":
            return self._view(record, user_id)
        if record.state in {"done", "cancelled"}:
            raise UserError(tr("This backup is already finished or cancelled."))
        if action in {"pause", "cancel"}:
            record.state = "paused" if action == "pause" else "cancelled"
            if action == "cancel":
                record.cancelled_at = self.store.now()
            self.store.backups.save(record)
            task = self.running.get(record.id)
            if task and not task.done():
                if not task.cancelling():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        elif action == "resume":
            stopping = self.running.get(record.id)
            if stopping is not None and not stopping.done():
                raise UserError(
                    tr("The backup is still stopping. Try again shortly.")
                )
            if record.state != "paused":
                raise UserError(tr("Pause this backup before resuming it."))
            if not self._allowed(record):
                raise UserError(
                    tr(
                        "The backup owner cannot use its original execution "
                        "account."
                    )
                )
            self._quota(record.owner, kind, excluding=record.id)
            if record.phase == "uncertain":
                if resolution not in {"retry", "skip"}:
                    raise UserError(
                        tr(
                            "Check the destination first, then use resume "
                            "<id> retry or resume <id> skip."
                        )
                    )
                if resolution == "skip":
                    record.cursor = max(record.cursor, record.inflight)
            record.state, record.phase, record.error = "active", "idle", ""
            record.next_run = self.store.now()
        elif action == "interval" and kind == "follow":
            if type(interval) is not int or interval < 60:
                raise UserError(
                    tr("Follow intervals must be at least 60 seconds.")
                )
            record.interval = interval
            record.next_run = self.store.now() + interval
        else:
            raise UserError(tr("Unknown backup operation."))
        self.store.backups.save(record)
        self.wake.set()
        return self._view(record, user_id)

    def _accepted(self, owner, kind, request_id):
        return next(
            (
                r
                for r in self.records.values()
                if request_id is not None
                and r.owner == owner
                and r.kind == kind
                and r.request_id == request_id
            ),
            None,
        )

    async def _create(self, owner, kind, ref, target, interval, request_id):
        accepted = self._accepted(owner, kind, request_id)
        if accepted is not None:
            return accepted
        self._quota(owner, kind)
        if not ref or type(interval) is not int or interval < 60:
            raise UserError(
                tr("Provide a source and an interval of at least 60 seconds.")
            )
        storage = self.store.internal_target()
        if storage is None:
            raise UserError(
                tr("Service setup is incomplete. Contact an administrator.")
            )
        executor = self.transfers.execution_account(owner)
        async with executor.tdl.gate.job(), asyncio.timeout(60):
            chat, title, upper = await source(executor.media, ref)
        destination = Target(**target) if target else None
        if destination:
            destination = await self.transfers.delivery.validate(
                destination, owner
            )
            if destination == storage:
                destination = None
        if str(chat) == storage.chat:
            raise UserError(
                tr("Internal storage cannot be used as a backup source.")
            )
        if kind == "follow":
            self._cycle_check(chat, (destination or storage).chat)
        elif str(chat) == (destination or storage).chat:
            raise UserError(
                tr("The backup source and destination must differ.")
            )
        # Network validation must not bypass a concurrent ban or role change.
        if not self.policy.is_allowed(owner) or (
            kind == "dump" and not self.policy.is_admin(owner)
        ):
            raise UserError(tr("This account is unauthorized or banned."))
        if not self.transfers.can_use_executor(owner, executor.key):
            raise UserError(tr("You cannot use this execution account."))
        accepted = self._accepted(owner, kind, request_id)
        if accepted is not None:
            return accepted
        self._quota(owner, kind)
        if any(
            r.owner == owner
            and r.kind == kind
            and r.source == chat
            and r.target == destination
            and r.storage == storage
            and r.state in {"active", "paused"}
            for r in self.records.values()
        ):
            raise UserError(
                tr(
                    "An equivalent backup already exists. View or resume it "
                    "instead."
                )
            )
        record = Backup(
            owner,
            kind,
            chat,
            ref,
            title,
            executor.key,
            storage,
            target=destination,
            interval=interval,
            cursor=upper if kind == "follow" else 0,
            upper=upper,
            next_run=self.store.now() + (interval if kind == "follow" else 0),
            request_id=request_id,
        )
        self.store.backups.save(record)
        self.records[record.id] = record
        self.wake.set()
        return record

    async def _loop(self):
        while not self.closing:
            self.wake.clear()
            for key, task in list(self.running.items()):
                if task.done():
                    del self.running[key]
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        self.records[key].state = "paused"
                        self.draining = True
                        _LOG.error(
                            "Backup checkpoint failed error=%s",
                            type(exc).__name__,
                        )

            if not self.draining:
                due = sorted(
                    (
                        r
                        for r in self.records.values()
                        if r.state == "active"
                        and r.next_run <= self.store.now()
                    ),
                    key=lambda r: r.next_run,
                )
                for record in due:
                    if len(self.running) >= self.workers:
                        break
                    if record.id not in self.running:
                        self.running[record.id] = asyncio.create_task(
                            self._cycle(record)
                        )
            try:
                await asyncio.wait_for(self.wake.wait(), 1)
            except TimeoutError:
                pass

    async def _cycle(self, record):
        job = None
        previous_cursor = record.cursor
        with use_language(self.store.language(record.owner)):
            try:
                if not self._allowed(record):
                    raise UserError(
                        tr(
                            "The backup owner cannot use its original "
                            "execution account."
                        )
                    )
                executor = self.transfers.accounts.get(record.executor)
                cooldown = self.pacer.remaining(executor.key)
                if cooldown > 0:
                    record.next_run = self.store.now() + cooldown
                    self.store.backups.save(record)
                    return
                async with executor.tdl.gate.job(), asyncio.timeout(60):
                    try:
                        chat, _, latest = await source(
                            executor.media, str(record.source)
                        )
                    except AccessDenied:
                        # Unjoined public chats need a username after restart.
                        chat, _, latest = await source(
                            executor.media, record.source_ref
                        )
                    if chat != record.source:
                        raise UserError(
                            tr(
                                "The source identity changed. Create a new "
                                "backup after checking it."
                            )
                        )
                    upper = record.upper if record.kind == "dump" else latest
                    page = await read_page(
                        executor.media,
                        chat,
                        record.cursor,
                        upper,
                        settle=record.kind == "follow",
                    )
                if record.target:
                    await self.transfers.delivery.validate(
                        record.target, record.owner
                    )
                for unit in page.units:
                    if self.draining or record.state != "active":
                        return
                    if not self._allowed(record):
                        raise UserError(
                            tr(
                                "The backup owner cannot use its original "
                                "execution account."
                            )
                        )
                    job = Job(
                        record.owner,
                        unit.urls[0],
                        record.target or record.storage,
                        owner_id=record.owner,
                        executor_key=record.executor,
                        storage_target=record.storage,
                        storage_only=record.target is None,
                        quiet=True,
                    )
                    # Avoid backup bursts after long uploads.
                    async with self.write_lock:
                        keys = self.pacer.keys(
                            record.executor, record.storage, record.target
                        )
                        if not await self.pacer.wait(
                            keys,
                            lambda: self.draining or record.state != "active",
                        ):
                            return
                        if not self._allowed(record):
                            raise UserError(
                                tr("This account is unauthorized or banned.")
                            )
                        record.phase, record.inflight = "writing", unit.end_id
                        self.store.backups.save(record)
                        try:
                            await self.transfers.backup_group(job, unit)
                        finally:
                            self.pacer.charge(keys, len(unit.messages))
                    record.cursor = unit.end_id
                    record.phase, record.error, record.attempts = "idle", "", 0
                    record.processed += 1
                    record.messages += len(unit.messages)
                    self.store.backups.save(record)
                    job = None
                    await asyncio.sleep(0)
                if record.state != "active" or self.draining:
                    return
                record.cursor = max(record.cursor, page.through)
                record.error, record.attempts = "", 0
                if record.kind == "dump" and page.complete:
                    record.state = "done"
                    self.store.backups.save(record)
                    await self._notify(record)
                catching_up = (
                    not page.complete and record.cursor > previous_cursor
                )
                record.next_run = self.store.now() + (
                    record.interval
                    if record.kind == "follow" and not catching_up
                    else 1
                )
                self.store.backups.save(record)
            except asyncio.CancelledError:
                if job and (job.upload_started or job.delivery_started):
                    record.phase = "uncertain"
                    record.error = tr(
                        "A write was interrupted. Check the destination, then"
                        " resume with retry or skip."
                    )
                else:
                    record.phase = "idle"
                if record.state == "active":
                    record.state = "paused"
                self.store.backups.save(record)
                raise
            except Exception as exc:
                cause = exc.__cause__ or exc
                delay = (
                    cause.seconds
                    if isinstance(cause, FloodWaitError)
                    else cause.retry_after
                    if isinstance(cause, TelegramRetryAfter)
                    else None
                )
                uncertain = job is not None and (
                    job.upload_started or job.delivery_started
                )
                if delay is not None:
                    self.pacer.cooldown(
                        record.executor,
                        delay,
                        bot=isinstance(cause, TelegramRetryAfter),
                    )
                if delay is not None or (
                    isinstance(cause, (OSError, TimeoutError, ClientError))
                    and not uncertain
                ):
                    record.attempts += 1
                    record.next_run = self.store.now() + (
                        delay
                        if delay is not None
                        else min(300, 5 * 2 ** min(record.attempts, 6))
                    )
                    record.phase = "idle"
                    record.error = tr(
                        "Waiting to retry after a temporary network or rate "
                        "limit error."
                    )
                else:
                    record.state = "paused"
                    record.phase = "uncertain" if uncertain else "idle"
                    record.error = (
                        error_message(exc)
                        if isinstance(exc, UserError)
                        else tr(
                            "Backup failed. Check source access and "
                            "destination permissions before resuming."
                        )
                    )
                    _LOG.warning("Backup paused error=%s", type(exc).__name__)
                    self.store.backups.save(record)
                    await self._notify(record)
                self.store.backups.save(record)
            finally:
                self.wake.set()

    async def _notify(self, record):
        if self.notify is None:
            return
        try:
            with use_language(self.store.language(record.owner)):
                await self.notify(
                    record.owner,
                    tr(
                        "Backup {id}: {state}. Processed {groups} groups / "
                        "{messages} messages. {error}",
                        id=record.id,
                        state=tr("Completed")
                        if record.state == "done"
                        else tr("Paused"),
                        groups=record.processed,
                        messages=record.messages,
                        error=saved_label(record.error),
                    ),
                )
        except Exception:
            _LOG.warning("Cannot send backup notification")
