"""Persist one workspace, optional personal targets and permission retries."""

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from tdl_bot.errors import UserError


@dataclass(frozen=True)
class Target:
    """A destination chat and optional forum topic."""

    chat: str
    topic_id: int | None = None


@dataclass(frozen=True)
class Destination:
    """A target plus its management scope; None means shared."""

    target: Target
    owner_id: int | None = None


@dataclass(frozen=True)
class PendingTransfer:
    """A failed transfer retaining its destination and execution account."""

    id: int
    url: str
    submitted_by: int
    destination: Destination
    executor_key: str


class Store:
    """Small workspace transactions used only on the event-loop thread."""

    def __init__(self, path: Path) -> None:
        """Open the database and migrate legacy state once, with a backup."""
        self._db = sqlite3.connect(path)
        try:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise UserError("数据库版本比当前程序更新，请使用匹配的版本。")
            if version == 0:
                tables = {
                    row[0]
                    for row in self._db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                legacy = {"targets", "pending"} <= tables
                if legacy:
                    self._backup(path)
                self._initialize(legacy)
        except BaseException:
            self._db.close()
            raise

    def _backup(self, path: Path) -> None:
        """Keep a private, consistent copy before migrating an old database."""
        backup = path.with_name(path.name + ".pre-workspace")
        try:
            fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise UserError(
                "已有迁移备份但数据库尚未迁移，请先检查备份后重试。"
            ) from None
        os.close(fd)
        try:
            destination = sqlite3.connect(backup)
            try:
                self._db.backup(destination)
            finally:
                destination.close()
        except BaseException:
            backup.unlink(missing_ok=True)
            raise

    def _initialize(self, legacy: bool) -> None:
        """Keep migration, imported records and version in one transaction."""
        with self._db:
            # Explicit BEGIN also makes SQLite DDL transactional.
            self._db.execute("BEGIN")
            self._db.execute(
                "CREATE TABLE workspace_targets ("
                "scope_key INTEGER PRIMARY KEY, chat TEXT NOT NULL, "
                "topic_id INTEGER)"
            )
            self._db.execute(
                "CREATE TABLE workspace_preferences ("
                "operator_id INTEGER PRIMARY KEY, private INTEGER NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE permission_retries ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, "
                "submitted_by INTEGER NOT NULL, scope_key INTEGER NOT NULL, "
                "chat TEXT NOT NULL, topic_key INTEGER NOT NULL, "
                "executor_key TEXT NOT NULL, "
                "UNIQUE(url, scope_key, chat, topic_key, executor_key))"
            )
            if legacy:
                targets = list(
                    self._db.execute(
                        "SELECT user_id, chat, topic_id FROM targets"
                    )
                )
                distinct = {(row[1], row[2]) for row in targets}
                shared = len(distinct) == 1
                if shared:
                    chat, topic = distinct.pop()
                    self._db.execute(
                        "INSERT INTO workspace_targets VALUES (0, ?, ?)",
                        (chat, topic),
                    )
                else:
                    for operator, chat, topic in targets:
                        self._db.execute(
                            "INSERT INTO workspace_targets VALUES (?, ?, ?)",
                            (operator, chat, topic),
                        )
                        self._db.execute(
                            "INSERT INTO workspace_preferences VALUES (?, 1)",
                            (operator,),
                        )
                for operator, url in self._db.execute(
                    "SELECT user_id, url FROM pending"
                ).fetchall():
                    target = self.target(operator)
                    if target:
                        self._insert_pending(
                            operator,
                            url,
                            Destination(target, None if shared else operator),
                            "shared",
                        )
                    else:
                        # Preserve old tables as an archive, including records
                        # without destinations that cannot yet be retried.
                        continue
            self._db.execute("PRAGMA user_version = 1")

    def private_selected(self, operator_id: int) -> bool:
        """Default every operator to the shared destination."""
        row = self._db.execute(
            "SELECT private FROM workspace_preferences WHERE operator_id=?",
            (operator_id,),
        ).fetchone()
        return bool(row and row[0])

    def destination(self, operator_id: int) -> Destination | None:
        """Resolve the selected scope without silently falling back."""
        owner = operator_id if self.private_selected(operator_id) else None
        target = self.target_for(owner)
        return Destination(target, owner) if target else None

    def target_for(self, owner_id: int | None) -> Target | None:
        """Read the shared target or a particular personal override."""
        row = self._db.execute(
            "SELECT chat, topic_id FROM workspace_targets WHERE scope_key=?",
            (owner_id or 0,),
        ).fetchone()
        return Target(*row) if row else None

    def target(self, operator_id: int) -> Target | None:
        """Read the operator's selected destination."""
        destination = self.destination(operator_id)
        return destination.target if destination else None

    def set_target(
        self, operator_id: int, target: Target, *, private: bool = False
    ) -> None:
        """Save a shared or personal target and select it for the operator."""
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO workspace_targets VALUES (?, ?, ?)",
                (operator_id if private else 0, target.chat, target.topic_id),
            )
            self._db.execute(
                "INSERT OR REPLACE INTO workspace_preferences VALUES (?, ?)",
                (operator_id, int(private)),
            )

    def select_target(self, operator_id: int, *, private: bool) -> None:
        """Switch scope only if a destination already exists."""
        if self.target_for(operator_id if private else None) is None:
            raise UserError("该范围尚未设置目标，请在命令后提供聊天／频道。")
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO workspace_preferences VALUES (?, ?)",
                (operator_id, int(private)),
            )

    def _insert_pending(
        self,
        operator_id: int,
        url: str,
        destination: Destination,
        executor_key: str,
    ) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO permission_retries "
            "(url, submitted_by, scope_key, chat, topic_key, executor_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                url,
                operator_id,
                destination.owner_id or 0,
                destination.target.chat,
                destination.target.topic_id or 0,
                executor_key,
            ),
        )

    def set_pending(
        self,
        operator_id: int,
        url: str,
        *,
        destination: Destination | None = None,
        executor_key: str = "shared",
    ) -> None:
        """Persist distinct failures without replacing another pending url."""
        destination = destination or self.destination(operator_id)
        if destination is None:
            raise UserError("待重试任务缺少上传目标。")
        with self._db:
            self._insert_pending(operator_id, url, destination, executor_key)

    def pending_items(self) -> list[PendingTransfer]:
        """Return ordered records for service-level permission filtering."""
        return [
            PendingTransfer(
                row[0],
                row[1],
                row[2],
                Destination(Target(row[4], row[5] or None), row[3] or None),
                row[6],
            )
            for row in self._db.execute(
                "SELECT id, url, submitted_by, scope_key, chat, topic_key, "
                "executor_key FROM permission_retries ORDER BY id"
            )
        ]

    def clear_pending(
        self,
        url: str,
        destination: Destination,
        executor_key: str,
        *,
        pending_id: int | None = None,
    ) -> None:
        """Clear only the successful destination/account combination."""
        with self._db:
            self._db.execute(
                "DELETE FROM permission_retries WHERE id=? OR (url=? "
                "AND scope_key=? AND chat=? AND topic_key=? "
                "AND executor_key=?)",
                (
                    pending_id,
                    url,
                    destination.owner_id or 0,
                    destination.target.chat,
                    destination.target.topic_id or 0,
                    executor_key,
                ),
            )

    def close(self) -> None:
        """Close the database."""
        self._db.close()
