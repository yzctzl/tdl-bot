"""Shared command argument formatting."""

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr


def page_number(args: str | None) -> int:
    if args is None or not args.strip():
        return 1
    if not args.strip().isdigit() or int(args) <= 0:
        raise UserError(tr("Page number must be a positive integer."))
    return int(args)
