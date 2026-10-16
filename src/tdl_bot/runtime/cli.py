"""Command-line login, startup and graceful shutdown."""

import argparse
import asyncio
import fcntl
import logging
import os
import signal
from collections.abc import Awaitable
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast

from aiogram import Bot
from telethon import TelegramClient
from telethon.tl import types

from tdl_bot.bot.application import BotApplication
from tdl_bot.bot.progress import ProgressMessages
from tdl_bot.core.config import Settings, runtime_directory
from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr
from tdl_bot.integrations.tdl.runner import Tdl
from tdl_bot.integrations.tdl.session import (
    load_tdl_session,
    load_tdl_sessions,
    proxy_settings,
)
from tdl_bot.integrations.telegram.client import (
    AccountLock,
    SharedSessionClient,
)
from tdl_bot.integrations.telegram.delivery import Delivery
from tdl_bot.integrations.telegram.media import TelegramMedia
from tdl_bot.integrations.telegram.uploads import UploadClient
from tdl_bot.runtime.engine import (
    EngineServer,
    RemoteService,
    code_version,
    ensure_engine,
    exchange,
    maintain_engine,
)
from tdl_bot.runtime.login import native_login
from tdl_bot.storage.store import Store
from tdl_bot.transfers.accounts import AccessPolicy, AccountPool, Executor
from tdl_bot.transfers.activity import ActivityRecorder
from tdl_bot.transfers.backups.service import BackupService
from tdl_bot.transfers.service import TransferService


@contextmanager
def instance_lock(path: Path):
    """Prevent two processes from sharing a session, binary or jobs folder."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UserError(
                tr(
                    "Another process is using this data directory or tdl "
                    "binary."
                )
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def run(settings: Settings, command: str, *, login_method="qr") -> None:
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
        upload_timeout=settings.upload_timeout,
        upload_threads=settings.upload_concurrency,
    )
    if command == "login" and settings.session_source == "tdl":
        await native_login(tdl, settings.data_dir, login_method)
        return
    proxy = proxy_settings(settings.tdl_proxy)
    if settings.session_source == "tdl":
        imported = await load_tdl_session(tdl, settings.data_dir)
        client = SharedSessionClient(
            imported.session,
            imported.api_id,
            imported.api_hash,
            proxy=proxy,
            account_lock=tdl.account_lock,
            upload_concurrency=settings.upload_concurrency,
        )
    else:
        client = UploadClient(
            str(settings.session_path),
            settings.api_id,
            settings.api_hash,
            proxy=proxy,
            upload_concurrency=settings.upload_concurrency,
        )
    if command == "login":
        try:
            # Inside this running loop, start() returns a coroutine.
            await cast(Awaitable[TelegramClient], client.start())
            # input_peer defaults to False, so get_me returns User or None.
            me = cast(types.User | None, await client.get_me())
            if me is None:
                raise UserError(tr("Cannot retrieve the signed-in account."))
            if me.bot:
                raise UserError(
                    tr("Sign in with a Telegram user account, not a Bot token.")
                )
            print(
                tr(
                    "User session is available. Account ID: {p0}; source: "
                    "{p1}.",
                    p0=me.id,
                    p1=settings.session_source,
                )
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
                raise UserError(
                    tr(
                        "The existing tdl session has expired. Check the "
                        "original tdl login."
                    )
                )
            raise UserError(tr("Run uv run tdl-bot login to sign in first."))
        me = cast(types.User | None, await client.get_me())
        if me is None:
            raise UserError(tr("Cannot retrieve the signed-in account."))
        if me.bot:
            raise UserError(
                tr("The Telethon session must belong to a user account.")
            )
        await tdl.version()
        media = TelegramMedia(
            client, tdl=tdl if settings.session_source == "tdl" else None
        )
        shared = Executor(
            "shared",
            tdl,
            media,
            user_id=me.id,
            username=getattr(me, "username", None),
            name=getattr(me, "first_name", "") or "",
        )
        personal = []
        session = client.session
        if session is None or session.auth_key is None:
            raise UserError(
                tr("The execution account has no valid authorization key.")
            )
        keys = {session.auth_key.key}
        discovered = (
            await load_tdl_sessions(tdl, settings.data_dir)
            if settings.session_source == "tdl"
            else {}
        )
        identities = {me.id}
        for namespace, imported in sorted(discovered.items()):
            key = imported.session.auth_key.key
            # The shared namespace is already connected. Copies of its key
            # must not open a second connection with competing API layers.
            if key in keys:
                continue
            keys.add(key)
            own_tdl = Tdl(
                settings.tdl_binary,
                namespace,
                settings.download_timeout,
                data_dir=settings.tdl_dir,
                proxy=settings.tdl_proxy,
                upload_timeout=settings.upload_timeout,
                upload_threads=settings.upload_concurrency,
                storage_lock=storage_locks[settings.tdl_dir],
            )
            own_tdl.gate = tdl.gate
            own = SharedSessionClient(
                imported.session,
                imported.api_id,
                imported.api_hash,
                proxy=proxy,
                account_lock=own_tdl.account_lock,
                upload_concurrency=settings.upload_concurrency,
            )
            clients.append(own)
            await own.connect()
            own_user = cast(
                types.User | None,
                await own.get_me() if await own.is_user_authorized() else None,
            )
            if own_user is None or own_user.bot or own_user.id in identities:
                # A logged-out namespace is not an available account. Multiple
                # namespaces for the same UID resolve deterministically to one.
                await own.disconnect()
                clients.remove(own)
                logging.warning(
                    "Skipping an unavailable or duplicate tdl account"
                )
                continue
            identities.add(own_user.id)
            personal.append(
                Executor(
                    f"operator:{own_user.id}",
                    own_tdl,
                    TelegramMedia(own, tdl=own_tdl),
                    own_user.id,
                    own_user.id,
                    getattr(own_user, "username", None),
                    getattr(own_user, "first_name", "") or "",
                )
            )
        accounts = AccountPool(shared, tuple(personal))
        bot = Bot(settings.bot_token)
        store = Store(settings.data_dir / "state.sqlite3")
        store.save_execution_catalog(accounts.catalog())

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
            activity=ActivityRecorder(bot, store),
        )
        service.start()
        service.backups = BackupService(
            service, notify=bot.send_message, workers=settings.workers
        )
        service.backups.start()
        await EngineServer(service, settings).serve()
    finally:
        try:
            if service:
                if service.backups is not None:
                    await service.backups.close()
                logging.info(
                    "Waiting for accepted downloads and uploads to finish…"
                )
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
    engine_monitor = asyncio.create_task(maintain_engine(settings))
    try:
        await application.register_commands()
        await application.dispatcher.start_polling(
            bot,
            close_bot_session=False,
            allowed_updates=["message", "callback_query"],
        )
    finally:
        engine_monitor.cancel()
        await asyncio.gather(engine_monitor, return_exceptions=True)
        # Updates execute in the engine even if their RPC waiter is cancelled.
        if application._update and not application._update.done():
            application._update.cancel()
            await asyncio.gather(application._update, return_exceptions=True)
        await bot.session.close()
        store.close()


async def _health(settings):
    try:
        await asyncio.wait_for(
            exchange(
                runtime_directory(settings) / "runtime.sock",
                {"method": "ping", "args": {}},
            ),
            5,
        )
    except (OSError, ValueError, KeyError, TimeoutError) as exc:
        raise UserError(tr("The task engine is unavailable.")) from exc


def parse_arguments(argv=None):
    """Scope options to commands while retaining the default run shortcut."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-e",
        "--env-file",
        type=Path,
        default=argparse.SUPPRESS,
        help="Path to the workspace .env",
    )
    parser = argparse.ArgumentParser(
        description="tdl Telegram media-group Bot",
        parents=[common],
        epilog="Run without a command to start the Bot. Use 'help <command>' "
        "or '<command> help' for command options.",
    )
    parser.set_defaults(namespace=None, login_method="qr")
    # Keep the old no-op flag out of help; reloads are always manual.
    parser.add_argument(
        "--no-reload", action="store_true", help=argparse.SUPPRESS
    )
    commands = parser.add_subparsers(title="commands", dest="command")
    descriptions = {
        "run": "Start the Bot (default); reload manually",
        "login": "Sign in to a Telegram execution account",
        "reload": "Restart the Bot without interrupting active transfers",
        "stop": "Stop the Bot and drain active transfers",
        "setup": "Create or interactively configure a workspace",
        "health": "Check the Bot supervisor and task engine",
        "container": "Run with graceful container shutdown",
        "engine": "Run the task engine (internal)",
        "frontend": "Run the polling frontend (internal)",
        "help": "Show general or command-specific help",
    }
    pages = {}
    for name, description in descriptions.items():
        pages[name] = commands.add_parser(
            name,
            help=description,
            description=description,
            parents=[] if name in {"setup", "help"} else [common],
        )
        if name != "help":
            pages[name].add_argument(
                "command_help",
                nargs="?",
                choices=["help"],
                metavar="help",
                help="Show this command's help",
            )
    pages["setup"].add_argument(
        "-d",
        "--directory",
        type=Path,
        default=Path("."),
        help="Workspace to initialize (default: current directory)",
    )
    pages["setup"].add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help=(
            "Configure the Bot, Telegram login, administrator and storage "
            "interactively"
        ),
    )
    pages["login"].add_argument(
        "-n", "--namespace", help="Namespace to sign in to"
    )
    pages["login"].add_argument(
        "-m",
        "--login-method",
        choices=["qr", "code", "desktop"],
        default="qr",
        help="Native tdl login method (default: qr)",
    )
    pages["reload"].add_argument(
        "-a",
        "--all",
        action="store_true",
        dest="all_instances",
        help="Reload every running instance owned by this system user",
    )
    pages["run"].add_argument(
        "--no-reload", action="store_true", help=argparse.SUPPRESS
    )
    pages["help"].add_argument(
        "topic", nargs="?", choices=descriptions, help="Command to describe"
    )
    args = parser.parse_args(argv)
    args.command = args.command or "run"
    args.env_file = getattr(args, "env_file", None)
    if getattr(args, "command_help", None) == "help":
        pages[args.command].print_help()
        parser.exit()
    if (
        args.command == "reload"
        and args.all_instances
        and args.env_file is not None
    ):
        parser.error("Use either -a or -e, not both")
    if args.command == "help":
        (pages[args.topic] if args.topic else parser).print_help()
        parser.exit()
    return parser, args


def reload_instances(env_file=None, *, all_instances=False):
    from tdl_bot.runtime.instances import Instance, discover

    instances = discover(lambda runtime: signal_supervisor(runtime, 0))
    if env_file is not None:
        config = env_file.expanduser().resolve()
        instances = [
            instance for instance in instances if instance.env_file == config
        ]
        if not instances:
            # Explicit selection also supports legacy instances outside cwd.
            settings = Settings.from_env(env_file=env_file)
            instances = [Instance(config, runtime_directory(settings))]
    if not instances:
        raise UserError(tr("No Bot is running."))
    if len(instances) > 1 and not all_instances:
        paths = "\n".join(str(instance.env_file) for instance in instances)
        raise UserError(
            tr(
                "Multiple instances are running. Use reload -a for all, or "
                "reload -e <config> for one:\n{paths}",
                paths=paths,
            )
        )
    failed = []
    for instance in instances:
        try:
            if not signal_supervisor(instance.runtime_dir, signal.SIGHUP):
                raise UserError(tr("No Bot is running."))
            path = instance.runtime_dir / "runtime.sock"
            if path.exists():
                # Discover newly logged-in accounts even without a code change.
                asyncio.run(
                    exchange(path, {"method": "ping", "args": {"reload": True}})
                )
            print(tr("Reload requested: {path}", path=instance.env_file))
        except (UserError, OSError, ValueError) as exc:
            failed.append(str(instance.env_file))
            print(tr("Reload failed: {path}", path=instance.env_file))
            logging.warning("Reload error: %s", type(exc).__name__)
    if failed:
        raise UserError(
            tr("Could not reload {count} instance(s).", count=len(failed))
        )


def main() -> None:
    """Parse the CLI and hold cross-process locks for the runtime."""
    parser, args = parse_arguments()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    # Newly created tokens, sessions and downloads are owner-only.
    os.umask(0o077)
    try:
        if args.command == "setup":
            from tdl_bot.runtime.setup import initialize

            if args.interactive:
                from tdl_bot.runtime.wizard import configure

                configure(args.directory)
            else:
                initialize(args.directory)
            return
        if args.command == "reload":
            reload_instances(args.env_file, all_instances=args.all_instances)
            return
        settings = Settings.from_env(
            login_only=args.command == "login", env_file=args.env_file
        )
        if settings.env_file is not None:
            os.environ["TDL_BOT_ENV_FILE"] = str(settings.env_file)
        runtime_dir = runtime_directory(settings)
        runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.namespace:
            settings = replace(settings, tdl_namespace=args.namespace)
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.command == "stop":
            pid_file = runtime_dir / "frontend.pid"
            if signal_supervisor(runtime_dir, signal.SIGTERM):
                for _ in range(100):
                    if not pid_file.exists():
                        break
                    import time

                    time.sleep(0.1)
            from tdl_bot.runtime.container import drain_engine

            asyncio.run(drain_engine(settings))
            return
        if args.command == "health":
            if not signal_supervisor(runtime_dir, 0):
                raise UserError(tr("No Bot is running."))
            asyncio.run(_health(settings))
            return
        if args.command in {"run", "container"}:
            from tdl_bot.runtime.instances import registered
            from tdl_bot.runtime.supervisor import supervise

            with (
                instance_lock(runtime_dir / ".supervisor.lock"),
                registered(settings),
            ):
                if args.command == "container":
                    from tdl_bot.runtime.container import drain_engine

                    asyncio.run(
                        supervise(
                            runtime_dir, shutdown=lambda: drain_engine(settings)
                        )
                    )
                else:
                    asyncio.run(supervise(runtime_dir))
            return
        if args.command == "frontend":
            with instance_lock(runtime_dir / ".polling.lock"):
                asyncio.run(frontend(settings))
            return
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
                tdl.storage_args(create=args.command == "login")
                # Discovery covers the whole storage directory. Claim it once
                # rather than taking a changing set of namespace locks.
                stack.enter_context(
                    instance_lock(settings.tdl_dir / ".bot-accounts.lock")
                )
                stack.enter_context(
                    instance_lock(
                        settings.tdl_dir / f".bot-{settings.tdl_namespace}.lock"
                    )
                )
            if args.command == "login":
                asyncio.run(
                    run(settings, args.command, login_method=args.login_method)
                )
            else:
                asyncio.run(run(settings, args.command))
    except (ValueError, UserError) as exc:
        parser.exit(1, f"{exc}\n")
    except KeyboardInterrupt:
        pass
