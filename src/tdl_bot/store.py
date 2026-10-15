"""Persist one workspace, optional personal targets and permission retries."""

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from tdl_bot.errors import UserError
from tdl_bot.i18n import tr


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

    def language(self, user_id: int | None) -> str:
        """Default to English independently of the Telegram client locale."""
        from tdl_bot.i18n import CATALOGS, DEFAULT_LANGUAGE

        row = self._db.execute(
            "SELECT value FROM workspace_metadata WHERE key=?",
            (f"language:{user_id}",),
        ).fetchone()
        return row[0] if row and row[0] in CATALOGS else DEFAULT_LANGUAGE

    def set_language(self, user_id: int, language: str) -> None:
        from tdl_bot.i18n import CATALOGS

        if language not in CATALOGS:
            raise ValueError("Unsupported language")
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO workspace_metadata (key, value) "
                "VALUES (?, ?)",
                (f"language:{user_id}", language),
            )

    def __init__(self, path: Path, *, migrate: bool = True) -> None:
        """Open the database and migrate legacy state once, with a backup."""
        self._db = sqlite3.connect(path)
        try:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > 7:
                raise UserError(
                    tr(
                        "The database is newer than this program. Use a "
                        "matching version."
                    )
                )
            if not migrate:
                if version < 5:
                    raise UserError(
                        tr(
                            "Wait for the task engine to initialize the "
                            "database before connecting."
                        )
                    )
                return
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
            if version < 4:
                if version == 3:
                    self._backup(path, suffix=".pre-results")
                with self._db:
                    self._db.execute("BEGIN")
                    self._db.execute(
                        "CREATE TABLE user_profiles ("
                        "user_id INTEGER PRIMARY KEY, "
                        "username TEXT, name TEXT NOT NULL)"
                    )
                    self._db.execute(
                        "CREATE TABLE transfer_results (url TEXT NOT NULL, "
                        "executor_key TEXT NOT NULL, "
                        "scope_key INTEGER NOT NULL, "
                        "chat TEXT NOT NULL, ids TEXT NOT NULL, "
                        "PRIMARY KEY(url, executor_key, scope_key))"
                    )
                    self._db.execute("PRAGMA user_version = 4")
            if version < 5:
                if version == 4:
                    self._backup(path, suffix=".pre-runtime")
                with self._db:
                    self._db.execute("BEGIN")
                    self._db.execute(
                        "CREATE TABLE runtime_jobs (id TEXT PRIMARY KEY, "
                        "payload TEXT NOT NULL)"
                    )
                    self._db.execute(
                        "CREATE TABLE accepted_requests ("
                        "operator INTEGER NOT NULL, "
                        "message INTEGER NOT NULL, payload TEXT NOT NULL, "
                        "PRIMARY KEY(operator, message))"
                    )
                    self._db.execute("PRAGMA user_version = 5")
            if version < 6:
                if version == 5:
                    self._backup(path, suffix=".pre-history")
                self._initialize_history()
            if version < 7:
                if version == 6:
                    self._backup(path, suffix=".pre-availability")
                self._initialize_availability()
        except BaseException:
            self._db.close()
            raise

    def _initialize_availability(self):
        """Support both early and complete v6 databases without data loss."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            columns = {
                row[1]
                for row in self._db.execute("PRAGMA table_info(stored_results)")
            }
            if "available" not in columns:
                self._db.execute(
                    "ALTER TABLE stored_results ADD COLUMN "
                    "available INTEGER NOT NULL DEFAULT 1"
                )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS stored_result_lookup "
                "ON stored_results(chat, ids, id)"
            )
            self._db.execute("PRAGMA user_version = 7")

    def _initialize_history(self):
        with self._db:
            self._db.execute("BEGIN")
            self._db.execute(
                "CREATE TABLE stored_results ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "executor_key TEXT NOT NULL, scope_key INTEGER NOT NULL, "
                "chat TEXT NOT NULL, ids TEXT NOT NULL, "
                "internal INTEGER NOT NULL, "
                "revealed INTEGER NOT NULL DEFAULT 0, "
                "created_at REAL NOT NULL, "
                "UNIQUE(executor_key, scope_key, chat, ids))"
            )
            self._db.execute(
                "CREATE INDEX stored_result_lookup "
                "ON stored_results(chat, ids, id)"
            )
            self._db.execute(
                "CREATE TABLE result_sources (url TEXT NOT NULL, "
                "result_id INTEGER "
                "NOT NULL, PRIMARY KEY(url, result_id))"
            )
            self._db.execute(
                "CREATE TABLE delivery_history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "result_id INTEGER NOT NULL, job_id TEXT NOT NULL, "
                "operator_id INTEGER NOT NULL, chat TEXT NOT NULL, "
                "topic_id INTEGER, "
                "ids TEXT NOT NULL, status TEXT NOT NULL, error TEXT, "
                "created_at REAL NOT NULL)"
            )
            target = self.internal_target()
            for url, executor, owner, chat, ids in self._db.execute(
                "SELECT url, executor_key, scope_key, chat, ids "
                "FROM transfer_results"
            ).fetchall():
                internal = bool(target and owner == 0 and chat == target.chat)
                self._db.execute(
                    "INSERT OR IGNORE INTO stored_results "
                    "(executor_key, scope_key, chat, ids, internal, "
                    "created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (executor, owner, chat, ids, internal, self.now()),
                )
                result_id = self._db.execute(
                    "SELECT id FROM stored_results WHERE executor_key=? "
                    "AND scope_key=? AND chat=? AND ids=?",
                    (executor, owner, chat, ids),
                ).fetchone()[0]
                self._db.execute(
                    "INSERT OR IGNORE INTO result_sources VALUES (?, ?)",
                    (url, result_id),
                )
            self._db.execute("PRAGMA user_version = 6")

    def _backup(self, path: Path, *, suffix: str = ".pre-workspace") -> None:
        """Keep a private, consistent copy before migrating an old database."""
        backup = path.with_name(path.name + suffix)
        try:
            fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise UserError(
                tr(
                    "A migration backup already exists but migration is "
                    "incomplete. Check the backup before retrying."
                )
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
                raise UserError(
                    tr(
                        "Initial membership requires valid operators and "
                        "administrators."
                    )
                )
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
            raise UserError(tr("Only administrators can manage accounts."))

    def _protect_admin(self, user_id: int) -> None:
        member = self.member(user_id)
        if member and member.role == "admin" and member.active(self.now()):
            count = sum(
                item.role == "admin" and item.active(self.now())
                for item in self.members()
            )
            if count <= 1:
                raise UserError(
                    tr(
                        "You cannot ban, remove, or demote the last active "
                        "administrator."
                    )
                )

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
            raise UserError(tr("Account ID must be a positive integer."))
        if self.member(user_id):
            raise UserError(tr("This account is already a member."))
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
            raise UserError(tr("This account is not a member."))
        self._protect_admin(user_id)
        if actor_id == user_id:
            raise UserError(
                tr(
                    "You cannot remove your own administrator account "
                    "through the Bot."
                )
            )
        with self._db:
            self._db.execute(
                "DELETE FROM workspace_members WHERE user_id=?", (user_id,)
            )
            self._member_event(actor_id, user_id, "remove")

    def ban_member(self, actor_id: int, user_id: int, seconds: int) -> None:
        self._require_admin(actor_id)
        if self.member(user_id) is None:
            raise UserError(tr("This account is not a member."))
        if seconds not in {86400, 7 * 86400, 30 * 86400}:
            raise UserError(
                tr("Ban duration must be one day, one week, or 30 days.")
            )
        self._protect_admin(user_id)
        if actor_id == user_id:
            raise UserError(
                tr("You cannot ban your own administrator account.")
            )
        with self._db:
            self._db.execute(
                "UPDATE workspace_members SET banned_until=? WHERE user_id=?",
                (self.now() + seconds, user_id),
            )
            self._member_event(actor_id, user_id, f"ban:{seconds}")

    def unban_member(self, actor_id: int, user_id: int) -> None:
        self._require_admin(actor_id)
        if self.member(user_id) is None:
            raise UserError(tr("This account is not a member."))
        with self._db:
            self._db.execute(
                "UPDATE workspace_members SET banned_until=0 WHERE user_id=?",
                (user_id,),
            )
            self._member_event(actor_id, user_id, "unban")

    def set_member_role(self, actor_id: int, user_id: int, role: str) -> None:
        self._require_admin(actor_id)
        if role not in {"user", "admin"} or self.member(user_id) is None:
            raise UserError(
                tr("Specify an existing member and the user or admin role.")
            )
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
            raise UserError(
                tr(
                    "No destination is configured for this scope. Provide "
                    "a chat or channel."
                )
            )
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
            raise UserError(tr("The retry task has no upload destination."))
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

    def remember_user(self, user_id: int, username, name: str) -> None:
        with self._db:
            # A username may be reassigned; it is never an authorization key.
            if username:
                self._db.execute(
                    "UPDATE user_profiles SET username=NULL "
                    "WHERE lower(username)=lower(?) AND user_id!=?",
                    (username, user_id),
                )
            self._db.execute(
                "INSERT OR REPLACE INTO user_profiles VALUES (?, ?, ?)",
                (user_id, username, name),
            )

    def user_label(self, user_id: int) -> str:
        row = self._db.execute(
            "SELECT username, name FROM user_profiles WHERE user_id=?",
            (user_id,),
        ).fetchone()
        label = (
            (f"@{row[0]}" if row[0] else row[1])
            if row
            else tr("Username unknown")
        )
        return f"{label} (UID: {user_id})"

    def saved_results(self, urls, executor_key, *, internal_only=False):
        rows = []
        for url in urls:
            rows.extend(
                self._db.execute(
                    "SELECT r.scope_key, r.chat, r.ids FROM stored_results r "
                    "JOIN result_sources s ON s.result_id=r.id WHERE s.url=? "
                    "AND r.available=1 "
                    "AND (? IS NULL OR r.executor_key=?) "
                    "AND (?=0 OR r.internal=1) ORDER BY r.id DESC",
                    (url, executor_key, executor_key, internal_only),
                ).fetchall()
            )
        return [
            (owner or None, chat, json.loads(ids))
            for owner, chat, ids in dict.fromkeys(rows)
        ]

    def result_info(self, chat, ids):
        row = self._db.execute(
            "SELECT id, executor_key, revealed FROM stored_results "
            "WHERE chat=? AND ids=? ORDER BY id DESC LIMIT 1",
            (chat, json.dumps(ids)),
        ).fetchone()
        return (
            {"id": row[0], "executor_key": row[1], "revealed": bool(row[2])}
            if row
            else None
        )

    def mark_unavailable(self, chat, ids):
        with self._db:
            self._db.execute(
                "UPDATE stored_results SET available=0 WHERE chat=? AND ids=?",
                (chat, json.dumps(ids)),
            )

    def mark_revealed(self, result_id):
        with self._db:
            self._db.execute(
                "UPDATE stored_results SET revealed=1 WHERE id=?", (result_id,)
            )

    def add_result_sources(self, result_id, urls):
        with self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO result_sources VALUES (?, ?)",
                [(url, result_id) for url in set(urls)],
            )

    def save_result(
        self, urls, executor_key, owner_id, chat, ids, *, internal=None
    ):
        if not ids:
            return None
        if internal is None:
            target = self.internal_target()
            internal = bool(target and owner_id is None and chat == target.chat)
        with self._db:
            encoded = json.dumps(ids)
            self._db.execute(
                "INSERT OR IGNORE INTO stored_results "
                "(executor_key, scope_key, chat, ids, internal, "
                "created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    executor_key,
                    owner_id or 0,
                    chat,
                    encoded,
                    internal,
                    self.now(),
                ),
            )
            result_id = self._db.execute(
                "SELECT id FROM stored_results WHERE executor_key=? "
                "AND scope_key=? AND chat=? AND ids=?",
                (executor_key, owner_id or 0, chat, encoded),
            ).fetchone()[0]
            self._db.executemany(
                "INSERT OR IGNORE INTO result_sources VALUES (?, ?)",
                [(url, result_id) for url in set(urls)],
            )
        return result_id

    def record_delivery(self, result_id, job, ids, status, error=None):
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO delivery_history "
                "(result_id, job_id, operator_id, chat, topic_id, ids, "
                "status, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result_id,
                    job.id,
                    job.user_id,
                    job.target.chat,
                    job.target.topic_id,
                    json.dumps(ids or []),
                    status,
                    error,
                    self.now(),
                ),
            )

            return cursor.lastrowid

    def finish_delivery(self, attempt, ids, status, error=None):
        with self._db:
            self._db.execute(
                "UPDATE delivery_history SET ids=?, status=?, error=? "
                "WHERE id=?",
                (json.dumps(ids or []), status, error, attempt),
            )

    def internal_target(self) -> Target | None:
        target = self.target_for(None)
        if target is None or target.chat == "me":
            return None
        chat = target.chat
        if chat.isdigit() and int(chat) > 0:
            chat = str(-(1_000_000_000_000 + int(chat)))
        return Target(chat, target.topic_id)

    def recipient(self, operator_id: int) -> Destination:
        return Destination(
            self.target_for(operator_id) or Target(str(operator_id)),
            operator_id,
        )

    def convert_retries_to_recipients(self) -> None:
        with self._db:
            if self._db.execute(
                "SELECT 1 FROM workspace_metadata WHERE key='internal_storage'"
            ).fetchone():
                return
            for table in ("permission_retries", "permission_retry_archive"):
                rows = self._db.execute(
                    f"SELECT id, submitted_by FROM {table} WHERE scope_key=0"
                ).fetchall()
                for retry_id, operator in rows:
                    target = self.recipient(operator).target
                    self._db.execute(
                        f"UPDATE OR IGNORE {table} "
                        "SET scope_key=?, chat=?, topic_key=? "
                        "WHERE id=?",
                        (operator, target.chat, target.topic_id or 0, retry_id),
                    )
                    # A matching personal retry can already exist.
                    self._db.execute(
                        f"DELETE FROM {table} WHERE id=? AND scope_key=0",
                        (retry_id,),
                    )
            self._db.execute(
                "INSERT INTO workspace_metadata "
                "VALUES ('internal_storage', '1')"
            )

    def controls(self):
        return {
            tuple(map(int, key.split(":")[1:])): set(json.loads(value))
            for key, value in self._db.execute(
                "SELECT key, value FROM workspace_metadata "
                "WHERE key LIKE 'control:%'"
            )
        }

    def save_controls(self, controls):
        with self._db:
            self._db.execute(
                "DELETE FROM workspace_metadata WHERE key LIKE 'control:%'"
            )
            self._db.executemany(
                "INSERT INTO workspace_metadata VALUES (?, ?)",
                [
                    (f"control:{owner}:{message}", json.dumps(sorted(tokens)))
                    for (owner, message), tokens in controls.items()
                ],
            )

    def accepted_request(self, operator: int, message: int | None):
        if message is None:
            return None
        row = self._db.execute(
            "SELECT payload FROM accepted_requests "
            "WHERE operator=? AND message=?",
            (operator, message),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_job(self, payload) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO runtime_jobs VALUES (?, ?)",
                (payload["id"], json.dumps(payload)),
            )
            if payload["source_message_id"] is not None:
                self._db.execute(
                    "INSERT OR REPLACE INTO accepted_requests VALUES (?, ?, ?)",
                    (
                        payload["user_id"],
                        payload["source_message_id"],
                        json.dumps(payload),
                    ),
                )

    def remove_job(self, job_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM runtime_jobs WHERE id=?", (job_id,))

    def active_job_payloads(self):
        return [
            json.loads(row[0])
            for row in self._db.execute(
                "SELECT payload FROM runtime_jobs ORDER BY rowid"
            )
        ]

    def close(self) -> None:
        """Close the database."""
        self._db.close()
