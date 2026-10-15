"""Workspace migration, execution routing and scope-based management."""

import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject

from tdl_bot.accounts import AccessPolicy, AccountPool, Executor
from tdl_bot.bot import BotApplication
from tdl_bot.config import Settings, read_account_bindings
from tdl_bot.errors import AccessDenied, UserError
from tdl_bot.service import TransferService
from tdl_bot.store import Destination, Store, Target
from tdl_bot.tdl import UpdateGate


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
    assert len(service.pending_items(3)) == 2
    assert len(service.pending_items(1)) == 3
    with pytest.raises(UserError, match="/pending"):
        service.pending_record(3)
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
    job = await service.retry(3, item.id)
    assert job.target == Target("me")
    assert job.executor_key == "operator:2"
    assert job.user_id == 3
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
    with pytest.raises(UserError, match="原执行账号"):
        await service.retry(3, store.pending_items()[0].id)
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
    await app.jobs(view, CommandObject(command="jobs"))
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
    await app.cancel(query)
    assert shared.done.is_set()
    assert service.current_job(2, private.id) is private
    await service.cancel(1, private.id)
    await service.close()


async def test_shared_control_cannot_be_reused_for_other_job(workspace):
    service, _, _ = workspace
    first = await service.submit(2, "https://t.me/channel/10")
    app = application(workspace)
    await app.jobs(message(3), CommandObject(command="jobs"))
    second = await service.submit(2, "https://t.me/channel/20")
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=3),
        data=f"cancel:{second.id}",
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=3), message_id=80
        ),
        answer=AsyncMock(),
    )
    await app.cancel(query)
    assert not second.cancel_requested
    await service.cancel(1, first.id)
    await service.cancel(1, second.id)
    await service.close()


async def test_target_switching_and_admin_override(workspace, monkeypatch):
    _, store, accounts = workspace

    async def validate(self, target, operator, *, storage=False):
        return target

    monkeypatch.setattr("tdl_bot.bot.Delivery.validate", validate)
    app = application(workspace)
    await app.target(
        message(2), CommandObject(command="target", args="private @mine 42")
    )
    assert store.destination(2) == Destination(Target("@mine", 42), 2)
    accounts.select(2).media.validate_target.assert_not_awaited()
    assert store.target(3) == Target("me")
    await app.target(message(2), CommandObject(command="target", args="bot"))
    assert store.recipient(2).target == Target("2")
    with pytest.raises(UserError, match="管理员"):
        await app.target(
            message(3), CommandObject(command="target", args="for 2 @other")
        )
    await app.target(
        message(1), CommandObject(command="target", args="for 2 @other")
    )
    assert store.target(2) == Target("@other")


async def test_join_uses_failed_executor_even_from_another_operator(workspace):
    service, store, accounts = workspace
    store.set_pending(2, "https://t.me/channel/10", executor_key="operator:2")
    item = store.pending_items()[0]
    app = application(workspace)
    await app.join(
        message(3),
        CommandObject(command="join", args=f"https://t.me/+invite {item.id}"),
    )
    accounts.select(2).media.join.assert_awaited_once()
    accounts.shared.media.join.assert_not_awaited()
    service.start()
    await service.close()


def test_first_configured_operator_is_default_admin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", "configured")
    monkeypatch.setenv("ALLOWED_USER_IDS", "3, 1, 2")
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    monkeypatch.setenv("SESSION_SOURCE", "tdl")
    monkeypatch.delenv("ACCOUNTS_FILE", raising=False)
    settings = Settings.from_env()
    assert settings.admin_users == frozenset({3})
    assert settings.allowed_users == frozenset({1, 2, 3})


@pytest.mark.parametrize(
    "invalid", ["duplicate", "unauthorized", "namespace", "unknown"]
)
def test_account_bindings_validation(tmp_path, invalid):
    settings = Settings(
        "", 0, "", frozenset({1, 2}), frozenset({1}), tmp_path, tmp_path / "tdl"
    )
    file = tmp_path / "accounts.toml"
    entry = '[[accounts]]\noperator_id = 2\nnamespace = "own"\n'
    content = {
        "duplicate": entry + entry,
        "unauthorized": entry.replace("= 2", "= 0"),
        "namespace": entry.replace('"own"', '"../own"'),
        "unknown": entry + "typo = true\n",
    }[invalid]
    file.write_text(content)
    with pytest.raises(ValueError, match="账号配置无效"):
        read_account_bindings(file, settings)


def test_account_binding_paths_and_proxy_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        "", 0, "", frozenset({1, 2}), frozenset({1}), tmp_path, tmp_path / "tdl"
    )
    settings = replace(settings, tdl_proxy="socks5://localhost:1080")
    file = tmp_path / "accounts.toml"
    file.write_text(
        '[[accounts]]\noperator_id = 2\nnamespace = "own"\n'
        'tdl_dir = "sessions"\n'
    )
    (binding,) = read_account_bindings(file, settings)
    assert binding.tdl_dir == tmp_path / "sessions"
    assert binding.proxy == settings.tdl_proxy
    assert "socks5" not in repr(binding)
