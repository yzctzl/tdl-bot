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
    user_id: int | None = None
    username: str | None = None
    name: str = ""


class AccountPool:
    """Default to the shared account when no own session is available."""

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

    def select(
        self, operator_id: int, preferred: str | None = None
    ) -> Executor:
        """Use a discovered own account or the workspace's shared account."""
        if preferred:
            return self.get(preferred)
        return self._personal.get(operator_id, self.shared)

    def get(self, key: str) -> Executor:
        """Keep retries on their original identity, with no silent fallback."""
        executor = self._executors.get(key)
        if (
            executor is None
            and self.shared.user_id is not None
            and key == f"operator:{self.shared.user_id}"
        ):
            # Older bindings could name the shared identity as personal.
            return self.shared
        if executor is None:
            raise UserError(
                tr(
                    "The execution account is unavailable. Sign in with tdl "
                    "and reload, or use /account auto for new tasks."
                )
            )
        return executor

    def can_use(self, operator_id: int, key: str, *, admin: bool = False):
        executor = self._executors.get(key)
        return (
            admin
            or key == "shared"
            or key == f"operator:{operator_id}"
            or (executor is not None and executor.owner_id == operator_id)
        )

    def catalog(self):
        return [
            {
                "key": executor.key,
                "owner_id": executor.owner_id,
                "user_id": executor.user_id or executor.owner_id,
                "username": executor.username,
                "name": executor.name,
            }
            for executor in self._executors.values()
        ]


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
        if self.store is None:
            return operator_id in self._operators
        # Permissions stay live without scanning every workspace member.
        member = self.store.member(operator_id)
        return member is not None and member.active(self.store.now())

    def is_admin(self, operator_id: int) -> bool:
        if self.store is None:
            return (
                operator_id in self._admins and operator_id in self._operators
            )
        member = self.store.member(operator_id)
        return (
            member is not None
            and member.role == "admin"
            and member.active(self.store.now())
        )

    def can_manage(self, operator_id: int, owner_id: int | None) -> bool:
        return self.is_allowed(operator_id) and (
            owner_id is None
            or operator_id == owner_id
            or self.is_admin(operator_id)
        )
