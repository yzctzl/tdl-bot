# Installation and maintenance

[简体中文](deployment_zh.md) · [Home](../README.md) · [User guide](manual.md)

## Choose an installation

| Method | Best for | What you need |
| --- | --- | --- |
| uv tool | A personal server or desktop; guided setup | uv and an interactive terminal |
| Docker Compose | A service with persistent mapped folders | Docker Engine + Compose, or Docker Desktop using Linux containers |
| Git checkout | Running from source or making changes | Git and uv |

Python 3.12 or newer is required for native installations; the examples ask uv
to use 3.12. Docker includes Python. A separate Go installation is not required.

| Platform | Native installation | Docker |
| --- | --- | --- |
| Linux x64 / ARM64 | Supported; manual hot reload | Linux images for amd64 and arm64 |
| macOS Intel / Apple Silicon | Supported; manual hot reload | Docker Desktop, using Linux images |
| Windows | Native single-process runtime; no hot reload or automatic Bot crash restart | Docker Desktop with Linux containers, or Linux installation inside WSL2 |

The same collection, destination, retry, dump/follow and member-management features
are available in native Windows. Stop and restart to refresh code or logged-in
accounts there. CI checks application behavior across the three operating systems;
it does not perform real Telegram transfers or certify every OS/architecture combination.

## Before you begin

1. Create a Bot with **@BotFather** and keep its token private
2. Prepare a storage channel or group; a private channel keeps the workspace tidy
3. Add the Bot to that storage with posting permissions, and ensure the Telegram
   account you will sign in with can also post there
4. Choose a writable workspace you want to keep, outside uv's installation/cache
   directories; configuration, login sessions and saved state live there

That signed-in Telegram account reads sources and prepares content. The Bot sends
content to your final destination. Both need access to storage. You do not need
`TELEGRAM_API_ID` or `TELEGRAM_API_HASH` for the default tdl login mode.

## Install with uv tool

```sh
uv tool install --python 3.12 git+https://github.com/yzctzl/tdl-bot.git
mkdir tdl-bot-workspace
cd tdl-bot-workspace
tdl-bot setup -i
tdl-bot run
```

The wizard confirms timezone for Docker use, checks the Bot token, guides Telegram
sign-in, asks you to confirm administrators, and selects storage. You can use the
signed-in account as administrator without looking up its UID separately.
For storage, enter an ID or `@username`; enter `list` only if you want to see
channels/groups you own. It does not list all your conversations.

Missing tdl binaries are downloaded, checksum-checked and installed automatically
at login or startup. Keep network access to GitHub available for the first install.
Existing valid login sessions are reused. Setup checks permissions without posting
a test message. Press Enter to keep existing values when re-running the wizard;
completed steps are saved. Stop the running service before changing its setup.

The above commands also work in PowerShell. If `tdl-bot` is not found after tool
installation, follow uv's PATH instructions and reopen the terminal.

From a different directory, use an explicit configuration:

```sh
tdl-bot setup -i -d /path/to/workspace
tdl-bot run -e /path/to/workspace/.env
```

On Windows use your own path, for example `C:\Users\you\tdl-bot-workspace`.
Quote paths that contain spaces. Keep the run terminal open, or use your system's
service manager to run this same command with the same workspace and OS user.
The CLI does not install an operating-system service for you.

## Install with Docker Compose

No host Python, uv or tdl is needed. Start with an empty workspace and download
these files from the **same release or Git revision**:

- [`compose.yaml`](../compose.yaml)
- [`env.example`](../src/tdl_bot/resources/env.example), saved as `.env`

The packaged template is the single maintained configuration template.
An image is available after that version's release workflow publishes it; check
the repository's Releases and package page. To build an unpublished revision,
clone the repository, run `docker build -t tdl-bot:local .`, and set
`TDL_BOT_IMAGE=tdl-bot:local` in the workspace `.env`.

Create the mapped directories in your workspace:

```sh
mkdir -p data/tdl bin
```

PowerShell equivalent:

```powershell
New-Item -ItemType Directory -Force data/tdl, bin
```

Edit `.env`, keeping its default paths, and fill in:

| Setting | Value |
| --- | --- |
| `BOT_TOKEN` | Token from @BotFather |
| `TZ` | Your timezone's region/city name, such as `Asia/Taipei` or `Europe/London` |
| `ADMIN_USER_IDS` | Initial administrators' Telegram user IDs, comma-separated; login below prints your UID |
| `TDL_BOT_IMAGE` | Optional release image, such as `ghcr.io/yzctzl/tdl-bot:v0.1.0`; otherwise Compose uses `latest` |

On Linux/WSL2, also set `PUID` and `PGID` if the owner of the mapped folders is not
1000/1000. Obtain the values with `id -u` and `id -g`. Docker Desktop manages host
file sharing. Prefer WSL2's Linux filesystem rather than a Windows-mounted folder.

Run from the workspace:

```sh
docker compose run --rm bot login
# Fill ADMIN_USER_IDS in .env using the UID printed by login, then:
docker compose up -d
docker compose logs -f bot
```

Open the Bot and configure **Bot management → Change storage**, or use
`/storage @your_storage_channel`. The Bot and execution account both need access.
Interactive `setup -i` is for native installations; the supplied Compose deployment
uses the file/login flow above because its configuration mount is read-only.

### Your mapped files

| Host location | Container location | Contents |
| --- | --- | --- |
| Workspace directory | `/workspace`, read-only | `.env` and Compose configuration |
| `DATA_DIR` — default `./data` | `/data` | Database, task state, logs and temporary work |
| `BIN_DIR` — default `./bin` | `/tdl-bin` | Managed tdl executables |
| `TDL_DIR` — default `./data/tdl` | `/data/tdl` | Telegram login sessions |

Create custom host directories before starting. Host paths are resolved by
Compose; the application uses fixed Linux paths inside the container. A host
`TDL_BINARY` override is deliberately ignored in Compose. Do not run native and
container instances against the same workspace simultaneously.

`TZ` is required for Compose. Host `tdl-bot setup` fills it from the system;
a downloaded blank template needs your input. Use an IANA region/city name,
not `CST`, whose meaning is ambiguous. Docker Desktop does not automatically
provide the desktop timezone to the application. The image includes timezone
data; no `/etc/localtime` mount is needed. After changing `TZ`, recreate the
container with `docker compose up -d`.

## Run from a Git checkout

```sh
git clone https://github.com/yzctzl/tdl-bot.git
cd tdl-bot
uv sync --locked --python 3.12
uv run tdl-bot setup -i
uv run tdl-bot run
```

The workspace defaults to the project directory. To keep it elsewhere, add
`-d /path/to/workspace` to `setup -i` and `-e /path/to/workspace/.env` to runtime
commands. Existing workspaces skip initialization; the wizard can update their
values while retaining advanced settings. Without a wizard, `setup` generates
`.env`, which you must edit before `login` and `run`.

## Workspace and advanced settings

```text
workspace/
├── .env
├── data/
│   ├── state.sqlite3    # Settings, permissions, tasks and history
│   ├── tdl/            # Telegram authorizations
│   ├── jobs/           # Temporary media, cleaned after processing
│   └── engine.log      # Unix engine log
└── bin/                # Managed executables, separated by OS/architecture
```

The generated `.env` remains the configuration file; the wizard saves it for you.
Relative paths resolve against **its directory**, regardless of where you run the
command. Set `-e <file>` explicitly, or use `TDL_BOT_ENV_FILE`; otherwise the CLI
uses `.env` in the current directory. Shell environment variables override file
values. Shell variable interpolation inside application `.env` values is not supported.

| Setting | Default / purpose |
| --- | --- |
| `DATA_DIR`, `BIN_DIR` | Explicit paths; generated as `./data`, `./bin` |
| `TDL_DIR`, `TDL_NAMESPACE` | `./data/tdl`, `default` in the template; shared login location and namespace |
| `BOT_TOKEN`, `ADMIN_USER_IDS` | Bot token and required initial administrator UIDs |
| `TDL_PROXY` | Optional SOCKS5, SOCKS4 or HTTP proxy used by execution sessions |
| `WORKERS`, `QUEUE_SIZE` | 2 foreground workers and 20 waiting tasks; backups have a separate worker pool of the same size |
| `DOWNLOAD_TIMEOUT`, `UPLOAD_TIMEOUT` | 7200 seconds each; clone timeout includes both download and upload |
| `UPLOAD_CONCURRENCY` | 8 per-file transfer threads/window, accepted range 1–32 |
| `TDL_BINARY` | Optional native executable override instead of a managed binary |
| `RUNTIME_DIR` | Native default: `DATA_DIR`; choose a shorter local path if Unix socket paths are too long |
| `SESSION_SOURCE` | Default `tdl`; legacy `telethon` mode additionally requires `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` |

More workers do not guarantee more bandwidth. Operations sharing one execution
session are coordinated; Telegram limits and the transfer backend still apply.
Keep defaults unless you have a concrete reason to change them.

`ADMIN_USER_IDS` initializes an empty workspace only. After initialization,
manage members in the Bot; changing the variable does not reset database roles.

### Existing and additional logins

`tdl-bot login` writes to the configured `TDL_DIR`, not tdl's standalone default
home directory. To reuse an existing tdl login, set `TDL_DIR` to its **root**,
not its nested `data` folder. Nothing is copied from your home directory automatically.

For code or Telegram Desktop login:

```sh
tdl-bot login -m code
tdl-bot login -m desktop
```

To add a personal execution account, stop the service and sign in under another
namespace in the same session directory:

```sh
tdl-bot stop
tdl-bot login -n personal
tdl-bot run
```

This does not change the shared namespace. Accounts are matched to Bot operators
by verified Telegram UID. Each execution account needs storage access. Use one
network exit for a shared authorization; avoid running it on another machine or
through a different proxy at the same time. Protect the entire session directory.

## Reload, stop and update

| Action | Linux / macOS / Linux containers | Native Windows |
| --- | --- | --- |
| `reload` | Bot reconnects while active transfers continue; engine changes wait for an idle handoff | Unavailable |
| `stop` | Drains accepted tasks and stops the service | Drains tasks and stops the single process |
| Replace package, dependencies or image | Stop first, upgrade, start again | Stop first, upgrade, start again |

```sh
tdl-bot reload                  # Select the only running instance
tdl-bot reload -a               # Reload all instances owned by this OS user
tdl-bot reload -e ./work/.env    # Select one workspace
tdl-bot stop -e ./work/.env
```

Multiple running instances require `-a` or `-e`; ambiguous reloads change nothing.
`stop` uses the selected workspace rather than reload's automatic discovery.
There is no directory watcher. Reload is not a package update, does not relocate
data, and cannot change Docker mounts or container environment variables.

On Unix, Ctrl+C stops the foreground Bot supervisor; the engine may keep
transferring. Use `stop` for a full shutdown. On native Windows, one Ctrl+C asks
the single process to drain. Keep the terminal open until it finishes.

Upgrade a uv tool installation:

```sh
tdl-bot stop
uv tool upgrade tdl-bot
tdl-bot run
```

For source installations: stop, update the checkout, run `uv sync --locked`, then
start again. For Compose:

```sh
docker compose pull
docker compose stop
docker compose up -d
```

Container-internal manual reload uses `docker compose exec bot tdl-bot reload`.
**Replacing the image is not hot reload.** The supplied stop grace period is
24 hours (`TDL_STOP_GRACE_PERIOD`); Docker kills work that exceeds it. A stopped
or killed container cannot keep transferring in the background.

Update tdl separately using **Bot management → Update tdl** or `/update_tdl`.
The updater checks versions before downloading, waits for active use, verifies
the download and replaces the executable. It currently uses official tdl releases.

## Back up and move your workspace

Stop the service, then back up `.env`, the whole data directory and any separately
configured session directory. Preserve permissions and keep the backup private.
Restart after copying. Program files can be reinstalled; login sessions and the
database are the valuable state. Retain a pre-upgrade backup before schema changes;
older programs reject newer database versions.

To move paths, stop with the **old** configuration, move files, update `.env`
and any Docker mounts, then start. Do not share live databases or sessions
between machines, containers or native installations.

## Troubleshooting

| Problem | Next step |
| --- | --- |
| Configuration not found | Run in the workspace or pass `-e /path/to/.env` |
| Setup says configuration exists | Use `setup -i` to edit it; plain `setup` never overwrites it |
| Missing tdl cannot be downloaded | Check GitHub access/proxy and binary-folder write permission |
| Login missing after using standalone tdl | Match `TDL_DIR` and namespace, or log in using `tdl-bot login` |
| Another instance owns a resource | Stop the old instance; do not delete locks to bypass active ownership |
| Container cannot write mapped files | Check folder creation and Linux `PUID`/`PGID` |
| Proxy works on host but not in Docker | Container `127.0.0.1` is the container itself; use an endpoint reachable from it |
| Timezone missing or incorrect | Verify system timezone natively; set Compose `TZ` and recreate its container |
| Native Windows rejects reload | Use `stop`, then `run`; Docker/WSL2 provides the Unix lifecycle |
| Long Unix socket path | Use a shorter local `RUNTIME_DIR`, after stopping the old instance |

`tdl-bot health` checks local process liveness, not Telegram reachability.
Use `tdl-bot help`, `tdl-bot help login` or `tdl-bot login help` without needing
a configured workspace. Logs may contain account identifiers; redact credentials
and private content before sharing diagnostics.

For in-chat failures, see the [user guide](manual.md#troubleshooting).
