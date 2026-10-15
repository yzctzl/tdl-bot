"""Task snapshots and cancellation-aware queue ownership."""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from uuid import uuid4

from tdl_bot.storage.models import Destination, Target


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
    stage: str = "Queued"
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
