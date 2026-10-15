"""Authorization for private Bot interactions."""

from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import (
    Message,
    TelegramObject,
)

from tdl_bot.core.i18n import tr, use_language
from tdl_bot.transfers.accounts import AccessPolicy


class Authorization(BaseMiddleware):
    """Only allowlisted users in private chats may control the user account."""

    def __init__(self, policy: AccessPolicy, refresh_menu=None) -> None:
        """Check current membership instead of caching startup identities."""
        self.policy = policy
        self.refresh_menu = refresh_menu

    async def __call__(
        self, handler, event: TelegramObject, data: dict[str, Any]
    ):
        """Reject unauthorized messages before running any command."""
        if not isinstance(event, Message):
            return None
        if event.chat.type != "private" or event.from_user is None:
            return None
        if self.policy.store is not None:
            self.policy.store.remember_user(
                event.from_user.id,
                getattr(event.from_user, "username", None),
                getattr(event.from_user, "full_name", ""),
            )
        command = (event.text or "").split(maxsplit=1)[0:1]
        if command and command[0].split("@")[0] == "/whoami":
            return await handler(event, data)
        if not self.policy.is_allowed(event.from_user.id):
            language = (
                self.policy.store.language(event.from_user.id)
                if self.policy.store is not None
                else "en"
            )
            with use_language(language):
                await event.answer(
                    tr(
                        "This account is unauthorized or banned. Use "
                        "/whoami to get your account ID."
                    )
                )
            return None
        if self.refresh_menu:
            await self.refresh_menu(event.from_user.id)
        return await handler(event, data)
