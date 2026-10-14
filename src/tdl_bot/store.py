"""Persist per-user destinations and permission retries in SQLite."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Target:
    """A destination chat and optional forum topic."""

    chat: str
    topic_id: int | None = None


class Store:
    """Small synchronous transactions used only on the event-loop thread."""

    def __init__(self, path: Path) -> None:
        """Open a database and initialize its schema."""
        self._db = sqlite3.connect(path)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS targets (
                user_id INTEGER PRIMARY KEY,
                chat TEXT NOT NULL,
                topic_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS pending (
                user_id INTEGER PRIMARY KEY,
                url TEXT NOT NULL
            );
            """
        )

    def target(self, user_id: int) -> Target | None:
        """Read a user's destination."""
        row = self._db.execute(
            "SELECT chat, topic_id FROM targets WHERE user_id = ?", (user_id,)
        ).fetchone()
        return Target(*row) if row else None

    def set_target(self, user_id: int, target: Target) -> None:
        """Atomically save a user's destination."""
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO targets VALUES (?, ?, ?)",
                (user_id, target.chat, target.topic_id),
            )

    def pending(self, user_id: int) -> str | None:
        """Read the most recent url awaiting permission."""
        row = self._db.execute(
            "SELECT url FROM pending WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0] if row else None

    def set_pending(self, user_id: int, url: str) -> None:
        """Remember a failed url across bot restarts."""
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO pending VALUES (?, ?)", (user_id, url)
            )

    def clear_pending(self, user_id: int, url: str) -> None:
        """Clear a successful retry without erasing a newer pending url."""
        with self._db:
            self._db.execute(
                "DELETE FROM pending WHERE user_id = ? AND url = ?",
                (user_id, url),
            )

    def close(self) -> None:
        """Close the database."""
        self._db.close()
