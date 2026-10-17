# Owner's guide

[简体中文](manual_zh.md) · [Home](../README.md) · [Installation](deployment.md)

This guide is for you as the person deploying, using and managing your own Bot.
You are its owner and administrator, whether you use it alone, from several of
your accounts, or with trusted collaborators. Everyday tasks use buttons; you do
not need to learn the command list or the implementation first.

Not installed yet? Complete the [installation guide](deployment.md), then start
with “First use” below.

## Find what you want to do

| Your next task | Start here |
| --- | --- |
| Open your newly deployed Bot and check that it works | [First use](#first-use) |
| Save a video or a complete album | [Save a message or album](#save-a-message-or-album) |
| Send content to your collection channel or a specific topic | [Set your destination](#set-your-destination) |
| Collect new posts continuously or copy existing history | [Scheduled tracking and history backups](#scheduled-tracking-and-history-backups) |
| Inspect or cancel tasks; join a source and retry | [Tasks and failed transfers](#tasks-and-failed-transfers) |
| Use several Telegram accounts | [Manage execution accounts](#manage-execution-accounts) |
| Give your other accounts or collaborators access | [Manage members and activity](#manage-members-and-activity) |
| Change storage, language or the tdl version | [Settings and maintenance](#settings-and-maintenance) |
| Work out what to check when something fails | [Troubleshooting](#troubleshooting) |

## Understand the three locations

Once configured, most work happens in Telegram. You use the terminal to start,
stop, upgrade or add a login. Three locations are worth distinguishing:

| Name | What it is | What you do with it |
| --- | --- | --- |
| Bot chat | Your private conversation with the Bot you created | Send links, press buttons and read task progress here |
| Internal storage | The channel/group selected during deployment, holding saved content and necessary activity records | Keep it accessible to the Bot and every execution account; do not casually empty it |
| Destination | Where you want to read your collection; your Bot chat by default | Optionally change it to a channel, group or forum topic |

Internal storage and your destination are independent settings. For your first
test, keep delivery in the Bot chat. Once one item arrives successfully, set up
a collection channel. This makes permission problems easier to locate.

## First use

### 1. Open your Bot

The setup wizard displays the Bot's `@username` when it verifies the token.
Search for it in Telegram, open its private chat, and press Start or send `/start`.
The first welcome message is bilingual. Choose **English** or **简体中文**; you
can change this in Settings later. Interface language does not translate source captions.

### 2. Check your administrator access

The main screen shows your account, destination, active tasks, pending retries
and tracking summary. You should see three main rows of buttons:

| First column | Second column |
| --- | --- |
| Task management | Scheduled tracking |
| User management | Bot management |
| Settings | Help |

If the second row is missing, the Telegram account you are using is not an
administrator. For a new installation, check that it is the account you confirmed
in setup step three. With an existing database, editing `ADMIN_USER_IDS` in `.env`
does not reset roles; use an existing administrator's account to make the change
in User management.

### 3. Check internal storage

Open **Bot management** and check that Internal storage shows the channel/group
you prepared. If the wizard already configured it, you do not need to repeat the
step. For a manual Docker installation without storage configured, press
**Change storage** and enter its `@username` or ID.

The Bot must be able to read and copy the stored messages; the execution account
must be able to post there. In a channel, make the Bot an administrator with
posting permission and disable content protection/restricted saving.
For setup and login issues, see [Before you begin](deployment.md#before-you-begin).

### 4. Save your first item

Choose a small video or photo message that your signed-in account can open.
Copy its message link and send it in the Bot chat. Wait for `5/5` and check that
the media and caption appear in that conversation.

A complete result confirms that source access, internal storage and final delivery
work together. Try albums, private sources and custom destinations afterward,
so you can verify each setting separately.

## Save a message or album

### Which link to send

Open the Telegram message you want to keep, copy its link from the message menu,
and paste it into your Bot chat. Use a **specific message link**, rather than a
channel homepage or an invitation link.

For example, `https://t.me/example_channel/123` identifies a message;
`https://t.me/example_channel` is only a channel homepage. These illustrate the
format; replace them with a real message you want to save.

For an album, send any one member's link. There is no need to send every photo
separately. The Bot finds the other members and processes photos, videos and
captions together, preserving order and text formatting. Saved content has its
spoiler covers and original-sender forwarding labels removed.

Public messages, private `/c/` messages, forum messages, `/s/` previews and supported
discussion-comment links are accepted. A private link is not an access grant;
the execution account still needs to be able to read the source.

### Read the progress message

The Bot replies to your link with one message and updates it in place, rather
than sending another message for each step:

| Step | What it means | What you should do |
| --- | --- | --- |
| 1/5 Queued | The task is accepted and waiting | Wait; do not send the same link again |
| 2/5 Locating | Finding the message and the rest of its album | No action needed |
| 3/5 Preparing | Preparing media; shows downloading, uploading or both when transfer is needed | Large files may take longer here; keep the service running |
| 4/5 Sending | Sending to your destination | Wait for the delivery result |
| 5/5 Complete | Media and captions have been sent | Open your destination |

Preparation time depends on file size, network conditions and Telegram limits;
the steps do not take equal time. Previously saved content that remains available
usually arrives sooner, without you selecting a different processing mode.

### Stop a task midway

Press **Cancel** below the progress message and wait for cancellation to finish.
This cleans the task's local temporary files. Content already sent to Telegram
may remain; cancellation does not recall it. The cancel button disappears when
the task ends.

Menu navigation does not replace task progress, so you can adjust other settings
while waiting.

## Set your destination

### Receive in the Bot chat

This is the default and is convenient for a first test, without adding the Bot
to another channel. To restore it, choose **Settings → Receiving settings →
Receive in Bot dialogue**. The equivalent command is:

```text
/target bot
```

### Receive in a channel or group

1. Prepare an existing Telegram channel/group, such as your collection channel
2. Add the Bot; channels need administrator posting permission, while groups
   must allow the relevant text and media messages
3. Ensure your own operator account also has access to this destination
4. Return to the Bot chat and choose **Settings → Receiving settings → Set group or channel**
5. Send the destination's `@username` or numeric ID when prompted; private channels
   generally need an ID
6. Check the destination on the returned page, then send a new message link to test it

In the button flow, send only the requested destination, without `/target`.
If you prefer a command, use the full form:

```text
/target @my_collection
```

The Bot does not create channels or initiate conversations with arbitrary private
accounts. A personal Bot-chat destination is your own conversation with it.

### Receive in a forum topic

Set up the group permissions as above, then append a topic ID after the destination,
separated by a space:

```text
/target @my_forum 42
```

A topic ID is its numeric identifier, not its title. For a link shaped like
`t.me/c/<chat>/42/<message>`, `42` is the topic ID and the final segment is a
message ID; `?thread=42` also explicitly identifies topic 42. If unsure, first
omit the topic to check group delivery, then configure the topic. Status includes a clickable destination/topic link you
can use to verify the location.

Destination changes affect **new tasks only**. Accepted tasks and pending retries
retain their original location; changing Settings does not redirect old work.

## Scheduled tracking and history backups

### Choose the right task

| What you want | Feature | Where to find it |
| --- | --- | --- |
| Collect newly published content from now on | Scheduled tracking; command name `follow` | Main screen → Scheduled tracking |
| Copy content already in the source | History backup; command name `dump` | Bot management → History backup |

Both support albums, individual media and plain text, ignoring service messages.
Neither creates a destination channel or synchronizes later edits and deletions.

### Create scheduled tracking

1. Choose **Scheduled tracking → New tracking**
2. Send the source's `@username`, ID or link; private sources require execution-account access
3. On the review page, check the source and press **Destination**
4. To use a separate backup channel, choose **Set group or channel** and enter it;
   to keep content only in internal storage, leave **Default** selected
5. Press **Interval** and select a preset, or enter a custom value such as `5m` or `1h`
6. Check the source, destination and interval, then press **Start tracking**

The default check interval is five minutes; the minimum is 60 seconds. After
creation, task details show its state, processed counts, interval and next check
time when applicable. Counts remain unchanged when the source has no new posts.

Tracking starts **after the latest message present at creation**. It does not
collect earlier history. **Default** here means **internal storage only**, not
your personal destination from Settings. Explicitly choose your collection channel
if you want tracking results to arrive there.

Equivalent command:

```text
/follow @source_channel to @backup_channel every 5m
```

### Create a history backup

1. Choose **Bot management → History backup → New backup**
2. Send the source channel or group
3. Choose a destination on the review page; omitting it saves to internal storage only
4. Confirm by pressing **Start backup**
5. Inspect task details for progress; history is copied oldest first, keeping albums together

The task copies only history visible to its execution account. It fixes its end
position at creation, so new posts do not keep extending the job. Create tracking
as well if you want to keep collecting future posts.

Equivalent command:

```text
/dump @source_channel to @backup_channel
```

Starting another backup of the same history may deliver those posts again.
History backups and tracking are independent tasks. If using both, create tracking
first to avoid a gap between creation times; overlapping content may be sent twice.

### Pause, change, cancel and resume

Open the relevant task list, select a task, then choose an action:

| Action | Result |
| --- | --- |
| Pause | Retains progress for later resumption |
| Resume | Continues from saved progress, rather than creating a new task from the beginning |
| Change interval | Changes the tracking frequency, keeping its original source and destination |
| Cancel | Ends the task permanently; already sent messages remain |

Cancelled tasks remain in lists for seven days, then are hidden. New destination
or execution-account settings do not alter existing tasks; create a new task for
a different destination. Large backups are paced automatically. Telegram rate
limits cause a wait; repeatedly pausing and resuming does not speed it up.

If the last send is unconfirmed, first inspect the destination for that item:

- Confirmed complete: choose **Skip current item** to avoid sending it again
- Confirmed absent: choose **Retry current item**
- Partly received or unclear: check the content before proceeding; repeated retries can leave duplicates

## Tasks and failed transfers

### Inspect and cancel active work

**Task management** shows current status directly. Open **Active tasks** and select
a task to inspect its source, destination, stage and cancel control. Ended tasks
leave the active list; the original progress message retains the result.
Use Refresh on dynamic lists for current counts; navigating back to other pages
loads their latest state automatically.

### Join a source and retry

If a link enters pending retries because the source is inaccessible:

1. Choose **Task management → Retry management**
2. Select the record and check its source and destination
3. If access is already fixed, press **Retry**
4. If not yet a member, press **Join and retry** and send the source homepage or invitation link
5. Check the execution account shown on the confirmation screen, then confirm joining

A successful join starts the retry. If approval is required, wait for the source
administrator to approve, then return to that record and press Retry. You can
also join independently through **Task management → Join**. The execution account
joins the source; this does not add the Bot to it.

Joining cannot recover deleted content, bypass bans or reveal unavailable history.
If one of your accounts can read a source but the Bot cannot, check which execution
account the Bot is actually using.

### Remove an unwanted retry

Open its details, press **Remove retry**, then confirm. This removes only the
record: it does not recall media or leave a group you have joined. If the retry
is already running, cancel the active task before removing its record.

Administrators are not subject to the three-record retry cap. Ordinary members
can keep three pending retries; they must retry or remove an entry before
submitting more links. This does not limit you while operating as administrator.

## Manage execution accounts

### Start with one account

You do not need multiple accounts for everyday use. The Telegram account signed
in during setup is the shared execution account, responsible for reading sources
and preparing media. You can operate the Bot from that account or from another
authorized Telegram account.

Members without a personal login use the shared executor. Source access therefore
depends on **the execution account**, not just the account that sent the link.

### Add another of your accounts

For a native installation, run these commands in your workspace terminal:

```sh
tdl-bot stop
tdl-bot login -n personal
tdl-bot run
```

`personal` is a name for this login; choose another memorable name if you prefer.
Scan with the intended account, rather than signing in as the original account
again. See [Reuse or add logins](deployment.md#reuse-or-add-logins) for Docker.
Every execution account needs permission to post to internal storage.

After restart, a logged-in personal account is used automatically when its UID
matches the account operating the Bot; otherwise the shared executor is used.
Logging in does not grant Bot membership. Add other operator accounts in User management too.

### Select an account as administrator

Open **Bot management → Execution account** to see the loaded accounts and current
selection. Select one to use it for your subsequent new tasks, or choose
**Automatic** to restore the own-account-otherwise-shared rule.

The selection affects only your new tasks. It does not change another member's
account or the identity of existing tasks and retries. Ordinary members cannot
use this selector to borrow someone else's personal account.

## Manage members and activity

### Using it alone? Skip member setup

The administrator confirmed at first setup already has management access; you
do not need to add yourself again. Add members only when you want other accounts
or collaborators to operate the Bot. This workspace is for trusted collaboration,
not private collections isolated from other members.

### Add an account

1. Open **User management** to see the member list
2. Press **Add account** and enter the person's `@username` or UID
3. After confirming, have that person open the same Bot's private chat and send `/start`
4. They can choose a language, send links and set their own destination

If they have no username or it cannot be resolved, have them use `/whoami` and
add the returned UID instead. Permissions attach to that UID; a later username
change does not transfer access.

### Inspect and change a member

Use pagination buttons in the list, then select an account to open its details.
Check its role, ban state, destination and follow usage before making changes:

| Action | When to use it |
| --- | --- |
| Change destination | Help another account correct where its content is sent |
| Set role | Grant administrator access for someone helping manage the Bot; otherwise keep the regular role |
| Ban / unban | Temporarily suspend access for a day, week or 30 days, or lift it early |
| Remove | Revoke the account's permission to use the Bot |
| Follow quota | Set an ordinary member's tracking allowance, from 0 to 1000 |

Ordinary members start with nine follows. Active and paused tasks both count;
cancelling releases a slot. Lowering a quota preserves existing tasks, but new
creation and resumption must fit the new allowance. Administrators have no count cap.

Banning/removing a member stops their active work. Completed content and server-side
login sessions remain. The last effective administrator cannot be removed, banned
or demoted, preserving access to management.

### See who processed what

Open History in User management for all link/join activity, or filter by member.
Switch tabs for management records, kept separately from content activity.

Links and explicit joins also leave compact records in internal storage; other
everyday buttons and commands are not all posted there. Records prefer usernames,
with failed/cancelled links struck through. Requests for already saved content
link to the existing media instead of adding another media copy to storage.

## Settings and maintenance

### Change language

Choose **Settings → Language settings**, then English or Simplified Chinese.
Each operator's choice is saved across restarts; source captions stay unchanged.

### Change internal storage

Prepare the new channel/group and grant access to the Bot and every execution
account. Choose **Bot management → Change storage** and send the new location
when prompted. Check the displayed location, then verify it with a new link.

This does not migrate old media. Accepted tasks keep their original storage
location, and existing results in the old channel may still be used. Do not
immediately delete the old channel. Change your destination separately in
Settings → Receiving settings.

### Update tdl versus updating the Bot

**Bot management → Update tdl** updates the program that handles transfers.
Choosing the latest version first checks the installed version; it downloads
only when needed and waits for active use before replacement. Failure preserves
the old executable. This does not update tdl-bot itself.

For Bot upgrades, backups, stopping and restarting, follow
[Stop, reload and upgrade](deployment.md#stop-reload-and-upgrade). Do not delete
installation or login folders as a way to fix a task error.

### When you need commands

**Help** displays the full command list for your role. Buttons cover everyday
work; commands are useful for direct entry once familiar. For example, `/status`
shows status and `/user h` opens history. Subcommands accept unambiguous prefixes;
an ambiguous prefix does not execute anything.

## Troubleshooting

### The Bot replies, but content does not arrive

Keep delivery in the Bot chat and check that the execution account can read the
source. Then check Internal storage in **Bot management**: the Bot must have
joined, have posting permissions, and storage must allow copying. If Bot-chat
delivery works but your collection channel does not, check that channel's Bot
permissions and topic ID. Internal storage permissions matter as well as the final target.

### The error says some media may already have been uploaded

Inspect the destination for all or part of the content. A connection can fail
after a successful send, so resubmitting immediately may create duplicates.
For a normal link task, decide whether to resubmit after checking; for backups
or tracking, use the task's retry/skip controls.

### You can read the source, but the Bot asks to join

Check the actual identity under **Bot management → Execution account**.
If using the shared account, that account must join, not only the account sending
the link. Approval may still be pending, and joining does not necessarily expose
older history restricted by the source.

### A new follow has not sent anything

Tracking does not copy pre-existing posts. Check that the task is Running and
look at its next check time. For a test, create tracking on a source you control,
publish a new message afterward, then wait at least one polling interval.
Use History backup for older content.

### An old button fails, or an old task still uses the previous destination

Send `/start` again to replace an expired menu. Target/account changes affect
new tasks only; existing tasks and retries retain their settings. Submit a new
task when you need the new location.

For startup, Docker folders, proxies and Windows issues, see
[installation troubleshooting](deployment.md#troubleshooting).
