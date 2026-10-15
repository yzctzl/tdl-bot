"""Reuse tdl's existing gotd authorization in an in-memory Telethon session.

tdl stores namespace keys 'session' and 'app' in Bolt. Its backup command
serializes map[namespace]map[key][]byte as base64-valued JSON, then Zstandard.
The session value is gotd's {Version: 1, Data: {DC, Addr, AuthKey, ...}} JSON.
The public app definitions match data/tdl/pkg/tclient/app.go.
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

import zstandard
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr
from tdl_bot.integrations.tdl.runner import Tdl

_APPS = {
    "builtin": (15055931, "021d433426cbb920eeb95164498fe3d3"),
    "desktop": (2040, "b18441a1ff607e10a989891a5462e627"),
}
_MAX_BACKUP_BYTES = 128 * 1024**2


@dataclass(frozen=True)
class TdlSession:
    """Imported credentials; sensitive values are excluded from repr."""

    session: MemorySession = field(repr=False)
    api_id: int
    api_hash: str = field(repr=False)


async def load_tdl_session(tdl: Tdl, temporary_root: Path) -> TdlSession:
    """Load authorization without a phone login or permanent session copy.

    Args:
        tdl: The executable, storage directory and namespace to reuse.
        temporary_root: Bot-owned directory for an owner-only temporary backup.

    Returns:
        A Telethon MemorySession using tdl's existing authorization key and
        the API application with which that key was created.

    Raises:
        UserError: The namespace or session is absent, invalid or unsupported.
    """
    # TemporaryDirectory is mode 0700 even if the caller has a permissive umask.
    with tempfile.TemporaryDirectory(
        prefix=".tdl-session-", dir=temporary_root
    ) as directory:
        backup = Path(directory) / "session.backup.tdl"
        await tdl.backup(backup)
        return await asyncio.to_thread(_read_backup, backup, tdl.namespace)


async def load_tdl_sessions(
    tdl: Tdl, temporary_root: Path
) -> dict[str, TdlSession]:
    """Discover logged-in namespaces through one credential-private backup."""
    with tempfile.TemporaryDirectory(
        prefix=".tdl-session-", dir=temporary_root
    ) as directory:
        backup = Path(directory) / "sessions.backup.tdl"
        await tdl.backup(backup)
        metadata = await asyncio.to_thread(_read_metadata, backup)
    sessions = {}
    for namespace, pairs in sorted(metadata.items()):
        if (
            not namespace
            or Path(namespace).name != namespace
            or not isinstance(pairs, dict)
            or "session" not in pairs
        ):
            continue
        try:
            sessions[namespace] = decode_session(metadata, namespace)
        except UserError:
            logging.getLogger(__name__).warning(
                "Skipping an invalid tdl authorization during discovery"
            )
    return sessions


def _read_backup(path: Path, namespace: str) -> TdlSession:
    return decode_session(_read_metadata(path), namespace)


def _read_metadata(path: Path) -> dict:
    """Bound decompression and remove credentials from parse error messages."""
    try:
        with path.open("rb") as file:
            with zstandard.ZstdDecompressor().stream_reader(file) as reader:
                raw = reader.read(_MAX_BACKUP_BYTES + 1)
        if len(raw) > _MAX_BACKUP_BYTES:
            raise ValueError("backup exceeds limit")
        metadata = json.loads(raw)
        if not isinstance(metadata, dict):
            raise ValueError("invalid backup metadata")
        return metadata
    except (OSError, ValueError, zstandard.ZstdError) as exc:
        raise UserError(
            tr(
                "Cannot read the tdl session backup. Check the storage "
                "integrity."
            )
        ) from exc


def decode_session(metadata: dict, namespace: str) -> TdlSession:
    """Validate gotd's schema and initialize the existing DC and auth key."""
    try:
        pairs = metadata[namespace]
        stored = json.loads(base64.b64decode(pairs["session"], validate=True))
        if stored["Version"] != 1:
            raise ValueError("unsupported gotd version")
        data = stored["Data"]
        key = base64.b64decode(data["AuthKey"], validate=True)
        if len(key) != 256:
            raise ValueError("invalid auth key")
        if data.get("AuthKeyID"):
            key_id = base64.b64decode(data["AuthKeyID"], validate=True)
            if key_id != hashlib.sha1(key).digest()[-8:]:
                raise ValueError("auth key ID mismatch")
        dc = data["DC"]
        if type(dc) is not int or not 1 <= dc <= 5:
            raise ValueError("invalid DC")
        if data.get("Config", {}).get("TestMode"):
            raise ValueError("test DCs are unsupported")
        # Desktop-imported sessions may have an empty Addr. In that case
        # select a regular DC endpoint from the saved Telegram config.
        if data.get("Addr"):
            host, port = data["Addr"].rsplit(":", 1)
        else:
            options = [
                option
                for option in data.get("Config", {}).get("DCOptions", [])
                if option["ID"] == dc
                and not option.get("MediaOnly")
                and not option.get("CDN")
                and not option.get("TCPObfuscatedOnly")
                and not option.get("Secret")
            ]
            options.sort(key=lambda option: bool(option.get("Ipv6")))
            if not options:
                raise ValueError("no saved endpoint for session DC")
            host, port = options[0]["IPAddress"], options[0]["Port"]
        address = str(ipaddress.ip_address(host.strip("[]")))
        port = int(port)
        if not 0 < port <= 65535:
            raise ValueError("invalid port")
        app = (
            base64.b64decode(pairs["app"], validate=True).decode("utf-8")
            if "app" in pairs
            else "builtin"
        )
        api_id, api_hash = _APPS[app]
        session = MemorySession()
        session.set_dc(dc, address, port)
        session.auth_key = AuthKey(key)
        return TdlSession(session, api_id, api_hash)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise UserError(
            tr(
                "The tdl namespace has no valid session or uses an "
                "unsupported format. Check TDL_DIR, TDL_NAMESPACE, and the "
                "original tdl login."
            )
        ) from exc


def proxy_settings(url: str) -> dict | None:
    """Use the same configured proxy as tdl for the shared auth key."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"socks5", "socks4", "http"}
            or not parts.hostname
            or not parts.port
        ):
            raise ValueError("unsupported proxy")
        return {
            "proxy_type": parts.scheme,
            "addr": parts.hostname,
            "port": parts.port,
            "rdns": True,
            "username": unquote(parts.username) if parts.username else None,
            "password": unquote(parts.password) if parts.password else None,
        }
    except ValueError as exc:
        raise UserError(
            tr("TDL_PROXY must be a socks5, socks4, or http proxy URL.")
        ) from exc
