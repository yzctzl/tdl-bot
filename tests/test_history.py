"""Output history, legacy imports and cross-account deduplication."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tdl_bot.integrations.tdl.gate import UpdateGate
from tdl_bot.integrations.telegram.delivery import MissingResult
from tdl_bot.storage.models import Target
from tdl_bot.storage.store import Store
from tdl_bot.transfers.accounts import AccessPolicy, AccountPool, Executor
from tdl_bot.transfers.jobs import Job
from tdl_bot.transfers.service import TransferService


def test_history_preserves_all_results_and_aliases_across_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    storage = Target("-1001234567890")
    store.set_target(1, storage)
    url = "https://t.me/channel/10"
    first = store.save_result([url], "shared", None, storage.chat, [100])
    second = store.save_result([url], "shared", None, storage.chat, [200])
    assert first != second
    store.close()
    store = Store(path)
    try:
        assert store.saved_results([url], None, internal_only=True) == [
            (None, storage.chat, [200]),
            (None, storage.chat, [100]),
        ]
        store.mark_unavailable(storage.chat, [200])
        assert store.saved_results([url], None, internal_only=True) == [
            (None, storage.chat, [100])
        ]
        with sqlite3.connect(path) as db:
            assert (
                db.execute("SELECT COUNT(*) FROM stored_results").fetchone()[0]
                == 2
            )
    finally:
        store.close()


def test_v5_cache_migration_is_private_and_frontend_never_migrates_live_engine(
    tmp_path,
):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    storage = Target("-1001234567890")
    store.set_target(1, storage)
    store.close()
    urls = ["https://t.me/channel/10", "https://t.me/channel/11"]
    with sqlite3.connect(path) as db:
        db.executescript(
            "DROP TABLE stored_results; DROP TABLE result_sources; "
            "DROP TABLE delivery_history; PRAGMA user_version=5;"
        )
        db.executemany(
            "INSERT INTO transfer_results VALUES (?, ?, ?, ?, ?)",
            [
                (url, "shared", 0, storage.chat, json.dumps([100, 101]))
                for url in urls
            ],
        )
    frontend = Store(path, migrate=False)
    assert frontend.internal_target() == storage
    frontend.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
    engine = Store(path)
    try:
        assert engine.saved_results(urls, None, internal_only=True) == [
            (None, storage.chat, [100, 101])
        ]
        with sqlite3.connect(path) as db:
            assert (
                db.execute("SELECT COUNT(*) FROM stored_results").fetchone()[0]
                == 1
            )
            assert (
                db.execute("SELECT COUNT(*) FROM result_sources").fetchone()[0]
                == 2
            )
        backup = path.with_name(path.name + ".pre-history")
        assert backup.stat().st_mode & 0o777 == 0o600
    finally:
        engine.close()


async def test_concurrent_accounts_prepare_one_album_and_record_each_delivery(
    tmp_path,
):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    policy = AccessPolicy(frozenset({1, 2}), frozenset({1}), store=store)
    storage = Target("-1001234567890")
    store.set_target(1, storage)
    urls = ["https://t.me/channel/10", "https://t.me/channel/11"]
    group = SimpleNamespace(
        messages=[SimpleNamespace(id=10, document=None)], urls=urls
    )
    media = SimpleNamespace(
        resolve=AsyncMock(return_value=group),
        copy_group=AsyncMock(return_value=[100]),
        reveal_messages=AsyncMock(return_value=True),
    )
    shared_tdl = SimpleNamespace(gate=UpdateGate())
    own_tdl = SimpleNamespace(gate=UpdateGate())
    accounts = AccountPool(
        Executor("shared", shared_tdl, media),
        (Executor("operator:2", own_tdl, media, 2),),
    )
    delivery = AsyncMock(side_effect=[[200], [300]])
    service = TransferService(
        accounts,
        store,
        tmp_path / "jobs",
        AsyncMock(),
        policy=policy,
        delivery=delivery,
    )
    service.jobs_dir.mkdir()
    try:
        await asyncio.gather(
            service._transfer(
                Job(1, urls[0], Target("1"), owner_id=1, storage_target=storage)
            ),
            service._transfer(
                Job(
                    2,
                    urls[1],
                    Target("2"),
                    owner_id=2,
                    executor_key="operator:2",
                    storage_target=storage,
                )
            ),
        )
        assert media.copy_group.await_count == 1
        assert media.reveal_messages.await_count == 1
        assert delivery.await_count == 2
        assert not service._work_locks
        with sqlite3.connect(path) as db:
            assert (
                db.execute("SELECT COUNT(*) FROM stored_results").fetchone()[0]
                == 1
            )
            rows = db.execute(
                "SELECT ids, status FROM delivery_history ORDER BY id"
            ).fetchall()
            assert rows == [("[200]", "sent"), ("[300]", "sent")]
    finally:
        store.close()


async def test_missing_latest_storage_result_falls_back_to_older_record(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite3")
    policy = AccessPolicy(frozenset({1, 2}), frozenset({1}), store=store)
    old_storage = Target("-1001234567890")
    new_storage = Target("-1009876543210")
    url = "https://t.me/channel/10"
    store.set_target(1, old_storage)
    store.save_result(
        [url], "operator:3", None, old_storage.chat, [100], internal=True
    )
    store.save_result(
        [url], "operator:3", None, old_storage.chat, [200], internal=True
    )
    store.set_target(1, new_storage)
    media = SimpleNamespace(
        resolve=AsyncMock(), reveal_messages=AsyncMock(return_value=True)
    )
    private_media = SimpleNamespace(reveal_messages=AsyncMock())
    delivery = AsyncMock(side_effect=[MissingResult("deleted"), [300], [400]])
    service = TransferService(
        AccountPool(
            Executor("shared", SimpleNamespace(gate=UpdateGate()), media),
            (
                Executor(
                    "operator:3",
                    SimpleNamespace(gate=UpdateGate()),
                    private_media,
                    3,
                ),
            ),
        ),
        store,
        tmp_path / "jobs",
        AsyncMock(),
        policy=policy,
        delivery=delivery,
    )
    service.jobs_dir.mkdir()
    try:
        for _ in range(2):
            await service._transfer(
                Job(2, url, Target("2"), owner_id=2, storage_target=new_storage)
            )
        media.resolve.assert_not_awaited()
        private_media.reveal_messages.assert_not_awaited()
        assert delivery.await_count == 3
        assert all(
            call.args[0].storage_target == old_storage
            for call in delivery.call_args_list
        )
        assert store.saved_results([url], None, internal_only=True) == [
            (None, old_storage.chat, [100])
        ]
    finally:
        store.close()


def test_early_v6_missing_available_column_migrates_without_losing_history(
    tmp_path,
):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    storage = Target("-1001234567890")
    store.set_target(1, storage)
    url = "https://t.me/channel/10"
    result_id = store.save_result([url], "shared", None, storage.chat, [100])
    job = Job(2, url, Target("2"), owner_id=2, storage_target=storage)
    store.record_delivery(result_id, job, [200], "sent")
    store.close()
    with sqlite3.connect(path) as db:
        db.executescript(
            "ALTER TABLE stored_results DROP COLUMN available; "
            "DROP INDEX stored_result_lookup; PRAGMA user_version=6;"
        )
        try:
            db.execute("SELECT available FROM stored_results").fetchall()
        except sqlite3.OperationalError as exc:
            assert "no such column" in str(exc)
        else:
            raise AssertionError("fixture must reproduce the live schema error")
    migrated = Store(path)
    try:
        assert migrated.saved_results([url], None, internal_only=True) == [
            (None, storage.chat, [100])
        ]
        backup = path.with_name(path.name + ".pre-availability")
        assert backup.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(backup) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 6
            assert "available" not in {
                row[1]
                for row in db.execute("PRAGMA table_info(stored_results)")
            }
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 7
            assert db.execute(
                "SELECT ids,status FROM delivery_history"
            ).fetchall() == [("[200]", "sent")]
        before = backup.read_bytes()
    finally:
        migrated.close()
    reopened = Store(path)
    reopened.close()
    assert backup.read_bytes() == before


def test_complete_v6_upgrade_preserves_unavailable_records(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    storage = Target("-1001234567890")
    store.set_target(1, storage)
    url = "https://t.me/channel/10"
    store.save_result([url], "shared", None, storage.chat, [100])
    store.mark_unavailable(storage.chat, [100])
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=6")
    migrated = Store(path)
    try:
        assert migrated.saved_results([url], None) == []
        with sqlite3.connect(path) as db:
            assert db.execute(
                "SELECT available FROM stored_results"
            ).fetchone() == (0,)
    finally:
        migrated.close()
