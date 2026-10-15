"""Verify official releases and atomically replace the tdl binary."""

import hashlib
import io
import os
import platform
import re
import tarfile
import tempfile
import zipfile
from pathlib import Path

import aiohttp

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr

_RELEASE_API = "https://api.github.com/repos/iyear/tdl/releases"


async def update(tdl, version: str = "latest") -> str:
    """Verify an official release and atomically replace the executable.

    Args:
        version: 'latest' or a release tag such as 'v0.20.4'.

    Returns:
        The version output of the installed release.

    Raises:
        UserError: Fetching, verification, or installation fails. The
            existing executable is preserved until verification succeeds.
    """
    if version != "latest" and not re.fullmatch(r"v?\d+\.\d+\.\d+", version):
        raise UserError(
            tr("Version must be latest or a version such as v0.20.4.")
        )
    suffix = "latest" if version == "latest" else f"tags/v{version.lstrip('v')}"
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
                current = version_number(await tdl.version())
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
            async with tdl.gate.update():
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
                tdl.binary.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(
                    prefix=".tdl-update-", dir=tdl.binary.parent
                ) as directory:
                    staged = Path(directory) / tdl.binary.name
                    staged.write_bytes(executable)
                    staged.chmod(0o755)
                    result = await tdl.version(staged)
                    if version_number(result) != desired:
                        raise UserError(
                            tr(
                                "The downloaded binary version does "
                                "not match the release."
                            )
                        )
                    os.replace(staged, tdl.binary)
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
