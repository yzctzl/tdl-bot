"""One command registry drives routing, menus and help."""

COMMANDS = (
    (
        "account",
        "execution.account",
        "View execution account; administrators can switch",
        True,
    ),
    ("language", "language", "Choose your language", False),
    ("start", "start", "Get started", False),
    ("help", "help", "Show help", False),
    ("whoami", "whoami", "Show your account ID", False),
    (
        "target",
        "destinations.target",
        "Set destination: Bot dialogue, group, or channel",
        False,
    ),
    (
        "status",
        "destinations.status",
        "View destination and task status",
        False,
    ),
    ("jobs", "tasks.jobs", "List tasks you can manage", False),
    ("cancel", "tasks.cancel_command", "Cancel an active task: task ID", False),
    ("pending", "tasks.pending", "List pending retries", False),
    ("retry", "tasks.retry", "Retry a task: retry ID", False),
    ("retry_cancel", "tasks.retry_cancel", "Remove a pending retry: ID", False),
    ("join", "tasks.join", "Join source group: group link and retry ID", False),
    (
        "storage",
        "destinations.storage",
        "Configure internal storage (admin)",
        True,
    ),
    ("update_tdl", "update", "Check and update tdl", True),
    ("users", "members.users", "List members and ban status", True),
    (
        "user_add",
        "members.user_add",
        "Add a regular account: UID/@username",
        True,
    ),
    (
        "user_remove",
        "members.user_remove",
        "Remove an account: UID/@username",
        True,
    ),
    (
        "user_ban",
        "members.user_ban",
        "Ban an account: UID/@username 1d/1w/1m",
        True,
    ),
    (
        "user_unban",
        "members.user_unban",
        "Unban an account: UID/@username",
        True,
    ),
    (
        "user_role",
        "members.user_role",
        "Set role: UID/@username user/admin",
        True,
    ),
    (
        "user_history",
        "members.user_history",
        "View account history: UID/@username",
        True,
    ),
)

HELP = (
    "Send a Telegram message URL to process its entire media group and "
    "captions.\n/target <chat> [topic_id] Set a destination group or "
    "channel\n/target bot Receive in this dialogue (default)\n/status View "
    "destination and task status\n/jobs List active tasks; /cancel <id> "
    "Cancel a task\n/pending List retries; /retry [id] Retry a "
    "task\n/retry_cancel <id> Remove a retry\n/join <group_url> [id] Join "
    "and retry\nRegular accounts can keep up to 3 pending "
    "retries.\n/status View status; /whoami View your account "
    "ID\n/language [en|zh] Choose your language"
)


ADMIN_HELP = (
    "\n\nAdministrators:\n/storage <chat> [topic_id] Configure internal "
    "storage\n/target for <UID|@username> [chat] [topic_id] Manage a "
    "personal destination\n/users List members\n/user_add <UID|@username> "
    "Add a regular account\n/user_remove <UID|@username> Remove an "
    "account\n/user_ban <UID|@username> <1d|1w|1m> Ban for a day, week, or "
    "30 days\n/user_unban <UID|@username> Unban an account\n/user_role "
    "<UID|@username> <user|admin> Set a role\n/user_history "
    "<UID|@username> View account management history\n/status "
    "<UID|@username> View another account's destination\n/update_tdl "
    "[latest|v0.20.4] Check and update tdl"
    "\n/account <auto|shared|UID> Switch execution account"
)
