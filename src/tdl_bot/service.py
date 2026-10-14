"""Run bounded background transfers and clean temporary files."""

import asyncio
import logging
import shutil
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from telethon import errors

from tdl_bot.errors import AccessDenied, UserError
from tdl_bot.links import MessageLink
from tdl_bot.store import Store, Target
from tdl_bot.tdl import Tdl
from tdl_bot.telegram import TelegramMedia

_LOGGER = logging.getLogger(__name__)
Notify = Callable[..., Awaitable[None]]


@dataclass(eq=False)
class Job:
    """A queued url with a snapshot of the user's selected destination."""

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
        tdl: Tdl,
        media: TelegramMedia,
        store: Store,
        jobs_dir: Path,
        notify: Notify,
        *,
        workers: int = 2,
        queue_size: int = 20,
    ) -> None:
        """Initialize a bounded queue and a single active job per user."""
        self.tdl = tdl
        self.media = media
        self.store = store
        self.jobs_dir = jobs_dir
        self.notify = notify
        self._worker_count = workers
        self._queue = JobQueue(queue_size)
        self._users: dict[int, Job] = {}
        self._workers: list[asyncio.Task] = []
        self._closing = False

    def start(self) -> None:
        """Remove abandoned owned directories and start queue workers."""
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        for path in self.jobs_dir.glob("job-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        self._workers = [
            asyncio.create_task(self._worker())
            for _ in range(self._worker_count)
        ]

    async def submit(
        self, user_id: int, url: str, *, source_message_id: int | None = None
    ) -> Job:
        """Reserve a queue slot and create one progress reply before work."""
        MessageLink.parse(url)
        if self._closing:
            raise UserError("Bot 正在关闭，请稍后重试。")
        if user_id in self._users:
            raise UserError("你已有一个排队或执行中的任务，请等待完成。")
        target = self.store.target(user_id)
        if target is None:
            raise UserError("请先用 /target 设置上传目标。")
        job = Job(user_id, url, target, source_message_id=source_message_id)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull as exc:
            raise UserError("下载队列已满，请稍后重试。") from exc
        self._users[user_id] = job
        try:
            # Initial delivery is mandatory; never start an invisible job.
            await self.notify(job, "1/5 下载任务已加入队列。", finished=False)
        except BaseException:
            job.finished = True
            self._queue.discard(job)
            self._release(job)
            raise
        finally:
            job.ready.set()
        return job

    def busy(self, user_id: int) -> bool:
        """Report whether a user has an outstanding transfer."""
        return user_id in self._users

    def current_job(self, user_id: int, job_id: str) -> Job | None:
        """Resolve an owned live job; stale buttons never target newer jobs."""
        job = self._users.get(user_id)
        return job if job and job.id == job_id and not job.finished else None

    async def cancel(self, user_id: int, job_id: str) -> bool:
        """Cancel one owned job and wait for cleanup and its terminal edit."""
        job = self.current_job(user_id, job_id)
        if job is None:
            return False
        if job.cancel_requested:
            await job.done.wait()
            return True
        job.cancel_requested = True
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

    def _release(self, job: Job) -> None:
        """Release ownership without clearing a more recent job."""
        if self._users.get(job.user_id) is job:
            del self._users[job.user_id]

    async def _cancelled(self, job: Job) -> None:
        """Report cancellation only after the transfer has cleaned files."""
        _LOGGER.info("任务 user=%s phase=cancelled cleaned=true", job.user_id)
        text = "已取消，临时文件已清理。"
        if job.upload_started:
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
        try:
            await self.notify(job, text, finished=finished)
        except Exception:
            _LOGGER.warning("无法更新进度 user=%s", job.user_id)

    async def _worker(self) -> None:
        """Process jobs and always release per-user queue ownership."""
        while True:
            job = await self._queue.get()
            try:
                await job.ready.wait()
                if job.finished:
                    continue
                job.task = asyncio.create_task(self._transfer(job))
                await job.task
            except asyncio.CancelledError:
                if not job.cancel_requested:
                    raise
                await self._cancelled(job)
            except AccessDenied:
                self.store.set_pending(job.user_id, job.url)
                await self._notify(
                    job,
                    "下载账号无权访问来源群。请发送 /join 群主页或邀请 url；"
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

    async def _transfer(self, job: Job) -> None:
        """Resolve, download, verify and upload under one update lease."""
        async with self.tdl.gate.job():
            _LOGGER.info("任务 user=%s phase=resolve", job.user_id)
            await self._notify(job, "2/5 正在定位媒体组。")
            group = await self.media.resolve(MessageLink.parse(job.url))
            with tempfile.TemporaryDirectory(
                prefix="job-", dir=self.jobs_dir
            ) as temporary:
                directory = Path(temporary)
                _LOGGER.info(
                    "任务 user=%s phase=download media=%s",
                    job.user_id,
                    len(group.messages),
                )
                await self._notify(
                    job, f"3/5 正在下载 {len(group.messages)} 个媒体文件。"
                )
                await self.tdl.download(group.urls, directory)
                files = await asyncio.to_thread(
                    _downloaded_files, directory, group.messages
                )
                await self._notify(
                    job, f"4/5 正在上传 {len(files)} 个媒体文件。"
                )
                _LOGGER.info(
                    "任务 user=%s phase=upload media=%s target=%s topic=%s",
                    job.user_id,
                    len(files),
                    job.target.chat,
                    job.target.topic_id,
                )
                job.upload_started = True
                await self.media.upload(group, files, job.target)
            self.store.clear_pending(job.user_id, job.url)
            _LOGGER.info(
                "任务 user=%s phase=complete cleaned=true", job.user_id
            )
            await self._notify(
                job,
                f"5/5 完成：已上传 {len(group.messages)} 个媒体及视频文字。",
                finished=True,
            )


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
