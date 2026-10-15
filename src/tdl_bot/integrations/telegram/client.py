"""Coordinate protocol layers when Telethon and tdl share authorization."""

import asyncio
import copy

from telethon import utils
from telethon.tl import functions
from telethon.tl.alltlobjects import LAYER

from tdl_bot.integrations.telegram.uploads import UploadClient


class AccountLock:
    """A task-reentrant lock shared by tdl processes and Telethon RPCs."""

    def __init__(self) -> None:
        """Initialize an unlocked account coordinator."""
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def __aenter__(self):
        """Allow request resolution to issue nested RPCs in the same task."""
        task = asyncio.current_task()
        if self._owner is not task:
            await self._lock.acquire()
            self._owner = task
        self._depth += 1
        return self

    async def __aexit__(self, *args) -> None:
        """Release the account after the outermost operation finishes."""
        self._depth -= 1
        if not self._depth:
            self._owner = None
            self._lock.release()


class LayerRequest(functions.InvokeWithLayerRequest):
    """Preserve the inner request's custom result decoder."""

    def __init__(self, layer, query, *, original=None):
        """Retain the original RPC behind an InitConnection envelope."""
        super().__init__(layer, query)
        self.original = original or query

    def read_result(self, reader):
        """Delegate vector and primitive result decoding to the original RPC."""
        return self.original.read_result(reader)


class SharedSessionClient(UploadClient):
    """Pin each RPC to Telethon's schema without another account login.

    tdl negotiates a different API layer on the same auth key. A layer
    selected only at connect() can therefore become stale after download.
    InvokeWithLayer + InitConnection restore the schema for each RPC;
    the shared account lock prevents tdl from changing it mid-request.
    """

    def __init__(self, *args, account_lock: AccountLock, **kwargs) -> None:
        """Disable unsolicited account updates and retain the coordinator."""
        self.account_lock = account_lock
        kwargs["receive_updates"] = False
        super().__init__(*args, **kwargs)

    async def _call(
        self, sender, request, ordered=False, flood_sleep_threshold=None
    ):
        """Resolve inner requests before wrapping and serialize account RPCs."""
        async with self.account_lock:
            many = utils.is_list_like(request)
            requests = list(request) if many else [request]
            wrapped = []
            for item in requests:
                # InvokeWithLayer.resolve is a no-op, so the real request must
                # resolve usernames/InputPeers first. Nested RPCs are allowed.
                await item.resolve(self, utils)
                init = copy.copy(self._init_request)
                init.query = item
                wrapped.append(LayerRequest(LAYER, init, original=item))
            return await super()._call(
                sender,
                wrapped if many else wrapped[0],
                ordered=ordered,
                flood_sleep_threshold=flood_sleep_threshold,
            )
