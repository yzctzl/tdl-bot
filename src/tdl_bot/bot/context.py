"""Shared dependencies for composed command groups."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tdl_bot.bot.application import BotApplication


class CommandGroup:
    """Hold stable service references; routing stays in the application."""

    def __init__(self, app: "BotApplication"):
        self.app = app
        self.bot = app.bot
        self.store = app.store
        self.policy = app.policy
        self.service = app.service
        self.tdl = app.tdl
