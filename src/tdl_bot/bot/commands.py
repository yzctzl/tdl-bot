"""One command registry drives routing, menus and help."""

COMMANDS = (
    ("dump", "backups.dump", "Back up channel history (admin)", True),
    ("follow", "backups.follow", "Track new channel or group messages", False),
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
    ("user", "members.user", "Manage members: /user for help", True),
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
    "\n/follow <source> [to <chat>] [every 5m] Track new messages"
    "\n/follow List tasks; /follow pause|resume|cancel <id> Manage a task"
)


USER_HELP = (
    "User management:\n"
    "/user list [page] List members\n"
    "/user add <UID|@username> Add a regular account\n"
    "/user remove <UID|@username> Remove an account\n"
    "/user ban <UID|@username> <1d|1w|1m> "
    "Ban for a day, week, or 30 days\n"
    "/user unban <UID|@username> Unban an account\n"
    "/user role <UID|@username> <user|admin> Set a role\n"
    "/user history <UID|@username> View account management history\n"
    "/user follow <UID|@username> <0-1000> Set follow quota\n"
    "/target for <UID|@username> [chat] [topic_id] Manage a personal "
    "destination\n"
    "/status <UID|@username> View another account's destination"
)


ADMIN_HELP = (
    "\n\nAdministrators:\n"
    "/storage <chat> [topic_id] Configure internal storage\n"
    "/update_tdl [latest|v0.20.4] Check and update tdl\n"
    "/account <auto|shared|UID> Switch execution account\n"
    "/dump <source> [to <chat>] Back up channel history\n"
    "/dump List tasks; /dump pause|resume|cancel <id> Manage a task"
)
