"""Select execution accounts independently of task management scope."""

from dataclasses import dataclass

from tdl_bot.errors import UserError
from tdl_bot.tdl import Tdl
from tdl_bot.telegram import TelegramMedia


@dataclass(frozen=True)
class Executor:
    """One coordinated tdl/Telethon account and its optional operator."""

    key: str
    tdl: Tdl
    media: TelegramMedia
    owner_id: int | None = None


class AccountPool:
    """Default to the shared account only when no own account is bound."""

    def __init__(
        self, shared: Executor, personal: tuple[Executor, ...] = ()
    ) -> None:
        self.shared = shared
        self._executors = {shared.key: shared}
        self._personal = {}
        for executor in personal:
            if (
                executor.key in self._executors
                or executor.owner_id is None
                or executor.owner_id in self._personal
            ):
                raise ValueError("执行账号配置重复。")
            executor.tdl.gate = shared.tdl.gate
            self._executors[executor.key] = executor
            self._personal[executor.owner_id] = executor

    def select(self, operator_id: int) -> Executor:
        """Use a configured own account or the workspace's shared account."""
        return self._personal.get(operator_id, self.shared)

    def get(self, key: str) -> Executor:
        """Keep retries on their original identity, with no silent fallback."""
        executor = self._executors.get(key)
        if executor is None:
            raise UserError("任务原执行账号未配置，请恢复账号绑定后再重试。")
        return executor


@dataclass(frozen=True)
class AccessPolicy:
    """One workspace's trusted operators and its administrators."""

    operators: frozenset[int]
    admins: frozenset[int]

    def can_manage(self, operator_id: int, owner_id: int | None) -> bool:
        """Allow shared management, personal ownership and admin overrides."""
        return operator_id in self.operators and (
            owner_id is None
            or operator_id == owner_id
            or operator_id in self.admins
        )
