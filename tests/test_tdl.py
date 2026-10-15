"""Update fairness, verified atomic installation and subprocess teardown."""

import asyncio
import hashlib
import io
import os
import sys
import tarfile
from unittest.mock import AsyncMock

import pytest

from tdl_bot.core.errors import AccessDenied, UserError
from tdl_bot.integrations.tdl.gate import UpdateGate
from tdl_bot.integrations.tdl.runner import Tdl
from tdl_bot.integrations.tdl.updates import (
    extract_binary,
    verify_checksum,
)

pytestmark = pytest.mark.usefixtures("chinese")


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


@pytest.mark.parametrize("state", ["update", "install", "corrupt"])
async def test_update_installs_only_verified_release(
    tmp_path, monkeypatch, state
):
    binary = tmp_path / "tdl"
    if state != "install":
        binary.write_bytes(b"original")
    script = f"#!{sys.executable}\nprint('Version: 1.2.3')\n".encode()
    archive = make_archive(script)
    checksum = hashlib.sha256(archive).hexdigest()
    name = "tdl_Linux_64bit.tar.gz"
    monkeypatch.setattr(
        "tdl_bot.integrations.tdl.updates.release_asset_name", lambda: name
    )

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
        return archive + b"corrupt" if state == "corrupt" else archive

    monkeypatch.setattr(
        "tdl_bot.integrations.tdl.updates.aiohttp.ClientSession", Session
    )
    monkeypatch.setattr("tdl_bot.integrations.tdl.updates._fetch", fetch)
    tdl = Tdl(binary, "default")
    actual_version = tdl.version

    async def version(path=None):
        return await actual_version(path) if path else "Version: 1.0.0"

    monkeypatch.setattr(tdl, "version", version)
    if state == "corrupt":
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
        f"assert all(os.environ[k] == {str(tmp_path)!r} "
        "for k in ('TMPDIR', 'TMP', 'TEMP'))\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    binary.chmod(0o755)
    tdl = Tdl(binary, "default", timeout=1)
    task = asyncio.create_task(
        tdl._run(["forward"], operation="clone", temporary_dir=tmp_path)
    )
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


async def test_tdl_permission_errors_are_recognized(tmp_path, caplog):
    binary = tmp_path / "tdl"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "print('rpc error: CHANNEL_PRIVATE private-caption-token')\n"
        "sys.exit(1)\n"
    )
    binary.chmod(0o755)
    with pytest.raises(AccessDenied):
        await Tdl(binary, "default")._run(["download"])
    with pytest.raises(UserError) as transfer_error:
        await Tdl(binary, "default")._run(["forward"], operation="clone")
    # Storage write errors must not become source-access pending retries.
    assert not isinstance(transfer_error.value, AccessDenied)
    assert "operation=clone exit=1 reason=permissions" in caplog.text
    assert "private-caption-token" not in caplog.text


@pytest.mark.parametrize("proxy", ["", "socks5://localhost:1080"])
async def test_executor_proxy_overrides_inherited_proxy(
    tmp_path, monkeypatch, proxy
):
    binary = tmp_path / "tdl"
    binary.write_text(
        f"#!{sys.executable}\n"
        'import os\nprint(os.environ.get("TDL_PROXY", "missing"))\n'
    )
    binary.chmod(0o755)
    monkeypatch.setenv("TDL_PROXY", "socks5://unrelated:1080")
    result = await Tdl(binary, "own", proxy=proxy)._run(["version"])
    assert result == proxy


async def test_shared_storage_lock_preserves_each_account_rpc_lock(tmp_path):
    from tdl_bot.integrations.telegram.client import AccountLock

    started = tmp_path / "started"
    binary = tmp_path / "tdl"
    binary.write_text(
        f"#!{sys.executable}\n"
        f"from pathlib import Path\nPath({str(started)!r}).touch()\n"
    )
    binary.chmod(0o755)
    storage = AccountLock()
    tdl = Tdl(binary, "own", storage_lock=storage)
    async with tdl.account_lock:
        task = asyncio.create_task(tdl._run(["version"]))
        await asyncio.sleep(0)
        assert not started.exists()
    await task
    assert started.exists()
    started.unlink()
    async with storage:
        task = asyncio.create_task(tdl._run(["version"]))
        await asyncio.sleep(0)
        assert not started.exists()
    await task
    assert started.exists()


@pytest.fixture
def update_release(monkeypatch):
    state = {"tag_name": "v1.2.3", "assets": []}

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            return state

    class Session(Response):
        def __init__(self, **kwargs):
            pass

        def get(self, url):
            return Response()

    monkeypatch.setattr(
        "tdl_bot.integrations.tdl.updates.aiohttp.ClientSession", Session
    )
    fetch = AsyncMock()
    monkeypatch.setattr("tdl_bot.integrations.tdl.updates._fetch", fetch)
    return state, fetch


@pytest.mark.parametrize(
    "installed,requested",
    [
        ("1.2.3", "v1.2.3"),
        ("1.10.0", "latest"),
    ],
)
async def test_current_version_skips_assets_and_never_blocks_jobs(
    tmp_path, update_release, installed, requested
):
    _, fetch = update_release
    binary = tmp_path / "tdl"
    binary.write_text(f'#!{sys.executable}\nprint("Version: {installed}")\n')
    binary.chmod(0o755)
    original = binary.read_bytes()
    tdl = Tdl(binary, "shared")
    async with tdl.gate.job(), tdl.account_lock:
        async with asyncio.timeout(2):
            result = await tdl.update(requested)
    assert result.startswith("无需更新")
    fetch.assert_not_awaited()
    assert binary.read_bytes() == original
    assert not list(tmp_path.glob(".tdl-update-*"))


async def test_waiting_update_rechecks_version_before_fetching(
    tmp_path, monkeypatch, update_release
):
    _, fetch = update_release
    (tmp_path / "tdl").touch()
    tdl = Tdl(tmp_path / "tdl", "shared")
    checked = asyncio.Event()
    calls = 0

    async def version(binary=None):
        nonlocal calls
        calls += 1
        checked.set()
        return "Version: 1.0.0" if calls == 1 else "Version: 1.2.3"

    monkeypatch.setattr(tdl, "version", version)
    async with tdl.gate.job():
        task = asyncio.create_task(tdl.update())
        await asyncio.wait_for(checked.wait(), 2)
        await asyncio.sleep(0)
        assert not task.done()
    result = await asyncio.wait_for(task, 2)
    assert result.startswith("无需更新")
    fetch.assert_not_awaited()
    assert calls == 2


async def test_unrecognized_version_preserves_binary_and_never_fetches(
    tmp_path, update_release
):
    _, fetch = update_release
    binary = tmp_path / "tdl"
    binary.write_text(f'#!{sys.executable}\nprint("unrecognized version")\n')
    binary.chmod(0o755)
    original = binary.read_bytes()
    with pytest.raises(UserError, match="无法识别"):
        await Tdl(binary, "shared").update()
    fetch.assert_not_awaited()
    assert binary.read_bytes() == original


async def test_release_must_match_requested_version(tmp_path, update_release):
    _, fetch = update_release
    with pytest.raises(UserError, match="请求不一致"):
        await Tdl(tmp_path / "tdl", "shared").update("v1.0.0")
    fetch.assert_not_awaited()


async def test_version_uses_disposable_storage_instead_of_account(
    tmp_path, monkeypatch
):
    import csv
    from pathlib import Path

    tdl = Tdl(tmp_path / "tdl", "own", data_dir=tmp_path / ".tdl")
    observed = []

    async def run(args, **kwargs):
        assert args[0] == "--storage"
        assert args[-1] == "version"
        options = dict(
            part.split("=", 1) for part in next(csv.reader([args[1]]))
        )
        directory = Path(options["path"])
        assert await asyncio.to_thread(directory.is_dir)
        assert directory != tdl.data_dir
        assert kwargs["coordinate"] is False
        observed.append(directory)
        return "Version: 1.2.3"

    monkeypatch.setattr(tdl, "_run", run)
    assert await tdl.version() == "Version: 1.2.3"
    assert not observed[0].exists()
    assert not (tmp_path / ".tdl").exists()


@pytest.mark.parametrize("state", ["success", "failure", "cancel"])
async def test_native_login_inherits_terminal_and_reaps_cancelled_process(
    tmp_path, monkeypatch, state
):
    import csv
    import json

    binary, output = tmp_path / "tdl", tmp_path / "result"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, json, time\n"
        f"open({str(output)!r}, 'w').write(json.dumps(\n"
        "{'pid': os.getpid(), 'args': sys.argv[1:], "
        "'proxy': os.getenv('TDL_PROXY')}))\n"
        + (
            "time.sleep(60)\n"
            if state == "cancel"
            else f"sys.exit({2 if state == 'failure' else 0})\n"
        )
    )
    binary.chmod(0o755)
    spawn = AsyncMock(wraps=asyncio.create_subprocess_exec)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("TDL_PROXY", "socks5://unrelated:1080")
    directory = tmp_path / "project,with comma" / ".tdl"
    tdl = Tdl(binary, "personal", data_dir=directory, proxy="")
    task = asyncio.create_task(tdl.login("code"))
    if state == "cancel":
        async with asyncio.timeout(3):
            while not output.exists():  # noqa: ASYNC110 - subprocess IPC
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif state == "failure":
        with pytest.raises(UserError):
            await task
    else:
        await task
    result = json.loads(output.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(result["pid"], 0)
    assert result["proxy"] == ""
    assert next(csv.reader([result["args"][1]])) == [
        "type=bolt",
        f"path={directory / 'data'}",
    ]
    assert result["args"][2:] == ["--ns", "personal", "login", "--type", "code"]
    assert not {"stdin", "stdout", "stderr"} & spawn.call_args.kwargs.keys()
