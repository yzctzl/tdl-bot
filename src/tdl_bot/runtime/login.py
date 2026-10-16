"""First-run native login using the same storage as the task engine."""

from typing import cast

from telethon.tl import types

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr
from tdl_bot.integrations.tdl.session import load_tdl_session, proxy_settings
from tdl_bot.integrations.telegram.client import SharedSessionClient


async def ensure_binary(tdl):
    """Install a missing executable through the verified release updater."""
    if not tdl.binary.is_file():
        print(
            tr(
                "Installing tdl from the official release "
                "with checksum verification…"
            )
        )
        await tdl.update()


async def native_login(tdl, temporary_root, method="qr"):
    proxy_settings(tdl.proxy)
    await ensure_binary(tdl)
    user = await _authorized_user(tdl, temporary_root)
    if user is None:
        print(
            tr(
                "Signing in with tdl. Session directory: {path}; "
                "namespace: {namespace}.",
                path=tdl.data_dir,
                namespace=tdl.namespace,
            )
        )
        await tdl.login(method)
        user = await _authorized_user(tdl, temporary_root)
        if user is None:
            raise UserError(
                tr("No authorized user session was created. Run login again.")
            )
    print(
        tr(
            "User session is available. Account ID: {p0}; source: {p1}.",
            p0=user.id,
            p1="tdl",
        )
    )


async def _authorized_user(tdl, temporary_root):
    try:
        imported = await load_tdl_session(tdl, temporary_root)
    except UserError:
        # Missing or invalid credentials are handled only by explicit login.
        return None
    client = SharedSessionClient(
        imported.session,
        imported.api_id,
        imported.api_hash,
        proxy=proxy_settings(tdl.proxy),
        account_lock=tdl.account_lock,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None
        user = cast(types.User | None, await client.get_me())
        if user is not None and user.bot:
            raise UserError(
                tr("Sign in with a Telegram user account, not a Bot token.")
            )
        return user
    finally:
        await client.disconnect()
