"""Workspace values shared by persistence and application services."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """A destination chat and optional forum topic."""

    chat: str
    topic_id: int | None = None


@dataclass(frozen=True)
class Destination:
    """A target plus its management scope; None means shared."""

    target: Target
    owner_id: int | None = None


@dataclass(frozen=True)
class PendingTransfer:
    """A failed transfer retaining its destination and execution account."""

    id: int
    url: str
    submitted_by: int
    destination: Destination
    executor_key: str
    archived: bool = False


@dataclass(frozen=True)
class Member:
    """A workspace operator; ban deadlines are stored as UTC timestamps."""

    user_id: int
    role: str
    banned_until: float = 0

    def active(self, now: float) -> bool:
        return self.banned_until <= now
