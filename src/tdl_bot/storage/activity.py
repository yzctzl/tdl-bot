"""Persist only submitted media links and explicit chat-join actions."""

import json
import time


class ActivityStore:
    def __init__(self, db):
        self.db = db

    def get(self, key):
        row = self.db.execute(
            "SELECT kind, user_id, created, payload FROM activity_events "
            "WHERE id=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return dict(
            id=key,
            kind=row[0],
            user_id=row[1],
            created=row[2],
            **json.loads(row[3]),
        )

    def create(self, key, kind, user_id, **payload):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO activity_events VALUES (?, ?, ?, ?, ?)",
                (key, kind, user_id, time.time(), json.dumps(payload)),
            )
        return self.get(key)

    def update(self, key, **changes):
        event = self.get(key)
        payload = {
            name: value
            for name, value in event.items()
            if name not in {"id", "kind", "user_id", "created"}
        }
        payload.update(changes)
        with self.db:
            self.db.execute(
                "UPDATE activity_events SET payload=? WHERE id=?",
                (json.dumps(payload), key),
            )
        return self.get(key)

    def list(self, user_id=None, *, offset=0, limit=20):
        rows = self.db.execute(
            "SELECT id FROM activity_events "
            "WHERE (? IS NULL OR user_id=?) ORDER BY created DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (user_id, user_id, limit, offset),
        ).fetchall()
        return [self.get(row[0]) for row in rows]
