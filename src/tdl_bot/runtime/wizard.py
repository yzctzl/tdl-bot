"""Minimal terminal onboarding using the existing login and storage services."""

import asyncio
import getpass
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import dotenv_values, set_key
from telethon.errors import RPCError
from telethon.tl import types

from tdl_bot.core.config import Settings, persisted_members, runtime_directory
from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr
from tdl_bot.integrations.tdl.runner import Tdl
from tdl_bot.integrations.tdl.session import load_tdl_session, proxy_settings
from tdl_bot.integrations.telegram.client import SharedSessionClient
from tdl_bot.integrations.telegram.delivery import Delivery
from tdl_bot.integrations.telegram.media import TelegramMedia
from tdl_bot.runtime.login import native_login
from tdl_bot.storage.models import Target
from tdl_bot.storage.store import Store


def save_values(config, **values):
    """Save completed settings atomically with private permissions."""
    temporary = None
    try:
        with NamedTemporaryFile(dir=config.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(config.read_bytes())
        for key, value in values.items():
            set_key(temporary, key, value)
        temporary.chmod(0o600)
        temporary.replace(config)
    except OSError as exc:
        raise UserError(
            tr("Cannot save configuration. Check workspace write permissions.")
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def configure(directory):
    from tdl_bot.runtime.cli import instance_lock
    from tdl_bot.runtime.setup import initialize

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise UserError(
            tr(
                "Interactive setup needs a terminal. Run setup -i in a "
                "terminal."
            )
        )
    root = directory.expanduser().resolve()
    config = root / ".env"
    if not config.exists():
        initialize(root, announce=False)
    settings = Settings.from_env(login_only=True, env_file=config)
    if settings.session_source != "tdl":
        raise UserError(
            tr(
                "Interactive login requires SESSION_SOURCE=tdl. Your "
                "existing configuration was kept."
            )
        )
    # Match runtime leases before changing credentials or opening a session.
    with ExitStack() as locks:
        for path in dict.fromkeys(
            (
                runtime_directory(settings) / ".supervisor.lock",
                settings.data_dir / ".instance.lock",
                settings.tdl_binary.with_suffix(".lock"),
                settings.tdl_dir / ".bot-accounts.lock",
            )
        ):
            locks.enter_context(instance_lock(path))
        print(
            tr(
                "Interactive setup. Press Enter to keep existing values; "
                "Ctrl+C to exit. Completed steps are saved."
            )
        )
        try:
            asyncio.run(_configure(settings, config))
        except (RPCError, OSError, TimeoutError) as exc:
            raise UserError(
                tr(
                    "Setup could not connect. Check your network and run "
                    "setup -i again; completed steps are saved."
                )
            ) from exc
        except EOFError as exc:
            raise UserError(
                tr("Setup interrupted. Run setup -i again to continue.")
            ) from exc
    print(tr("Setup complete. Start with: tdl-bot run -e {path}", path=config))


async def _configure(settings, config):
    values = dotenv_values(config, interpolate=False)
    print(tr("1/4 Bot: obtain a token from @BotFather. Input is hidden."))
    while True:
        token = (
            getpass.getpass(tr("Bot token (Enter to keep): ")).strip()
            or values.get("BOT_TOKEN")
            or ""
        )
        try:
            validate_token(token)
        except TokenValidationError:
            print(tr("Invalid token format. Paste the token from @BotFather."))
            continue
        bot = Bot(token)
        try:
            identity = await bot.get_me()
        except TelegramAPIError:
            print(
                tr(
                    "Cannot verify the Bot. Check the token and network, "
                    "then try again."
                )
            )
        else:
            break
        finally:
            await bot.session.close()
    print(tr("Bot verified: @{name}", name=identity.username))
    save_values(config, BOT_TOKEN=token)

    print(
        tr("2/4 Telegram account: reuse an existing login or scan the QR code.")
    )
    tdl = Tdl(
        settings.tdl_binary,
        settings.tdl_namespace,
        data_dir=settings.tdl_dir,
        proxy=settings.tdl_proxy,
    )
    await native_login(tdl, settings.data_dir)
    imported = await load_tdl_session(tdl, settings.data_dir)
    client = SharedSessionClient(
        imported.session,
        imported.api_id,
        imported.api_hash,
        proxy=proxy_settings(settings.tdl_proxy),
        account_lock=tdl.account_lock,
    )
    bot = Bot(token)
    try:
        await client.connect()
        user = cast(types.User | None, await client.get_me())
        if user is None or user.bot:
            raise UserError(tr("Cannot retrieve the signed-in account."))
        print(
            tr(
                "3/4 Administrator: confirm the signed-in account or enter "
                "administrator UIDs, separated by commas."
            )
        )
        existing = persisted_members(settings.data_dir / "state.sqlite3")
        if existing is None:
            default = values.get("ADMIN_USER_IDS") or str(user.id)
            print(
                tr(
                    "Signed-in account: @{name} (UID: {uid})",
                    name=user.username or "—",
                    uid=user.id,
                )
            )
            while True:
                admins = (
                    input(  # noqa: ASYNC250 - Keep terminal Ctrl+C interruptible.
                        tr("Administrator UIDs [{value}]: ", value=default)
                    ).strip()
                    or default
                )
                parts = [part.strip() for part in admins.split(",")]
                if parts and all(
                    part.isascii() and part.isdigit() and int(part) > 0
                    for part in parts
                ):
                    admins = ",".join(dict.fromkeys(parts))
                    save_values(config, ADMIN_USER_IDS=admins)
                    actor = int(parts[0])
                    break
                print(tr("Enter positive numeric UIDs separated by commas."))
        else:
            if not existing[1]:
                raise UserError(
                    tr(
                        "No administrator exists in the database. Restore "
                        "administrator access before setup."
                    )
                )
            actor = min(existing[1])
            print(
                tr(
                    "Existing database members and roles are kept. Use "
                    "/user to manage administrators."
                )
            )
        await _storage(bot, client, settings, actor)
    finally:
        await client.disconnect()
        await bot.session.close()


async def _storage(bot, client, settings, actor):
    print(
        tr(
            "4/4 Storage: choose an existing channel or group. Add the Bot "
            "as an administrator with posting permission first."
        )
    )
    # List joined destinations so private channels do not require an ID lookup.
    choices = []
    async for dialog in client.iter_dialogs():
        if dialog.is_channel or dialog.is_group:
            choices.append(dialog)
            print(f"{len(choices)}. {dialog.name} ({dialog.id})")
    store = Store(settings.data_dir / "state.sqlite3")
    try:
        current = store.internal_target()
        if current:
            print(tr("Current storage: {chat}", chat=current.chat))
        while True:
            answer = input(  # noqa: ASYNC250 - Keep terminal Ctrl+C interruptible.
                tr("Storage number, @username or chat ID (Enter to keep): ")
            ).strip()
            if not answer and current:
                target = current
            elif answer.isdigit() and 1 <= int(answer) <= len(choices):
                target = Target(str(choices[int(answer) - 1].id))
            elif answer:
                target = Target(answer)
            else:
                continue
            try:
                target = await Delivery(bot).validate(
                    target, actor, storage=True
                )
                peer = await TelegramMedia(client).validate_target(target)
                await _check_write_access(client, peer)
            except UserError as exc:
                print(str(exc))
                continue
            except (RPCError, OSError, TimeoutError):
                print(
                    tr(
                        "Cannot check storage. Check your network and account "
                        "permissions, then retry."
                    )
                )
                continue
            store.set_target(actor, target)
            break
    finally:
        store.close()


async def _check_write_access(client, peer):
    entity = await client.get_entity(peer)
    permissions = await client.get_permissions(peer, "me")
    if getattr(entity, "broadcast", False):
        writable = permissions.post_messages
    else:
        restrictions = (
            getattr(entity, "default_banned_rights", None),
            getattr(permissions.participant, "banned_rights", None),
        )
        writable = not permissions.has_left and (
            permissions.is_admin
            or not any(
                getattr(rights, flag, False)
                for rights in restrictions
                for flag in (
                    "view_messages",
                    "send_messages",
                    "send_media",
                    "send_photos",
                    "send_videos",
                    "send_docs",
                    "send_audios",
                )
            )
        )
    if not writable:
        raise UserError(
            tr(
                "The execution account needs permission to post media "
                "to storage."
            )
        )
