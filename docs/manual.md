# User guide

[简体中文](manual_zh.md) · [Home](../README.md) · [Installation](deployment.md)

## First use

Your administrator installs the Bot and grants you access. Open its **private
chat**, send `/start`, and choose English or Simplified Chinese. Your language
choice is saved; it does not change the language of media captions.

The main screen shows your account, destination and task summary. Its buttons are:

| Section | What you can do |
| --- | --- |
| Task management | See current status, active tasks and pending retries; join a source |
| Scheduled tracking | Create a follow, see its progress, pause, resume, change its interval or cancel |
| Settings | Choose a destination and interface language |
| Help | Show the commands available to your role |
| User management — administrators | Manage members, permissions, quotas and history |
| Bot management — administrators | Back up history, configure internal storage, choose an execution account and update tdl |

Opening or returning to a page loads its current summary. Progress messages are
separate, so you can browse menus while a task runs. If an old button expires,
send `/start` again. Commands remain available alongside buttons; sending a command
exits a menu's pending text-input step.

## Save a message or album

Send a Telegram **message link** in the Bot chat. A link to any member of an album
selects the whole album, including captions attached to other members. Photos,
videos and their text stay in source order; formatting is preserved and spoiler
covers are removed from the saved content.

Public links, private `/c/` links, forum message links, `/s/` preview links and
supported discussion-comment links are accepted. A private link does not itself
grant access: the account doing the transfer must be able to read that source.

The Bot replies to your link with **one progress message**, updated in place:

| Step | What it means |
| --- | --- |
| 1/5 Queued | Your task has been accepted |
| 2/5 Locating | Finding the message and other members of its album |
| 3/5 Preparing | Preparing the media; when byte transfer is needed, the message shows downloading, uploading, or both |
| 4/5 Sending | Sending the prepared content to your destination |
| 5/5 Complete | Media and captions have been sent |

Use **Cancel** beneath that message, or choose a task in Task management.
Cancellation waits for local cleanup. Content already sent to Telegram may remain;
cancelling is not a recall operation. Terminal states remove the cancel button.

Previously saved content can be sent without another download. This also works
for another link into the same album when the Bot can identify it. It depends on
the saved copy still being accessible; it is not an unlimited archive of deleted content.

## Choose where content arrives

Open **Settings → Receiving settings**. The default is your Bot chat. You can instead
enter a channel/group `@username` or ID, with a topic ID when needed:

```text
/target bot
/target @my_collection
/target @my_forum 42
```

Prepare an existing destination; the Bot does not create channels. Add the Bot
with permission to send the relevant messages and media. For a channel, make it
an administrator with posting permission. Private destinations may require a numeric ID.
A destination/topic link in status opens the corresponding location where Telegram supports it.

Changes apply to **new tasks**. Accepted tasks and retries keep the destination
and execution account they were created with. Changing your destination does not
redirect a pending retry. Use `/status` to check your current selection.

## Tasks, retries and joining sources

Task management shows the current state directly. Open active tasks for progress
and cancellation; pending retries have controls to retry, remove, or join a source
before retrying. Ordinary members manage their own personal tasks; administrators
can manage all tasks, subject to execution-account access rules.

If the source requires membership, choose **Join**, then send its public homepage
or invitation link. Joining is also available independently of a failed task.
A join linked to a retry uses that task's original execution account. Approval
requests may need a source administrator's action before a retry can succeed.

```text
/pending
/retry <id>
/join https://t.me/+INVITE <retry_id>
/retry_cancel <id>
```

Ordinary members can keep **three pending retries**. When the list is full, retry
or remove existing entries before sending more links. A join request awaiting
approval uses the same entry, not an extra slot. Remove only deletes the retry
record; cancel an active retry before removing its record. Administrators are
not subject to this cap.

Joining cannot restore deleted messages, overcome a ban, or reveal history the
source does not make available to your account. If the original execution account
is unavailable, ask your administrator to restore it; retries do not switch identities silently.

## Scheduled tracking and history backups

### Follow new posts

Use **Scheduled tracking** to select a source, an optional destination and an
interval. A follow starts **after the latest message present when it is created**.
It checks every five minutes by default; the shortest interval is 60 seconds.
It copies new albums, individual media and text, ignoring service notifications.
Edits and deletions are not synchronized.

```text
/follow @source_channel to @backup_channel every 5m
/follow @source_channel every 10m
/follow interval <id> 1h
```

Prepare the destination and Bot permissions as for normal delivery. Omitting the
destination saves to the service's default storage only; it does **not** send to
your personal destination. No channel is created automatically.

Ordinary members have **nine follows** by default. Active and paused follows both
count; cancelling frees a slot. Administrators can change a member's quota.
A lower quota keeps existing follows, but creation and resumption must fit the
new quota. Administrators have no count cap.

### Back up earlier messages — administrators

In **Bot management → History backup**, choose the source and optional destination.
A dump copies visible history from oldest to newest, keeping albums together.
The last message to include is fixed at creation, so new posts do not extend the
backup indefinitely. Use a follow for later posts.

```text
/dump @source_channel to @backup_channel
```

The execution account must have access to the source. Only history visible to
that account can be copied. Omitting a destination saves to internal storage only.
Running a new dump of the same history can deliver those posts again.
A dump and a follow are separate tasks, not an atomic handover: if you need both,
creating the follow first avoids a gap between their creation times, but the
overlapping posts may be delivered twice.

### Pause, resume and recover

Both task types have status, pause, resume and cancel controls. Pausing retains
progress; cancelling is final. Cancelled tasks stay in lists for seven days.
Tasks keep their original execution account and destination.

Telegram rate limits and safe network failures trigger delayed retries. If the
result of a send is uncertain, the backup pauses instead of guessing. Inspect
the destination, then use the offered retry/skip controls. Retrying may duplicate
a successful but unacknowledged send; skipping may omit an item that never arrived.

Large backups are paced automatically. A quiet task may be waiting for Telegram's
requested delay or its next follow interval, rather than being stuck.

## Accounts and collaboration

This is a shared workspace for trusted people, not an isolated public hosting
service. Do not use one workspace for people whose collections must be isolated.
Members have personal destinations and language settings. Administrators
manage access, tasks and history. Submitted links and explicit join actions are
recorded for administrators, including compact records in internal storage.
Other everyday commands are not posted there as activity records.

The account sending commands to the Bot and the account reading sources are not
necessarily the same. If your own Telegram account is already logged in on the
server, it is used automatically; otherwise the shared execution account is used.
Ordinary members cannot select another person's session. Your administrator
handles server-side logins; the Bot chat does not collect login codes.

## Administrator controls

Use **User management** to list members with pagination buttons. Select a member
to see their destination, role, ban state and follow usage, then make changes.
Most member commands accept a UID or `@username`; an unresolvable username may
still need a UID. Permissions attach to the UID, so renaming a username does not transfer access.

| Operation | Command alternative |
| --- | --- |
| Add / remove | `/user add @person` · `/user remove @person` |
| Ban / unban | `/user ban @person 1d` · `/user unban @person` |
| Change role | `/user role @person user` or `admin` |
| Follow quota | `/user follow @person 9` — range 0–1000 |
| History | `/user history [@person]` — activity, with a management-history tab |
| Member destination | `/target for @person @destination` |

Ban durations are `1d`, `1w` and `1m` (30 days). Removing/banning a member stops
their active work; completed content remains. Removing a member does not delete
server-side Telegram sessions. The final effective administrator is protected
against deletion, banning and demotion.

**Bot management** shows tdl version, internal storage and execution account.
Internal storage must remain readable by the Bot and writable by every execution
account. Deleting its media can prevent reuse. An administrator may select any
loaded execution account; the choice affects only that administrator's new tasks.
Updating tdl checks the installed version first and preserves the old executable
if installation fails.

## Command shortcuts

Use Help for the complete role-specific command list. `/start` opens the menu;
`/whoami` shows your own UID. Subcommands accept an unambiguous prefix, such as
`/user h` for history. Ambiguous prefixes do nothing and ask for a longer spelling:
`/user r` can mean `remove` or `role`. Values such as usernames and URLs are not abbreviated.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Access denied | Ask the administrator to add or unban your Bot account |
| Source unavailable / pending retry | Check the execution account's membership, approval status and visible history |
| Sending failed | Add the Bot to the destination **and internal storage**; check posting/media permissions and topic ID |
| Some media may already have been uploaded | Inspect the destination before resending; an interrupted acknowledgement can leave a successful send |
| Queue full | Wait for a task to finish; submitting the same link repeatedly does not help |
| Three pending retries already exist | Retry or remove an entry before submitting another link |
| A follow has no old posts | Follows start with new posts; ask an administrator for a history backup |
| An old button no longer works | Open `/start` again |
| Destination changed but retry goes elsewhere | Retries keep their original destination; submit a new task for the new location |

For startup, proxy, Windows, Docker or upgrade issues, use the
[installation and maintenance guide](deployment.md#troubleshooting).
