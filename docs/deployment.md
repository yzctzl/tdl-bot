# Deployment

Choose one installation method. All three use a workspace that you own, with an
explicit `.env` file. Program installation, workspace configuration, and Telegram
authorization have independent lifetimes.

| Host | Docker | Native uv tool / source |
| --- | --- | --- |
| Linux x86-64 / ARM64 | Supported deployment target | Supported |
| macOS Intel / Apple Silicon | Linux containers through Docker Desktop | CI covers native macOS |
| Windows | Docker Desktop in Linux-container mode, or WSL2 | Native Windows is not supported yet |

The repository includes Linux/macOS checks and multi-architecture image builds.
A configured workflow is not evidence that it has already run successfully on
GitHub; check the Actions results for the revision you deploy.

## Workspace and configuration

A new workspace looks like this after initialization and login:

```text
workspace/
├── .env
├── compose.yaml          # Only needed for Compose deployments
├── data/
│   ├── state.sqlite3
│   ├── tdl/              # Telegram namespaces and authorization
│   ├── jobs/             # Owned temporary transfer directories
│   └── engine.log
└── bin/
    └── linux-amd64/tdl   # Selected by runtime OS and architecture
```

- Every start requires an existing `.env`; missing files produce an actionable
  error rather than silently selecting another workspace.
- `DATA_DIR` and `BIN_DIR` must be explicit. An existing `TDL_BINARY` can replace
  `BIN_DIR` for native installations.
- Relative paths resolve against the directory containing `.env`, including
  paths supplied through environment overrides. `~` and absolute paths work.
- Environment variables override file values. File values are not exported into
  the parent process, so child processes re-read file edits on manual reload.
  Shell-style variable interpolation inside the application `.env` is not used.
- `TDL_DIR` is independent of `DATA_DIR`; new templates use `./data/tdl`. Existing
  `TDL_DIR=.tdl` and custom `TDL_BINARY` values remain valid for native deployment.
- Native managed binaries have separate OS/architecture directories, so a macOS
  executable cannot accidentally replace the Linux executable used in Docker.
- `RUNTIME_DIR` controls sockets, frontend PID and polling/supervisor locks. Native
  runs default to `DATA_DIR`. Compose uses a private container-local tmpfs.
  For deep workspaces, choose a short local `RUNTIME_DIR` to stay within the
  operating system's Unix socket path limit. Resource locks protecting the database, sessions and binary stay with those
  resources, preventing two instances from using them concurrently.

From anywhere, select a configuration explicitly:

```sh
tdl-bot --env-file /path/to/workspace/.env login
tdl-bot --env-file /path/to/workspace/.env run
tdl-bot --env-file /path/to/workspace/.env reload
tdl-bot --env-file /path/to/workspace/.env stop
```

`TDL_BOT_ENV_FILE` is an alternative to `--env-file`. Otherwise the CLI uses
`.env` in the current directory. All spawned processes inherit the resolved
configuration filename. Use the same workspace for login, run, reload and stop.
`init` never replaces an existing `.env`.

Before changing data, session, binary or runtime locations, stop the old instance
with its original configuration. Move data explicitly, edit the paths, then
start again. An in-container reload can apply Bot settings and refresh accounts;
it does not relocate bind mounts or change the container's environment.

## Docker Compose

Install Docker Engine with Compose on Linux, or Docker Desktop with Linux
containers on Windows/macOS. No host Python, uv or tdl installation is required.
Images target `linux/amd64` and `linux/arm64`.

Create a workspace. Place `compose.yaml` and `.env.example` from the same revision
of this repository in it. Published images become available after the container
workflow succeeds on the repository's default branch. Before then, build locally
with `docker build -t tdl-bot:local <repository>` and set
`TDL_BOT_IMAGE=tdl-bot:local` in `.env`.

Linux/macOS/WSL2:

```sh
cp .env.example .env
mkdir -p data/tdl bin
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force data/tdl, bin
```

Edit `.env`. Keep the explicit paths, fill in `BOT_TOKEN`, and obtain your UID
through login before filling `ADMIN_USER_IDS` for the first Bot start. Existing
workspace membership remains authoritative after initialization.

On Linux/WSL2, the container's UID/GID must match the owner of the mounted files.
The defaults are 1000/1000. Check `id -u` and `id -g`; set `PUID` and `PGID` in
`.env` if different. Windows/macOS Docker Desktop handles host file sharing.
For WSL2 deployment, prefer its Linux filesystem for the workspace.

Run these commands from the directory containing `compose.yaml` and `.env`:

```sh
docker compose run --rm bot login
# Fill ADMIN_USER_IDS in .env, then:
docker compose up -d
docker compose logs -f bot
```

Login automatically installs a missing tdl into the mapped binary directory
using the existing checksum/version verification. Interactive QR/code login
uses the same mounted session directory as the service. For another namespace,
stop the service and run `docker compose run --rm bot login --namespace personal`.

The Compose file maps:

| Host path | Container path | Purpose |
| --- | --- | --- |
| Workspace directory | `/workspace`, read-only | Read `.env`; directory mounting also sees atomic editor replacements |
| `DATA_DIR` | `/data`, writable | Database, checkpoints, downloads, logs |
| `BIN_DIR` | `/tdl-bin`, writable | Managed tdl executables and updates |
| `TDL_DIR` | `/data/tdl`, writable | Existing or new Telegram authorizations |
| Container tmpfs | `/run/tdl-bot` | Private local process communication |

Create configured host directories before running Compose. Container environment
values deliberately override host-specific paths in `.env`; a host `D:/...` or
`/Users/...` path is used by Compose for the mount, not as a Linux application
path. Compose selects the container's managed Linux tdl rather than a native
`TDL_BINARY` override. Do not run native and container instances on one workspace
at the same time.

Proxy addresses must be reachable from the container. A host proxy bound to
`127.0.0.1` is not the container's loopback address. Configure a reachable proxy
endpoint when using `TDL_PROXY`.

### Manual reload and image upgrades

**Docker supports manual hot reload inside the running container:**

```sh
docker compose exec bot tdl-bot reload
```

The frontend restarts immediately. Existing tdl processes continue in the
resident engine, which checkpoints and hands off at task/unit boundaries.
There is no source-directory watcher. Application code comes from the image;
reload does not fetch or install another image.

**Replacing the image is a graceful restart, not hot reload:**

```sh
docker compose pull
docker compose stop
docker compose up -d
```

On stop, the supervisor first stops polling and its engine health monitor. It
then requests engine shutdown: dump/follow scheduling quiesces after the current
unit, and accepted URL jobs finish. The container remains alive until the engine
releases its instance lock after database closure and client disconnection.
`TDL_STOP_GRACE_PERIOD` defaults to `24h`; choose a value long enough for your
accepted workload. Docker forcibly terminates the container if that deadline
expires. Interrupted uncertain writes require manual inspection/resume; saving
checkpoints does not make a killed tdl process continue.

Pulling alone does not change a running container. Use `TDL_BOT_IMAGE` to select
an immutable `sha-<full-commit>` image instead of `latest`. Database schema changes
may require restoring a pre-upgrade backup when rolling back to older code.

## uv tool: Linux and macOS, or inside WSL2

Until a PyPI release is published, install directly from GitHub:

```sh
uv tool install --python 3.12 git+https://github.com/yzctzl/tdl-bot.git
tdl-bot init --directory /path/to/workspace
```

Edit the generated `.env`, then use the explicit `--env-file` commands above, or
change into the workspace and run `tdl-bot login` followed by `tdl-bot run`.
The workspace can be placed on a user-selected disk. It must not be created
inside uv's tool environment or cache.

Package upgrades and dependency replacement use a graceful stop/start:

```sh
tdl-bot --env-file /path/to/workspace/.env stop
uv tool upgrade tdl-bot
tdl-bot --env-file /path/to/workspace/.env run
```

A package upgrade does not automatically switch a running Python process.
`uv tool upgrade` may replace dependencies still needed by an old engine, so
finish the stop before upgrading. The external workspace survives reinstalling
or uninstalling the tool. Ordinary manual `reload` preserves active work; it is
not a guarantee that an in-place replacement of the Python environment is safe.

The source project's `uv.lock` is used for source and image builds. Tool installs
resolve dependencies from the package metadata; CI therefore also checks a real
built-wheel tool install outside the checkout.

## Run from a Git checkout

```sh
git clone https://github.com/yzctzl/tdl-bot.git
cd tdl-bot
uv sync --locked --python 3.12
uv run tdl-bot init --directory .
# Edit .env with paths, BOT_TOKEN, and ADMIN_USER_IDS.
uv run tdl-bot login
uv run tdl-bot run
```

For an existing `.env`, skip `init` and keep explicit `DATA_DIR` plus `BIN_DIR`
or the existing `TDL_BINARY`. The existing Telegram session location is reused.
The first upgrade to explicit configuration should use `stop` then `run`, so
an old supervisor that exported dotenv values does not keep stale settings.

Code/configuration development uses manual `uv run tdl-bot reload`. A version
upgrade that also changes dependencies should stop first, update the checkout,
run `uv sync --locked`, then start. No deployment mode watches file changes.

## CI and publishing

`checks.yaml` runs Ruff, Ty, the offline test suite, and package builds on Linux
and macOS. It installs the built wheel with uv tool and initializes a workspace
outside the checkout. `container.yaml` runs those checks before Docker smoke
checks and multi-architecture publishing to GHCR. Pull requests build/test only;
publication uses the repository's default branch and `GITHUB_TOKEN` with package
write permission. Public pull access may require making the GHCR package public
once in GitHub's package settings. Forks should set `TDL_BOT_IMAGE` to their own
GHCR image.

Images receive `latest` and `sha-<full-commit>` tags. Workflows do not create Git
tags or publish to PyPI, and real Telegram transfers are not part of CI.
Native Windows runtime support remains a separate future change.
