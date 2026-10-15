"""Authorization and permission retry commands through the bot layer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.filters import CommandObject

from tdl_bot.bot.application import BotApplication
from tdl_bot.core.errors import JoinPending, UserError
from tdl_bot.integrations.tdl.runner import UpdateGate
from tdl_bot.storage.store import Store, Target
from tdl_bot.transfers.accounts import AccessPolicy, AccountPool, Executor
from tdl_bot.transfers.service import Job

pytestmark = pytest.mark.usefixtures("chinese")


@pytest.mark.parametrize("approval_required", [False, True])
async def test_join_retries_only_after_membership(tmp_path, approval_required):
    store = Store(tmp_path / "state.sqlite3")
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    store.set_target(1, Target("me"))
    store.set_pending(1, "https://t.me/c/123456/10")
    media = SimpleNamespace(join=AsyncMock())
    if approval_required:
        media.join.side_effect = JoinPending("等待审核")
    item = store.pending_items()[0]
    service = SimpleNamespace(
        policy=AccessPolicy(frozenset({1}), frozenset({1})),
        pending_record=Mock(return_value=item),
        retry=AsyncMock(),
        accounts=AccountPool(
            Executor("shared", SimpleNamespace(gate=UpdateGate()), media)
        ),
    )
    application = BotApplication(
        SimpleNamespace(),
        SimpleNamespace(allowed_users=frozenset({1})),
        store,
        SimpleNamespace(),
        service,
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1), answer=AsyncMock(), message_id=10
    )
    await application.join(
        message, CommandObject(command="join", args="https://t.me/+invite")
    )
    assert service.retry.call_count == int(not approval_required)
    assert store.pending_items()
    store.close()


@pytest.mark.parametrize("invalid", [None, "owner", "stale", "message", "chat"])
async def test_cancel_callback_checks_owner_token_and_progress_message(
    tmp_path, invalid
):
    store = Store(tmp_path / "state.sqlite3")
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    job = Job(
        1, "https://t.me/channel/20", Target("me"), progress_message_id=99
    )
    service = SimpleNamespace(
        policy=AccessPolicy(frozenset({1}), frozenset({1})),
        current_job=Mock(return_value=None if invalid == "stale" else job),
        cancel=AsyncMock(),
    )
    application = BotApplication(
        SimpleNamespace(),
        SimpleNamespace(allowed_users=frozenset({1})),
        store,
        SimpleNamespace(),
        service,
    )
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=2 if invalid == "owner" else 1),
        data=f"cancel:{job.id}",
        message=SimpleNamespace(
            message_id=100 if invalid == "message" else 99,
            chat=SimpleNamespace(
                type="group" if invalid == "chat" else "private", id=1
            ),
        ),
        answer=AsyncMock(),
    )
    await application.cancel(callback)
    assert service.cancel.await_count == int(invalid is None)
    callback.answer.assert_awaited_once()
    store.close()


async def test_update_requires_admin(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    tdl = SimpleNamespace(update=AsyncMock())
    application = BotApplication(
        SimpleNamespace(),
        SimpleNamespace(
            allowed_users=frozenset({1}), admin_users=frozenset({2})
        ),
        store,
        tdl,
        SimpleNamespace(policy=AccessPolicy(frozenset({1}), frozenset())),
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1), answer=AsyncMock()
    )
    with pytest.raises(UserError, match="管理员"):
        await application.update(message, CommandObject(command="update_tdl"))
    tdl.update.assert_not_awaited()
    store.close()
