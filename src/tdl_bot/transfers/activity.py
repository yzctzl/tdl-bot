"""Mirror the two supported activity kinds into internal storage."""

import logging
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr

_LOGGER = logging.getLogger(__name__)


def media_link(chat, ids):
    if ids and str(chat).startswith("-100"):
        return f"https://t.me/c/{str(chat)[4:]}/{ids[0]}"
    if ids and str(chat).startswith("@"):
        return f"https://t.me/{str(chat)[1:]}/{ids[0]}"
    return None


def activity_line(event, label):
    """Share one compact HTML row between storage posts and admin pages."""
    timestamp = datetime.fromtimestamp(
        event["created"], ZoneInfo("Asia/Tokyo")
    ).strftime("%Y-%m-%d %H:%M JST")
    result = media_link(
        event.get("result_chat", ""), event.get("result_ids", [])
    )
    if result:
        # Keep stored media accessible without another visible field.
        timestamp = f'<a href="{escape(result, quote=True)}">{timestamp}</a>'
    action = tr("Link") if event["kind"] == "media" else tr("Join")
    link = f'<a href="{escape(event["url"], quote=True)}">{escape(action)}</a>'
    if event.get("status") in {"failed", "cancelled"}:
        link = f"<s>{link}</s>"
    suffix = ""
    if event.get("status") == "pending":
        suffix = " · " + escape(tr("Awaiting approval"))
    elif event.get("status") in {"recording", "recorded"}:
        suffix = " · …"
    return f"{timestamp} · {escape(' '.join(label.split()))} · {link}{suffix}"


class ActivityRecorder:
    def __init__(self, bot, store):
        self.bot = bot
        self.store = store

    def text(self, event):
        return activity_line(event, self.store.user_label(event["user_id"]))

    async def begin(self, key, kind, user_id, url, target, executor_key):
        event = self.store.activity.get(key)
        if event is not None:
            if not event.get("message_id"):
                raise UserError(
                    tr(
                        "The activity record needs administrator review "
                        "before retrying."
                    )
                )
            return event
        event = self.store.activity.create(
            key,
            kind,
            user_id,
            url=url,
            target=target.chat,
            topic_id=target.topic_id,
            executor_key=executor_key,
            status="recording",
        )
        try:
            message = await self.bot.send_message(
                chat_id=target.chat,
                message_thread_id=target.topic_id,
                text=self.text(event),
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as exc:
            self.store.activity.update(key, status="failed")
            raise UserError(
                tr(
                    "Cannot record this action. Ask an administrator "
                    "to check the service."
                )
            ) from exc
        return self.store.activity.update(
            key, message_id=message.message_id, status="recorded"
        )

    async def finish(self, key, **changes):
        event = self.store.activity.update(key, **changes)
        if event.get("message_id"):
            try:
                await self.bot.edit_message_text(
                    chat_id=event["target"],
                    message_id=event["message_id"],
                    text=self.text(event),
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramAPIError as exc:
                # The durable event is authoritative; never resend media after
                # a failed cosmetic edit or an uncertain acknowledgement.
                _LOGGER.warning(
                    "Cannot update activity record: %s", type(exc).__name__
                )
