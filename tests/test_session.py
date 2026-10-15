"""Existing tdl authorization import without login or persistent copies."""

import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import zstandard

from tdl_bot.cli import run
from tdl_bot.config import Settings
from tdl_bot.errors import UserError
from tdl_bot.session import decode_session, load_tdl_session, proxy_settings
from tdl_bot.tdl import Tdl

pytestmark = pytest.mark.usefixtures("chinese")


def metadata(app="builtin", address="149.154.167.51:443"):
    key = bytes(range(256))
    stored = {
        "Version": 1,
        "Data": {
            "DC": 2,
            "Addr": address,
            "AuthKey": base64.b64encode(key).decode(),
            "AuthKeyID": base64.b64encode(
                hashlib.sha1(key).digest()[-8:]
            ).decode(),
            "Config": {
                "DCOptions": [
                    {
                        "ID": 2,
                        "IPAddress": "2001:db8::1",
                        "Port": 443,
                        "Ipv6": True,
                    },
                    {
                        "ID": 2,
                        "IPAddress": "192.0.2.2",
                        "Port": 443,
                        "MediaOnly": True,
                    },
                    {"ID": 2, "IPAddress": "149.154.167.51", "Port": 443},
                ]
            },
        },
    }
    return {
        "default": {
            "session": base64.b64encode(json.dumps(stored).encode()).decode(),
            "app": base64.b64encode(app.encode()).decode(),
        }
    }


@pytest.mark.parametrize(
    "app, expected_id", [("builtin", 15055931), ("desktop", 2040)]
)
@pytest.mark.parametrize(
    "address", ["149.154.167.51:443", "", "[2001:db8::1]:443"]
)
def test_imports_existing_key_dc_and_app(app, expected_id, address):
    imported = decode_session(metadata(app, address), "default")
    assert imported.api_id == expected_id
    assert imported.session.auth_key.key == bytes(range(256))
    assert imported.session.dc_id == 2
    assert imported.session.port == 443
    assert imported.session.server_address == (
        "2001:db8::1" if address.startswith("[") else "149.154.167.51"
    )
    assert "api_hash" not in repr(imported)


@pytest.mark.parametrize(
    "invalid", ["namespace", "version", "key", "key_id", "app"]
)
def test_rejects_invalid_session_without_falling_back_to_login(invalid):
    data = metadata()
    if invalid == "namespace":
        data = {"other": data["default"]}
    elif invalid == "app":
        data["default"]["app"] = base64.b64encode(b"unknown").decode()
    else:
        stored = json.loads(base64.b64decode(data["default"]["session"]))
        if invalid == "version":
            stored["Version"] = 99
        elif invalid == "key":
            stored["Data"]["AuthKey"] = base64.b64encode(b"short").decode()
        else:
            stored["Data"]["AuthKeyID"] = base64.b64encode(b"wrong-id").decode()
        data["default"]["session"] = base64.b64encode(
            json.dumps(stored).encode()
        ).decode()
    with pytest.raises(UserError):
        decode_session(data, "default")


@pytest.mark.parametrize("fail", [False, True])
async def test_private_temporary_backup_is_removed_on_every_exit(
    tmp_path, fail
):
    async def backup(destination):
        assert destination.parent.stat().st_mode & 0o077 == 0
        encoded = zstandard.ZstdCompressor().compress(
            json.dumps(metadata()).encode()
        )
        destination.write_bytes(encoded)
        if fail:
            raise UserError("backup failed")

    tdl = SimpleNamespace(backup=backup, namespace="default")
    if fail:
        with pytest.raises(UserError):
            await load_tdl_session(tdl, tmp_path)
    else:
        result = await load_tdl_session(tdl, tmp_path)
        assert result.session.auth_key.key == bytes(range(256))
    assert not list(tmp_path.iterdir())


async def test_cancelled_backup_is_cleaned(tmp_path):
    started = asyncio.Event()

    async def backup(destination):
        destination.write_bytes(b"partial backup")
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        load_tdl_session(
            SimpleNamespace(backup=backup, namespace="default"), tmp_path
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("legacy", [False, True])
async def test_backup_and_download_use_same_existing_storage(tmp_path, legacy):
    directory = tmp_path / "tdl,storage"
    directory.mkdir()
    if legacy:
        (directory / "data.kv").touch()
    else:
        (directory / "data").mkdir()
        (directory / "data/default").touch()
    tdl = Tdl(tmp_path / "binary", "default", data_dir=directory)
    tdl._run = AsyncMock()
    await tdl.backup(tmp_path / "backup")
    backup_args = tdl._run.call_args.args[0]
    await tdl.download(["https://t.me/channel/1"], tmp_path)
    download_args = tdl._run.call_args.args[0]
    assert backup_args[:4] == download_args[:4]
    assert backup_args[0] == "--storage"
    assert (
        "type=legacy" in backup_args[1]
        if legacy
        else "type=bolt" in backup_args[1]
    )
    assert '"path=' in backup_args[1]  # CSV-quoted path containing a comma
    assert str(directory) in backup_args[1]


def test_missing_namespace_does_not_create_empty_database(tmp_path):
    with pytest.raises(UserError, match="没有指定 namespace"):
        Tdl(
            tmp_path / "binary", "default", data_dir=tmp_path / ".tdl"
        ).storage_args()
    assert not (tmp_path / ".tdl").exists()


@pytest.mark.parametrize("source", ["tdl", "telethon"])
def test_api_credentials_required_only_for_independent_login(
    tmp_path, monkeypatch, source
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", "configured")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("ADMIN_USER_IDS", "1")
    monkeypatch.setenv("SESSION_SOURCE", source)
    monkeypatch.setenv("TELEGRAM_API_ID", "")
    monkeypatch.setenv("TELEGRAM_API_HASH", "")
    if source == "telethon":
        with pytest.raises(ValueError, match="TELEGRAM_API_ID"):
            Settings.from_env()
    else:
        settings = Settings.from_env()
        assert settings.tdl_dir == tmp_path / ".tdl"
        assert settings.session_source == "tdl"


async def test_login_command_only_checks_reused_authorization(
    tmp_path, monkeypatch
):
    imported = decode_session(metadata(), "default")
    monkeypatch.setattr(
        "tdl_bot.cli.load_tdl_session", AsyncMock(return_value=imported)
    )
    client = SimpleNamespace(
        connect=AsyncMock(),
        start=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=True),
        get_me=AsyncMock(return_value=SimpleNamespace(bot=False, id=123)),
        disconnect=AsyncMock(),
    )
    constructor = Mock(return_value=client)
    monkeypatch.setattr("tdl_bot.cli.SharedSessionClient", constructor)
    settings = Settings(
        "", 0, "", frozenset(), frozenset(), tmp_path, tmp_path / "tdl"
    )
    await run(settings, "login")
    assert constructor.call_args.args[0] is imported.session
    client.start.assert_not_awaited()
    client.connect.assert_awaited_once()
    assert not list(tmp_path.glob("*.session"))


def test_shared_proxy_settings_decode_credentials():
    assert proxy_settings("") is None
    proxy = proxy_settings("socks5://name:p%40ss@127.0.0.1:1080")
    assert proxy is not None
    assert proxy["proxy_type"] == "socks5"
    assert proxy["password"] == "p@ss"
    with pytest.raises(UserError, match="TDL_PROXY"):
        proxy_settings("https://127.0.0.1:1080")
