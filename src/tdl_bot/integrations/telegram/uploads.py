"""Bounded upload pipelining over Telethon's existing account connection."""

import asyncio
import inspect
import logging
from pathlib import Path
from time import monotonic

from telethon import TelegramClient, errors, helpers
from telethon.tl import functions, types

_LOGGER = logging.getLogger(__name__)
_PART_SIZE = 512 * 1024
_BIG_FILE = 10 * 1024 * 1024


class UploadClient(TelegramClient):
    """Pipeline large local files without creating additional auth sessions."""

    def __init__(self, *args, upload_concurrency: int = 8, **kwargs):
        if not 1 <= upload_concurrency <= 32:
            raise ValueError("Upload concurrency must be between 1 and 32")
        self.upload_concurrency = upload_concurrency
        super().__init__(*args, **kwargs)

    async def upload_file(self, file, **kwargs):
        # Keep Telethon's stream, encryption and custom-option behavior intact.
        if not isinstance(file, (str, Path)) or kwargs.keys() - {
            "progress_callback"
        }:
            return await super().upload_file(file, **kwargs)
        path = Path(file)
        size = (await asyncio.to_thread(path.stat)).st_size
        if size <= _BIG_FILE:
            return await super().upload_file(file, part_size_kb=512, **kwargs)

        part_count = (size + _PART_SIZE - 1) // _PART_SIZE
        file_id = helpers.generate_random_long()
        started = monotonic()
        for start in range(0, part_count, self.upload_concurrency):
            stop = min(start + self.upload_concurrency, part_count)
            read = asyncio.create_task(
                asyncio.to_thread(_read_parts, path, start, stop, size)
            )
            try:
                parts = await asyncio.shield(read)
            except asyncio.CancelledError:
                # Close the worker's file handle before task cleanup removes it.
                await read
                raise
            requests = [
                functions.upload.SaveBigFilePartRequest(
                    file_id, index, part_count, part
                )
                for index, part in zip(range(start, stop), parts, strict=True)
            ]
            try:
                # Send a complete window under one shared-session lock.
                # gather(per-part calls) would serialize on that same lock.
                results = await self(requests, ordered=False)
            except errors.MultiError as exc:
                # MultiError.__new__ creates this attribute dynamically.
                failures = exc.exceptions  # ty: ignore[unresolved-attribute]
                raise next(error for error in failures if error) from exc
            if len(results) != len(requests) or not all(results):
                raise RuntimeError("Telegram rejected an upload part")
            callback = kwargs.get("progress_callback")
            if callback:
                progress = callback(min(stop * _PART_SIZE, size), size)
                if inspect.isawaitable(progress):
                    await progress
        elapsed = max(monotonic() - started, 0.001)
        _LOGGER.info(
            "Upload completed bytes=%s seconds=%.2f "
            "MiB_per_second=%.2f window=%s",
            size,
            elapsed,
            size / elapsed / 1024**2,
            self.upload_concurrency,
        )
        return types.InputFileBig(file_id, part_count, path.name)


def _read_parts(path, start, stop, size):
    parts = []
    with path.open("rb") as stream:
        stream.seek(start * _PART_SIZE)
        for index in range(start, stop):
            expected = min(_PART_SIZE, size - index * _PART_SIZE)
            part = stream.read(expected)
            if len(part) != expected:
                raise ValueError("Upload file changed while reading")
            parts.append(part)
    return parts
