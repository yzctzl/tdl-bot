"""Authorization and permission retry commands through the bot layer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.filters import CommandObject
from aiogram.types import Message, TelegramObject

from tdl_bot.accounts import AccountPool, Executor
from tdl_bot.bot import Authorization, BotApplication, ProgressMessages
from tdl_bot.errors import JoinPending, UserError
from tdl_bot.service import Job
from tdl_bot.store import Store, Target
from tdl_bot.tdl import UpdateGate


@pytest.mark.parametrize(
    ("user_id", "chat_type", "text", "authorized"),
    [
        (1, "private", "/start", True),
        (2, "private", "/start", False),
        (2, "private", "/whoami", True),
        (2, "private", "/whoami@mybot", True),
        (1, "group", "/update_tdl", False),
    ],
)
async def test_authorization(user_id, chat_type, text, authorized):
    event = Mock(
        spec=Message,
        chat=SimpleNamespace(type=chat_type),
        from_user=SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )
    handler = AsyncMock()
    await Authorization(frozenset({1}))(handler, event, {})
    assert handler.await_count == int(authorized)


async def test_authorization_rejects_non_message_events():
    handler = AsyncMock()
    await Authorization(frozenset({1}))(handler, TelegramObject(), {})
    handler.assert_not_awaited()


@pytest.mark.parametrize("approval_required", [False, True])
async def test_join_retries_only_after_membership(tmp_path, approval_required):
    store = Store(tmp_path / "state.sqlite3")
    store.set_target(1, Target("me"))
    store.set_pending(1, "https://t.me/c/123456/10")
    media = SimpleNamespace(join=AsyncMock())
    if approval_required:
        media.join.side_effect = JoinPending("等待审核")
    item = store.pending_items()[0]
    service = SimpleNamespace(
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


async def test_progress_uses_one_reply_and_edits_without_extra_messages():
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
        edit_message_text=AsyncMock(),
    )
    job = Job(1, "https://t.me/channel/20", Target("me"), source_message_id=77)
    progress = ProgressMessages(bot)
    await progress(job, "queued")
    for text in ("resolve", "download", "upload"):
        await progress(job, text)
    await progress(job, "done", finished=True)
    bot.send_message.assert_awaited_once()
    assert (
        bot.send_message.call_args.kwargs["reply_parameters"].message_id == 77
    )
    assert bot.edit_message_text.await_count == 4
    assert all(
        call.kwargs["message_id"] == 99
        for call in bot.edit_message_text.call_args_list
    )
    button = bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[
        0
    ][0]
    assert button.callback_data == f"cancel:{job.id}"
    assert bot.edit_message_text.call_args.kwargs["reply_markup"] is None


@pytest.mark.parametrize("invalid", [None, "owner", "stale", "message", "chat"])
async def test_cancel_callback_checks_owner_token_and_progress_message(
    tmp_path, invalid
):
    store = Store(tmp_path / "state.sqlite3")
    job = Job(
        1, "https://t.me/channel/20", Target("me"), progress_message_id=99
    )
    service = SimpleNamespace(
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
    tdl = SimpleNamespace(update=AsyncMock())
    application = BotApplication(
        SimpleNamespace(),
        SimpleNamespace(
            allowed_users=frozenset({1}), admin_users=frozenset({2})
        ),
        store,
        tdl,
        SimpleNamespace(),
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1), answer=AsyncMock()
    )
    with pytest.raises(UserError, match="管理员"):
        await application.update(message, CommandObject(command="update_tdl"))
    tdl.update.assert_not_awaited()
    store.close()
