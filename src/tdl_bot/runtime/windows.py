"""Single-process Windows runtime with cooperative shutdown and no local RPC."""

import asyncio
import logging
import signal
import time
from contextlib import contextmanager

from tdl_bot.core.config import runtime_directory
from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr
from tdl_bot.runtime.locks import busy

_STOP = "stop.request"
_HEALTH = "polling.heartbeat"


@contextmanager
def lifetime(directory):
    """Clear stale control files while holding the supervisor lease."""
    for name in (_STOP, _HEALTH):
        (directory / name).unlink(missing_ok=True)
    try:
        yield
    finally:
        for name in (_STOP, _HEALTH):
            (directory / name).unlink(missing_ok=True)


def control(settings, command):
    directory = runtime_directory(settings)
    lock = directory / ".supervisor.lock"
    if not busy(lock):
        raise UserError(tr("No Bot is running."))
    if command == "health":
        try:
            age = time.time() - (directory / _HEALTH).stat().st_mtime
        except OSError:
            age = float("inf")
        if age > 15:
            raise UserError(tr("The task engine is unavailable."))
        return
    # Never send SIGTERM on Windows: os.kill would terminate without cleanup.
    (directory / _STOP).touch()
    logging.info("Waiting for accepted work and account cleanup to finish")
    while busy(lock):
        time.sleep(0.1)


async def poll(application):
    """Share existing services directly instead of introducing another IPC."""
    directory = runtime_directory(application.settings)
    heartbeat = directory / _HEALTH
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}
    tasks = []
    handlers = set()

    async def track(handler, event, data):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            return await handler(event, data)
        finally:
            handlers.discard(task)

    application.dispatcher.update.outer_middleware(track)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(
                sig, lambda *_: loop.call_soon_threadsafe(stopping.set)
            )
        await application.register_commands()

        async def watch():
            while not stopping.is_set():
                heartbeat.touch()
                if (directory / _STOP).exists():
                    stopping.set()
                    return
                await asyncio.sleep(0.5)

        async def reconcile():
            while True:
                await asyncio.sleep(0.5)
                await application.service.reconcile_permissions()
                if application.service.backups is not None:
                    await application.service.backups.reconcile_permissions()

        tasks = [
            asyncio.create_task(
                application.dispatcher.start_polling(
                    application.bot,
                    handle_signals=False,
                    handle_as_tasks=True,
                    close_bot_session=False,
                    allowed_updates=["message", "callback_query"],
                )
            ),
            asyncio.create_task(watch()),
            asyncio.create_task(reconcile()),
            asyncio.create_task(stopping.wait()),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            if task.done() and not task.cancelled():
                task.result()
    finally:
        # Stop scheduling backups before the outer runtime drains all work.
        if application.service.backups is not None:
            application.service.backups.quiesce()
        heartbeat.unlink(missing_ok=True)
        try:
            # Let aiogram stop its children before cancelling its parent task.
            if tasks and not tasks[0].done():
                await application.dispatcher.stop_polling()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                # Polling has stopped. Finish accepted commands before the
                # outer runtime drains transfers and closes clients.
                while handlers:
                    await asyncio.gather(*handlers, return_exceptions=True)
                await application.close()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
