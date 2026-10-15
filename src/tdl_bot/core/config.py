"""Environment-based configuration."""

import os
import sqlite3
import tomllib
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from tdl_bot.core.i18n import tr


@dataclass(frozen=True)
class AccountBinding:
    """An operator's existing tdl session, configured only on the server."""

    operator_id: int
    tdl_dir: Path
    namespace: str
    proxy: str = field(default="", repr=False)


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
    account_bindings: tuple[AccountBinding, ...] = ()
    upload_concurrency: int = 8

    @property
    def session_path(self) -> Path:
        """Return the user-account session path."""
        return self.data_dir / "telegram"

    @classmethod
    def from_env(cls, *, login_only: bool = False) -> "Settings":
        """Read and validate settings from the environment and .env.

        Args:
            login_only: Allow login without bot credentials or an allowlist.

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
            allowed = ids("ALLOWED_USER_IDS")
            first_operator = next(
                (
                    int(value.strip())
                    for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
                    if value.strip()
                ),
                None,
            )
            admins = ids("ADMIN_USER_IDS") or (
                frozenset({first_operator}) if first_operator else frozenset()
            )
        source = os.getenv("SESSION_SOURCE", "tdl").strip()
        if source not in {"tdl", "telethon"}:
            raise ValueError(tr("SESSION_SOURCE must be tdl or telethon."))
        if source == "telethon" and (api_id <= 0 or not api_hash):
            raise ValueError(tr("Set TELEGRAM_API_ID and TELEGRAM_API_HASH."))
        if not login_only and (not token or not allowed):
            raise ValueError(tr("Set BOT_TOKEN and ALLOWED_USER_IDS."))
        if not admins <= allowed:
            raise ValueError(
                tr("ADMIN_USER_IDS must be a subset of ALLOWED_USER_IDS.")
            )
        settings = cls(
            bot_token=token,
            api_id=api_id,
            api_hash=api_hash,
            allowed_users=allowed,
            admin_users=admins,
            data_dir=data_dir,
            tdl_binary=Path(os.getenv("TDL_BINARY", "bin/tdl")).resolve(),
            tdl_namespace=os.getenv("TDL_NAMESPACE", "default"),
            tdl_dir=Path(os.getenv("TDL_DIR", ".tdl")).resolve(),
            session_source=source,
            tdl_proxy=os.getenv("TDL_PROXY", "").strip(),
            workers=int(os.getenv("WORKERS", "2")),
            queue_size=int(os.getenv("QUEUE_SIZE", "20")),
            download_timeout=int(os.getenv("DOWNLOAD_TIMEOUT", "7200")),
            upload_concurrency=int(os.getenv("UPLOAD_CONCURRENCY", "8")),
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
        if not 1 <= settings.upload_concurrency <= 32:
            raise ValueError(tr("UPLOAD_CONCURRENCY must be between 1 and 32."))
        account_file = Path(
            os.getenv("ACCOUNTS_FILE", str(settings.data_dir / "accounts.toml"))
        ).resolve()
        if account_file.is_file():
            from dataclasses import replace

            settings = replace(
                settings,
                account_bindings=read_account_bindings(account_file, settings),
            )
        elif os.getenv("ACCOUNTS_FILE", "").strip():
            raise ValueError(
                tr(
                    "The account configuration file specified by "
                    "ACCOUNTS_FILE does not exist."
                )
            )
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


def read_account_bindings(
    path: Path, settings: Settings
) -> tuple[AccountBinding, ...]:
    """Validate bindings without including account or proxy data in errors."""
    try:
        with path.open("rb") as file:
            config = tomllib.load(file)
        if set(config) - {"accounts"}:
            raise ValueError
        entries = config.get("accounts", [])
        if not isinstance(entries, list):
            raise ValueError
        result = []
        operators = set()
        sources = set()
        for entry in entries:
            if set(entry) - {"operator_id", "tdl_dir", "namespace", "proxy"}:
                raise ValueError
            operator = entry["operator_id"]
            namespace = entry["namespace"]
            directory = Path(entry.get("tdl_dir", settings.tdl_dir)).resolve()
            proxy = entry.get("proxy", settings.tdl_proxy)
            if (
                type(operator) is not int
                or operator <= 0
                or operator in operators
                or not isinstance(namespace, str)
                or not namespace
                or Path(namespace).name != namespace
                or not isinstance(proxy, str)
                or (directory, namespace) in sources
            ):
                raise ValueError
            operators.add(operator)
            sources.add((directory, namespace))
            result.append(AccountBinding(operator, directory, namespace, proxy))
        return tuple(result)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(
            tr(
                "Invalid account configuration. Check the accounts list, "
                "allowed users, directories, and unique namespaces."
            )
        ) from exc
