"""Cancellable tdl subprocesses and coordinated binary updates."""

import asyncio
import csv
import io
import json
import logging
import os
import tempfile
from pathlib import Path

from tdl_bot.core.errors import AccessDenied, UserError
from tdl_bot.core.i18n import tr
from tdl_bot.integrations.tdl.gate import UpdateGate
from tdl_bot.integrations.tdl.updates import update
from tdl_bot.integrations.telegram.client import AccountLock

_LOGGER = logging.getLogger(__name__)

_ACCESS_ERRORS = (
    "CHANNEL_PRIVATE",
    "CHAT_FORBIDDEN",
    "USER_NOT_PARTICIPANT",
    "CHANNEL_INVALID",
    "PEER_ID_INVALID",
)


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
        upload_timeout: int = 7200,
        upload_threads: int = 8,
    ) -> None:
        """Configure the executable and account namespace."""
        self.binary = binary
        self.namespace = namespace
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        self.upload_threads = upload_threads
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

    async def clone(self, urls, target, markers, directory: Path):
        """Let tdl preserve original media metadata during native transfer."""
        chat = target.chat
        if chat.startswith("-100"):
            chat = str(-int(chat) - 1_000_000_000_000)
        elif chat.startswith("-"):
            chat = chat[1:]
        destination = {"Peer": chat, "Thread": target.topic_id or 0}
        args = [
            *self.storage_args(),
            "--ns",
            self.namespace,
            "--threads",
            str(self.upload_threads),
            "forward",
            "--mode",
            "clone",
            # Single-message receipts identify every member reliably. The final
            # album restores original order and captions using media references.
            "--single",
            "--to",
            json.dumps(destination),
            "--edit",
            json.dumps(markers) + "[string(Message.ID)]",
        ]
        for url in urls:
            args.extend(["--from", url])
        await self._run(
            args,
            timeout=self.upload_timeout,
            operation="clone",
            temporary_dir=directory,
        )

    async def _run(
        self,
        args: list[str],
        *,
        binary: Path | None = None,
        timeout: int | None = None,
        coordinate: bool = True,
        operation: str = "download",
        temporary_dir: Path | None = None,
    ) -> str:
        """Drain both streams, and reap the process on timeout/cancellation."""
        # Version uses disposable storage and no account authorization.
        # It must remain available while downloads hold their account locks.
        account_lock = self.account_lock if coordinate else AccountLock()
        storage_lock = self._process_lock if coordinate else account_lock
        async with account_lock, storage_lock:
            environment = os.environ.copy()
            if temporary_dir is not None:
                # Go's clone files stay inside the job's owned cleanup folder.
                environment.update(
                    {
                        key: str(temporary_dir)
                        for key in ("TMPDIR", "TMP", "TEMP")
                    }
                )
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
                if operation == "clone":
                    raise UserError(
                        tr(
                            "tdl transfer timed out. Temporary files have "
                            "been cleaned up."
                        )
                    ) from exc
                raise UserError(
                    tr(
                        "tdl download timed out. Temporary files have been "
                        "cleaned up."
                    )
                ) from exc
            output = tail.decode("utf-8", errors="replace")
            if process.returncode:
                _LOGGER.error(
                    "tdl command failed operation=%s exit=%s reason=%s",
                    operation,
                    process.returncode,
                    _failure_reason(output),
                )
                if operation == "clone":
                    raise UserError(
                        tr(
                            "tdl transfer failed. Check account permissions "
                            "and network access before retrying."
                        )
                    )
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
        """Install a verified release after active transfers finish."""
        return await update(self, version)


def _failure_reason(output: str) -> str:
    """Classify known failures without logging private native command output."""
    text = output.lower()
    reasons = {
        "caption": (
            "get caption",
            "resolve caption",
            "parse caption",
            "caption must return",
        ),
        "peer_resolution": (
            "resolve chat",
            "resolve destination",
            "get target peer",
            "failed to get peer",
        ),
        "permissions": (
            "channel_private",
            "chat_write_forbidden",
            "chat_admin_required",
            "user_banned_in_channel",
        ),
        "authorization": (
            "auth_key_unregistered",
            "auth_key_duplicated",
            "session_revoked",
        ),
        "network": (
            "connection refused",
            "connection reset",
            "context deadline exceeded",
            "i/o timeout",
        ),
        "storage_lock": (
            "database is used by another process",
            "database is locked",
        ),
    }
    return next(
        (
            reason
            for reason, markers in reasons.items()
            if any(marker in text for marker in markers)
        ),
        "unclassified",
    )
