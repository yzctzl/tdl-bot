"""Command-line login, startup and graceful shutdown."""

import argparse
import asyncio
import logging
import os
from collections.abc import Awaitable
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import cast

from aiogram import Bot
from telethon import TelegramClient
from telethon.tl import types

from tdl_bot.bot import BotApplication, ProgressMessages
from tdl_bot.client import SharedSessionClient
from tdl_bot.config import Settings
from tdl_bot.errors import UserError
from tdl_bot.service import TransferService
from tdl_bot.session import load_tdl_session, proxy_settings
from tdl_bot.store import Store
from tdl_bot.tdl import Tdl
from tdl_bot.telegram import TelegramMedia


@contextmanager
def instance_lock(path: Path):
    """Prevent two processes from sharing a session, binary or jobs folder."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UserError("已有进程使用该 data 目录或 tdl 程序。") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def run(settings: Settings, command: str) -> None:
    """Run user-account login or the aiogram polling service."""
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tdl = Tdl(
        settings.tdl_binary,
        settings.tdl_namespace,
        settings.download_timeout,
        data_dir=settings.tdl_dir,
    )
    proxy = proxy_settings(settings.tdl_proxy)
    if settings.session_source == "tdl":
        imported = await load_tdl_session(tdl, settings.data_dir)
        client = SharedSessionClient(
            imported.session,
            imported.api_id,
            imported.api_hash,
            proxy=proxy,
            account_lock=tdl.account_lock,
        )
    else:
        # Telethon accepts None, but its proxy annotation omits it.
        client = TelegramClient(
            str(settings.session_path),
            settings.api_id,
            settings.api_hash,
            proxy=proxy,  # ty: ignore[invalid-argument-type]
        )
    if command == "login":
        try:
            if settings.session_source == "tdl":
                await client.connect()
                if not await client.is_user_authorized():
                    raise UserError(
                        "tdl 的已有会话已失效，请检查原 tdl 登录状态。"
                    )
            else:
                # Inside this running loop, start() returns a coroutine.
                await cast(Awaitable[TelegramClient], client.start())
            # input_peer defaults to False, so get_me returns User or None.
            me = cast(types.User | None, await client.get_me())
            if me is None:
                raise UserError("无法获取已登录账号的信息。")
            if me.bot:
                raise UserError(
                    "必须使用 Telegram 用户账号登录，而不是 Bot token。"
                )
            print(
                f"用户会话可用，账号 ID：{me.id}，"
                f"来源：{settings.session_source}。"
            )
        finally:
            await client.disconnect()
        return

    bot = Bot(settings.bot_token)
    store = Store(settings.data_dir / "state.sqlite3")
    service = None
    application = None
    try:
        await client.connect()
        if not await client.is_user_authorized():
            if settings.session_source == "tdl":
                raise UserError("tdl 的已有会话已失效，请检查原 tdl 登录状态。")
            raise UserError("请先运行 uv run tdl-bot login 完成用户账号登录。")
        me = cast(types.User | None, await client.get_me())
        if me is None:
            raise UserError("无法获取已登录账号的信息。")
        if me.bot:
            raise UserError("Telethon 会话必须属于用户账号。")
        await tdl.version()
        media = TelegramMedia(client)

        service = TransferService(
            tdl,
            media,
            store,
            settings.data_dir / "jobs",
            ProgressMessages(bot),
            workers=settings.workers,
            queue_size=settings.queue_size,
        )
        service.start()
        application = BotApplication(bot, settings, store, media, tdl, service)
        await application.dispatcher.start_polling(
            bot,
            close_bot_session=False,
            allowed_updates=["message", "callback_query"],
        )
    finally:
        try:
            if service:
                logging.info("等待已接收的下载／上传任务结束……")
                await service.close()
            if application:
                await application.close()
        finally:
            store.close()
            await client.disconnect()
            await bot.session.close()


def main() -> None:
    """Parse the CLI and hold cross-process locks for the runtime."""
    parser = argparse.ArgumentParser(description="tdl Telegram media-group Bot")
    parser.add_argument(
        "command", nargs="?", choices=["run", "login"], default="run"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    # Newly created tokens, sessions and downloads are owner-only.
    os.umask(0o077)
    try:
        settings = Settings.from_env(login_only=args.command == "login")
        with ExitStack() as stack:
            stack.enter_context(
                instance_lock(settings.data_dir / ".instance.lock")
            )
            stack.enter_context(
                instance_lock(settings.tdl_binary.with_suffix(".lock"))
            )
            if settings.session_source == "tdl":
                tdl = Tdl(
                    settings.tdl_binary,
                    settings.tdl_namespace,
                    data_dir=settings.tdl_dir,
                )
                tdl.storage_args()
                stack.enter_context(
                    instance_lock(
                        settings.tdl_dir / f".bot-{settings.tdl_namespace}.lock"
                    )
                )
            asyncio.run(run(settings, args.command))
    except (ValueError, UserError) as exc:
        parser.exit(1, f"{exc}\n")
    except KeyboardInterrupt:
        pass
