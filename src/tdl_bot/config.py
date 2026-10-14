"""Environment-based configuration."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """Settings shared by the CLI and bot services."""

    bot_token: str
    api_id: int
    api_hash: str
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
                raise ValueError(f"{name} 必须包含正整数用户 ID。")
            return result

        token = os.getenv("BOT_TOKEN", "").strip()
        api_id = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
        api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        allowed = ids("ALLOWED_USER_IDS")
        admins = ids("ADMIN_USER_IDS") or allowed
        source = os.getenv("SESSION_SOURCE", "tdl").strip()
        if source not in {"tdl", "telethon"}:
            raise ValueError("SESSION_SOURCE 必须是 tdl 或 telethon。")
        if source == "telethon" and (api_id <= 0 or not api_hash):
            raise ValueError("请设置 TELEGRAM_API_ID 和 TELEGRAM_API_HASH。")
        if not login_only and (not token or not allowed):
            raise ValueError("请设置 BOT_TOKEN 和 ALLOWED_USER_IDS。")
        if not admins <= allowed:
            raise ValueError("ADMIN_USER_IDS 必须属于 ALLOWED_USER_IDS。")
        settings = cls(
            bot_token=token,
            api_id=api_id,
            api_hash=api_hash,
            allowed_users=allowed,
            admin_users=admins,
            data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
            tdl_binary=Path(os.getenv("TDL_BINARY", "bin/tdl")).resolve(),
            tdl_namespace=os.getenv("TDL_NAMESPACE", "default"),
            tdl_dir=Path(os.getenv("TDL_DIR", ".tdl")).resolve(),
            session_source=source,
            tdl_proxy=os.getenv("TDL_PROXY", "").strip(),
            workers=int(os.getenv("WORKERS", "2")),
            queue_size=int(os.getenv("QUEUE_SIZE", "20")),
            download_timeout=int(os.getenv("DOWNLOAD_TIMEOUT", "7200")),
        )
        if (
            min(
                settings.workers, settings.queue_size, settings.download_timeout
            )
            <= 0
        ):
            raise ValueError(
                "WORKERS、QUEUE_SIZE、DOWNLOAD_TIMEOUT 必须为正数。"
            )
        return settings
