"""Account identity and administrator member management pages."""

from html import escape

from tdl_bot.bot.menus.base import MenuBase, button
from tdl_bot.bot.menus.copy import tr
from tdl_bot.core.errors import UserError


class MemberMenus(MenuBase):
    async def member_page(self, message, state, path):
        actor = message.from_user.id
        parts = path.split(":")
        if parts[0] == "accounts":
            if not self.policy.is_admin(actor):
                await self.app.menus.page(message, state, "root")
                return
            parts = ["users", "1"]
        if not self.policy.is_admin(actor):
            raise UserError(tr("Only administrators can manage accounts."))
        if parts[0] == "users":
            page = int(parts[1])
            state["users_page"] = page
            members = self.store.members()
            rows = []
            for member in members[(page - 1) * 8 : page * 8]:
                status = (
                    tr("Administrator")
                    if member.role == "admin"
                    else tr("Regular account")
                )
                if not member.active(self.store.now()):
                    status = tr("Banned")
                rows.append(
                    [
                        button(
                            self.store.user_label(member.user_id)
                            + " · "
                            + status,
                            f"member:{member.user_id}",
                            wide=True,
                        )
                    ]
                )
            rows += self.pages(page, len(members), 8, "users")
            rows += [
                [
                    button(tr("Add account"), "useradd"),
                    button(tr("History"), "history:0:activity:1"),
                ],
                [button(tr("Back"), "root")],
            ]
            text = (
                tr("User management")
                + "\n\n"
                + tr(
                    "Members: {total} · Banned: {banned}",
                    total=len(members),
                    banned=sum(
                        not member.active(self.store.now())
                        for member in members
                    ),
                )
            )
            await self.show(message, state, text, rows)
        elif parts[0] == "member":
            owner = int(parts[1])
            member = next(
                (m for m in self.store.members() if m.user_id == owner), None
            )
            if member is None:
                raise UserError(tr("The target account is not a member."))
            target = await self.app.destinations.target_link(
                self.store.recipient(owner).target, owner
            )
            self.require_admin(actor)
            text = self.app.members.user_link(owner) + "\n\n"
            text += (
                tr("Administrator")
                if member.role == "admin"
                else tr("Regular account")
            )
            text += " · " + (
                tr("Active")
                if member.active(self.store.now())
                else tr(
                    "Banned until {p0}",
                    p0=self.app.members._deadline(member.banned_until),
                )
            )
            text += "\n" + tr("Destination: {p0}", p0=target)
            count = sum(
                r.kind == "follow"
                and r.owner == owner
                and r.state in {"active", "paused"}
                for r in self.store.backups.all()
            )
            text += "\n" + tr(
                "Follow slots: {used}/{limit} (active and paused).",
                used=count,
                limit=tr("Unlimited")
                if member.role == "admin"
                else self.store.follow_limit(owner),
            )
            await self.show(
                message,
                state,
                text,
                [
                    [button(tr("Destination"), f"target:{owner}")],
                    [button(tr("Follow quota"), f"quota:{owner}")],
                    [
                        button(tr("Role"), f"role:{owner}"),
                        button(
                            tr("Ban")
                            if member.active(self.store.now())
                            else tr("Unban"),
                            f"ban:{owner}"
                            if member.active(self.store.now())
                            else f"userop:unban:{owner}",
                        ),
                    ],
                    [button(tr("History"), f"history:{owner}:activity:1")],
                    [button(tr("Remove account"), f"userop:remove:{owner}")],
                    [
                        button(
                            tr("Back"), f"users:{state.get('users_page', 1)}"
                        ),
                    ],
                ],
            )
        elif parts[0] == "useradd":
            await self.prompt(
                message,
                state,
                "user_add",
                tr("Send the account UID or @username to add."),
                "users:1",
            )
        elif parts[0] == "quota":
            state["member"] = int(parts[1])
            await self.prompt(
                message,
                state,
                "user_quota",
                tr(
                    "Send the follow quota (0–1000). Existing tasks will be "
                    "retained."
                ),
                "member:" + parts[1],
            )
        elif parts[0] in {"role", "ban"}:
            owner = parts[1]
            choices = (
                (
                    (tr("Regular account"), "user"),
                    (tr("Administrator"), "admin"),
                )
                if parts[0] == "role"
                else (
                    (tr("One day"), "1d"),
                    (tr("One week"), "1w"),
                    (tr("30 days"), "1m"),
                )
            )
            await self.show(
                message,
                state,
                self.app.members.user_link(int(owner)),
                [
                    [button(label, f"userop:{parts[0]}:{owner}:{value}")]
                    for label, value in choices
                ]
                + [[button(tr("Back"), "member:" + owner)]],
            )
        elif parts[0] == "userop":
            operation, owner = parts[1:3]
            action = "userapply:" + ":".join(parts[1:])
            await self.confirm(
                message,
                state,
                action,
                tr(
                    "Apply this account change?\n{change}\nAccount: {account}",
                    change=escape(
                        tr(
                            {
                                "remove": "Remove account",
                                "ban": "Ban",
                                "unban": "Unban",
                                "role": "Role",
                                "add": "Add account",
                                "follow": "Follow quota",
                            }[operation]
                        )
                        + (" · " + parts[3] if len(parts) > 3 else "")
                    ),
                    account=escape(self.store.user_label(int(owner))),
                ),
                "member:" + owner if operation != "add" else "users:1",
            )
        elif parts[0] == "userapply":
            back = "users:1" if parts[1] == "remove" else "member:" + parts[2]
            await self.command(
                message,
                state,
                "user",
                " ".join(parts[1:]),
                back,
                present=False,
            )
            if not self.policy.is_admin(actor):
                await self.app.menus.page(message, state, "root")
            else:
                await self.member_page(message, state, back)
        elif parts[0] == "history":
            owner, kind, page = int(parts[1]) or None, parts[2], int(parts[3])
            text, markup = self.app.members._history_page(
                actor, owner, page, kind
            )
            rows = []
            for row in markup.inline_keyboard:
                converted = []
                for item in row:
                    fields = item.callback_data.split(":")
                    converted.append(
                        button(
                            item.text,
                            f"history:{fields[2]}:{fields[3]}:{fields[4]}",
                        )
                    )
                rows.append(converted)
            rows.append(
                [button(tr("Back"), f"member:{owner}" if owner else "users:1")]
            )
            await self.show(message, state, text, rows)
