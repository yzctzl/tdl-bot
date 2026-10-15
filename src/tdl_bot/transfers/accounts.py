"""Select execution accounts independently of task management scope."""

from dataclasses import dataclass

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr
from tdl_bot.integrations.tdl.runner import Tdl
from tdl_bot.integrations.telegram.media import TelegramMedia
from tdl_bot.storage.store import Store


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
                raise ValueError(
                    tr("Duplicate execution account configuration.")
                )
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
            raise UserError(
                tr(
                    "The task's original execution account is not "
                    "configured. Restore its binding before retrying."
                )
            )
        return executor


class AccessPolicy:
    """Evaluate current membership and bans at every protected operation."""

    def __init__(
        self,
        operators: frozenset[int],
        admins: frozenset[int],
        *,
        store: Store | None = None,
    ) -> None:
        self._operators = operators
        self._admins = admins
        self.store = store
        if store is not None:
            store.seed_members(operators, admins)

    @property
    def operators(self) -> frozenset[int]:
        if self.store is None:
            return self._operators
        return frozenset(
            member.user_id
            for member in self.store.members()
            if member.active(self.store.now())
        )

    @property
    def admins(self) -> frozenset[int]:
        if self.store is None:
            return self._admins
        return frozenset(
            member.user_id
            for member in self.store.members()
            if member.role == "admin" and member.active(self.store.now())
        )

    def is_known(self, operator_id: int) -> bool:
        return (
            self.store.member(operator_id) is not None
            if self.store
            else operator_id in self._operators
        )

    @property
    def menu_ids(self) -> frozenset[int]:
        return self.store.menu_member_ids() if self.store else self._operators

    def is_allowed(self, operator_id: int) -> bool:
        return operator_id in self.operators

    def is_admin(self, operator_id: int) -> bool:
        return operator_id in self.admins and self.is_allowed(operator_id)

    def can_manage(self, operator_id: int, owner_id: int | None) -> bool:
        return self.is_allowed(operator_id) and (
            owner_id is None
            or operator_id == owner_id
            or self.is_admin(operator_id)
        )
