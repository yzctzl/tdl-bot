"""Drain the detached engine before the container's main process exits."""

import asyncio
import fcntl
import logging

from tdl_bot.core.config import runtime_directory
from tdl_bot.runtime.engine import exchange


def engine_busy(settings):
    with (settings.data_dir / ".instance.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


async def drain_engine(settings):
    logging.info("Draining accepted work before shutting down the container")
    path = runtime_directory(settings) / "runtime.sock"
    requested = False
    while engine_busy(settings):
        if not requested:
            try:
                await exchange(path, {"method": "shutdown", "args": {}})
                requested = True
            except (OSError, ValueError, KeyError):
                # The frontend may have stopped during engine startup.
                pass
        await asyncio.sleep(0.1)
    # The instance lease covers final client disconnects and database closure.
