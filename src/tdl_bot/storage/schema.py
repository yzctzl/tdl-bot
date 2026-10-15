"""Ordered SQLite migrations and private backups of older schemas."""

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr
from tdl_bot.storage.models import Destination

if TYPE_CHECKING:
    from tdl_bot.storage.store import Store


class SchemaMigrations:
    def __init__(self, store: "Store"):
        self.store = store
        self._db = store._db

    def run(self, path: Path, *, migrate: bool):
        try:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > 8:
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
            if version < 8:
                if version == 7:
                    self._backup(path, suffix=".pre-backups")
                with self._db:
                    self._db.execute("BEGIN IMMEDIATE")
                    self._db.execute(
                        "CREATE TABLE IF NOT EXISTS backup_tasks "
                        "(id TEXT PRIMARY KEY, "
                        "payload TEXT NOT NULL)"
                    )
                    self._db.execute("PRAGMA user_version = 8")
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
            target = self.store.internal_target()
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
                    (executor, owner, chat, ids, internal, self.store.now()),
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
                    target = self.store.target(operator)
                    if target:
                        self.store._insert_pending(
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
            for member in self.store.members():
                if member.role == "user":
                    self.store._archive_excess(member.user_id)
            self._db.execute("PRAGMA user_version = 3")
