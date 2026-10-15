"""Restart only the polling frontend when explicitly requested."""

import asyncio
import logging
import os
import signal
from pathlib import Path

from tdl_bot.runtime.engine import spawn_process


async def _stop_child(child, waiter):
    if child.poll() is None:
        child.terminate()
    try:
        await asyncio.wait_for(asyncio.shield(waiter), 10)
    except TimeoutError:
        child.kill()
        await waiter


async def supervise(data_dir: Path):
    loop = asyncio.get_running_loop()
    restart = asyncio.Event()
    stopping = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)
    loop.add_signal_handler(signal.SIGHUP, restart.set)
    pid_file = data_dir / "frontend.pid"
    pid_file.write_text(str(os.getpid()))
    child = waiter = None
    try:
        while not stopping.is_set():
            restart.clear()
            child = spawn_process("frontend", start_new_session=True)
            waiter = asyncio.create_task(asyncio.to_thread(child.wait))
            controls = [
                asyncio.create_task(event.wait())
                for event in (restart, stopping)
            ]
            try:
                await asyncio.wait(
                    [waiter, *controls], return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for control in controls:
                    control.cancel()
                await asyncio.gather(*controls, return_exceptions=True)
            # Stop only polling; the resident engine and tdl are untouched.
            await _stop_child(child, waiter)
            if not stopping.is_set():
                logging.info(
                    "Restarting Bot frontend; the task engine remains running."
                )
                await asyncio.sleep(0.5 if restart.is_set() else 3)
    finally:
        if child is not None and waiter is not None:
            await _stop_child(child, waiter)
        pid_file.unlink(missing_ok=True)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.remove_signal_handler(sig)
