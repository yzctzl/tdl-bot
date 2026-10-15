"""Environment-based configuration."""

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from tdl_bot.core.i18n import tr


@dataclass(frozen=True)
class Settings:
    """Settings shared by the CLI and bot services."""

    bot_token: str = field(repr=False)
    api_id: int
    api_hash: str = field(repr=False)
    allowed_users: frozenset[int]
    admin_users: frozenset[int]
    data_dir: Path
    tdl_binary: Path
    tdl_namespace: str = "default"
    tdl_dir: Path = Path(".tdl")
    session_source: str = "tdl"
    tdl_proxy: str = field(default="", repr=False)
    workers: int = 2
    queue_size: int = 20
    download_timeout: int = 7200
    upload_concurrency: int = 8
    upload_timeout: int = 7200

    @property
    def session_path(self) -> Path:
        """Return the user-account session path."""
        return self.data_dir / "telegram"

    @classmethod
    def from_env(cls, *, login_only: bool = False) -> "Settings":
        """Read and validate settings from the environment and .env.

        Args:
            login_only: Allow login before Bot credentials and admin setup.

        Returns:
            Validated settings with absolute filesystem paths.

        Raises:
            ValueError: A required setting is missing or malformed.
        """
        load_dotenv(Path.cwd() / ".env")

        def ids(name: str) -> frozenset[int]:
            result = frozenset(
                int(value.strip())
                for value in os.getenv(name, "").split(",")
                if value.strip()
            )
            if any(value <= 0 for value in result):
                raise ValueError(
                    tr("{p0} must contain positive integer user IDs.", p0=name)
                )
            return result

        token = os.getenv("BOT_TOKEN", "").strip()
        api_id = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
        api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        data_dir = Path(os.getenv("DATA_DIR", "data")).resolve()
        persisted = persisted_members(data_dir / "state.sqlite3")
        if persisted is not None:
            allowed, admins = persisted
        else:
            admins = ids("ADMIN_USER_IDS")
            allowed = admins
            if not login_only and not admins:
                raise ValueError(
                    tr("Set ADMIN_USER_IDS before the first start.")
                )
        source = os.getenv("SESSION_SOURCE", "tdl").strip()
        if source not in {"tdl", "telethon"}:
            raise ValueError(tr("SESSION_SOURCE must be tdl or telethon."))
        if source == "telethon" and (api_id <= 0 or not api_hash):
            raise ValueError(tr("Set TELEGRAM_API_ID and TELEGRAM_API_HASH."))
        if not login_only and not token:
            raise ValueError(tr("Set BOT_TOKEN."))
        settings = cls(
            bot_token=token,
            api_id=api_id,
            api_hash=api_hash,
            allowed_users=allowed,
            admin_users=admins,
            data_dir=data_dir,
            tdl_binary=Path(os.getenv("TDL_BINARY", "bin/tdl")).resolve(),
            tdl_namespace=os.getenv("TDL_NAMESPACE", "default"),
            tdl_dir=Path(os.getenv("TDL_DIR", ".tdl")).expanduser().resolve(),
            session_source=source,
            tdl_proxy=os.getenv("TDL_PROXY", "").strip(),
            workers=int(os.getenv("WORKERS", "2")),
            queue_size=int(os.getenv("QUEUE_SIZE", "20")),
            download_timeout=int(os.getenv("DOWNLOAD_TIMEOUT", "7200")),
            upload_concurrency=int(os.getenv("UPLOAD_CONCURRENCY", "8")),
            upload_timeout=int(os.getenv("UPLOAD_TIMEOUT", "7200")),
        )
        if (
            min(
                settings.workers, settings.queue_size, settings.download_timeout
            )
            <= 0
        ):
            raise ValueError(
                tr(
                    "WORKERS, QUEUE_SIZE, and DOWNLOAD_TIMEOUT must be "
                    "positive."
                )
            )
        if settings.upload_timeout <= 0:
            raise ValueError(tr("UPLOAD_TIMEOUT must be positive."))
        if not 1 <= settings.upload_concurrency <= 32:
            raise ValueError(tr("UPLOAD_CONCURRENCY must be between 1 and 32."))
        return settings


def persisted_members(path: Path):
    """Read initialized membership without creating or migrating a database."""
    if not path.is_file():
        return None
    try:
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        ) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='workspace_metadata'"
            ).fetchone():
                return None
            if not db.execute(
                "SELECT 1 FROM workspace_metadata WHERE key='members_seeded'"
            ).fetchone():
                return None
            rows = db.execute(
                "SELECT user_id, role FROM workspace_members"
            ).fetchall()
            return (
                frozenset(row[0] for row in rows),
                frozenset(row[0] for row in rows if row[1] == "admin"),
            )
    except sqlite3.Error as exc:
        raise ValueError(
            tr("Cannot read the initialized membership database.")
        ) from exc
