"""Conservative backup pacing; Telegram's actual limits remain adaptive."""

import asyncio
import time


class BackupPacer:
    def __init__(self, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.clock = clock
        self.sleep = sleep
        self.deadlines = {}

    @staticmethod
    def keys(executor, storage, target):
        # Topics share their parent chat's limit; accounts share the Bot API.
        return {
            ("account", executor): 1,
            ("chat", storage.chat): 3,
            **({("chat", target.chat): 3} if target else {}),
            ("bot", "shared"): 0,
        }

    async def wait(self, keys, stopped):
        while not stopped():
            delay = (
                max((self.deadlines.get(key, 0) for key in keys), default=0)
                - self.clock()
            )
            if delay <= 0:
                return True
            # Remain responsive to cancellation and manual engine handoff.
            await self.sleep(min(delay, 1))
        return False

    async def reserve(self, keys, count, stopped):
        """Reserve admission spacing without locking the byte transfer."""
        if not await self.wait(keys, stopped):
            return False
        # No await between admission and charging: concurrent waiters observe
        # the reservation before another task can enter.
        self.charge(keys, count)
        return True

    def charge(self, keys, count):
        now = self.clock()
        for key, seconds in keys.items():
            self.deadlines[key] = max(
                self.deadlines.get(key, 0), now + max(1, count) * seconds
            )

    def cooldown(self, executor, seconds, *, bot=False):
        key = ("bot", "shared") if bot else ("account", executor)
        self.deadlines[key] = max(
            self.deadlines.get(key, 0), self.clock() + max(1, seconds)
        )

    def remaining(self, executor):
        return max(
            0, self.deadlines.get(("account", executor), 0) - self.clock()
        )
