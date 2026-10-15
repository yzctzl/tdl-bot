"""Dynamic membership, command menus, retry limits and account revocation."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.filters import CommandObject
from aiogram.types import BotCommandScopeChat, Message

from tdl_bot.accounts import AccessPolicy, AccountPool, Executor
from tdl_bot.bot import _COMMANDS, Authorization, BotApplication
from tdl_bot.errors import AccessDenied, UserError
from tdl_bot.service import TransferService
from tdl_bot.store import Store, Target
from tdl_bot.tdl import UpdateGate


@pytest.fixture
def managed(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    policy = AccessPolicy(frozenset({1, 2, 3}), frozenset({1}), store=store)
    store.set_target(1, Target("me"))
    group = SimpleNamespace(
        messages=[SimpleNamespace(id=10, document=None)],
        urls=["https://t.me/channel/10"],
    )

    async def download(urls, directory):
        (directory / "10_photo.jpg").write_bytes(b"photo")

    tdl = SimpleNamespace(
        gate=UpdateGate(),
        download=AsyncMock(side_effect=download),
        update=AsyncMock(),
    )
    media = SimpleNamespace(
        resolve=AsyncMock(return_value=group),
        upload=AsyncMock(),
        join=AsyncMock(),
        validate_target=AsyncMock(),
    )
    accounts = AccountPool(Executor("shared", tdl, media))
    service = TransferService(
        accounts,
        store,
        tmp_path / "jobs",
        AsyncMock(),
        policy=policy,
        workers=1,
        queue_size=20,
    )
    service.jobs_dir.mkdir()
    bot = SimpleNamespace(
        set_my_commands=AsyncMock(), edit_message_text=AsyncMock()
    )

    async def validate(self, target, operator, *, storage=False):
        return target

    monkeypatch.setattr("tdl_bot.bot.Delivery.validate", validate)
    app = BotApplication(bot, SimpleNamespace(), store, tdl, service)
    yield app, service, store, policy
    store.close()


def message(user=1):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user),
        message_id=70,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=80)),
    )


def command(name, args=None):
    return CommandObject(command=name, args=args)


def test_deleted_members_and_roles_survive_restart(managed, tmp_path):
    _, _, store, policy = managed
    store.add_member(1, 4)
    assert policy.is_allowed(4)
    store.set_member_role(1, 4, "admin")
    store.remove_member(1, 2)
    reopened = Store(tmp_path / "state.sqlite3")
    try:
        current = AccessPolicy(
            frozenset({1, 2, 3}), frozenset({1}), store=reopened
        )
        assert not current.is_allowed(2)
        assert current.is_admin(4)
        assert [row[1] for row in reopened.member_history(1, 2)] == ["remove"]
    finally:
        reopened.close()


@pytest.mark.parametrize("seconds", [86400, 7 * 86400, 30 * 86400])
def test_ban_expiry_is_evaluated_dynamically(managed, monkeypatch, seconds):
    _, _, store, policy = managed
    monkeypatch.setattr(store, "now", lambda: 1000)
    store.ban_member(1, 2, seconds)
    assert not policy.is_allowed(2)
    assert store.member(2).banned_until == 1000 + seconds
    monkeypatch.setattr(store, "now", lambda: 1000 + seconds)
    assert policy.is_allowed(2)
    assert policy.can_manage(2, 2)


@pytest.mark.parametrize("operation", ["remove", "ban", "demote"])
def test_last_admin_cannot_be_revoked(managed, operation):
    _, _, store, policy = managed
    with pytest.raises(UserError, match="最后一个"):
        if operation == "remove":
            store.remove_member(1, 1)
        elif operation == "ban":
            store.ban_member(1, 1, 86400)
        else:
            store.set_member_role(1, 1, "user")
    assert policy.is_admin(1)


def test_banned_admin_cannot_manage_accounts(managed):
    _, _, store, policy = managed
    store.set_member_role(1, 2, "admin")
    store.ban_member(1, 2, 86400)
    assert not policy.is_admin(2)
    with pytest.raises(UserError, match="管理员"):
        store.add_member(2, 4)


async def test_ordinary_target_cannot_change_shared_destination(managed):
    app, _, store, _ = managed
    media = app.service.accounts.shared.media
    with pytest.raises(UserError, match="接收位置"):
        await app.target(message(2), command("target", "shared @other"))
    media.validate_target.assert_not_awaited()
    assert store.target_for(None) == Target("me")
    await app.target(message(2), command("target", "@mine 42"))
    assert store.target_for(2) == Target("@mine", 42)
    assert store.target_for(None) == Target("me")
    await app.target(message(2), command("target", "bot"))
    assert store.recipient(2).target == Target("2")


@pytest.mark.parametrize(
    "handler,args",
    [
        ("users", None),
        ("user_add", "4"),
        ("user_remove", "3"),
        ("user_ban", "3 1d"),
        ("user_unban", "3"),
        ("user_role", "3 admin"),
        ("user_history", "3"),
        ("update", "latest"),
    ],
)
async def test_ordinary_accounts_cannot_use_admin_commands(
    managed, handler, args
):
    app, _, store, _ = managed
    before = store.members()
    with pytest.raises(UserError, match="管理员"):
        await getattr(app, handler)(message(2), command(handler, args))
    assert store.members() == before


async def test_retry_cancellation_checks_scope_and_active_transfer(managed):
    _, service, store, _ = managed
    store.set_target(2, Target("@personal"), private=True)
    store.set_pending(2, "https://t.me/channel/10")
    item = store.pending_items()[0]
    with pytest.raises(UserError, match="无权"):
        service.discard_pending(3, item.id)
    job = await service.retry(2, item.id)
    with pytest.raises(UserError, match="正在重试"):
        service.discard_pending(1, item.id)
    await service.cancel(1, job.id)
    service.discard_pending(1, item.id)
    assert not store.pending_items()
    await service.close()


async def test_parallel_failures_never_exceed_three_retries(managed):
    _, service, store, _ = managed
    service.accounts.shared.media.resolve.side_effect = AccessDenied(
        "no access"
    )
    for number in range(1, 7):
        await service.submit(2, f"https://t.me/channel/{number}")
    service.start()
    await service.close()
    assert store.pending_count(2) == 3
    texts = [call.args[1] for call in service.notify.call_args_list]
    assert sum("未加入待重试列表" in text for text in texts) == 3
    with pytest.raises(UserError, match="3 条"):
        await service.submit(2, "https://t.me/channel/99")


async def test_retry_and_discard_release_pending_capacity(managed):
    _, service, store, _ = managed
    for number in range(1, 4):
        store.set_pending(2, f"https://t.me/channel/{number}", limit=3)
    item = store.pending_items()[0]
    job = await service.retry(2, item.id)
    await service.cancel(2, job.id)
    assert store.pending_count(2) == 3
    service.discard_pending(
        3, item.id
    )  # shared records are collaboratively managed
    assert store.pending_count(2) == 2
    job = await service.submit(2, "https://t.me/channel/99")
    await service.cancel(2, job.id)
    await service.close()


async def test_admin_retry_limit_is_unrestricted(managed):
    _, service, store, _ = managed
    service.accounts.shared.media.resolve.side_effect = AccessDenied(
        "no access"
    )
    for number in range(1, 6):
        await service.submit(1, f"https://t.me/channel/{number}")
    service.start()
    await service.close()
    assert store.pending_count(1) == 5
    store.set_member_role(1, 3, "admin")
    store.set_member_role(1, 1, "user")
    assert store.pending_count(1) == 3
    assert len(service.pending_items(1)) == 3
    assert len(service.pending_items(3)) == 5
    assert sum(item.archived for item in service.pending_items(3)) == 2


def test_retry_limit_is_atomic_across_connections(managed, tmp_path):
    _, _, store, _ = managed
    path = tmp_path / "state.sqlite3"

    def add(number):
        connection = Store(path)
        try:
            return connection.set_pending(
                2, f"https://t.me/channel/{number}", limit=3
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        saved = list(pool.map(add, range(6)))
    assert sum(saved) == 3
    assert store.pending_count(2) == 3


async def test_registered_menus_cover_every_command_and_follow_roles(managed):
    app, _, store, _ = managed
    await app.register_commands()
    names = {item.command for item in app.command_menu(admin=True)}
    assert names == {name for name, _, _, _ in _COMMANDS}
    assert "retry_cancel" in {item.command for item in app.command_menu()}
    assert "user_add" not in {item.command for item in app.command_menu()}
    assert "update_tdl" not in {item.command for item in app.command_menu()}
    handlers = app.dispatcher.sub_routers[0].message.handlers
    for _, name, _, _ in _COMMANDS:
        assert any(
            handler.callback == getattr(app, name) for handler in handlers
        )
    admin_call = next(
        call
        for call in app.bot.set_my_commands.call_args_list
        if isinstance(call.kwargs["scope"], BotCommandScopeChat)
        and call.kwargs["scope"].chat_id == 1
    )
    assert "user_add" in {item.command for item in admin_call.args[0]}
    store.ban_member(1, 2, 86400)
    await app.refresh_menu(2)
    assert [
        item.command for item in app.bot.set_my_commands.call_args.args[0]
    ] == ["whoami"]
    store.unban_member(1, 2)
    await app.refresh_menu(2)
    assert "retry" in {
        item.command for item in app.bot.set_my_commands.call_args.args[0]
    }


async def test_adding_member_without_open_chat_retries_menu_later(managed):
    app, _, store, policy = managed
    app.bot.set_my_commands.side_effect = RuntimeError("chat unavailable")
    await app.user_add(message(1), command("user_add", "4"))
    assert policy.is_allowed(4)
    assert 4 not in app._menu_roles
    app.bot.set_my_commands.side_effect = None
    event = Mock(
        spec=Message,
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=4),
        text="/start",
    )
    handler = AsyncMock()
    await Authorization(policy, app.refresh_menu)(handler, event, {})
    handler.assert_awaited_once()
    assert app._menu_roles[4] == "user"


@pytest.mark.parametrize("action", ["user_ban", "user_remove"])
async def test_revocation_cancels_active_download_and_cleans_files(
    managed, action
):
    app, service, _, policy = managed
    started = asyncio.Event()

    async def partial(urls, directory):
        (directory / "partial").write_bytes(b"partial")
        started.set()
        await asyncio.Event().wait()

    service.accounts.shared.tdl.download.side_effect = partial
    service.start()
    job = await service.submit(2, "https://t.me/channel/10")
    await asyncio.wait_for(started.wait(), 2)
    async with asyncio.timeout(2):
        await getattr(app, action)(
            message(1), command(action, "2 1d" if action == "user_ban" else "2")
        )
    assert job.done.is_set()
    assert not policy.is_allowed(2)
    assert not list(service.jobs_dir.iterdir())
    with pytest.raises(UserError, match="未获授权"):
        await service.submit(2, "https://t.me/channel/20")
    await service.close()


async def test_revoke_during_failed_initial_reply_never_deadlocks(managed):
    _, service, store, _ = managed
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail(*args, **kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("reply delivery failed")

    service.notify.side_effect = fail
    submitted = asyncio.create_task(
        service.submit(2, "https://t.me/channel/10")
    )
    await entered.wait()
    store.ban_member(1, 2, 86400)
    revoked = asyncio.create_task(service.cancel_submitted(1, 2))
    release.set()
    with pytest.raises(RuntimeError):
        await submitted
    await asyncio.wait_for(revoked, 2)
    assert not service._jobs
    await service.close()


async def test_retry_cancel_buttons_check_origin_and_remove_record(managed):
    app, service, store, _ = managed
    store.set_pending(2, "https://t.me/channel/10")
    item = store.pending_items()[0]
    view = message(3)
    await app.pending(view, command("pending"))
    markup = view.answer.call_args.kwargs["reply_markup"]
    assert (
        markup.inline_keyboard[0][0].callback_data == f"retry_cancel:{item.id}"
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=3),
        data=f"retry_cancel:{item.id}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=3, type="private"), message_id=99
        ),
        answer=AsyncMock(),
    )
    await app.cancel_retry(query)
    assert store.pending_items()
    query.message.message_id = 80
    await app.cancel_retry(query)
    assert not store.pending_items()
    assert app.bot.edit_message_text.call_args.kwargs["reply_markup"] is None
    await service.close()


def test_v1_migration_keeps_workspace_data_and_creates_private_backup(tmp_path):
    path = tmp_path / "state.sqlite3"
    previous = Store(path)
    previous.set_target(1, Target("me"))
    previous.set_pending(1, "https://t.me/channel/10")
    previous.close()
    with sqlite3.connect(path) as db:
        db.executescript(
            "DROP TABLE workspace_members; DROP TABLE workspace_metadata; "
            "DROP TABLE member_events; DROP TABLE permission_retry_archive; "
            "DROP TABLE user_profiles; DROP TABLE transfer_results; "
            "DROP TABLE runtime_jobs; DROP TABLE accepted_requests; "
            "DROP TABLE stored_results; DROP TABLE result_sources; "
            "DROP TABLE delivery_history; "
            "PRAGMA user_version=1;"
        )
    current = Store(path)
    try:
        assert current.target_for(None) == Target("me")
        assert len(current.pending_items()) == 1
        backup = path.with_name(path.name + ".pre-members")
        assert backup.stat().st_mode & 0o077 == 0
        with sqlite3.connect(backup) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        current.close()


async def test_archived_overflow_is_admin_only_and_can_be_removed(managed):
    app, service, store, _ = managed
    for number in range(5):
        store.set_pending(2, f"https://t.me/channel/{number + 1}")
    store.set_member_role(1, 2, "user")
    assert store.pending_count(2) == 3
    archived = [item for item in service.pending_items(1) if item.archived]
    assert len(archived) == 2
    assert len(service.pending_items(2)) == 3
    assert len(service.pending_items(3)) == 3
    with pytest.raises(UserError, match="无权"):
        service.discard_pending(2, archived[0].id)
    view = message(1)
    await app.pending(view, command("pending"))
    assert "归档" in view.answer.call_args.args[0]
    service.discard_pending(1, archived[0].id)
    assert len(service.pending_items(1)) == 4
    job = await service.retry(1, archived[1].id)
    service.start()
    await service.close()
    assert job.done.is_set()
    assert len(service.pending_items(1)) == 3


async def test_removed_admin_menu_is_cleared_again_after_restart(managed):
    app, _, store, policy = managed
    store.add_member(1, 4)
    store.set_member_role(1, 4, "admin")
    store.remove_member(1, 4)
    assert 4 in policy.menu_ids
    await app.register_commands()
    removed = next(
        call
        for call in app.bot.set_my_commands.call_args_list
        if isinstance(call.kwargs["scope"], BotCommandScopeChat)
        and call.kwargs["scope"].chat_id == 4
    )
    assert [item.command for item in removed.args[0]] == ["whoami"]


def test_seed_archives_old_ordinary_overflow_without_losing_urls(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        store.set_target(1, Target("me"))
        for number in range(1, 6):
            store.set_pending(2, f"https://t.me/channel/{number}")
        policy = AccessPolicy(frozenset({1, 2}), frozenset({1}), store=store)
        assert policy.is_allowed(2)
        assert store.pending_count(2) == 3
        records = store.pending_items(include_archived=True)
        assert len(records) == 5
        assert [record.id for record in records] == [1, 2, 3, 4, 5]
        assert sum(record.archived for record in records) == 2
    finally:
        store.close()


def test_archive_upgrade_keeps_v2_members_and_creates_private_backup(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    policy = AccessPolicy(frozenset({1, 2}), frozenset({1}), store=store)
    store.set_target(1, Target("me"))
    for number in range(5):
        store.set_pending(2, f"https://t.me/channel/{number + 1}")
    assert policy.is_admin(1)
    store.close()
    with sqlite3.connect(path) as db:
        db.executescript(
            "DROP TABLE permission_retry_archive; "
            "DROP TABLE user_profiles; DROP TABLE transfer_results; "
            "DROP TABLE runtime_jobs; DROP TABLE accepted_requests; "
            "DROP TABLE stored_results; DROP TABLE result_sources; "
            "DROP TABLE delivery_history; "
            "PRAGMA user_version=2;"
        )
    migrated = Store(path)
    try:
        admin = migrated.member(1)
        assert admin is not None and admin.role == "admin"
        assert migrated.pending_count(2) == 3
        assert len(migrated.pending_items(include_archived=True)) == 5
        backup = path.with_name(path.name + ".pre-retry-cap")
        assert backup.stat().st_mode & 0o077 == 0
        with sqlite3.connect(backup) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM permission_retries"
                ).fetchone()[0]
                == 5
            )
    finally:
        migrated.close()


def test_config_uses_persisted_members_instead_of_stale_env(
    managed, tmp_path, monkeypatch
):
    from tdl_bot.config import Settings

    _, _, store, _ = managed
    store.set_member_role(1, 3, "admin")
    store.remove_member(3, 1)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOT_TOKEN", "configured")
    monkeypatch.setenv("SESSION_SOURCE", "tdl")
    monkeypatch.setenv("ALLOWED_USER_IDS", "")
    monkeypatch.setenv("ADMIN_USER_IDS", "1")
    monkeypatch.delenv("ACCOUNTS_FILE", raising=False)
    settings = Settings.from_env()
    assert settings.allowed_users == frozenset({2, 3})
    assert settings.admin_users == frozenset({3})


async def test_demotion_revokes_borrowed_private_task_management(managed):
    app, service, store, policy = managed
    store.set_target(2, Target("@private"), private=True)
    store.set_pending(2, "https://t.me/channel/10")
    pending = store.pending_items()[0]
    borrowed = await service.retry(1, pending.id)
    own = await service.submit(1, "https://t.me/channel/20")
    store.set_member_role(1, 3, "admin")
    await app.user_role(message(1), command("user_role", "1 user"))
    assert not policy.is_admin(1)
    assert borrowed.done.is_set()
    assert borrowed.cancel_requested
    assert service.current_job(1, own.id) is own
    assert store.pending_items()[0].id == pending.id
    await service.cancel(1, own.id)
    await service.close()


async def test_worker_rechecks_changed_private_permissions(managed):
    _, service, store, _ = managed
    store.set_target(2, Target("@private"), private=True)
    store.set_pending(2, "https://t.me/channel/10")
    borrowed = await service.retry(1, store.pending_items()[0].id)
    store.set_member_role(1, 3, "admin")
    store.set_member_role(1, 1, "user")
    service.start()
    await service.close()
    assert borrowed.cancel_requested
    service.accounts.shared.media.resolve.assert_not_awaited()


async def test_username_management_resolves_live_uid_and_shows_both(managed):
    app, service, store, _ = managed
    service.accounts.shared.media.resolve_user = AsyncMock(
        return_value=SimpleNamespace(
            id=4,
            username="example",
            first_name="Test",
            last_name=None,
        )
    )
    event = message(1)
    await app.user_add(event, command("user_add", "@example"))
    assert store.member(4)
    assert "@example" in event.answer.call_args.args[0]
    assert "UID: 4" in event.answer.call_args.args[0]
    # Reassigned usernames resolve to their current UID, never cached privilege.
    service.accounts.shared.media.resolve_user.return_value.id = 5
    await app.user_add(event, command("user_add", "@example"))
    assert "@example" not in store.user_label(4)
    assert "@example" in store.user_label(5)


async def test_admin_status_shows_all_targets_but_other_users_cannot(managed):
    app, _, store, _ = managed
    store.set_target(2, Target("personal", 77), private=True)
    event = message(1)
    await app.status(event, command("status", "2"))
    text = event.answer.call_args.args[0]
    assert "接收位置：personal" in text
    assert "共享目标" not in text
    assert "me" not in text
    assert "77" in text
    with pytest.raises(UserError, match="只有管理员"):
        await app.status(message(3), command("status", "2"))


@pytest.mark.parametrize("username", [None, "example_forum"])
async def test_users_links_bot_dialogue_and_channel_topic(managed, username):
    app, _, store, _ = managed
    store.remember_user(1, None, "Admin <owner> & friends")
    store.set_target(2, Target("-1001234567890", 77), private=True)
    app.bot.get_me = AsyncMock(
        return_value=SimpleNamespace(username="example_bot")
    )
    app.bot.get_chat = AsyncMock(
        return_value=SimpleNamespace(title="Forum <test>", username=username)
    )
    event = message(1)
    await app.users(event, command("users"))
    text = event.answer.call_args.args[0]
    assert "Admin &lt;owner&gt; &amp; friends" in text
    assert '<a href="https://t.me/example_bot">Bot 对话</a>' in text
    url = (
        "https://t.me/example_forum/77"
        if username
        else "https://t.me/c/1234567890/77"
    )
    assert f'<a href="{url}">Forum &lt;test&gt; / 话题 77</a>' in text
    assert event.answer.call_args.kwargs["parse_mode"] == "HTML"
    assert event.answer.call_args.kwargs["link_preview_options"].is_disabled


def test_result_cache_and_profiles_survive_restart(managed, tmp_path):
    _, _, store, _ = managed
    store.remember_user(2, "example", "Test")
    store.save_result(["https://t.me/channel/1"], "shared", None, "me", [10])
    reopened = Store(tmp_path / "state.sqlite3")
    try:
        assert reopened.user_label(2) == "@example (UID: 2)"
        assert reopened.saved_results(["https://t.me/channel/1"], "shared") == [
            (None, "me", [10])
        ]
    finally:
        reopened.close()


def test_v3_result_migration_backs_up_and_preserves_members(managed, tmp_path):
    _, _, store, _ = managed
    path = tmp_path / "state.sqlite3"
    store.close()
    with sqlite3.connect(path) as db:
        db.executescript(
            "DROP TABLE user_profiles; DROP TABLE transfer_results; "
            "DROP TABLE runtime_jobs; DROP TABLE accepted_requests; "
            "DROP TABLE stored_results; DROP TABLE result_sources; "
            "DROP TABLE delivery_history; "
            "PRAGMA user_version=3;"
        )
    migrated = Store(path)
    try:
        member = migrated.member(1)
        assert member is not None and member.role == "admin"
        assert migrated.target(1) == Target("me")
        backup = path.with_name(path.name + ".pre-results")
        assert backup.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(backup) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 7
    finally:
        migrated.close()


async def test_status_renders_clickable_target_and_escapes_titles(managed):
    app, _, store, _ = managed
    store.set_target(2, Target("-1001234567890"), private=True)
    app.bot.get_chat = AsyncMock(
        return_value=SimpleNamespace(
            title="A < B & C",
            username="example",
        )
    )
    event = message(2)
    await app.status(event)
    text = event.answer.call_args.args[0]
    assert '<a href="https://t.me/example">A &lt; B &amp; C</a>' in text
    assert "话题" not in text
    assert event.answer.call_args.kwargs["parse_mode"] == "HTML"
    app.bot.get_chat.return_value.username = None
    store.set_target(2, Target("-1001234567890", 42), private=True)
    await app.status(event)
    assert (
        'href="https://t.me/c/1234567890/42"' in event.answer.call_args.args[0]
    )
    assert "话题 42" in event.answer.call_args.args[0]


async def test_status_bot_destination_links_to_bot_instead_of_operator(managed):
    app, _, _, _ = managed
    app.bot.get_me = AsyncMock(
        return_value=SimpleNamespace(username="examplebot")
    )
    event = message(2)
    await app.status(event)
    assert (
        '<a href="https://t.me/examplebot">Bot 对话</a>'
        in event.answer.call_args.args[0]
    )
    assert "话题" not in event.answer.call_args.args[0]


async def test_target_admin_queries_render_same_clickable_destination(managed):
    args = "for 2"
    app, _, store, _ = managed
    store.set_target(2, Target("-1001234567890", 42), private=True)
    app.bot.get_chat = AsyncMock(
        return_value=SimpleNamespace(title="Target & Name", username="example")
    )
    event = message(1)
    await app.target(event, command("target", args))
    text = event.answer.call_args.args[0]
    assert 'href="https://t.me/example/42"' in text
    assert 'href="tg://user?id=2"' in text
    assert "Target &amp; Name" in text
    assert event.answer.call_args.kwargs["parse_mode"] == "HTML"
    assert store.recipient(2).target == Target("-1001234567890", 42)


async def test_target_self_query_renders_only_clickable_destination(managed):
    args = None
    app, _, _, _ = managed
    app.bot.get_me = AsyncMock(
        return_value=SimpleNamespace(username="examplebot")
    )
    event = message(2)
    await app.target(event, command("target", args))
    text = event.answer.call_args.args[0]
    assert 'href="https://t.me/examplebot"' in text
    assert "账号：" not in text
    assert "执行账号" not in text
    assert "可管理任务" not in text
    assert event.answer.call_args.kwargs["parse_mode"] == "HTML"


async def test_target_get_cannot_modify_or_expose_other_accounts(managed):
    app, _, store, _ = managed
    for args in ("get", "get 3", "get 2 @other"):
        with pytest.raises(UserError, match="直接使用 /target"):
            await app.target(message(2), command("target", args))
    assert store.target_for(2) is None


async def test_target_legacy_positive_channel_id_still_generates_private_link(
    managed,
):
    app, _, store, _ = managed
    store.set_target(2, Target("1234567890"), private=True)
    event = message(2)
    await app.status(event)
    assert 'href="https://t.me/c/1234567890"' in event.answer.call_args.args[0]


@pytest.mark.parametrize(
    "username,topic,url",
    [
        ("example", None, "https://t.me/example"),
        (None, 42, "https://t.me/c/1234567890/42"),
    ],
)
async def test_target_channel_query_and_save_feedback_show_destination_link(
    managed,
    username,
    topic,
    url,
):
    app, _, store, _ = managed
    app.bot.get_chat = AsyncMock(
        return_value=SimpleNamespace(
            title="Videos & Photos",
            username=username,
        )
    )
    event = message(2)
    args = "-1001234567890" + (f" {topic}" if topic else "")
    await app.target(event, command("target", args))
    saved = event.answer.call_args.args[0]
    assert f'href="{url}"' in saved
    assert "Videos &amp; Photos" in saved
    assert "已保存" in saved
    assert event.answer.call_args.kwargs["parse_mode"] == "HTML"
    await app.target(event, command("target"))
    queried = event.answer.call_args.args[0]
    assert f'href="{url}"' in queried
    assert "Videos &amp; Photos" in queried
    assert "执行账号" not in queried
    assert store.recipient(2).target == Target("-1001234567890", topic)
