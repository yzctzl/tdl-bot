"""Language defaults, catalog integrity, persistence, and async isolation."""

import asyncio

import pytest

from tdl_bot.i18n import CATALOGS, current_language, tr, use_language
from tdl_bot.store import Store


def test_default_and_unknown_languages_are_english():
    assert current_language() == "en"
    assert tr("Cancel") == "Cancel"
    with use_language("unknown"):
        assert tr("Cancel") == "Cancel"
    with use_language("zh"):
        assert tr("Cancel") == "取消"
        assert (
            tr("Future untranslated message") == "Future untranslated message"
        )
    assert current_language() == "en"


def test_user_language_persists_without_changing_other_accounts(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    assert store.language(1) == store.language(2) == "en"
    store.set_language(1, "zh")
    with pytest.raises(ValueError, match="Unsupported"):
        store.set_language(2, "unknown")
    store.close()
    store = Store(path, migrate=False)
    assert store.language(1) == "zh"
    assert store.language(2) == "en"
    store.close()


async def test_concurrent_languages_and_parameter_values_are_isolated():
    async def render(language):
        with use_language(language):
            await asyncio.sleep(0)
            return tr("Your user ID: {p0}", p0="{unchanged}")

    assert await asyncio.gather(render("zh"), render("en")) == [
        "你的用户 ID：{unchanged}",
        "Your user ID: {unchanged}",
    ]
    assert current_language() == "en"
    assert set(CATALOGS) >= {"en", "zh"}
