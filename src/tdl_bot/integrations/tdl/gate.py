"""Coordinate transfers with exclusive binary updates."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager


class UpdateGate:
    """Pause new jobs while an update waits for active jobs to finish."""

    def __init__(self) -> None:
        """Initialize a writer-priority, cancellation-safe gate."""
        self._condition = asyncio.Condition()
        self._active = 0
        self._updating = False
        self._waiting = 0

    @asynccontextmanager
    async def job(self) -> AsyncGenerator[None]:
        """Hold a lease for the entire download/upload/cleanup operation."""
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._updating and not self._waiting
            )
            self._active += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def update(self) -> AsyncGenerator[None]:
        """Acquire exclusive access after all active jobs complete."""
        async with self._condition:
            self._waiting += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._active and not self._updating
                )
                self._updating = True
            finally:
                self._waiting -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._updating = False
                self._condition.notify_all()
