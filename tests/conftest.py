"""Shared opt-in locale for regression tests written against the Chinese UI."""

import pytest

from tdl_bot.i18n import use_language


@pytest.fixture
def chinese():
    with use_language("zh"):
        yield
