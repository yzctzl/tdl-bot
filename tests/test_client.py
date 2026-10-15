"""Protocol coordination for a shared tdl authorization key."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl import functions
from telethon.tl.alltlobjects import LAYER

from tdl_bot.client import AccountLock, LayerRequest, SharedSessionClient

pytestmark = pytest.mark.usefixtures("chinese")


async def test_account_lock_serializes_tasks_and_allows_nested_rpc():
    lock = AccountLock()
    started = asyncio.Event()
    release = asyncio.Event()
    events = []

    async def tdl():
        async with lock:
            started.set()
            await release.wait()
            events.append("tdl completed")

    async def telethon():
        async with lock:
            async with lock:
                events.append("rpc completed")

    process = asyncio.create_task(tdl())
    await started.wait()
    rpc = asyncio.create_task(telethon())
    await asyncio.sleep(0)
    assert events == []
    release.set()
    await asyncio.gather(process, rpc)
    assert events == ["tdl completed", "rpc completed"]


async def test_cancelled_waiter_does_not_break_account_lock():
    lock = AccountLock()

    async def waiting():
        async with lock:
            pytest.fail("waiter should not acquire lock")

    async with lock:
        task = asyncio.create_task(waiting())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with lock:
        pass


async def test_each_rpc_resolves_and_pins_layer(monkeypatch):
    parent = AsyncMock(return_value="result")
    monkeypatch.setattr(TelegramClient, "_call", parent)
    client = SharedSessionClient(
        MemorySession(), 1, "test", account_lock=AccountLock()
    )
    query = functions.help.GetConfigRequest()
    resolve = AsyncMock()
    query.resolve = resolve
    assert await client._call("sender", query) == "result"
    resolve.assert_awaited_once()
    wrapped = parent.call_args.args[1]
    assert isinstance(wrapped, LayerRequest)
    assert wrapped.layer == LAYER
    assert isinstance(wrapped.query, functions.InitConnectionRequest)
    assert wrapped.query.query is query
    assert client._no_updates


def test_layer_wrapper_preserves_custom_result_decoder(monkeypatch):
    query = functions.help.GetConfigRequest()
    monkeypatch.setattr(
        query, "read_result", lambda reader: "decoded with original method"
    )
    assert LayerRequest(LAYER, query).read_result(None) == (
        "decoded with original method"
    )
