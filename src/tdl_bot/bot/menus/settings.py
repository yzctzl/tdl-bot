"""Administrator storage, execution account and binary update controls."""

from html import escape

from tdl_bot.bot.menus.base import MenuBase, button
from tdl_bot.bot.menus.copy import tr
from tdl_bot.integrations.tdl.updates import version_number


class SettingsMenus(MenuBase):
    @staticmethod
    def execution_label(data):
        selected = next(
            item for item in data["accounts"] if item["key"] == data["selected"]
        )
        identity = (
            "@" + selected["username"]
            if selected["username"]
            else selected["name"] or str(selected["user_id"] or "")
        )
        label = (
            tr("Shared account")
            if selected["key"] == "shared"
            else tr("Personal account")
        )
        if identity:
            label += " · " + escape(identity)
        return label

    def execution_summary(self, data):
        return tr(
            "Execution account: {account}\nSelection: {mode}\n"
            "Signed-in accounts: {count}",
            account=self.execution_label(data),
            mode=tr("Automatic") if data["automatic"] else tr("Manual"),
            count=len(data["accounts"]),
        )

    async def tdl_version(self):
        async with self.tdl.gate.job():
            version = version_number(await self.tdl.version())
        return tr("tdl: {version}", version="v" + ".".join(map(str, version)))

    async def bot_summary(self, actor):
        target = self.store.internal_target()
        location = (
            await self.app.destinations.target_link(target, actor)
            if target
            else tr("Not configured")
        )
        accounts = await self.service.execution_accounts(actor)
        text = tr("Bot management") + "\n\n" + await self.tdl_version()
        text += "\n" + tr("Internal storage: {p0}", p0=location)
        text += "\n" + tr(
            "Execution account: {account} · {mode}",
            account=self.execution_label(accounts),
            mode=tr("Automatic") if accounts["automatic"] else tr("Manual"),
        )
        self.require_admin(actor)
        return text

    async def admin_page(self, message, state, path):
        actor = message.from_user.id
        section = path.split(":")[0]
        if section in {"storage", "storageinput"}:
            await self.prompt(
                message,
                state,
                "storage",
                tr(
                    "Send the internal storage @username or ID, optionally "
                    "followed by a topic ID."
                ),
                "admin",
            )
        elif section in {"execution", "selectexec"}:
            data = await self.service.execution_accounts(
                actor,
                path.split(":", 1)[1] if section == "selectexec" else None,
            )
            self.require_admin(actor)
            rows = [
                [
                    button(
                        ("✓ " if data["automatic"] else "") + tr("Automatic"),
                        "selectexec:auto",
                    )
                ]
            ]
            for item in data["accounts"]:
                label = (
                    tr("Shared account")
                    if item["key"] == "shared"
                    else (
                        "@" + item["username"]
                        if item["username"]
                        else str(item["user_id"])
                    )
                )
                rows.append(
                    [
                        button(
                            ("✓ " if data["selected"] == item["key"] else "")
                            + label,
                            "selectexec:"
                            + (
                                "shared"
                                if item["key"] == "shared"
                                else str(item["user_id"])
                            ),
                            wide=item["key"] != "shared",
                        )
                    ]
                )
            rows.append([button(tr("Back"), "admin")])
            await self.show(
                message,
                state,
                self.execution_summary(data)
                + "\n\n"
                + tr(
                    "Changes apply to new tasks; retries keep their original "
                    "execution account."
                ),
                rows,
            )
        elif section == "update":
            await self.show(
                message,
                state,
                tr("Update tdl") + "\n\n" + await self.tdl_version(),
                [
                    [button(tr("Update to latest"), "updatelatest")],
                    [button(tr("Choose version"), "version")],
                    [button(tr("Back"), "admin")],
                ],
            )
        elif section == "version":
            await self.prompt(
                message,
                state,
                "version",
                tr("Send a tdl version, such as v0.20.4."),
                "update",
            )
        elif section == "updatelatest":
            await self.command(message, state, "update_tdl", "latest", "admin")
        elif section == "updateapply":
            await self.command(
                message, state, "update_tdl", path.split(":", 1)[1], "admin"
            )
