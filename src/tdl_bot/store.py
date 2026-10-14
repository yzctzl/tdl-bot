"""Persist one workspace, optional personal targets and permission retries."""

import os
import sqlite3
import time
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
    archived: bool = False


@dataclass(frozen=True)
class Member:
    """A workspace operator; ban deadlines are stored as UTC timestamps."""

    user_id: int
    role: str
    banned_until: float = 0

    def active(self, now: float) -> bool:
        return self.banned_until <= now


class Store:
    """Small workspace transactions used only on the event-loop thread."""

    def __init__(self, path: Path) -> None:
        """Open the database and migrate legacy state once, with a backup."""
        self._db = sqlite3.connect(path)
        try:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > 3:
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
            if version < 2:
                if version == 1:
                    self._backup(path, suffix=".pre-members")
                self._initialize_members()
            if version < 3:
                if version == 2:
                    self._backup(path, suffix=".pre-retry-cap")
                self._initialize_retry_archive()
        except BaseException:
            self._db.close()
            raise

    def _backup(self, path: Path, *, suffix: str = ".pre-workspace") -> None:
        """Keep a private, consistent copy before migrating an old database."""
        backup = path.with_name(path.name + suffix)
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

    @staticmethod
    def now() -> float:
        return time.time()

    def _initialize_members(self) -> None:
        with self._db:
            self._db.execute("BEGIN")
            self._db.execute(
                "CREATE TABLE workspace_members (user_id INTEGER PRIMARY KEY, "
                "role TEXT NOT NULL CHECK(role IN ('user', 'admin')), "
                "banned_until REAL NOT NULL DEFAULT 0)"
            )
            self._db.execute(
                "CREATE TABLE workspace_metadata "
                "(key TEXT PRIMARY KEY, value TEXT)"
            )
            self._db.execute(
                "CREATE TABLE member_events "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "actor_id INTEGER NOT NULL, member_id INTEGER NOT NULL, "
                "action TEXT NOT NULL, created_at REAL NOT NULL)"
            )
            self._db.execute("PRAGMA user_version = 2")

    def _initialize_retry_archive(self) -> None:
        with self._db:
            self._db.execute("BEGIN")
            self._db.execute(
                "CREATE TABLE permission_retry_archive AS "
                "SELECT * FROM permission_retries WHERE 0"
            )
            self._db.execute(
                "CREATE UNIQUE INDEX retry_archive_id "
                "ON permission_retry_archive(id)"
            )
            for member in self.members():
                if member.role == "user":
                    self._archive_excess(member.user_id)
            self._db.execute("PRAGMA user_version = 3")

    def _archive_excess(self, operator_id: int) -> None:
        """Keep overflow for admins and enforce ordinary retry capacity."""
        rows = self._db.execute(
            "SELECT id FROM permission_retries WHERE submitted_by=? "
            "ORDER BY id LIMIT -1 OFFSET 3",
            (operator_id,),
        ).fetchall()
        for row in rows:
            self._db.execute(
                "INSERT INTO permission_retry_archive "
                "SELECT * FROM permission_retries WHERE id=?",
                row,
            )
            self._db.execute("DELETE FROM permission_retries WHERE id=?", row)

    def seed_members(
        self, operators: frozenset[int], admins: frozenset[int]
    ) -> None:
        """Use environment identities once, so removals survive restarts."""
        with self._db:
            if self._db.execute(
                "SELECT 1 FROM workspace_metadata WHERE key='members_seeded'"
            ).fetchone():
                return
            if not operators or not admins or not admins <= operators:
                raise UserError("首次初始化成员需要有效的操作账号和管理员。")
            self._db.executemany(
                "INSERT INTO workspace_members (user_id, role) VALUES (?, ?)",
                [
                    (operator, "admin" if operator in admins else "user")
                    for operator in operators
                ],
            )
            for operator in operators - admins:
                self._archive_excess(operator)
            self._db.execute(
                "INSERT INTO workspace_metadata VALUES ('members_seeded', '1')"
            )

    def members(self) -> list[Member]:
        return [
            Member(*row)
            for row in self._db.execute(
                "SELECT user_id, role, banned_until FROM workspace_members "
                "ORDER BY user_id"
            )
        ]

    def member(self, user_id: int) -> Member | None:
        row = self._db.execute(
            "SELECT user_id, role, banned_until FROM workspace_members "
            "WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return Member(*row) if row else None

    def menu_member_ids(self) -> frozenset[int]:
        return frozenset(
            row[0]
            for row in self._db.execute(
                "SELECT user_id FROM workspace_members UNION "
                "SELECT member_id FROM member_events"
            )
        )

    def _require_admin(self, actor_id: int) -> None:
        actor = self.member(actor_id)
        if (
            actor is None
            or actor.role != "admin"
            or not actor.active(self.now())
        ):
            raise UserError("只有管理员可以管理账号。")

    def _protect_admin(self, user_id: int) -> None:
        member = self.member(user_id)
        if member and member.role == "admin" and member.active(self.now()):
            count = sum(
                item.role == "admin" and item.active(self.now())
                for item in self.members()
            )
            if count <= 1:
                raise UserError("不能封禁、删除或降级最后一个有效管理员。")

    def _member_event(self, actor_id: int, user_id: int, action: str) -> None:
        self._db.execute(
            "INSERT INTO member_events "
            "(actor_id, member_id, action, created_at) "
            "VALUES (?, ?, ?, ?)",
            (actor_id, user_id, action, self.now()),
        )

    def add_member(self, actor_id: int, user_id: int) -> None:
        self._require_admin(actor_id)
        if user_id <= 0:
            raise UserError("账号 ID 必须为正整数。")
        if self.member(user_id):
            raise UserError("该账号已在成员列表中。")
        with self._db:
            self._db.execute(
                "INSERT INTO workspace_members (user_id, role) "
                "VALUES (?, 'user')",
                (user_id,),
            )
            self._archive_excess(user_id)
            self._member_event(actor_id, user_id, "add")

    def remove_member(self, actor_id: int, user_id: int) -> None:
        self._require_admin(actor_id)
        if self.member(user_id) is None:
            raise UserError("该账号不在成员列表中。")
        self._protect_admin(user_id)
        if actor_id == user_id:
            raise UserError("不能在 Bot 内删除自己的管理员账号。")
        with self._db:
            self._db.execute(
                "DELETE FROM workspace_members WHERE user_id=?", (user_id,)
            )
            self._member_event(actor_id, user_id, "remove")

    def ban_member(self, actor_id: int, user_id: int, seconds: int) -> None:
        self._require_admin(actor_id)
        if self.member(user_id) is None:
            raise UserError("该账号不在成员列表中。")
        if seconds not in {86400, 7 * 86400, 30 * 86400}:
            raise UserError("封禁时长必须是一天、一周或 30 天。")
        self._protect_admin(user_id)
        if actor_id == user_id:
            raise UserError("不能封禁自己的管理员账号。")
        with self._db:
            self._db.execute(
                "UPDATE workspace_members SET banned_until=? WHERE user_id=?",
                (self.now() + seconds, user_id),
            )
            self._member_event(actor_id, user_id, f"ban:{seconds}")

    def unban_member(self, actor_id: int, user_id: int) -> None:
        self._require_admin(actor_id)
        if self.member(user_id) is None:
            raise UserError("该账号不在成员列表中。")
        with self._db:
            self._db.execute(
                "UPDATE workspace_members SET banned_until=0 WHERE user_id=?",
                (user_id,),
            )
            self._member_event(actor_id, user_id, "unban")

    def set_member_role(self, actor_id: int, user_id: int, role: str) -> None:
        self._require_admin(actor_id)
        if role not in {"user", "admin"} or self.member(user_id) is None:
            raise UserError("请指定已有成员和 user／admin 角色。")
        if role == "user":
            self._protect_admin(user_id)
        with self._db:
            self._db.execute(
                "UPDATE workspace_members SET role=? WHERE user_id=?",
                (role, user_id),
            )
            if role == "user":
                self._archive_excess(user_id)
            self._member_event(actor_id, user_id, f"role:{role}")

    def member_history(self, actor_id: int, user_id: int):
        self._require_admin(actor_id)
        return self._db.execute(
            "SELECT actor_id, action, created_at FROM member_events "
            "WHERE member_id=? ORDER BY id DESC LIMIT 10",
            (user_id,),
        ).fetchall()

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
        limit: int | None = None,
    ) -> bool:
        """Persist distinct failures without replacing another pending url."""
        destination = destination or self.destination(operator_id)
        if destination is None:
            raise UserError("待重试任务缺少上传目标。")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            existing = self._db.execute(
                "SELECT 1 FROM permission_retries WHERE url=? AND scope_key=? "
                "AND chat=? AND topic_key=? AND executor_key=?",
                (
                    url,
                    destination.owner_id or 0,
                    destination.target.chat,
                    destination.target.topic_id or 0,
                    executor_key,
                ),
            ).fetchone()
            if existing:
                return True
            if limit is not None and self.pending_count(operator_id) >= limit:
                return False
            self._insert_pending(operator_id, url, destination, executor_key)
        return True

    def pending_count(self, operator_id: int) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM permission_retries WHERE submitted_by=?",
            (operator_id,),
        ).fetchone()[0]

    def delete_pending(self, pending_id: int) -> None:
        with self._db:
            for table in ("permission_retries", "permission_retry_archive"):
                self._db.execute(
                    f"DELETE FROM {table} WHERE id=?", (pending_id,)
                )

    def pending_items(
        self, *, include_archived: bool = False
    ) -> list[PendingTransfer]:
        """Return active retries, optionally including archived overflow."""
        columns = (
            "id, url, submitted_by, scope_key, chat, topic_key, executor_key"
        )
        sql = f"SELECT {columns}, 0 FROM permission_retries"
        if include_archived:
            sql += (
                f" UNION ALL SELECT {columns}, 1 FROM permission_retry_archive"
            )
        sql += " ORDER BY id"
        return [
            PendingTransfer(
                row[0],
                row[1],
                row[2],
                Destination(Target(row[4], row[5] or None), row[3] or None),
                row[6],
                bool(row[7]),
            )
            for row in self._db.execute(sql)
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
            for table in ("permission_retries", "permission_retry_archive"):
                self._db.execute(
                    f"DELETE FROM {table} WHERE id=? OR (url=? "
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
