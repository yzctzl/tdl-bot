"""Command-line login, startup and graceful shutdown."""

import argparse
import asyncio
import fcntl
import logging
import os
import signal
from collections.abc import Awaitable
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import cast

from aiogram import Bot
from telethon import TelegramClient
from telethon.tl import types

from tdl_bot.accounts import AccessPolicy, AccountPool, Executor
from tdl_bot.bot import BotApplication, ProgressMessages
from tdl_bot.client import AccountLock, SharedSessionClient
from tdl_bot.config import Settings
from tdl_bot.delivery import Delivery
from tdl_bot.errors import UserError
from tdl_bot.runtime import (
    EngineServer,
    RemoteService,
    code_version,
    ensure_engine,
    exchange,
)
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
    storage_locks = {settings.tdl_dir: AccountLock()}
    tdl = Tdl(
        settings.tdl_binary,
        settings.tdl_namespace,
        settings.download_timeout,
        data_dir=settings.tdl_dir,
        proxy=settings.tdl_proxy,
        storage_lock=storage_locks[settings.tdl_dir],
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

    bot = None
    store = None
    service = None
    clients = [client]
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
        shared = Executor("shared", tdl, media)
        personal = []
        session = client.session
        if session is None or session.auth_key is None:
            raise UserError("执行账号缺少有效的授权密钥。")
        keys = {session.auth_key.key}
        for binding in settings.account_bindings:
            if (binding.tdl_dir, binding.namespace) == (
                settings.tdl_dir,
                settings.tdl_namespace,
            ):
                if me.id != binding.operator_id:
                    raise UserError(
                        "专属执行会话的账号与绑定的操作账号不一致。"
                    )
                if binding.proxy != settings.tdl_proxy:
                    raise UserError("同一会话不能绑定不同代理。")
                personal.append(
                    Executor(
                        f"operator:{binding.operator_id}",
                        tdl,
                        media,
                        binding.operator_id,
                    )
                )
                continue
            own_tdl = Tdl(
                settings.tdl_binary,
                binding.namespace,
                settings.download_timeout,
                data_dir=binding.tdl_dir,
                proxy=binding.proxy,
                storage_lock=storage_locks.setdefault(
                    binding.tdl_dir, AccountLock()
                ),
            )
            own_tdl.gate = tdl.gate
            imported = await load_tdl_session(own_tdl, settings.data_dir)
            key = imported.session.auth_key.key
            if key in keys:
                raise UserError(
                    "执行会话重复，请绑定原目录和 namespace，避免使用会话副本。"
                )
            keys.add(key)
            own = SharedSessionClient(
                imported.session,
                imported.api_id,
                imported.api_hash,
                proxy=proxy_settings(binding.proxy),
                account_lock=own_tdl.account_lock,
            )
            clients.append(own)
            await own.connect()
            if not await own.is_user_authorized():
                raise UserError("专属 tdl 会话已失效，请检查账号绑定。")
            own_user = cast(types.User | None, await own.get_me())
            if (
                own_user is None
                or own_user.bot
                or own_user.id != binding.operator_id
            ):
                raise UserError("专属执行会话的账号与绑定的操作账号不一致。")
            personal.append(
                Executor(
                    f"operator:{binding.operator_id}",
                    own_tdl,
                    TelegramMedia(own),
                    binding.operator_id,
                )
            )
        accounts = AccountPool(shared, tuple(personal))
        bot = Bot(settings.bot_token)
        store = Store(settings.data_dir / "state.sqlite3")

        policy = AccessPolicy(
            settings.allowed_users, settings.admin_users, store=store
        )
        store.convert_retries_to_recipients()
        service = TransferService(
            accounts,
            store,
            settings.data_dir / "jobs",
            ProgressMessages(bot),
            policy=policy,
            workers=settings.workers,
            queue_size=settings.queue_size,
            delivery=Delivery(bot, policy=policy),
            durable=True,
        )
        service.start()
        await EngineServer(service, settings).serve()
    finally:
        try:
            if service:
                logging.info("等待已接收的下载／上传任务结束……")
                await service.close()
        finally:
            if store:
                store.close()
            # Attempt every disconnect even if another account fails to close.
            await asyncio.gather(
                *(account.disconnect() for account in clients),
                return_exceptions=True,
            )
            if bot:
                await bot.session.close()


def signal_supervisor(data_dir: Path, sig: int) -> bool:
    """Check the live supervisor lock before using its persisted PID."""
    with (data_dir / ".supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                pid = int((data_dir / "frontend.pid").read_text())
                os.kill(pid, sig)
                return True
            except (OSError, ValueError):
                return False
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False


async def frontend(settings: Settings) -> None:
    """Reconnect polling to a resident engine without reopening tdl sessions."""
    path = await ensure_engine(settings)
    await exchange(
        path, {"method": "ping", "args": {"version": code_version()}}
    )
    store = Store(settings.data_dir / "state.sqlite3", migrate=False)
    bot = Bot(settings.bot_token)
    service = RemoteService(settings, store)
    application = BotApplication(bot, settings, store, service.tdl, service)
    try:
        await application.register_commands()
        await application.dispatcher.start_polling(
            bot,
            close_bot_session=False,
            allowed_updates=["message", "callback_query"],
        )
    finally:
        # Updates execute in the engine even if their RPC waiter is cancelled.
        if application._update and not application._update.done():
            application._update.cancel()
            await asyncio.gather(application._update, return_exceptions=True)
        await bot.session.close()
        store.close()


def main() -> None:
    """Parse the CLI and hold cross-process locks for the runtime."""
    parser = argparse.ArgumentParser(description="tdl Telegram media-group Bot")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["run", "login", "engine", "frontend", "reload", "stop"],
        default="run",
    )
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="兼容旧参数；现在默认仅手动 reload",
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
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.command == "stop":
            pid_file = settings.data_dir / "frontend.pid"
            if signal_supervisor(settings.data_dir, signal.SIGTERM):
                for _ in range(100):
                    if not pid_file.exists():
                        break
                    import time

                    time.sleep(0.1)
            path = settings.data_dir / "runtime.sock"
            if path.exists():
                asyncio.run(exchange(path, {"method": "shutdown", "args": {}}))
            return
        if args.command == "reload":
            if not signal_supervisor(settings.data_dir, signal.SIGHUP):
                raise UserError("没有运行中的 Bot 监控进程。")
            return
        if args.command == "run":
            from tdl_bot.reload import supervise

            with instance_lock(settings.data_dir / ".supervisor.lock"):
                asyncio.run(supervise(settings.data_dir))
            return
        if args.command == "frontend":
            with instance_lock(settings.data_dir / ".polling.lock"):
                asyncio.run(frontend(settings))
            return
        with ExitStack() as stack:
            stack.enter_context(
                instance_lock(settings.data_dir / ".instance.lock")
            )
            stack.enter_context(
                instance_lock(settings.tdl_binary.with_suffix(".lock"))
            )
            sources = {
                (binding.tdl_dir, binding.namespace)
                for binding in settings.account_bindings
            }
            if settings.session_source == "tdl":
                sources.add((settings.tdl_dir, settings.tdl_namespace))
            # Stable ordering prevents two startups taking locks in reverse.
            for directory, namespace in sorted(sources):
                Tdl(
                    settings.tdl_binary, namespace, data_dir=directory
                ).storage_args()
                stack.enter_context(
                    instance_lock(directory / f".bot-{namespace}.lock")
                )
            asyncio.run(run(settings, args.command))
    except (ValueError, UserError) as exc:
        parser.exit(1, f"{exc}\n")
    except KeyboardInterrupt:
        pass
