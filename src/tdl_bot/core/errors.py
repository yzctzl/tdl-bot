"""Errors that can safely be shown to bot users."""


class UserError(Exception):
    """An actionable error without credentials or subprocess output."""


class AccessDenied(UserError):
    """The downloading account cannot access the source chat."""


class JoinPending(UserError):
    """A Telegram administrator must approve the join request."""
