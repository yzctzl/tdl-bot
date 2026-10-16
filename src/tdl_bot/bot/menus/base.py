"""Shared menu rendering and command presentation without synthetic updates."""

from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)
from pydantic import Field

from tdl_bot.bot.context import CommandGroup
from tdl_bot.bot.menus.copy import tr
from tdl_bot.core.errors import UserError


class MenuButton(InlineKeyboardButton):
    full_width: bool = Field(default=False, exclude=True)


def button(label, path, *, wide=False):
    return MenuButton(text=label, callback_data="menu:" + path, full_width=wide)


def keyboard(rows):
    """Use two columns, retaining full-width rows for dynamic object names."""
    result, pair = [], []
    for item in (item for row in rows for item in row):
        wide = isinstance(item, MenuButton) and item.full_width
        if wide:
            if pair:
                result.append(pair)
                pair = []
            result.append([item])
        else:
            pair.append(item)
            if len(pair) == 2:
                result.append(pair)
                pair = []
    if pair:
        result.append(pair)
    return InlineKeyboardMarkup(inline_keyboard=result)


class CommandPage:
    """Present an existing command's response inside its owning menu page."""

    def __init__(self, menus, message, state, back, *, present=True):
        self.present = present
        self.menus, self.message, self.state, self.back = (
            menus,
            message,
            state,
            back,
        )

    def __getattr__(self, name):
        return getattr(self.message, name)

    async def answer(self, text, **kwargs):
        if not self.present:
            return self.message
        if kwargs.pop("parse_mode", None) != "HTML":
            text = escape(str(text))
        markup = kwargs.pop("reply_markup", None)
        rows = list(markup.inline_keyboard) if markup else []
        if self.back.startswith("member:") and not self.menus.policy.is_admin(
            self.message.from_user.id
        ):
            self.back = "root"
        rows.append([button(tr("Back"), self.back)])
        return await self.menus.show(self.message, self.state, text, rows)


class MenuBase(CommandGroup):
    def require_admin(self, actor):
        if not self.policy.is_admin(actor):
            raise UserError(tr("Only administrators can manage accounts."))

    async def show(self, message, state, text, rows):
        # One durable menu message owns the current input flow. Progress replies
        # remain independent, so navigating never overwrites a running task.
        actor = message.from_user.id
        if not self.policy.is_allowed(actor):
            raise UserError(tr("This account is unauthorized or banned."))
        state["version"] = state.get("version", 0) + 1
        markup = keyboard(rows)
        for row in markup.inline_keyboard:
            for item in row:
                if item.callback_data and item.callback_data.startswith(
                    "menu:"
                ):
                    item.callback_data = (
                        f"menu:{state['version']}:" + item.callback_data[5:]
                    )
        try:
            result = await self.bot.edit_message_text(
                chat_id=actor,
                message_id=state["message"],
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                raise
            result = message
        self.store.save_menu(actor, state)
        return result

    async def command(self, message, state, name, args, back, *, present=True):
        entry = next(
            item for item in self.app.command_registry if item[0] == name
        )
        await self.app.handler(entry[1])(
            CommandPage(self, message, state, back, present=present),
            CommandObject(command=name, args=args or None),
        )

    async def prompt(self, message, state, field, text, back):
        state["wait"] = field
        state["back"] = back
        await self.show(message, state, text, [[button(tr("Back"), back)]])

    async def confirm(self, message, state, action, text, back):
        from uuid import uuid4

        token = uuid4().hex[:12]
        state["confirmation"] = {"token": token, "action": action, "back": back}
        await self.show(
            message,
            state,
            text,
            [
                [button(tr("Confirm"), "confirm:" + token)],
                [button(tr("Back"), back)],
            ],
        )

    @staticmethod
    def pages(page, total, size, path):
        rows = []
        if page > 1:
            rows.append(button(tr("Previous"), f"{path}:{page - 1}"))
        if page * size < total:
            rows.append(button(tr("Next"), f"{path}:{page + 1}"))
        return [rows] if rows else []
