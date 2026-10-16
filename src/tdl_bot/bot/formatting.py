"""Shared command argument formatting."""

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr


def page_number(args: str | None) -> int:
    if args is None or not args.strip():
        return 1
    if not args.strip().isdigit() or int(args) <= 0:
        raise UserError(tr("Page number must be a positive integer."))
    return int(args)


def command_prefix(value, commands):
    """Resolve unique verb prefixes; leave unrelated argument values intact."""
    word = value.lower()
    if word in commands:
        return word
    matches = sorted(
        command for command in commands if command.startswith(word)
    )
    if len(matches) > 1:
        raise UserError(
            tr(
                "Ambiguous command '{prefix}'. Use: {choices}.",
                prefix=value,
                choices=", ".join(matches),
            )
        )
    return matches[0] if matches else value
