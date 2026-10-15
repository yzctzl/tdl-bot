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
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp

from tdl_bot.core.errors import AccessDenied, UserError
from tdl_bot.core.i18n import tr
from tdl_bot.integrations.telegram.client import AccountLock

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
    async def job(self) -> AsyncGenerator[None]:
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
    async def update(self) -> AsyncGenerator[None]:
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
        proxy: str | None = None,
        storage_lock: AccountLock | None = None,
    ) -> None:
        """Configure the executable and account namespace."""
        self.binary = binary
        self.namespace = namespace
        self.timeout = timeout
        self.data_dir = data_dir.resolve() if data_dir is not None else None
        self.proxy = proxy
        # tdl's default Bolt storage uses an exclusive database lock.
        self.account_lock = AccountLock()
        self._process_lock = storage_lock or self.account_lock
        self.gate = UpdateGate()

    def storage_args(self) -> list[str]:
        """Select an existing namespace without creating an empty database."""
        if self.data_dir is None:
            return []
        if not self.namespace or Path(self.namespace).name != self.namespace:
            raise UserError(
                tr("TDL_NAMESPACE must be a single namespace name.")
            )
        bolt = self.data_dir / "data"
        legacy = self.data_dir / "data.kv"
        if (bolt / self.namespace).is_file():
            driver, path = "bolt", bolt
        elif not bolt.exists() and legacy.is_file():
            driver, path = "legacy", legacy
        else:
            raise UserError(
                tr(
                    "TDL_DIR has no session for this namespace. Check the "
                    "directory and TDL_NAMESPACE. The Bot will not create "
                    "an empty session or sign in automatically."
                )
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
        # tdl's root command initializes storage even for version. An explicit
        # disposable store avoids default-session migration and database locks.
        with tempfile.TemporaryDirectory(prefix="tdl-version-") as directory:
            options = io.StringIO()
            csv.writer(options, lineterminator="").writerow(
                ["type=bolt", f"path={directory}"]
            )
            return await self._run(
                ["--storage", options.getvalue(), "version"],
                binary=binary,
                timeout=30,
                coordinate=False,
            )

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
        coordinate: bool = True,
    ) -> str:
        """Drain both streams, and reap the process on timeout/cancellation."""
        # Version uses disposable storage and no account authorization.
        # It must remain available while downloads hold their account locks.
        account_lock = self.account_lock if coordinate else AccountLock()
        storage_lock = self._process_lock if coordinate else account_lock
        async with account_lock, storage_lock:
            environment = os.environ.copy()
            if self.proxy is not None:
                # Keep each executor's tdl and Telethon on the same egress.
                environment["TDL_PROXY"] = self.proxy
            try:
                process = await asyncio.create_subprocess_exec(
                    str(binary or self.binary),
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    stdin=asyncio.subprocess.DEVNULL,
                    env=environment,
                )
            except OSError as exc:
                raise UserError(
                    tr("Cannot start tdl. Check TDL_BINARY.")
                ) from exc
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
                raise UserError(
                    tr(
                        "tdl download timed out. Temporary files have been "
                        "cleaned up."
                    )
                ) from exc
            output = tail.decode("utf-8", errors="replace")
            if process.returncode:
                if any(marker in output.upper() for marker in _ACCESS_ERRORS):
                    raise AccessDenied(
                        tr("The tdl download account cannot access this group.")
                    )
                raise UserError(
                    tr(
                        "tdl failed. Check account login, network access, "
                        "and whether the message can be downloaded."
                    )
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
            raise UserError(
                tr("Version must be latest or a version such as v0.20.4.")
            )
        suffix = (
            "latest" if version == "latest" else f"tags/v{version.lstrip('v')}"
        )
        try:
            timeout = aiohttp.ClientTimeout(total=300)
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "tdl-bot", "Accept": "application/json"},
            ) as session:
                async with session.get(f"{_RELEASE_API}/{suffix}") as response:
                    response.raise_for_status()
                    release = await response.json()
                tag = release["tag_name"].lstrip("v")
                if not re.fullmatch(r"\d+\.\d+\.\d+", tag):
                    raise UserError(
                        tr(
                            "Invalid release version. The existing binary "
                            "was retained."
                        )
                    )
                desired = tuple(int(part) for part in tag.split("."))
                if version != "latest" and tag != version.lstrip("v"):
                    raise UserError(
                        tr(
                            "Release version does not match the requested "
                            "version. The existing binary was retained."
                        )
                    )

                async def unnecessary() -> bool:
                    current = version_number(await self.version())
                    return current == desired or (
                        version == "latest" and current > desired
                    )

                # No writer lease or assets are needed for an unchanged binary.
                if await unnecessary():
                    return tr(
                        (
                            "No update needed: the current tdl meets the "
                            "version requirement (target {p0})."
                        ),
                        p0=tag,
                    )
                async with self.gate.update():
                    # Another update may have completed while we were waiting.
                    if await unnecessary():
                        return tr(
                            (
                                "No update needed: the current tdl meets "
                                "the target version ({p0})."
                            ),
                            p0=tag,
                        )
                    assets = {
                        asset["name"]: asset["browser_download_url"]
                        for asset in release["assets"]
                    }
                    name = release_asset_name()
                    if name not in assets or "tdl_checksums.txt" not in assets:
                        raise UserError(
                            tr(
                                "This release has no binary or checksum "
                                "file for this platform."
                            )
                        )
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
                        if version_number(result) != desired:
                            raise UserError(
                                tr(
                                    "The downloaded binary version does "
                                    "not match the release."
                                )
                            )
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
            TypeError,
            tarfile.TarError,
            zipfile.BadZipFile,
        ) as exc:
            raise UserError(
                tr(
                    "tdl update failed. The existing binary was retained. "
                    "Try again later."
                )
            ) from exc


def version_number(output: str) -> tuple[int, int, int]:
    """Parse tdl's version output before comparing or installing releases."""
    match = re.search(r"(?m)^Version:\s*v?(\d+)\.(\d+)\.(\d+)\s*$", output)
    if match is None:
        raise UserError(
            tr("Cannot identify the installed tdl version. Update stopped.")
        )
    return int(match[1]), int(match[2]), int(match[3])


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
        raise UserError(
            tr(
                "Automatic tdl updates are unavailable for this OS or CPU "
                "architecture."
            )
        )
    extension = "zip" if system == "Windows" else "tar.gz"
    return f"tdl_{system}_{arch}.{extension}"


async def _fetch(session: aiohttp.ClientSession, url: str, limit: int) -> bytes:
    """Fetch an official release asset with a bounded response size."""
    if not url.startswith("https://github.com/iyear/tdl/releases/download/"):
        raise UserError(
            tr("The release asset URL is not from the official tdl repository.")
        )
    data = bytearray()
    async with session.get(url) as response:
        response.raise_for_status()
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > limit:
                raise UserError(tr("The release asset exceeds the size limit."))
    return bytes(data)


def verify_checksum(archive: bytes, name: str, checksums: str) -> None:
    """Require a matching SHA-256 entry for the exact release asset."""
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            if hashlib.sha256(archive).hexdigest() == parts[0].lower():
                return
            break
    raise UserError(tr("tdl release SHA-256 verification failed."))


def extract_binary(archive: bytes, name: str) -> bytes:
    """Read just the executable; never extract archive paths to disk."""
    wanted = "tdl.exe" if name.endswith(".zip") else "tdl"
    stream = io.BytesIO(archive)
    if name.endswith(".zip"):
        with zipfile.ZipFile(stream) as zipped:
            entry = zipped.getinfo(wanted)
            if entry.file_size > 128 * 1024**2:
                raise UserError(tr("The tdl binary exceeds the size limit."))
            return zipped.read(entry)
    with tarfile.open(fileobj=stream, mode="r:gz") as tar:
        entry = next(
            (item for item in tar if item.name in {wanted, f"./{wanted}"}),
            None,
        )
        if entry is None or not entry.isfile() or entry.size > 128 * 1024**2:
            raise UserError(tr("The release contains no valid tdl binary."))
        file = tar.extractfile(entry)
        assert file is not None
        return file.read()
