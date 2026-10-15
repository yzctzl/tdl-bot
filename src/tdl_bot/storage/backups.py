"""Durable backup snapshots; all mutations belong to the task engine."""

import json
from dataclasses import asdict, dataclass, field, fields
from uuid import uuid4

from tdl_bot.storage.models import Target


@dataclass
class Backup:
    owner: int
    kind: str
    source: int
    source_ref: str
    title: str
    executor: str
    storage: Target
    target: Target | None = None
    interval: int = 300
    cursor: int = 0
    upper: int = 0
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    state: str = "active"
    phase: str = "idle"
    inflight: int = 0
    processed: int = 0
    messages: int = 0
    next_run: float = 0
    attempts: int = 0
    error: str = ""
    request_id: int | None = None

    def payload(self):
        return {
            item.name: getattr(self, item.name) for item in fields(self)
        } | {
            "storage": asdict(self.storage),
            "target": asdict(self.target) if self.target else None,
        }

    @classmethod
    def restore(cls, payload):
        values = dict(payload)
        values["storage"] = Target(**values["storage"])
        if values.get("target"):
            values["target"] = Target(**values["target"])
        return cls(**values)


class BackupStore:
    def __init__(self, db):
        self.db = db

    def all(self):
        return [
            Backup.restore(json.loads(row[0]))
            for row in self.db.execute(
                "SELECT payload FROM backup_tasks ORDER BY rowid"
            )
        ]

    def save(self, task):
        with self.db:
            self.db.execute(
                "INSERT INTO backup_tasks VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (task.id, json.dumps(task.payload())),
            )
