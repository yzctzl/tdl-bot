"""Regression tests for Telegram locations and persistent user settings."""

import pytest

from tdl_bot.errors import UserError
from tdl_bot.links import JoinLink, MessageLink
from tdl_bot.store import Store, Target

pytestmark = pytest.mark.usefixtures("chinese")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://t.me/channel/12", MessageLink("channel", 12)),
        ("https://t.me/s/channel/12", MessageLink("channel", 12)),
        ("https://telegram.me/channel/8/12", MessageLink("channel", 12)),
        ("https://t.me/c/123456/12", MessageLink(-1000000123456, 12)),
        ("https://t.me/c/123456/8/12", MessageLink(-1000000123456, 12)),
        (
            "https://t.me/channel/12?comment=42",
            MessageLink("channel", 12, 42),
        ),
        ("https://t.me/channel/12?thread=8", MessageLink("channel", 12)),
    ],
)
def test_message_urls(url, expected):
    assert MessageLink.parse(url) == expected
    assert MessageLink.parse(expected.url_for(99)).message_id == 99


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/channel/12",
        "https://t.me.evil.com/channel/12",
        "https://alice@t.me/channel/12",
        "https://t.me/channel/0",
        "https://t.me/channel/12?comment=no",
        "https://t.me/+invite",
        "https://t.me/channel",
        "https://t.me/c/123456/no/12",
        "https://t.me/channel/12;rm",
        "https://t.me:bad/channel/12",
        "",
    ],
)
def test_invalid_urls(url):
    with pytest.raises(UserError):
        MessageLink.parse(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://t.me/+Ab-c_12", JoinLink(invite_hash="Ab-c_12")),
        ("https://t.me/joinchat/Ab12", JoinLink(invite_hash="Ab12")),
        ("https://t.me/channel", JoinLink(username="channel")),
    ],
)
def test_join_urls(url, expected):
    assert JoinLink.parse(url) == expected


def test_store_persists_shared_and_personal_targets(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    store.set_target(1, Target("me", 42))
    store.set_target(2, Target("@personal"), private=True)
    store.set_pending(1, "old")
    store.set_pending(1, "new")
    store.close()
    reopened = Store(path)
    assert reopened.target(1) == Target("me", 42)
    assert reopened.target(2) == Target("@personal")
    assert reopened.target(3) == Target("me", 42)
    assert [item.url for item in reopened.pending_items()] == ["old", "new"]
    item = reopened.pending_items()[0]
    reopened.clear_pending(item.url, item.destination, item.executor_key)
    assert [item.url for item in reopened.pending_items()] == ["new"]
    reopened.select_target(2, private=False)
    assert reopened.target(2) == Target("me", 42)
    reopened.close()
