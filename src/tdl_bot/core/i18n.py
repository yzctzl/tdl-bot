"""Per-request language contexts backed by extensible JSON catalogs.

English source messages are stable translation keys. Add a locale JSON file
with a display name and translated messages to offer another language.
"""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from string import Formatter

DEFAULT_LANGUAGE = "en"
_language = ContextVar("language", default=DEFAULT_LANGUAGE)


def _fields(text):
    return {
        (field, specification, conversion)
        for _, field, specification, conversion in Formatter().parse(text)
        if field
    }


def _unique_entries(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate locale entry: {key}")
        result[key] = value
    return result


def _load_catalogs():
    catalogs = {}
    for path in sorted((Path(__file__).parents[1] / "locales").glob("*.json")):
        catalog = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_entries
        )
        for source, translated in catalog["messages"].items():
            if _fields(source) != _fields(translated):
                raise ValueError(
                    f"Translation placeholders differ: {path.name}"
                )
        catalogs[path.stem] = catalog
    return catalogs


CATALOGS = _load_catalogs()


def current_language():
    return _language.get()


@contextmanager
def use_language(language):
    """Keep concurrent requests isolated, falling back to English."""
    token = _language.set(
        language if language in CATALOGS else DEFAULT_LANGUAGE
    )
    try:
        yield
    finally:
        _language.reset(token)


class Translation(str):
    """Keep a template available until background notification is delivered."""

    message: str
    values: dict

    def __new__(cls, text, message, values):
        instance = super().__new__(cls, text)
        instance.message = message
        instance.values = values
        return instance


def render(value):
    if isinstance(value, Translation):
        return tr(
            value.message,
            **{key: render(item) for key, item in value.values.items()},
        )
    return value


def tr(message: str, **values) -> str:
    """Translate a template before interpolating user-controlled values."""
    catalog = CATALOGS.get(current_language(), {}).get("messages", {})
    default = CATALOGS[DEFAULT_LANGUAGE]["messages"].get(message, message)
    template = catalog.get(message, default)
    text = template.format(**values) if values else template
    return Translation(text, message, values)


def saved_label(value: str) -> str:
    """Render persisted stage labels, including labels from Chinese releases."""
    for catalog in CATALOGS.values():
        for source, translated in catalog["messages"].items():
            if translated == value:
                return tr(source)
    return tr(value)


def localized(handler):
    """Use the recipient's saved language for commands, buttons, and errors."""

    @wraps(handler)
    async def wrapped(self, event, *args, **kwargs):
        if isinstance(event, int):
            user_id = event
        else:
            update = getattr(event, "update", None)
            origin = (
                (
                    getattr(update, "message", None)
                    or getattr(update, "callback_query", None)
                )
                if update is not None
                else event
            )
            user_id = getattr(getattr(origin, "from_user", None), "id", None)
        with use_language(self.store.language(user_id)):
            return await handler(self, event, *args, **kwargs)

    return wrapped


def job_language(handler):
    """Cancellation notifications belong to the submitter, not the manager."""

    @wraps(handler)
    async def wrapped(self, job, *args, **kwargs):
        with use_language(self.store.language(job.user_id)):
            return await handler(self, job, *args, **kwargs)

    return wrapped
