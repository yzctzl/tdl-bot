# Source architecture

The command line remains `uv run tdl-bot`. Internal modules are grouped by
responsibility; imports should use their package paths directly.

| Package | Responsibility |
| --- | --- |
| `core` | Configuration, links, shared errors and language contexts |
| `bot` | Commands, authorization and progress messages |
| `transfers` | Account selection, permissions and transfer lifetimes |
| `storage` | SQLite migrations, workspace state and result history |
| `integrations/telegram` | MTProto media operations and Bot API delivery |
| `integrations/tdl` | Native downloads, binary updates and session import |
| `runtime` | CLI, manual reload supervision and private engine RPC |
| `locales` | English and Chinese catalogs; additional languages go here |

The Bot application composes `DestinationCommands`, `MemberCommands` and
`TaskCommands`; it binds their methods through the command registry. Independent
middleware and progress adapters keep transport details out of handlers. Shared
values live in `storage/models.py`, task snapshots in `transfers/jobs.py`, schema
upgrades in `storage/schema.py`, and binary updates in `integrations/tdl/updates.py`.

The root `cli.py` is a temporary compatibility entry point for supervisors already
running before this reorganization. New children use the stable package-level
`main()` entry point, so future internal module moves do not affect spawning.

## Transfer flow

1. The Bot captures a destination and execution account when accepting a URL.
2. The resident engine looks for a saved storage result before reading the source.
3. A new source album is copied server-side where possible. Protected albums
   fall back to native tdl clone, followed by server-side
   album assembly. Standalone Telethon login retains its own uploader.
4. Storage copies have spoilers removed, and their IDs are saved before delivery.
5. The Bot copies the saved album to the user's destination. A failed delivery
   keeps the storage result available for a later request.

The Bot process can restart independently of the resident engine. Manual reload
hashes all Python sources recursively and all locale catalogs. New engine code
is loaded after existing work finishes; frontend restarts do not kill tdl.

## Native cloning and server-side album assembly

Native `tdl forward --mode clone` handles media bytes, original attributes and
previews using its existing storage and authorization. Session backup is decoded
into a memory-only Telethon session for surrounding MTProto operations. Default
and personal native sessions use the same path.

Native clone does not return structured message IDs and can exit successfully
after individual transfer failures. `--single` plus an edit expression maps each
source message ID to a random per-task receipt and original position. Exact
receipts distinguish concurrent jobs and unrelated posts; neither upload order
nor the newest message ID is treated as file identity.

Only a complete, unambiguous set with matching document sizes is assembled into
an album using existing Telegram media references. Telethon restores captions
and removes spoilers without uploading file bytes. Final album IDs enter the
result ledger before Bot delivery. Temporary tagged posts are removed on success,
failure and cancellation; cleanup errors never mask the original failure.

Native Go temporary files use the job directory through TMPDIR, TMP and TEMP.
Cancellation first terminates and reaps tdl, then removes that directory. A hard
engine crash can leave staging posts, while frontend reloads keep the engine
alive. Standalone Telethon sessions retain the separate download/upload path.

## Boundaries worth preserving

- Re-check permissions after network calls before exposing or changing private
  state: a ban or role change can happen while awaiting a response.
- Record uncertain writes before sending. Restart recovery must not blindly
  repeat an upload or delivery whose acknowledgement may have been lost.
- Keep queue cancellation, subprocess termination and temporary-file cleanup in
  the task engine. A frontend reconnect cannot safely own those lifetimes.
- Serialize clients sharing the same authorization and coordinate binary updates
  across accounts. Native tdl storage and MTProto sessions have different locks.
- Keep source strings, comments and developer-facing logs in English. Translate
  user messages through `tr()` and keep Chinese text in the locale catalog.

## Small logic improvements

Authorization uses a single indexed member lookup rather than loading all
members for every check. Uploads reuse the peer returned by destination
validation. Task and retry list buttons share the same bounded, persisted control
history, and download verification reuses file sizes between completeness checks.

## Execution identity and routing

The engine imports an owner-only tdl backup to discover valid namespaces in
TDL_DIR. Each session is verified with Telegram; its returned UID is its owner.
The configured shared session takes priority over duplicates. Other UIDs use
stable `operator:<UID>` keys, preserving existing task and retry snapshots.

Automatic routing selects the operator's own identity, otherwise shared. Only
administrators may persist a different choice. The workspace publishes an
identity-only catalog to frontends; credentials remain in memory. The resident
engine validates selections and executor permissions again when accepting work.
Personal executor permissions are independent of shared target management, so
managing a shared retry never grants the use of somebody else's session.

Manual reload requests a new engine generation even without a code change,
allowing login/logout discovery after active work drains. No background watcher
reads or changes tdl sessions. Bolt namespaces have independent storage locks; legacy storage retains a
directory-wide lock because all namespaces share one database. Accounts share
an update gate, while each account retains its own RPC lock.

## Initial setup and native login

First initialization requires explicit ADMIN_USER_IDS. Those UIDs become the
initial members and administrators; later permission changes use SQLite only.
Login is allowed before Bot credentials and administrators are configured.

The explicit login command delegates QR, code and Desktop login to native tdl
with the same namespace and storage arguments used by transfers. Only this path
may create a missing namespace. Its child inherits the terminal for interaction;
credentials are not piped into Python. Existing valid authorization is reused.
Missing binaries are installed through the verified official-release updater.

Normal engine startup remains read-only with respect to login: missing sessions
fail instead of creating an empty database. Login holds the runtime, storage and
binary locks, so it cannot replace an authorization used by the resident engine.
Cancellation terminates and reaps the native child before releasing these locks.

## Durable dumps and follows

`storage.backups` stores task snapshots in schema v8; migration from v7 keeps
membership, result history and delivery history and creates a private backup.
Creation also retains the initiating Telegram message ID, so a redelivered
command reuses its original snapshot even after completion.
`integrations.telegram.history` reads bounded chronological history pages and
extends them to an album boundary. Checkpoint identities use marked chat IDs;
public usernames are used only when native tdl needs a resolvable media URL.
Basic groups retain a Telethon download fallback because they have no tdl URL.

The backup scheduler runs in the resident engine, independently of polling.
Dump tasks freeze an upper message ID; follows freeze their initial cursor and
read newer messages on each poll. Both reuse `TransferService.backup_group`,
including storage caching, direct copies, native clone receipts and final Bot
delivery. Storage-only tasks skip final delivery. Protected text copies preserve
entities while removing spoilers. Quiet per-unit jobs do not spam Bot progress.

Before a unit starts, its ending ID and writing phase are committed. The cursor
is committed only after transfer and delivery complete. Restarting a writing
phase requires explicit retry/skip resolution. Safe read failures and definite
rate-limit rejections back off; uncertain sends pause. Pause/cancel awaits child
termination and cleanup. Membership changes are checked both between units and
by the engine's permission reconciliation loop.

Reload quiesces the scheduler at unit boundaries, and the engine waits for
backup work as well as regular jobs and RPC calls. The frontend's health task
ensures a replacement engine starts after handoff without requiring a new user
request. This health check never hashes code or watches directories. Shutdown
stops the frontend health task before draining the resident engine.

Backup cycles use the configured worker count without a separate two-worker
ceiling. Before starting a unit, the shared pacer atomically reserves account and
destination admission spacing; albums count every member. No global write lease
is held during byte transfer. Admission spacing does not guarantee spacing
between completion times: transfers of different sizes can finish together.
FloodWait defers backups on that execution account; Bot API rate limits defer
backup Bot deliveries globally. Waiting stays cancellable and checks reload
before recording a writing checkpoint.

Follow quotas live in workspace metadata and default to nine ordinary-member
active/paused tasks. Admin-only quota changes use the existing membership audit
log and are read again on creation/resume. Lower limits preserve existing tasks.
Cancelled snapshots retain a timestamp; list pagination excludes cancellations
older than seven days without deleting request identities or checkpoints.

## Deployment boundaries

Configuration is parsed from an explicit workspace `.env`, without exporting
file values into the process environment. Paths resolve against that file's
parent. Child processes receive its absolute filename through
`TDL_BOT_ENV_FILE`, so manual reload reads fresh values. An explicit environment
override takes priority. New managed binaries are scoped by OS/architecture;
legacy executable/session paths remain explicit overrides.

`RUNTIME_DIR` separates Unix sockets and frontend control files from persistent
data. Compose keeps these on a private tmpfs while resource leases stay next to
the database, sessions and executable. The configuration directory is mounted
read-only, so atomic `.env` replacements remain visible. Other writable mounts
use fixed container paths and caller-selected host paths.

The container entrypoint uses the normal frontend supervisor with an additional
shutdown hook. Reload only restarts the frontend. Container shutdown reaps that
frontend first, drains the engine through an idempotent shutdown RPC, then waits
for the engine's instance lease to be released. Engine shutdown rejects new URL
jobs before quiescing backups. Replacing a container or Python dependency
environment requires this graceful stop; it is not cross-version hot upgrade.


## Link and join audit

Schema v9 adds `activity_events` independently of administrator management
history. Only accepted media-link jobs and explicit chat joins create events;
quiet backup units and other commands are excluded. The resident engine owns
recording and checks execution-account access for joins. A private event key
identifies each job, and the shared-storage message is acknowledged before media
processing or joining begins. An unacknowledged record is not blindly reposted.
The stored-results ledger remains the source of media reuse; activity posts link
to its existing messages rather than creating duplicate media. Administrators
query by immutable UID through `/user history`, with ownership-checked callback pagination. Tabs
switch between link/join events and administrator management history, retaining
the member filter and showing 20 compact rows per page. Display names prefer usernames without appending a redundant UID.


## Bilingual message maintenance

Application source and developer logs stay English; Chinese application copy
belongs only in `src/tdl_bot/locales/zh.json`. English source templates supply
default copy and translation keys.
`en.json` contains only the language name and an empty message map; other locale
files translate source templates. Missing translations fall back to source
English. When changing a persisted template, keep an alias from its old wording
to the new key. Aliases are resolved before translation and parameter rendering.
Catalog loading rejects duplicate keys and changes to placeholder names,
conversions or format specifications. The localization regression test checks
Chinese coverage against source templates, dynamic job stages and executable
command words in help. Historical error/stage entries remain for older task
records; obsolete help blocks have been removed.

Persisted backup errors and RPC errors preserve templates plus parameters.
Rendering uses the viewer's current language, including after a restart or a
language change during a pending operation. The RPC response retains its plain
error field for older clients. Legacy plain-text errors stay readable; exact
catalog messages can be translated, while already-formatted old strings with
lost parameters cannot always be reconstructed.


## Native Windows runtime

Windows uses the same transfer services directly in the polling process, without
Unix RPC or detached supervisors. Workspace leases use a nonblocking one-byte
`msvcrt` lock on Windows and `flock` on Unix. Locks are released by the operating
system on exit; control-file contents never grant ownership.

A stop request file is checked by the event loop; `stop` waits for the supervisor
lease to release after services drain and clients disconnect. Incoming commands run concurrently; an update middleware tracks accepted
handlers so shutdown waits for them before closing shared services. A separate
maintenance task reconciles transfer and backup permissions every 0.5 seconds,
matching the Unix engine. The polling
heartbeat provides a local liveness check and is removed before draining. Stale
control files are cleared under the supervisor lease on startup. Console signals
request cooperative shutdown; noninteractive tdl children use their own Windows
process group so Ctrl+C does not bypass the parent's drain. Explicit job
cancellation still terminates and reaps its child.

Native Windows has no reload, engine handoff or automatic frontend restart.
Persisted jobs and backup checkpoints retain their existing recovery behavior.
Linux/macOS and containers keep the resident engine and hot reload unchanged.


Shutdown ordering keeps startup and cleanup separate: Windows monitors stop
requests while command menus register, cancels unfinished registration, and
finishes any permission-revocation cleanup before draining accepted commands.

On Unix, frontends pass their supervisor PID to spawned engines. Shutdown
removes the supervisor PID file before draining; a delayed engine checks that
its launching supervisor still matches after acquiring the instance lease.
This prevents an engine from starting after the shutdown idle check, including
when a replacement supervisor has already started.


## Concurrency and transport boundaries

`WORKERS` bounds foreground job lifetimes and, separately, concurrent backup
cycles. Increasing it allows unrelated content to resolve, transfer and deliver
concurrently. There is no whole-task execution-account mutex. URL and media-group
locks still prevent duplicate preparation; cancellation owns only its job's
process and temporary directory. Foreground and backup worker pools are separate,
so their combined active task count can exceed `WORKERS`.

Native tdl still holds its namespace storage lock and authorization coordinator
for the lifetime of a subprocess. Telethon RPCs using that authorization wait for
it; removing this coordination would permit competing API layers. Different Bolt
namespaces no longer block each other's native processes. Legacy databases still
require shared storage coordination.

Upstream `core/forwarder/forwarder.go` processes source messages sequentially;
`core/forwarder/clone.go` downloads a file completely before starting its upload.
Its transfer threads parallelize chunks within the current direction. More Bot
workers or tdl threads do not turn native clone into full-duplex streaming, and
`forward` does not use the root task `--limit` setting in this implementation.
Same-account full duplex therefore requires a transport change, rather than
parallel native processes sharing a Bolt namespace. No bandwidth-saturation or
fixed Telegram account-concurrency guarantee follows from these scheduler changes.
