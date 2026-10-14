"""Update fairness, verified atomic installation and subprocess teardown."""

import asyncio
import hashlib
import io
import os
import sys
import tarfile
from unittest.mock import AsyncMock

import pytest

from tdl_bot.errors import AccessDenied, UserError
from tdl_bot.tdl import Tdl, UpdateGate, extract_binary, verify_checksum


async def test_update_waits_for_active_job_and_precedes_new_jobs():
    gate = UpdateGate()
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    update_started = asyncio.Event()
    release_update = asyncio.Event()
    events = []

    async def old_job():
        async with gate.job():
            old_started.set()
            await release_old.wait()
            events.append("old completed")

    async def update():
        async with gate.update():
            events.append("update started")
            update_started.set()
            await release_update.wait()
            events.append("update completed")

    async def new_job():
        async with gate.job():
            events.append("new started")

    old = asyncio.create_task(old_job())
    await old_started.wait()
    updating = asyncio.create_task(update())
    await asyncio.sleep(0)
    new = asyncio.create_task(new_job())
    await asyncio.sleep(0)
    assert events == []
    release_old.set()
    await update_started.wait()
    assert events == ["old completed", "update started"]
    release_update.set()
    await asyncio.gather(old, updating, new)
    assert events[-2:] == ["update completed", "new started"]


async def test_cancelled_update_does_not_leave_jobs_blocked():
    gate = UpdateGate()

    async def update():
        async with gate.update():
            pytest.fail("update should not acquire gate")

    async with gate.job():
        task = asyncio.create_task(update())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with asyncio.timeout(1):
        async with gate.job():
            pass


def make_archive(binary: bytes, member_name="tdl") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo(member_name)
        member.size = len(binary)
        archive.addfile(member, io.BytesIO(binary))
    return output.getvalue()


def test_digest_and_archive_validation():
    archive = make_archive(b"executable")
    checksum = hashlib.sha256(archive).hexdigest()
    verify_checksum(archive, "asset.tar.gz", f"{checksum}  asset.tar.gz")
    assert extract_binary(archive, "asset.tar.gz") == b"executable"
    with pytest.raises(UserError, match="SHA-256"):
        verify_checksum(
            archive + b"corrupt", "asset.tar.gz", f"{checksum} asset.tar.gz"
        )
    with pytest.raises(UserError):
        extract_binary(make_archive(b"evil", "../../tdl"), "asset.tar.gz")


@pytest.mark.parametrize("corrupt", [False, True])
async def test_update_installs_only_verified_release(
    tmp_path, monkeypatch, corrupt
):
    binary = tmp_path / "tdl"
    binary.write_bytes(b"original")
    script = f"#!{sys.executable}\nprint('Version: 1.2.3')\n".encode()
    archive = make_archive(script)
    checksum = hashlib.sha256(archive).hexdigest()
    name = "tdl_Linux_64bit.tar.gz"
    monkeypatch.setattr("tdl_bot.tdl.release_asset_name", lambda: name)

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            return {
                "tag_name": "v1.2.3",
                "assets": [
                    {"name": asset, "browser_download_url": asset}
                    for asset in (name, "tdl_checksums.txt")
                ],
            }

    class Session(Response):
        def __init__(self, **kwargs):
            pass

        def get(self, url):
            return Response()

    async def fetch(session, url, limit):
        if url == "tdl_checksums.txt":
            return f"{checksum} {name}".encode()
        return archive + b"corrupt" if corrupt else archive

    monkeypatch.setattr("tdl_bot.tdl.aiohttp.ClientSession", Session)
    monkeypatch.setattr("tdl_bot.tdl._fetch", fetch)
    tdl = Tdl(binary, "default")
    if corrupt:
        with pytest.raises(UserError, match="SHA-256"):
            await tdl.update("v1.2.3")
        assert binary.read_bytes() == b"original"
    else:
        assert await tdl.update("v1.2.3") == "Version: 1.2.3"
        assert binary.read_bytes() == script
        assert os.access(binary, os.X_OK)
    assert not list(tmp_path.glob(".tdl-update-*"))


async def test_download_arguments_preserve_urls_without_shell(tmp_path):
    tdl = Tdl(tmp_path / "tdl", "account")
    tdl._run = AsyncMock()
    urls = ["https://t.me/channel/1", "https://t.me/channel/2"]
    await tdl.download(urls, tmp_path)
    args = tdl._run.call_args.args[0]
    assert args[:2] == ["--ns", "account"]
    assert args[-4:] == ["--url", urls[0], "--url", urls[1]]


@pytest.mark.parametrize("cancel", [True, False])
async def test_subprocess_is_reaped_on_cancel_or_timeout(tmp_path, cancel):
    binary = tmp_path / "tdl"
    pid_path = tmp_path / "pid"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    binary.chmod(0o755)
    tdl = Tdl(binary, "default", timeout=1)
    task = asyncio.create_task(tdl._run(["download"]))
    async with asyncio.timeout(3):
        while not pid_path.exists():  # noqa: ASYNC110 - subprocess IPC file
            await asyncio.sleep(0.01)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(UserError, match="超时"):
            await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)


async def test_tdl_permission_errors_are_recognized(tmp_path):
    binary = tmp_path / "tdl"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import sys\nprint('rpc error: CHANNEL_PRIVATE')\nsys.exit(1)\n"
    )
    binary.chmod(0o755)
    with pytest.raises(AccessDenied):
        await Tdl(binary, "default")._run(["download"])
