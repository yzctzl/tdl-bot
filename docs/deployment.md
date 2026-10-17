# Installation and maintenance guide

[简体中文](deployment_zh.md) · [Home](../README.md) · [Owner's guide](manual.md)

This guide takes you from an empty workspace to your own working Telegram Bot.
You will have a Bot, a signed-in Telegram execution account and a permanent folder
for configuration and data. Everyday collection, tracking and management then
happen inside Telegram.

## Choose your route

For a first deployment with uv already installed and no need for containers,
start with **uv tool and the interactive wizard**. Choose Docker Compose if you
prefer managing a persistent container service. Use a Git checkout if you want
to modify the code or run directly from the repository. Choose one route; you
do not need to install all three.

| Method | What the host needs | Configuration | Instructions |
| --- | --- | --- | --- |
| uv tool | uv and access to GitHub | Guided terminal setup | [Use uv tool](#use-uv-tool) |
| Docker Compose | Docker Engine + Compose, or Docker Desktop with Linux containers | Edit configuration, then sign in | [Use Docker Compose](#use-docker-compose) |
| Git checkout | Git, uv and access to GitHub | The same terminal wizard | [Use a Git checkout](#use-a-git-checkout) |

Read the reference sections when needed:
[Platforms](#platform-support) · [Workspace](#workspace-and-configuration) ·
[Stopping and upgrades](#stop-reload-and-upgrade) · [Troubleshooting](#troubleshooting)

## Before you begin

### 1. Create your Bot

Open **@BotFather** in Telegram, send `/newbot`, and follow the prompts to choose
a Bot name and username. Save the **Bot token** it provides: you will paste it
into the setup wizard or `.env`. Keep the Bot's `@username` too, so you can add
it to storage and open its private chat afterward.

The token controls access to your Bot. Do not post it in groups, publish it in
screenshots or commit it to a repository.

### 2. Prepare internal storage

Create a private Telegram channel with a memorable name, such as “Collection
storage”. Your signed-in account must be able to post there. Open the channel's
administrator settings, add your new Bot as an administrator, and enable posting.
Disable content protection/restricted saving so the Bot can copy messages from it.

This channel holds saved content and necessary activity records; it does not have
to be your collaborators' collection channel. You can set another channel/group
as your final destination after startup. Keep delivery in the Bot chat for the
first test.

You do not need to look up a storage ID before using the wizard:

- For a public channel, enter its `@username`
- For a private channel, enter `list` at wizard step four, find it among the
  channels/groups you own, and copy the displayed ID
- The manual Docker flow has no wizard list; use the method below for a private
  channel ID, without making the channel public

To obtain a private channel ID, post a message in it and copy that message's link.
For example, in `https://t.me/c/1234567890/1`, `1234567890` is the channel number.
Prefix it with `-100` to get `-1001234567890`. The final `1` is a message ID, not
part of the channel ID. This illustrates the format; use the number from your own link.

### 3. Choose the Telegram account to sign in

Have a Telegram user account ready that can post to storage. The default flow
signs in by QR code and keeps the login on your machine. This account reads
sources and prepares content; the Bot handles interaction and sends the results.

For the simplest setup, sign in as yourself and confirm the same account as
administrator. You will not need to look up your own UID separately or provide
`TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.

### 4. Choose a workspace

The workspace holds `.env`, the task database, login sessions and managed
executables. Use a writable location you intend to keep, outside uv's cache and
tool-installation folders. Updating the application does not require a new
workspace or a fresh login.

## Use uv tool

Run these steps in a terminal on the machine that will host the Bot. Use a shell
on Linux/macOS or PowerShell on Windows. The example creates a folder named
`tdl-bot-workspace` in your current directory; choose another name if you prefer.

### 1. Install and enter the workspace

```sh
uv tool install --python 3.12 git+https://github.com/yzctzl/tdl-bot.git
mkdir tdl-bot-workspace
cd tdl-bot-workspace
```

Continue configuration and startup from this directory. If `tdl-bot` is not found,
follow uv's PATH instructions, reopen the terminal, then return to the workspace.
You do not need to install Go or build tdl yourself.

### 2. Start the interactive wizard

```sh
tdl-bot setup -i
```

The wizard first confirms the timezone for Docker use; native runs continue to
follow the system timezone. Then it guides you through four steps:

| Step | What you enter or do | What confirms success |
| --- | --- | --- |
| 1/4 Bot | Paste the token from @BotFather; input is hidden | The Bot's verified `@username` |
| 2/4 Telegram login | Scan the terminal QR code with your chosen Telegram account | Login completes; an existing valid session is reused |
| 3/4 Administrator | On first setup, check the displayed account and press Enter to use it; other administrators need numeric UIDs | Administrator settings are saved |
| 4/4 Internal storage | Enter the channel's `@username` or ID; enter `list` to see locations you own | Permissions are verified and setup prints the startup command |

A missing tdl executable is downloaded, verified and installed automatically;
first setup needs GitHub access. Completed settings are saved. If interrupted,
run `setup -i` again to continue, pressing Enter to keep existing values.
Permission checks do not post test media. If step four fails, check that the Bot
is a channel administrator, rather than only adding your user account there.

### 3. Start the service

```sh
tdl-bot run
```

Keep this terminal running for the Bot to respond. In Telegram, open your Bot,
send `/start`, check administrator access and complete the
[first-save walkthrough](manual.md#first-use). A quiet terminal alone is not a
complete check; save a small video or photo yourself.

To configure or run from another directory, specify the path:

```sh
tdl-bot setup -i -d /path/to/workspace
tdl-bot run -e /path/to/workspace/.env
```

On Windows, use your own path, such as `C:\Users\you\tdl-bot-workspace`, and quote
paths containing spaces. To keep running after closing a terminal, use your
system's service manager with the same command, OS user and workspace. tdl-bot
does not install a system service automatically.

## Use Docker Compose

The host does not need Python, uv or tdl. This route uses manual configuration,
rather than the interactive wizard; work through each step in order.

### 1. Prepare the folder and two files

Create and enter your workspace. Download these files from the **same release
or Git revision** of the repository:

- [`compose.yaml`](../compose.yaml), keeping its filename
- [`env.example`](../src/tdl_bot/resources/env.example), renamed to `.env`, without
  the `.example` extension

Create the data directories inside the workspace:

```sh
mkdir -p data/tdl bin
```

PowerShell equivalent:

```powershell
New-Item -ItemType Directory -Force data/tdl, bin
```

You should now have `.env`, `compose.yaml`, `data/tdl/` and `bin/`.

### 2. Fill in the initial settings

Open `.env` in a text editor, keep the template paths, and fill in:

| Setting | What to enter |
| --- | --- |
| `BOT_TOKEN` | The token from @BotFather |
| `TZ` | Your region/city timezone, such as `Asia/Taipei` or `Europe/London` |
| `ADMIN_USER_IDS` | Leave blank for now; login prints your UID in the next step |
| `TDL_BOT_IMAGE` | Optional published version image; otherwise uses `latest` |

On Linux/WSL2, run `id -u` and `id -g` to check directory ownership. If these differ
from the default 1000/1000, add the corresponding `PUID` and `PGID` values to `.env`.
Docker Desktop handles host file sharing. In WSL2, prefer its Linux filesystem
for the workspace.

Before first installation, check that the selected repository Release has published
its image. For unpublished source, see [Images and mapped folders](#images-and-mapped-folders).

### 3. Sign in and set the administrator UID

From the workspace containing `compose.yaml`, run:

```sh
docker compose run --rm bot login
```

Scan the QR code with your chosen Telegram account. Note the account UID printed
when login completes. Return to `.env`, enter that number as `ADMIN_USER_IDS`,
and save. Multiple initial administrators use comma-separated numbers; these are
numeric IDs, not `@usernames`.

### 4. Start and configure storage

```sh
docker compose up -d
docker compose logs -f bot
```

`up -d` runs the service in the background. Leaving log-follow mode does not stop
the container. In Telegram, open the Bot chat, send `/start`, and choose a language.
Select **Bot management → Change storage** and send your prepared channel's
`@username` or ID. Content processing requires internal storage to be configured.

The supplied Compose setup mounts configuration read-only, so it does not use
an in-container `setup -i` wizard. Now complete the
[first-use walkthrough](manual.md#first-use), then configure your collection destination.

### Images and mapped folders

Published images target Linux amd64/arm64; Docker Desktop also uses Linux containers.
To pin a version, add `TDL_BOT_IMAGE=ghcr.io/yzctzl/tdl-bot:v0.1.0` to `.env`,
replacing the example with an available release. For unpublished code, clone the
repository, run `docker build -t tdl-bot:local .` from its root, then set
`TDL_BOT_IMAGE=tdl-bot:local`.

| Host location | Container location | What it holds |
| --- | --- | --- |
| Workspace directory | `/workspace`, read-only | Configuration |
| `DATA_DIR`, default `./data` | `/data` | Database, tasks, logs and temporary files |
| `BIN_DIR`, default `./bin` | `/tdl-bin` | Automatically installed tdl executables |
| `TDL_DIR`, default `./data/tdl` | `/data/tdl` | Telegram login state |

Create custom host locations before setting their paths in `.env`. Docker uses
a Linux executable inside the container; a host `TDL_BINARY` override is ignored.
Do not run a native instance and container on the same workspace simultaneously.

Container timezone comes from `.env`'s `TZ`, not automatically from desktop
settings. Use a region/city name rather than ambiguous `CST`. The image includes
timezone data, without mounting system timezone files. After changing `TZ`,
recreate the container with `docker compose up -d`.

## Use a Git checkout

Choose this route to manage repository files directly. Install dependencies,
then use the same wizard:

```sh
git clone https://github.com/yzctzl/tdl-bot.git
cd tdl-bot
uv sync --locked --python 3.12
uv run tdl-bot setup -i
uv run tdl-bot run
```

The four setup steps are the same as [uv tool](#use-uv-tool). By default, the project
directory is your workspace. For subsequent terminal commands, use `uv run tdl-bot`
in place of `tdl-bot`. To keep the workspace elsewhere, add `-d` to setup and `-e`
to run, login and stop commands. Without the wizard, plain `setup` only generates
`.env`; you must edit it, sign in and start the service yourself.

## Platform support

Native installation requires Python 3.12 or newer; the examples request 3.12 from
uv. Docker includes Python. None of the three installation methods requires Go.

| Platform | Native runtime | Docker |
| --- | --- | --- |
| Linux x64 / ARM64 | Supported; manual hot reload | Linux amd64/arm64 images |
| macOS Intel / Apple Silicon | Supported; manual hot reload | Linux images through Docker Desktop |
| Windows | Collection, retries, tracking, history backups and management; no hot reload or automatic Bot crash restart | Linux containers through Docker Desktop, or Linux installation inside WSL2 |

Native Windows needs a stop/start to load code changes or newly signed-in accounts.
Use a local folder writable by your user and protect sessions with Windows file
permissions. CI checks application behavior on three operating systems, without
real Telegram transfers or hardware validation of every architecture combination.

## Workspace and configuration

### What to keep

```text
workspace/
├── .env
├── data/
│   ├── state.sqlite3
│   ├── tdl/
│   ├── jobs/
│   └── engine.log
└── bin/
```

`.env` holds configuration, `state.sqlite3` holds permissions, destinations, tasks
and history, and `tdl/` holds login authorizations. `jobs/` contains temporary media
cleaned after processing. `engine.log` supports Unix engine troubleshooting;
executables in `bin/` can be reinstalled. Do not delete the whole `data/` folder
as if it were a temporary downloads folder.

The wizard fills `.env` for you; the file is still needed. Relative paths resolve
against **its directory**, not the terminal's current directory. Run from the
workspace or pass `-e /path/to/.env` when configuration is not found.
`TDL_BOT_ENV_FILE` can also select the file. Explicit process environment values
override file values; the application does not expand shell variables inside `.env`.

### Reuse or add logins

Standalone `tdl login` normally saves authorization in its own home-directory
location. `tdl-bot login` uses `.env`'s `TDL_DIR`, defaulting to `./data/tdl` in a
new workspace. The two commands may therefore use different locations; the Bot
does not copy authorization from your home directory automatically. To reuse an
existing session, stop the old service and point `TDL_DIR` at tdl's root directory,
not its nested `data` folder.

For code login or Telegram Desktop import:

```sh
tdl-bot login -m code
tdl-bot login -m desktop
```

For a separate login belonging to another account, native installations use:

```sh
tdl-bot stop
tdl-bot login -n personal
tdl-bot run
```

Docker equivalent:

```sh
docker compose stop
docker compose run --rm bot login -n personal
docker compose up -d
```

Sign in with the other Telegram account. Replace `personal` with a unique,
memorable name if you prefer. This does not replace the shared login. See
[execution-account management](manual.md#manage-execution-accounts) for automatic
selection. Do not run one authorization simultaneously on different machines or
network exits. Each account needs internal-storage access.

### Settings reference, when needed

Keep the wizard defaults unless you have a specific reason to change them.

| Setting | Default / purpose |
| --- | --- |
| `DATA_DIR`, `BIN_DIR` | Explicit paths; template uses `./data`, `./bin` |
| `TDL_DIR`, `TDL_NAMESPACE` | Template uses `./data/tdl`, `default`; shared login location and name |
| `ADMIN_USER_IDS` | Initial administrator numeric UIDs; manage roles in the Bot afterward |
| `TDL_PROXY` | Optional SOCKS5/SOCKS4/HTTP proxy for execution accounts |
| `WORKERS`, `QUEUE_SIZE` | 2 foreground workers and 20 waiting tasks; backups have a separate worker pool of the same size |
| `DOWNLOAD_TIMEOUT`, `UPLOAD_TIMEOUT` | 7200 seconds each; clone timeout covers download and upload together |
| `UPLOAD_CONCURRENCY` | Default 8; per-file transfer threads/window, range 1–32 |
| `TDL_BINARY` | Native override for an existing executable instead of the managed location |
| `RUNTIME_DIR` | Native default: `DATA_DIR`; choose a short local path if socket paths are too long |
| `SESSION_SOURCE` | Default `tdl`; only legacy `telethon` mode additionally needs API ID/hash |

More workers do not guarantee full bandwidth; account coordination, network and
Telegram limits still apply. `TDL_PROXY` does not change every connection made by
the host or container.

## Stop, reload and upgrade

### Stop completely

Run the appropriate command from your workspace:

| Installation | Stop | Start again |
| --- | --- | --- |
| uv tool | `tdl-bot stop` | `tdl-bot run` |
| Git checkout | `uv run tdl-bot stop` | `uv run tdl-bot run` |
| Docker | `docker compose stop` | `docker compose up -d` |

Stopping waits for accepted tasks to finish and clean up, so it may take time.
Wait before changing files, moving folders or shutting down the machine. On Unix,
Ctrl+C stops the foreground Bot supervisor, but the engine may keep working;
use `stop` for full shutdown. On native Windows, one Ctrl+C requests a complete
drain; keep the terminal open until it finishes.

### Reload manually

Linux, macOS and Linux containers support manual reload: the Bot interface
restarts while active transfers continue. Native Windows uses stop/start instead.

```sh
tdl-bot reload
tdl-bot reload -a
tdl-bot reload -e /path/to/workspace/.env
```

One running instance is selected automatically. With several, `-a` reloads all
and `-e` selects one. Source installations prepend `uv run`; Docker uses
`docker compose exec bot tdl-bot reload`. There is no file watcher. Reload does
not upgrade packages, replace images, move data or change container environment
values. Task-engine updates wait for existing work to become idle before handoff.

### Upgrade the application

Back up first, especially for a release with database changes. For uv tool,
run from the workspace:

```sh
tdl-bot stop
uv tool upgrade tdl-bot
tdl-bot run
```

For source: stop, update the checkout, run `uv sync --locked`, then start.
For Docker:

```sh
docker compose pull
docker compose stop
docker compose up -d
```

**Replacing a Docker image is not hot reload.** The default stop grace period is
24 hours, configurable with `TDL_STOP_GRACE_PERIOD`; Docker forcibly stops work
that exceeds it. A killed process cannot keep transferring. After restart,
inspect the destination before resolving any unconfirmed send.

“Update tdl” in Bot management updates only the transfer program, not tdl-bot.

## Backup and migration

1. Stop completely using the original configuration and wait for shutdown
2. Copy `.env`, the entire data directory and any separately located session
   directory, preserving file permissions
3. Protect the backup: it contains your Bot token and Telegram authorization
4. Start the service after the copy finishes

To move to another machine or directory, stop before moving files, update `.env`
paths and any Docker mounts, then start. Never have two instances use the same
data or session simultaneously. Older code may reject an upgraded database;
retain and use a matching pre-upgrade backup when rolling back.

## Troubleshooting

| Symptom | Check first | What to do |
| --- | --- | --- |
| Configuration not found | Whether the terminal is in the workspace | Enter it or pass `-e` explicitly |
| Setup says configuration exists | Whether `.env` was already generated | Continue with `setup -i`; plain setup never overwrites |
| Administrator buttons missing | Whether you are using the initial administrator account | Open the Bot as that account; manage existing database roles in User management |
| tdl download fails | GitHub connectivity and binary-folder permissions | Fix network/proxy or write access, then retry login/startup |
| Login reported missing | Whether `TDL_DIR` and login name match | Run `tdl-bot login` with the same configuration, or point to the existing directory |
| Container folder is not writable | Folder existence and Linux UID/GID | Create it and correct `PUID`/`PGID` |
| Host proxy works, container proxy fails | Whether the endpoint is the container's own `127.0.0.1` | Use an endpoint reachable from the container |
| Wrong timezone | Native system timezone or Compose `TZ` | Correct system settings, or set `TZ` and recreate the container |
| Resource already owned | Whether an old instance is still running | Stop it; do not delete locks to bypass ownership |
| Unix socket path too long | Workspace path depth | Stop, then select a shorter local `RUNTIME_DIR` |

`tdl-bot health` checks local liveness, not Telegram connectivity. Use
`tdl-bot help`, `tdl-bot help login` or `tdl-bot login help` before configuration
if needed. When asking for help, include OS, installation method, steps and error
logs, removing tokens, login credentials and private content first.
See the [owner's guide](manual.md#troubleshooting) for in-chat delivery and retry issues.
