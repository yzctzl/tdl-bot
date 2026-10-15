"""View automatic execution routing and administrator account overrides."""

from html import escape

from aiogram.filters import CommandObject
from aiogram.types import Message

from tdl_bot.bot.context import CommandGroup
from tdl_bot.core.i18n import localized, tr


class ExecutionCommands(CommandGroup):
    @localized
    async def account(self, message: Message, command: CommandObject):
        assert message.from_user is not None
        operator = message.from_user.id
        result = await self.service.execution_accounts(
            operator, (command.args or "").strip() or None
        )
        mode = tr("Automatic") if result["automatic"] else tr("Manual")
        lines = [tr("Execution account selection: {mode}", mode=mode)]
        for account in result["accounts"]:
            label = (
                tr("Shared account")
                if account["key"] == "shared"
                else tr("Personal account")
            )
            # Ordinary operators see their own identity, never other sessions.
            if account["key"] != "shared" or self.policy.is_admin(operator):
                identity = (
                    f"@{account['username']}"
                    if account["username"]
                    else account["name"] or tr("No username")
                )
                label += f" · {identity} (UID: {account['user_id']})"
            if account["key"] == result["selected"]:
                label = "✓ " + label
            lines.append(label)
        if self.policy.is_admin(operator):
            lines.append(
                tr(
                    "Use /account auto, /account shared, or /account <UID>. "
                    "Changes apply to new tasks; retries keep their "
                    "original account."
                )
            )
        await message.answer(escape("\n".join(lines)), parse_mode="HTML")
