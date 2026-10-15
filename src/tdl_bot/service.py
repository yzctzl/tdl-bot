"""Run bounded background transfers and clean temporary files."""

import asyncio
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

from telethon import errors

from tdl_bot.accounts import AccessPolicy, AccountPool
from tdl_bot.delivery import MissingResult
from tdl_bot.errors import AccessDenied, UserError
from tdl_bot.links import MessageLink
from tdl_bot.store import Destination, PendingTransfer, Store, Target

_LOGGER = logging.getLogger(__name__)
Notify = Callable[..., Awaitable[None]]


@dataclass(eq=False)
class Job:
    """Separate notification, management and execution identities per task."""

    user_id: int
    url: str
    target: Target
    source_message_id: int | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    progress_message_id: int | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    cancel_requested: bool = False
    finished: bool = False
    upload_started: bool = False
    owner_id: int | None = None
    executor_key: str = "shared"
    retry_id: int | None = None
    stage: str = "排队中"
    storage_target: Target | None = None
    delivery_started: bool = False

    def payload(self):
        return {
            name: getattr(self, name)
            for name in (
                "user_id",
                "url",
                "source_message_id",
                "id",
                "progress_message_id",
                "cancel_requested",
                "finished",
                "upload_started",
                "owner_id",
                "executor_key",
                "retry_id",
                "stage",
                "delivery_started",
            )
        } | {
            "target": {
                "chat": self.target.chat,
                "topic_id": self.target.topic_id,
            },
            "storage_target": (
                {
                    "chat": self.storage_target.chat,
                    "topic_id": self.storage_target.topic_id,
                }
                if self.storage_target
                else None
            ),
        }

    @classmethod
    def restore(cls, payload):
        data = dict(payload)
        data["target"] = Target(**data["target"])
        if data.get("storage_target"):
            data["storage_target"] = Target(**data["storage_target"])
        job = cls(**data)
        job.ready.set()
        return job

    @property
    def destination(self) -> Destination:
        return Destination(self.target, self.owner_id)


class JobQueue(asyncio.Queue[Job]):
    """A bounded queue whose pending jobs can be removed on cancellation."""

    # asyncio initializes this deque, but typeshed omits the private field.
    _queue: deque[Job]

    def discard(self, job: Job) -> None:
        """Free a pending queue slot without interrupting other workers.

        This queue uses put_nowait only, so there are no blocked producers
        to wake. Removing from its underlying deque keeps FIFO ordering.
        """
        try:
            self._queue.remove(job)
        except ValueError:
            return  # A worker already owns the job.
        self.task_done()


class TransferService:
    """Manage transfer lifetimes separately from Telegram update handlers."""

    def __init__(
        self,
        accounts: AccountPool,
        store: Store,
        jobs_dir: Path,
        notify: Notify,
        *,
        policy: AccessPolicy,
        workers: int = 2,
        queue_size: int = 20,
        delivery=None,
        durable: bool = False,
    ) -> None:
        """Initialize a bounded workspace queue and execution account pool."""
        self.delivery = delivery
        self.durable = durable
        self.store = store
        self.jobs_dir = jobs_dir
        self.notify = notify
        self.policy = policy
        self.accounts = accounts
        self._worker_count = workers
        self._queue = JobQueue(queue_size)
        self._jobs: dict[str, Job] = {}
        self._workers: list[asyncio.Task] = []
        self._closing = False
        self._transfer_locks: dict[int, asyncio.Lock] = {}
        self._work_locks: dict[str, tuple[asyncio.Lock, int]] = {}

    def start(self) -> None:
        """Remove abandoned owned directories and start queue workers."""
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        for path in self.jobs_dir.glob("job-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        if self.durable:
            payloads = self.store.active_job_payloads()
            self._queue = JobQueue(max(self._queue.maxsize, len(payloads)))
            for payload in payloads:
                job = Job.restore(payload)
                if job.delivery_started or job.upload_started:
                    # An interrupted write may have succeeded without an ack.
                    job.stage = "需确认目标后重试"
                    job.finished = True
                    self.store.remove_job(job.id)
                    asyncio.create_task(
                        self._notify(
                            job,
                            "任务执行进程曾中断，请先检查接收位置，再重新发送链接。",
                            finished=True,
                        )
                    )
                    continue
                self._jobs[job.id] = job
                self._queue.put_nowait(job)
        self._workers = [
            asyncio.create_task(self._worker())
            for _ in range(self._worker_count)
        ]

    async def submit(
        self, user_id: int, url: str, *, source_message_id: int | None = None
    ) -> Job:
        """Capture the selected destination and own-or-shared executor."""
        if not self.policy.is_allowed(user_id):
            raise UserError("当前账号未获授权或处于封禁中。")
        if (
            not self.policy.is_admin(user_id)
            and self.store.pending_count(user_id) >= 3
        ):
            raise UserError(
                "你的待重试任务已达 3 条，请先用 /retry_cancel 清理或完成重试。"
            )
        destination = (
            self.store.recipient(user_id)
            if self.delivery is not None
            else self.store.destination(user_id)
        )
        if destination is None:
            raise UserError("请先用 /target 设置共享目标或个人目标。")
        return await self._submit(
            user_id,
            url,
            destination,
            self.accounts.select(user_id).key,
            source_message_id,
        )

    async def retry(
        self,
        user_id: int,
        pending_id: int | None,
        *,
        source_message_id: int | None = None,
    ) -> Job:
        """Keep the failed task's exact target, scope and execution account."""
        pending = self.pending_record(user_id, pending_id)
        return await self._submit(
            user_id,
            pending.url,
            pending.destination,
            pending.executor_key,
            source_message_id,
            pending.id,
        )

    def pending_items(self, user_id: int) -> list[PendingTransfer]:
        return [
            item
            for item in self.store.pending_items(
                include_archived=self.policy.is_admin(user_id)
            )
            if self.policy.can_manage(user_id, item.destination.owner_id)
        ]

    def pending_record(
        self, user_id: int, pending_id: int | None = None
    ) -> PendingTransfer:
        items = self.pending_items(user_id)
        if pending_id is None:
            if len(items) != 1:
                raise UserError(
                    "请使用 /pending 查看待重试任务，再用 /retry <编号>。"
                )
            return items[0]
        for item in items:
            if item.id == pending_id:
                return item
        raise UserError("待重试任务不存在或你无权管理。")

    def discard_pending(self, user_id: int, pending_id: int) -> None:
        """Remove a manageable retry, without racing an active transfer."""
        item = self.pending_record(user_id, pending_id)
        for job in self._jobs.values():
            if not job.finished and (
                job.retry_id == item.id
                or (
                    MessageLink.parse(job.url) == MessageLink.parse(item.url)
                    and job.destination == item.destination
                    and job.executor_key == item.executor_key
                )
            ):
                raise UserError("该任务正在重试，请先用 /cancel 取消活动任务。")
        self.store.delete_pending(item.id)

    async def cancel_submitted(self, admin_id: int, operator_id: int) -> None:
        """Revoke an operator's already accepted jobs and wait for cleanup."""
        if not self.policy.is_admin(admin_id):
            raise UserError("只有管理员可以管理账号。")
        jobs = [
            job for job in self._jobs.values() if job.user_id == operator_id
        ]
        for job in jobs:
            # Initial reply delivery may be in progress; let it finish first.
            await job.ready.wait()
            await self._cancel_job(job)

    def _can_run(self, job: Job) -> bool:
        if not self.policy.can_manage(job.user_id, job.owner_id):
            return False
        if job.retry_id is not None and not self.policy.is_admin(job.user_id):
            for item in self.store.pending_items(include_archived=True):
                if item.id == job.retry_id and item.archived:
                    return False
        return True

    async def reconcile_permissions(self) -> None:
        """Cancel accepted jobs whose submitter lost management rights."""
        for job in list(self._jobs.values()):
            if not self._can_run(job):
                await job.ready.wait()
                await self._cancel_job(job)

    async def _submit(
        self,
        user_id: int,
        url: str,
        destination: Destination,
        executor_key: str,
        source_message_id: int | None,
        retry_id: int | None = None,
    ) -> Job:
        if self.durable:
            accepted = self.store.accepted_request(user_id, source_message_id)
            if accepted is not None:
                return Job.restore(accepted)
        link = MessageLink.parse(url)
        if not self.policy.can_manage(user_id, destination.owner_id):
            raise UserError("无权管理此任务。")
        executor = self.accounts.get(executor_key)
        if self._closing:
            raise UserError("Bot 正在关闭，请稍后重试。")
        for current in self._jobs.values():
            if (
                not current.finished
                and MessageLink.parse(current.url) == link
                and current.destination == destination
                and self.accounts.get(current.executor_key).tdl is executor.tdl
            ):
                raise UserError("相同来源、目标和执行账号的任务已在队列中。")
        url = link.url_for(link.message_id)
        if link.comment_id is not None:
            url += f"?comment={link.comment_id}"
        storage = (
            self.store.internal_target() if self.delivery is not None else None
        )
        if self.delivery is not None and storage is None:
            raise UserError("服务尚未配置完成，请联系管理员。")
        job = Job(
            user_id,
            url,
            destination.target,
            source_message_id=source_message_id,
            owner_id=destination.owner_id,
            executor_key=executor_key,
            retry_id=retry_id,
            storage_target=storage,
        )
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull as exc:
            raise UserError("下载队列已满，请稍后重试。") from exc
        self._jobs[job.id] = job
        self._checkpoint(job)
        try:
            await self.notify(job, "1/5 下载任务已加入队列。", finished=False)
        except BaseException:
            job.finished = True
            self._queue.discard(job)
            self._release(job)
            job.done.set()
            raise
        finally:
            job.ready.set()
            if job.id in self._jobs:
                self._checkpoint(job)
        return job

    def jobs(self, user_id: int) -> list[Job]:
        """List all shared jobs plus manageable personal jobs."""
        return [
            job
            for job in self._jobs.values()
            if not job.finished
            and self.policy.can_manage(user_id, job.owner_id)
        ]

    def busy(self, user_id: int) -> bool:
        return bool(self.jobs(user_id))

    def current_job(self, user_id: int, job_id: str) -> Job | None:
        """Accept exact IDs or unique eight-character prefixes."""
        if len(job_id) < 8:
            return None
        matches = [
            job
            for job in self.jobs(user_id)
            if job.ready.is_set() and job.id.startswith(job_id)
        ]
        return matches[0] if len(matches) == 1 else None

    async def cancel(self, user_id: int, job_id: str) -> bool:
        """Cancel a manageable job and wait for cleanup and terminal edit."""
        job = self.current_job(user_id, job_id)
        if job is None:
            return False
        return await self._cancel_job(job)

    async def _cancel_job(self, job: Job) -> bool:
        if job.finished:
            await job.done.wait()
            return True
        if job.cancel_requested:
            await job.done.wait()
            return True
        job.cancel_requested = True
        job.stage = "取消中"
        if job.task is not None:
            job.task.cancel()
            await job.done.wait()
        else:
            self._queue.discard(job)
            job.finished = True
            job.ready.set()
            try:
                await self._cancelled(job)
            finally:
                self._release(job)
                job.done.set()
        return True

    def _checkpoint(self, job: Job) -> None:
        if self.durable and job.id in self._jobs:
            self.store.save_job(job.payload())

    def _release(self, job: Job) -> None:
        self._jobs.pop(job.id, None)
        if self.durable:
            self.store.remove_job(job.id)

    async def _cancelled(self, job: Job) -> None:
        """Report cancellation only after the transfer has cleaned files."""
        _LOGGER.info("任务 user=%s phase=cancelled cleaned=true", job.user_id)
        text = "已取消，临时文件已清理。"
        if self.delivery is not None:
            if job.delivery_started:
                text += "已发送的媒体可能仍保留在接收位置。"
        elif job.upload_started:
            text += "已上传的媒体可能仍保留在目标中。"
        await self._notify(job, text, finished=True)

    async def close(self) -> None:
        """Finish queued transfers before disconnecting Telegram clients."""
        self._closing = True
        try:
            await self._queue.join()
        finally:
            for task in self._workers:
                task.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)

    async def _notify(
        self, job: Job, text: str, *, finished: bool = False
    ) -> None:
        """Do not let an unavailable bot recipient interrupt a transfer."""
        if finished:
            job.finished = True
        self._checkpoint(job)
        try:
            await self.notify(job, text, finished=finished)
            self._checkpoint(job)
        except Exception:
            _LOGGER.warning("无法更新进度 user=%s", job.user_id)

    async def _worker(self) -> None:
        """Process workspace jobs and always release the task registry entry."""
        while True:
            job = await self._queue.get()
            try:
                await job.ready.wait()
                if job.finished:
                    continue
                if not self._can_run(job):
                    job.cancel_requested = True
                    await self._cancelled(job)
                    continue
                job.task = asyncio.create_task(self._transfer(job))
                await job.task
            except asyncio.CancelledError:
                if not job.cancel_requested:
                    raise
                await self._cancelled(job)
            except AccessDenied:
                if job.retry_id is None:
                    saved = self.store.set_pending(
                        job.user_id,
                        job.url,
                        destination=job.destination,
                        executor_key=job.executor_key,
                        limit=None if self.policy.is_admin(job.user_id) else 3,
                    )
                    if not saved:
                        await self._notify(
                            job,
                            "来源无访问权限；你的待重试记录已达 3 条。"
                            "本任务未加入待重试列表，临时文件已清理。"
                            "请清理或完成已有记录后重新发送链接。",
                            finished=True,
                        )
                        continue
                await self._notify(
                    job,
                    "下载账号无权访问来源群。请用 /pending 查看编号，"
                    "发送 /join 群主页或邀请 url [编号]；"
                    "加入后会自动重试。若需审核，批准后发送 /retry。"
                    "请确认来源群链接正确、账号未被封禁且历史可见。",
                    finished=True,
                )
            except UserError as exc:
                await self._notify(job, str(exc), finished=True)
            except errors.FloodWaitError as exc:
                await self._notify(
                    job,
                    f"Telegram 限流，请 {exc.seconds} 秒后重试。",
                    finished=True,
                )
            except sqlite3.Error as exc:
                # Only structural reasons are logged; never bound values.
                reason = re.search(
                    r"no such (?:column|table): [A-Za-z_][A-Za-z0-9_.]*",
                    str(exc),
                )
                _LOGGER.error(
                    "任务数据库异常 stage=%s code=%s reason=%s",
                    job.stage,
                    getattr(exc, "sqlite_errorname", type(exc).__name__),
                    reason.group() if reason else type(exc).__name__,
                )
                await self._notify(
                    job,
                    "服务数据异常，请联系管理员修复后重新发送链接。"
                    "已有媒体结果会保留；临时文件已清理。",
                    finished=True,
                )
            except Exception as exc:
                # Do not log private urls, invitations, captions or tokens.
                _LOGGER.error(
                    "任务失败 user=%s error=%s", job.user_id, type(exc).__name__
                )
                await self._notify(
                    job,
                    "处理失败，临时文件已清理。请检查网络和目标写入权限后重试。"
                    "部分媒体可能已上传，请先检查目标以免重复。",
                    finished=True,
                )
            finally:
                self._release(job)
                job.done.set()
                self._queue.task_done()

    @asynccontextmanager
    async def _work_lock(self, key):
        lock, users = self._work_locks.get(key, (asyncio.Lock(), 0))
        self._work_locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            remaining = self._work_locks[key][1] - 1
            if remaining:
                self._work_locks[key] = (lock, remaining)
            else:
                del self._work_locks[key]

    async def _transfer(self, job: Job) -> None:
        """Resolve, download, verify and upload under one update lease."""
        executor = self.accounts.get(job.executor_key)
        lock = self._transfer_locks.setdefault(id(executor.tdl), asyncio.Lock())
        async with (
            lock,
            self._work_lock("url:" + job.url),
            executor.tdl.gate.job(),
        ):
            _LOGGER.info("任务 user=%s phase=resolve", job.user_id)
            job.stage = "定位媒体组"
            await self._notify(job, "2/5 正在定位媒体组。")
            if await self._reuse(job, [job.url]):
                return
            if self.delivery is not None:
                prepare = getattr(self.delivery, "prepare", None)
                if prepare is not None:
                    await prepare(job)
            group = await executor.media.resolve(MessageLink.parse(job.url))
            aliases = [*group.urls]
            fingerprint = _media_fingerprint(group.messages)
            if fingerprint:
                aliases.append(fingerprint)
            async with self._work_lock(
                "group:" + (fingerprint or group.urls[0])
            ):
                await self._process_group(job, group, aliases)

    async def _process_group(self, job, group, aliases):
        executor = self.accounts.get(job.executor_key)
        if await self._reuse(job, aliases):
            return
        copier = getattr(executor.media, "copy_group", None)
        if copier is not None:
            job.stage = "直接转发"
            await self._notify(job, "正在处理媒体组……")
            job.upload_started = True
            self._checkpoint(job)
            ids = await copier(group, job.storage_target or job.target)
            if ids:
                await self._complete(job, aliases, ids, "直接转发")
                return
            job.upload_started = False
        with tempfile.TemporaryDirectory(
            prefix="job-", dir=self.jobs_dir
        ) as temporary:
            directory = Path(temporary)
            _LOGGER.info(
                "任务 user=%s phase=download media=%s",
                job.user_id,
                len(group.messages),
            )
            job.stage = "下载中"
            await self._notify(
                job, f"3/5 正在下载 {len(group.messages)} 个媒体文件。"
            )
            await executor.tdl.download(group.urls, directory)
            files = await asyncio.to_thread(
                _downloaded_files, directory, group.messages
            )
            job.stage = "上传中"
            await self._notify(job, f"4/5 正在上传 {len(files)} 个媒体文件。")
            _LOGGER.info(
                "任务 user=%s phase=upload media=%s target=%s topic=%s",
                job.user_id,
                len(files),
                job.target.chat,
                job.target.topic_id,
            )
            job.upload_started = True
            self._checkpoint(job)
            ids = await executor.media.upload(
                group, files, job.storage_target or job.target
            )
        if self.delivery is not None:
            await self._complete(job, aliases, ids, "处理")
            return
        if isinstance(ids, list) and len(ids) == len(group.messages):
            self.store.save_result(
                [job.url, *aliases],
                job.executor_key,
                job.owner_id,
                job.target.chat,
                ids,
            )
        self.store.clear_pending(
            job.url,
            job.destination,
            job.executor_key,
            pending_id=job.retry_id,
        )
        _LOGGER.info("任务 user=%s phase=complete cleaned=true", job.user_id)
        await self._notify(
            job,
            f"5/5 完成：已上传 {len(group.messages)} 个媒体及视频文字。",
            finished=True,
        )

    async def _reuse(self, job: Job, urls: list[str]) -> bool:
        executor = self.accounts.get(job.executor_key)
        copier = getattr(executor.media, "copy_messages", None)
        if copier is None and self.delivery is None:
            return False
        for owner, chat, ids in self.store.saved_results(
            urls,
            None if self.delivery is not None else job.executor_key,
            internal_only=self.delivery is not None,
        ):
            if not self.policy.can_manage(job.user_id, owner):
                continue
            if self.delivery is not None:
                if owner is not None:
                    continue
                try:
                    await self._complete(
                        job, urls, ids, "处理", source_chat=chat
                    )
                except MissingResult:
                    self.store.mark_unavailable(chat, ids)
                    job.delivery_started = False
                    self._checkpoint(job)
                    continue
                return True
            if copier is None:
                return False
            job.stage = "复用历史结果"
            await self._notify(job, "正在转发以前处理的结果，无需重新下载。")
            job.upload_started = True
            try:
                copied = await copier(chat, ids, job.target)
            except (
                AccessDenied,
                errors.ChannelPrivateError,
                errors.ChatForwardsRestrictedError,
            ):
                copied = None
            if copied:
                await self._complete(job, urls, copied, "复用历史结果")
                return True
            job.upload_started = False
        return False

    async def _complete(self, job, urls, ids, mode, *, source_chat=None):
        chat = source_chat or (job.storage_target or job.target).chat
        info = self.store.result_info(chat, ids) if source_chat else None
        if info:
            result_id = info["id"]
            self.store.add_result_sources(result_id, [job.url, *urls])
        else:
            result_id = self.store.save_result(
                [job.url, *urls],
                job.executor_key,
                None if self.delivery is not None else job.owner_id,
                chat,
                ids,
                internal=self.delivery is not None,
            )
        if self.delivery is not None:
            if not info or not info["revealed"]:
                executor = self.accounts.get(job.executor_key)
                if info:
                    try:
                        executor = self.accounts.get(info["executor_key"])
                    except UserError:
                        pass  # A storage admin may still edit older copies.
                reveal = getattr(executor.media, "reveal_messages", None)
                if reveal is not None and not await reveal(chat, ids):
                    raise MissingResult("结果已失效，需要重新处理来源。")
                self.store.mark_revealed(result_id)
            job.upload_started = False
            job.stage = "发送中"
            await self._notify(job, "正在发送到你的接收位置……")
            job.delivery_started = True
            self._checkpoint(job)
            attempt = self.store.record_delivery(result_id, job, [], "sending")
            try:
                sent = await self.delivery(
                    replace(job, storage_target=Target(chat)), ids
                )
            except BaseException as exc:
                self.store.finish_delivery(
                    attempt,
                    getattr(exc, "message_ids", []),
                    "cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else "failed",
                    type(exc).__name__,
                )
                raise
            self.store.finish_delivery(attempt, sent, "sent")
            job.delivery_started = False
            mode = "发送"
        self.store.clear_pending(
            job.url,
            job.destination,
            job.executor_key,
            pending_id=job.retry_id,
        )
        await self._notify(
            job,
            f"完成：已{mode} {len(ids)} 个媒体及视频文字。",
            finished=True,
        )


def _media_fingerprint(messages) -> str | None:
    """Reuse identical Telegram media only when captions/entities also match."""
    parts = []
    for message in messages:
        media = getattr(message, "document", None) or getattr(
            message, "photo", None
        )
        media_id = getattr(media, "id", None)
        if media_id is None:
            return None
        entities = [
            entity.to_dict()
            for entity in (message.entities or [])
            if type(entity).__name__ != "MessageEntitySpoiler"
        ]
        parts.append(
            (type(media).__name__, media_id, message.message, entities)
        )
    encoded = json.dumps(parts, sort_keys=True, default=str).encode()
    return "media:" + hashlib.sha256(encoded).hexdigest()


def _downloaded_files(directory: Path, messages) -> list[Path]:
    """Require exactly one complete file per resolved media-group member."""
    files = []
    for message in messages:
        matches = [
            path
            for path in directory.glob(f"{message.id}_*")
            if path.is_file() and not path.is_symlink()
        ]
        if len(matches) != 1 or matches[0].stat().st_size == 0:
            raise UserError(
                "tdl 未完整下载 media group，已停止上传并清理文件。"
            )
        file = matches[0]
        # Document sizes are exact; photos may use different resolutions.
        if message.document and file.stat().st_size != message.document.size:
            raise UserError("文件大小不完整，已停止上传并清理文件。")
        files.append(file)
    return files
