"""Parse Telegram message and invitation urls without shell interpretation."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr

_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
_USERNAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{3,31}\Z")


@dataclass(frozen=True)
class MessageLink:
    """A Telegram message location, including an optional comment."""

    peer: str | int
    message_id: int
    comment_id: int | None = None

    @classmethod
    def parse(cls, url: str) -> "MessageLink":
        """Parse public, private, forum, preview, and discussion urls."""
        parts = _parts(url)
        path = parts.path.strip("/").split("/")
        if path[0] == "s":
            path = path[1:]
        private = path[0] == "c"
        if private:
            if len(path) not in (3, 4) or not path[1].isdigit():
                raise UserError(tr("Invalid private message URL format."))
            channel_id = int(path[1])
            if not 0 < channel_id < 1_000_000_000_000:
                raise UserError(tr("Invalid private chat ID."))
            peer: str | int = -(1_000_000_000_000 + channel_id)
            numbers = path[2:]
        else:
            if len(path) not in (2, 3) or not _USERNAME.fullmatch(path[0]):
                raise UserError(
                    tr("Send a Telegram message URL, not a group homepage.")
                )
            peer = path[0]
            numbers = path[1:]
        if any(not value.isdigit() or int(value) <= 0 for value in numbers):
            raise UserError(
                tr("Message and topic IDs must be positive integers.")
            )
        query = parse_qs(parts.query)
        comment = query.get("comment", [None])[0]
        if comment is not None and (not comment.isdigit() or int(comment) <= 0):
            raise UserError(tr("Invalid comment message ID."))
        return cls(peer, int(numbers[-1]), int(comment) if comment else None)

    def url_for(self, message_id: int) -> str:
        """Return a canonical download url for a media-group member."""
        if isinstance(self.peer, int):
            if -1_000_000_000_000 < self.peer < 0:
                return f"telegram:chat:{-self.peer}:{message_id}"
            channel_id = -self.peer - 1_000_000_000_000
            return f"https://t.me/c/{channel_id}/{message_id}"
        return f"https://t.me/{self.peer}/{message_id}"


@dataclass(frozen=True)
class JoinLink:
    """An explicit public-chat or private-invitation url."""

    username: str | None = None
    invite_hash: str | None = None

    @classmethod
    def parse(cls, url: str) -> "JoinLink":
        """Accept group homepages and invitation urls only."""
        path = _parts(url).path.strip("/").split("/")
        invite = None
        if len(path) == 1 and path[0].startswith("+"):
            invite = path[0][1:]
        elif len(path) == 2 and path[0] == "joinchat":
            invite = path[1]
        elif len(path) == 1 and _USERNAME.fullmatch(path[0]):
            return cls(username=path[0])
        if invite and re.fullmatch(r"[a-zA-Z0-9_-]+", invite):
            return cls(invite_hash=invite)
        raise UserError(
            tr("Send a group homepage or an https://t.me/+… invite URL.")
        )


def _parts(url: str):
    """Require an ordinary Telegram HTTPS url."""
    try:
        parts = urlsplit(url.strip())
        valid = (
            parts.scheme in {"https", "http"}
            and parts.hostname in _HOSTS
            and not parts.username
            and not parts.password
            and parts.port in {None, 80, 443}
        )
    except ValueError as exc:
        raise UserError(tr("Invalid URL format.")) from exc
    if not valid:
        raise UserError(
            tr(
                "Only Telegram message URLs supported by tdl (such as "
                "t.me) are accepted."
            )
        )
    return parts
