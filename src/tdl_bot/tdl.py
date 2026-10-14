"""Cancellable tdl subprocesses and coordinated binary updates."""

import asyncio
import csv
import hashlib
import io
import os
import platform
import re
import tarfile
import tempfile
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp

from tdl_bot.client import AccountLock
from tdl_bot.errors import AccessDenied, UserError

_RELEASE_API = "https://api.github.com/repos/iyear/tdl/releases"
_ACCESS_ERRORS = (
    "CHANNEL_PRIVATE",
    "CHAT_FORBIDDEN",
    "USER_NOT_PARTICIPANT",
    "CHANNEL_INVALID",
    "PEER_ID_INVALID",
)


class UpdateGate:
    """Pause new jobs while an update waits for active jobs to finish."""

    def __init__(self) -> None:
        """Initialize a writer-priority, cancellation-safe gate."""
        self._condition = asyncio.Condition()
        self._active = 0
        self._updating = False
        self._waiting = 0

    @asynccontextmanager
    async def job(self) -> AsyncIterator[None]:
        """Hold a lease for the entire download/upload/cleanup operation."""
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._updating and not self._waiting
            )
            self._active += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def update(self) -> AsyncIterator[None]:
        """Acquire exclusive access after all active jobs complete."""
        async with self._condition:
            self._waiting += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._active and not self._updating
                )
                self._updating = True
            finally:
                self._waiting -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._updating = False
                self._condition.notify_all()


class Tdl:
    """Run tdl using argument arrays and retain no unbounded output."""

    def __init__(
        self,
        binary: Path,
        namespace: str,
        timeout: int = 7200,
        *,
        data_dir: Path | None = None,
    ) -> None:
        """Configure the executable and account namespace."""
        self.binary = binary
        self.namespace = namespace
        self.timeout = timeout
        self.data_dir = data_dir.resolve() if data_dir is not None else None
        # tdl's default Bolt storage uses an exclusive database lock.
        self.account_lock = AccountLock()
        self._process_lock = self.account_lock
        self.gate = UpdateGate()

    def storage_args(self) -> list[str]:
        """Select an existing namespace without creating an empty database."""
        if self.data_dir is None:
            return []
        if not self.namespace or Path(self.namespace).name != self.namespace:
            raise UserError("TDL_NAMESPACE 必须是单个 namespace 名称。")
        bolt = self.data_dir / "data"
        legacy = self.data_dir / "data.kv"
        if (bolt / self.namespace).is_file():
            driver, path = "bolt", bolt
        elif not bolt.exists() and legacy.is_file():
            driver, path = "legacy", legacy
        else:
            raise UserError(
                "TDL_DIR 中没有指定 namespace 的会话，请检查目录和 "
                "TDL_NAMESPACE。Bot 不会创建空会话或自动重新登录。"
            )
        output = io.StringIO()
        csv.writer(output, lineterminator="").writerow(
            [f"type={driver}", f"path={path}"]
        )
        return ["--storage", output.getvalue()]

    async def backup(self, destination: Path) -> None:
        """Read existing account metadata through tdl's backup interface."""
        await self._run(
            [
                *self.storage_args(),
                "--ns",
                self.namespace,
                "backup",
                "--dst",
                str(destination),
            ],
            timeout=30,
        )

    async def version(self, binary: Path | None = None) -> str:
        """Return the installed version without contacting Telegram."""
        return await self._run(["version"], binary=binary, timeout=30)

    async def download(self, urls: list[str], directory: Path) -> None:
        """Download exact media-group members using message-ID filenames."""
        args = [
            *self.storage_args(),
            "--ns",
            self.namespace,
            "download",
            "--dir",
            str(directory),
            "--template",
            "{{ .MessageID }}_{{ filenamify .FileName }}",
        ]
        for url in urls:
            args.extend(["--url", url])
        await self._run(args)

    async def _run(
        self,
        args: list[str],
        *,
        binary: Path | None = None,
        timeout: int | None = None,
    ) -> str:
        """Drain both streams, and reap the process on timeout/cancellation."""
        async with self._process_lock:
            try:
                process = await asyncio.create_subprocess_exec(
                    str(binary or self.binary),
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    stdin=asyncio.subprocess.DEVNULL,
                )
            except OSError as exc:
                raise UserError("无法启动 tdl，请检查 TDL_BINARY。") from exc
            tail = bytearray()

            async def drain() -> None:
                assert process.stdout is not None
                while chunk := await process.stdout.read(4096):
                    tail.extend(chunk)
                    del tail[:-16384]
                await process.wait()

            reader = asyncio.create_task(drain())
            try:
                await asyncio.wait_for(
                    asyncio.shield(reader), timeout or self.timeout
                )
            except (TimeoutError, asyncio.CancelledError) as exc:
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                await reader
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise UserError("tdl 下载超时，临时文件已清理。") from exc
            output = tail.decode("utf-8", errors="replace")
            if process.returncode:
                if any(marker in output.upper() for marker in _ACCESS_ERRORS):
                    raise AccessDenied("tdl 下载账号没有访问该群的权限。")
                raise UserError(
                    "tdl 执行失败，请检查账号登录、网络和消息是否可下载。"
                )
            return output.strip()

    async def update(self, version: str = "latest") -> str:
        """Verify an official release and atomically replace the executable.

        Args:
            version: 'latest' or a release tag such as 'v0.20.4'.

        Returns:
            The version output of the installed release.

        Raises:
            UserError: Fetching, verification, or installation fails. The
                existing executable is preserved until verification succeeds.
        """
        if version != "latest" and not re.fullmatch(
            r"v?\d+\.\d+\.\d+", version
        ):
            raise UserError("版本格式应为 latest 或 v0.20.4。")
        suffix = (
            "latest" if version == "latest" else f"tags/v{version.lstrip('v')}"
        )
        async with self.gate.update():
            try:
                timeout = aiohttp.ClientTimeout(total=300)
                async with aiohttp.ClientSession(
                    timeout=timeout,
                    headers={
                        "User-Agent": "tdl-bot",
                        "Accept": "application/json",
                    },
                ) as session:
                    async with session.get(
                        f"{_RELEASE_API}/{suffix}"
                    ) as response:
                        response.raise_for_status()
                        release = await response.json()
                    assets = {
                        asset["name"]: asset["browser_download_url"]
                        for asset in release["assets"]
                    }
                    name = release_asset_name()
                    if name not in assets or "tdl_checksums.txt" not in assets:
                        raise UserError("该版本缺少本平台的程序或校验文件。")
                    checksums = await _fetch(
                        session, assets["tdl_checksums.txt"], 1024 * 1024
                    )
                    archive = await _fetch(session, assets[name], 128 * 1024**2)
                verify_checksum(archive, name, checksums.decode("utf-8"))
                executable = extract_binary(archive, name)
                self.binary.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(
                    prefix=".tdl-update-", dir=self.binary.parent
                ) as directory:
                    staged = Path(directory) / self.binary.name
                    staged.write_bytes(executable)
                    staged.chmod(0o755)
                    result = await self.version(staged)
                    tag = release["tag_name"].lstrip("v")
                    if not re.search(
                        rf"(?m)^Version:\s*{re.escape(tag)}\s*$", result
                    ):
                        raise UserError("下载程序的版本与 release 不一致。")
                    os.replace(staged, self.binary)
                return result
            except UserError:
                raise
            except (
                aiohttp.ClientError,
                TimeoutError,
                OSError,
                ValueError,
                KeyError,
                tarfile.TarError,
                zipfile.BadZipFile,
            ) as exc:
                raise UserError(
                    "tdl 更新失败，现有程序保留，可稍后重试。"
                ) from exc


def release_asset_name() -> str:
    """Select an official archive for the current OS and CPU."""
    system = {"Linux": "Linux", "Darwin": "MacOS", "Windows": "Windows"}.get(
        platform.system()
    )
    arch = {
        "x86_64": "64bit",
        "amd64": "64bit",
        "i386": "32bit",
        "i686": "32bit",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armv7",
        "armv6l": "armv6",
        "riscv64": "riscv64",
        "loongarch64": "loong64",
    }.get(platform.machine().lower())
    if not system or not arch:
        raise UserError("当前操作系统或 CPU 架构不支持自动更新 tdl。")
    extension = "zip" if system == "Windows" else "tar.gz"
    return f"tdl_{system}_{arch}.{extension}"


async def _fetch(session: aiohttp.ClientSession, url: str, limit: int) -> bytes:
    """Fetch an official release asset with a bounded response size."""
    if not url.startswith("https://github.com/iyear/tdl/releases/download/"):
        raise UserError("release 资源地址不是官方 tdl 仓库。")
    data = bytearray()
    async with session.get(url) as response:
        response.raise_for_status()
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > limit:
                raise UserError("release 资源超出大小限制。")
    return bytes(data)


def verify_checksum(archive: bytes, name: str, checksums: str) -> None:
    """Require a matching SHA-256 entry for the exact release asset."""
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            if hashlib.sha256(archive).hexdigest() == parts[0].lower():
                return
            break
    raise UserError("tdl release 的 SHA-256 校验失败。")


def extract_binary(archive: bytes, name: str) -> bytes:
    """Read just the executable; never extract archive paths to disk."""
    wanted = "tdl.exe" if name.endswith(".zip") else "tdl"
    stream = io.BytesIO(archive)
    if name.endswith(".zip"):
        with zipfile.ZipFile(stream) as zipped:
            entry = zipped.getinfo(wanted)
            if entry.file_size > 128 * 1024**2:
                raise UserError("tdl 程序超出大小限制。")
            return zipped.read(entry)
    with tarfile.open(fileobj=stream, mode="r:gz") as tar:
        entry = next(
            (item for item in tar if item.name in {wanted, f"./{wanted}"}),
            None,
        )
        if entry is None or not entry.isfile() or entry.size > 128 * 1024**2:
            raise UserError("release 中没有有效的 tdl 程序。")
        file = tar.extractfile(entry)
        assert file is not None
        return file.read()
