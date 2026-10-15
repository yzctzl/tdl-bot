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
reads or changes tdl sessions. Separate namespaces share a storage lock and
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
