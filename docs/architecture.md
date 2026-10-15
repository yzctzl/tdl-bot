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
   fall back to native tdl download and folder upload, followed by server-side
   album assembly. Standalone Telethon login retains its own uploader.
4. Storage copies have spoilers removed, and their IDs are saved before delivery.
5. The Bot copies the saved album to the user's destination. A failed delivery
   keeps the storage result available for a later request.

The Bot process can restart independently of the resident engine. Manual reload
hashes all Python sources recursively and all locale catalogs. New engine code
is loaded after existing work finishes; frontend restarts do not kill tdl.

## Native uploads and server-side album assembly

Native tdl handles file bytes and its own storage/authorization. Session backup
is decoded into a memory-only Telethon session for the small MTProto operations
that surround transfers. Default and personal native sessions use the same path.

The inspected upload CLI sends files independently, does not return structured
message IDs, and may exit successfully after a partial upload. We therefore
upload an isolated folder of hard links into internal storage. A caption
expression maps each filename to a random per-task marker plus its original
position. These exact receipts distinguish concurrent jobs and unrelated posts;
we never infer file identity from the newest message IDs or upload order.

Original video thumbnails are downloaded as small `.thumb` sidecars for native
upload. tdl does not automatically extract a preview frame; omitting these
sidecars can leave correct video dimensions but broken album previews. Sidecar
names follow tdl's `TrimRight` convention, and uploaded videos are checked for
thumbnail presence before assembly.

Only a complete, unambiguous set with matching document sizes is assembled into
an album using existing Telegram media references. Telethon transfers captions
and media references at this step, not the file bytes. Final album IDs enter the
existing result ledger before Bot delivery. Temporary tagged messages are
removed even on failure or cancellation; cleanup errors are logged and never
mask the original transfer failure. A hard engine crash can leave temporary
storage posts, while ordinary frontend reloads keep the engine alive.

Native forwarding still lacks the exact result receipts and spoiler handling
required by the direct-copy path, so that path remains an MTProto operation.

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
