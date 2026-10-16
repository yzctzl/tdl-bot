"""Workspace migration, execution routing and scope-based management."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject

from tdl_bot.bot.application import BotApplication
from tdl_bot.core.config import Settings
from tdl_bot.core.errors import AccessDenied, UserError
from tdl_bot.integrations.tdl.gate import UpdateGate
from tdl_bot.storage.models import Destination, Target
from tdl_bot.storage.store import Store
from tdl_bot.transfers.accounts import AccessPolicy, AccountPool, Executor
from tdl_bot.transfers.service import TransferService

pytestmark = pytest.mark.usefixtures("chinese")


def legacy_database(path, targets):
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE targets(user_id INTEGER PRIMARY KEY, "
        "chat TEXT, topic_id INTEGER);"
        "CREATE TABLE pending(user_id INTEGER PRIMARY KEY, url TEXT);"
    )
    db.executemany("INSERT INTO targets VALUES (?, ?, ?)", targets)
    db.executemany(
        "INSERT INTO pending VALUES (?, ?)",
        [
            (operator, f"https://t.me/channel/{operator}")
            for operator, _, _ in targets
        ],
    )
    db.commit()
    db.close()


def test_migration_shares_single_destination_and_preserves_all_retries(
    tmp_path,
):
    path = tmp_path / "state.sqlite3"
    legacy_database(path, [(1, "me", 42), (2, "me", 42)])
    store = Store(path)
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    assert store.destination(3) == Destination(Target("me", 42))
    assert len(store.pending_items()) == 2
    assert all(
        item.destination.owner_id is None for item in store.pending_items()
    )
    backup = path.with_name(path.name + ".pre-workspace")
    assert backup.stat().st_mode & 0o077 == 0
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 2
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    store.set_target(3, Target("new"))
    store.close()
    reopened = Store(path)
    assert reopened.target(1) == Target("new")
    assert len(reopened.pending_items()) == 2
    reopened.close()


def test_migration_keeps_conflicting_targets_personal(tmp_path):
    path = tmp_path / "state.sqlite3"
    legacy_database(path, [(1, "me", None), (2, "@other", 42)])
    store = Store(path)
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    assert store.destination(1) == Destination(Target("me"), 1)
    assert store.destination(2) == Destination(Target("@other", 42), 2)
    assert store.target(3) is None
    assert {item.destination.owner_id for item in store.pending_items()} == {
        1,
        2,
    }
    store.close()


def test_migration_transaction_rolls_back_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    legacy_database(path, [(1, "me", None)])

    def fail(*args):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(Store, "_insert_pending", fail)
    with pytest.raises(RuntimeError):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name='workspace_targets'"
            ).fetchone()[0]
            == 0
        )


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path / "workspace.sqlite3")
    for user_id in range(1, 6):
        store.set_language(user_id, "zh")
    store.set_target(1, Target("me"))
    group = SimpleNamespace(
        messages=[SimpleNamespace(id=10, document=None)],
        urls=["https://t.me/channel/10"],
    )

    def executor(key, owner=None):
        async def download(urls, directory):
            (directory / "10_photo.jpg").write_bytes(b"photo")

        return Executor(
            key,
            SimpleNamespace(
                gate=UpdateGate(), download=AsyncMock(side_effect=download)
            ),
            SimpleNamespace(
                resolve=AsyncMock(return_value=group),
                upload=AsyncMock(),
                join=AsyncMock(),
                validate_target=AsyncMock(),
            ),
            owner,
            user_id=owner or 4,
            username=f"user{owner or 4}",
        )

    shared = executor("shared")
    personal = executor("operator:2", 2)
    accounts = AccountPool(shared, (personal,))
    policy = AccessPolicy(frozenset({1, 2, 3}), frozenset({1}))
    service = TransferService(
        accounts,
        store,
        tmp_path / "jobs",
        AsyncMock(),
        policy=policy,
        workers=1,
        queue_size=10,
    )
    service.jobs_dir.mkdir()
    yield service, store, accounts
    store.close()


async def test_multiple_jobs_per_operator_and_shared_management(workspace):
    service, _, _ = workspace
    one = await service.submit(2, "https://t.me/channel/10")
    two = await service.submit(2, "https://t.me/channel/20")
    assert service.current_job(3, one.id) is one
    assert service.jobs(3) == [one, two]
    assert await service.cancel(3, one.id[:8])
    assert await service.cancel(1, two.id)
    await service.close()


async def test_personal_scope_visibility_and_admin_cancellation(workspace):
    service, store, _ = workspace
    store.set_target(2, Target("@private"), private=True)
    job = await service.submit(2, "https://t.me/channel/10")
    assert not service.jobs(3)
    assert service.jobs(1) == service.jobs(2) == [job]
    assert not await service.cancel(3, job.id)
    assert await service.cancel(1, job.id)
    await service.close()


async def test_executor_routing_independent_of_target_scope(workspace):
    service, store, accounts = workspace
    assert accounts.select(3) is accounts.shared
    assert accounts.select(2).owner_id == 2
    assert accounts.select(2).tdl.gate is accounts.shared.tdl.gate
    personal = await service.submit(2, "https://t.me/channel/10")
    shared = await service.submit(3, "https://t.me/channel/20")
    assert personal.owner_id is None  # shared management despite own executor
    store.set_target(3, Target("@private"), private=True)
    own_target = await service.submit(3, "https://t.me/channel/30")
    assert own_target.owner_id == 3
    assert own_target.executor_key == "shared"
    service.start()
    await service.close()
    assert accounts.select(2).media.upload.await_count == 1
    assert accounts.shared.media.upload.await_count == 2
    assert personal.target == shared.target == Target("me")


async def test_cross_operator_duplicate_uses_canonical_link(workspace):
    service, _, _ = workspace
    job = await service.submit(1, "https://t.me/s/channel/10?thread=99")
    with pytest.raises(UserError, match="已在"):
        await service.submit(3, "https://t.me/channel/10")
    assert not await service.cancel(99, job.id)
    await service.cancel(3, job.id)
    await service.close()


async def test_pending_scope_and_account_do_not_overwrite_each_other(workspace):
    service, store, _ = workspace
    store.set_pending(2, "https://t.me/channel/10", executor_key="operator:2")
    store.set_pending(3, "https://t.me/channel/20")
    store.set_target(2, Target("@private"), private=True)
    store.set_pending(2, "https://t.me/channel/30", executor_key="operator:2")
    assert len(store.pending_items()) == 3
    assert len(service.pending_items(3)) == 1
    assert len(service.pending_items(1)) == 3
    assert service.pending_record(3).executor_key == "shared"
    private = store.pending_items()[-1]
    with pytest.raises(UserError, match="无权"):
        await service.retry(3, private.id)
    await service.close()


async def test_retry_snapshots_account_target_and_clears_only_itself(workspace):
    service, store, accounts = workspace
    store.set_pending(2, "https://t.me/s/channel/10", executor_key="operator:2")
    store.set_pending(3, "https://t.me/channel/20")
    item = store.pending_items()[0]
    store.set_target(3, Target("@new"))
    with pytest.raises(UserError, match="无权"):
        await service.retry(3, item.id)
    job = await service.retry(1, item.id)
    assert job.target == Target("me")
    assert job.executor_key == "operator:2"
    assert job.user_id == 1
    service.start()
    await service.close()
    accounts.select(2).media.upload.assert_awaited_once()
    accounts.shared.media.upload.assert_not_awaited()
    assert [pending.url for pending in store.pending_items()] == [
        "https://t.me/channel/20"
    ]


async def test_missing_original_executor_never_falls_back(workspace):
    service, store, accounts = workspace
    store.set_pending(2, "https://t.me/channel/10", executor_key="missing")
    with pytest.raises(UserError, match="执行账号不可用"):
        await service.retry(1, store.pending_items()[0].id)
    accounts.shared.media.resolve.assert_not_awaited()
    await service.close()


async def test_repeated_denial_retains_same_retry_record(workspace):
    service, store, accounts = workspace
    store.set_pending(3, "https://t.me/s/channel/10")
    item = store.pending_items()[0]
    accounts.shared.media.resolve.side_effect = AccessDenied("no access")
    service.start()
    await service.retry(3, item.id)
    await service.close()
    assert [record.id for record in store.pending_items()] == [item.id]


def application(workspace):
    service, store, accounts = workspace
    settings = SimpleNamespace(
        allowed_users=frozenset({1, 2, 3}), admin_users=frozenset({1})
    )
    return BotApplication(
        SimpleNamespace(),
        settings,
        store,
        accounts.shared.tdl,
        service,
    )


def message(operator=3, message_id=70):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=operator),
        message_id=message_id,
        answer=AsyncMock(
            return_value=SimpleNamespace(message_id=80, edit_text=AsyncMock())
        ),
    )


async def test_cross_account_buttons_and_private_visibility(workspace):
    service, store, _ = workspace
    shared = await service.submit(2, "https://t.me/channel/10")
    store.set_target(2, Target("@private"), private=True)
    private = await service.submit(2, "https://t.me/channel/20")
    app = application(workspace)
    view = message(3)
    await app.tasks.jobs(view, CommandObject(command="jobs"))
    markup = view.answer.call_args.kwargs["reply_markup"]
    assert len(markup.inline_keyboard) == 1
    assert markup.inline_keyboard[0][0].callback_data == f"cancel:{shared.id}"
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=3),
        data=f"cancel:{shared.id}",
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=3), message_id=80
        ),
        answer=AsyncMock(),
    )
    await app.tasks.cancel(query)
    assert shared.done.is_set()
    assert service.current_job(2, private.id) is private
    await service.cancel(1, private.id)
    await service.close()


async def test_shared_control_cannot_be_reused_for_other_job(workspace):
    service, _, _ = workspace
    first = await service.submit(2, "https://t.me/channel/10")
    app = application(workspace)
    await app.tasks.jobs(message(3), CommandObject(command="jobs"))
    second = await service.submit(2, "https://t.me/channel/20")
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=3),
        data=f"cancel:{second.id}",
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=3), message_id=80
        ),
        answer=AsyncMock(),
    )
    await app.tasks.cancel(query)
    assert not second.cancel_requested
    await service.cancel(1, first.id)
    await service.cancel(1, second.id)
    await service.close()


async def test_target_switching_and_admin_override(workspace, monkeypatch):
    _, store, accounts = workspace

    async def validate(self, target, operator, *, storage=False):
        return target

    monkeypatch.setattr("tdl_bot.bot.destinations.Delivery.validate", validate)
    app = application(workspace)
    await app.destinations.target(
        message(2), CommandObject(command="target", args="private @mine 42")
    )
    assert store.destination(2) == Destination(Target("@mine", 42), 2)
    accounts.select(2).media.validate_target.assert_not_awaited()
    assert store.target(3) == Target("me")
    await app.destinations.target(
        message(2), CommandObject(command="target", args="bot")
    )
    assert store.recipient(2).target == Target("2")
    with pytest.raises(UserError, match="管理员"):
        await app.destinations.target(
            message(3), CommandObject(command="target", args="for 2 @other")
        )
    await app.destinations.target(
        message(1), CommandObject(command="target", args="for 2 @other")
    )
    assert store.target(2) == Target("@other")


async def test_join_uses_failed_executor_even_from_another_operator(workspace):
    service, store, accounts = workspace
    store.set_pending(2, "https://t.me/channel/10", executor_key="operator:2")
    item = store.pending_items()[0]
    app = application(workspace)
    with pytest.raises(UserError, match="无权"):
        await app.tasks.join(
            message(3),
            CommandObject(
                command="join", args=f"https://t.me/+invite {item.id}"
            ),
        )
    accounts.select(2).media.join.assert_not_awaited()
    await app.tasks.join(
        message(1),
        CommandObject(command="join", args=f"https://t.me/+invite {item.id}"),
    )
    accounts.select(2).media.join.assert_awaited_once()
    accounts.shared.media.join.assert_not_awaited()
    service.start()
    await service.close()


def test_first_start_requires_explicit_admins_and_ignores_legacy_allowlist(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DATA_DIR=data\nBIN_DIR=bin\nTDL_DIR=.tdl\n")
    monkeypatch.setenv("BOT_TOKEN", "configured")
    monkeypatch.setenv("ALLOWED_USER_IDS", "3, 1, 2")
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    monkeypatch.setenv("SESSION_SOURCE", "tdl")
    with pytest.raises(ValueError, match="ADMIN_USER_IDS"):
        Settings.from_env()
    assert Settings.from_env(login_only=True).allowed_users == frozenset()
    monkeypatch.setenv("ADMIN_USER_IDS", "2, 4")
    settings = Settings.from_env()
    assert settings.admin_users == settings.allowed_users == frozenset({2, 4})


def test_legacy_binding_file_is_no_longer_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DATA_DIR=data\nBIN_DIR=bin\nTDL_DIR=.tdl\n")
    monkeypatch.setenv("BOT_TOKEN", "configured")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("ADMIN_USER_IDS", "1")
    monkeypatch.setenv("SESSION_SOURCE", "tdl")
    monkeypatch.setenv("ACCOUNTS_FILE", str(tmp_path / "absent.toml"))
    settings = Settings.from_env()
    assert not hasattr(settings, "account_bindings")


async def test_admin_selection_persists_and_only_affects_new_tasks(
    workspace, monkeypatch
):
    service, store, accounts = workspace
    admin = await service.submit(1, "https://t.me/channel/10")
    assert admin.executor_key == "shared"
    selected = await service.execution_accounts(1, "2")
    assert selected["selected"] == "operator:2" and not selected["automatic"]
    assert len(selected["accounts"]) == 2
    own = await service.submit(1, "https://t.me/channel/20")
    assert own.executor_key == "operator:2"
    assert admin.executor_key == "shared"
    viewer = await service.execution_accounts(3)
    assert [item["key"] for item in viewer["accounts"]] == ["shared"]
    with pytest.raises(UserError, match="管理员"):
        await service.execution_accounts(3, "2")
    with pytest.raises(UserError):
        await service.execution_accounts(99)
    with pytest.raises(UserError):
        await service.execution_accounts(1, "999")
    assert store.executor_choice(1) == "operator:2"
    reopened = Store(
        service.jobs_dir.parent / "workspace.sqlite3", migrate=False
    )
    try:
        assert reopened.executor_choice(1) == "operator:2"
    finally:
        reopened.close()
    from tdl_bot.runtime.engine import EngineServer, RemoteService

    store.save_execution_catalog(accounts.catalog())
    server = EngineServer(service, SimpleNamespace())
    remote = RemoteService(
        SimpleNamespace(
            allowed_users=frozenset({1, 2, 3}), admin_users=frozenset({1})
        ),
        store,
    )

    async def rpc(method, **args):
        return await server.dispatch(method, args)

    monkeypatch.setattr(remote, "call", rpc)
    assert remote.accounts.select(1).key == "operator:2"
    assert remote.accounts.select(2).key == "operator:2"
    assert remote.accounts.select(3).key == "shared"
    app = application(workspace)
    with pytest.raises(UserError, match="管理员"):
        await app.execution.account(
            message(3), CommandObject(command="account")
        )
    assert "account" not in {item.command for item in app.command_menu()}
    assert "account" in {item.command for item in app.command_menu(admin=True)}
    reply = message(1)
    await app.execution.account(reply, CommandObject(command="account"))
    assert "@user2 (UID: 2)" in reply.answer.call_args.args[0]
    # A new frontend reads the same database selection without opening sessions.
    assert (await remote.execution_accounts(1))["selected"] == "operator:2"
    await remote.execution_accounts(1, "4")
    assert remote.accounts.select(1).key == "shared"
    await remote.execution_accounts(1, "shared")
    assert remote.accounts.select(1).key == "shared"
    await remote.execution_accounts(1, "auto")
    assert store.executor_choice(1) is None
    # Former admins cannot keep an override into another person's account.
    store.seed_members(frozenset({1, 2, 3}), frozenset({1}))
    await remote.execution_accounts(1, "2")
    store.set_member_role(1, 2, "admin")
    store.set_member_role(2, 1, "user")
    service.policy = remote.policy
    assert remote.accounts.select(1).key == "shared"
    assert service.execution_account(1).key == "shared"
    service.start()
    await service.close()
    accounts.select(2).media.upload.assert_not_awaited()


async def test_reload_refreshes_accounts_without_code_change(workspace):
    from tdl_bot.runtime.engine import EngineServer

    service, _, _ = workspace
    server = EngineServer(service, SimpleNamespace())
    await server.dispatch("ping", {"version": server.version})
    assert not server.reload_requested
    await server.dispatch("ping", {"reload": True})
    assert server.reload_requested
