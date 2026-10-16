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
tdl-bot login -e /path/to/workspace/.env
tdl-bot run -e /path/to/workspace/.env
tdl-bot reload -e /path/to/workspace/.env
tdl-bot stop -e /path/to/workspace/.env
```

Use `tdl-bot help` to list commands. `tdl-bot help login`,
`tdl-bot login help`, and `tdl-bot login -h` (or `--help`) display identical
command help and work before configuration. Login options `--namespace` and
`--login-method` belong after `login`; `--directory` belongs after `setup`.
Short options are `-e` (env file), `-d` (workspace), `-n` (namespace), and
`-m` (login method). `setup` defaults to the current directory; `login`
defaults to QR sign-in. Long options remain supported.
`--env-file` works before or after runtime commands. Running without a command
still starts the Bot.

`TDL_BOT_ENV_FILE` is an alternative to `--env-file`. Otherwise the CLI uses
`.env` in the current directory. All spawned processes inherit the resolved
configuration filename. Use the same workspace for login, run and stop.

`tdl-bot reload` automatically selects the only running instance owned by the
current system user, regardless of the current `.env`. With multiple instances,
use `tdl-bot reload -a` for all or `tdl-bot reload -e <workspace>/.env` for one.
An ambiguous command lists configurations and reloads nothing. Each selected
instance reports whether its reload request succeeded; failures do not prevent
other selected instances from receiving requests. `-a` and `-e` cannot be combined.

New supervisors register configuration/runtime paths and their PID (no tokens)
in `$XDG_STATE_HOME/tdl-bot/instances` (default `~/.local/state/tdl-bot/instances`).
Records are removed on exit; stale records are ignored using the supervisor lock
and live PID. Older supervisors can be discovered from `.env` in the current
directory or immediate child directories. For an older instance elsewhere, use
`-e` until its next full stop/start. Containers only discover instances within
their own filesystem/process environment.
`setup` creates a template and refuses to replace an existing `.env`.
Use `tdl-bot setup -i` for native interactive onboarding, or
`setup -i -d <workspace>` for another directory. It verifies the Bot token,
reuses or creates a native tdl login, explicitly confirms administrators,
and accepts a storage channel/group UID or @username. Enter `list` to display
only channels/groups owned by the signed-in execution account; nothing is
listed automatically. No UID lookup is
needed when the signed-in account is the administrator. Both Bot and execution
account permissions are checked without sending test messages. Add the Bot to
storage with posting permissions first.

Enter keeps existing values; completed steps are saved with private file
permissions. Existing database membership and advanced settings are preserved.
Stop the service before interactive setup. The Compose workspace is read-only;
its existing manual configuration/login flow remains required for containers.

Before changing data, session, binary or runtime locations, stop the old instance
with its original configuration. Move data explicitly, edit the paths, then
start again. An in-container reload can apply Bot settings and refresh accounts;
it does not relocate bind mounts or change the container's environment.

## Docker Compose

Install Docker Engine with Compose on Linux, or Docker Desktop with Linux
containers on Windows/macOS. No host Python, uv or tdl installation is required.
Images target `linux/amd64` and `linux/arm64`.

Create a workspace. Download `compose.yaml` and the configuration template
[`src/tdl_bot/resources/env.example`](../src/tdl_bot/resources/env.example)
from the same revision of this repository. Save the template as `.env` in the
workspace. This packaged resource is the only maintained template; native
`tdl-bot setup` generates `.env` from it. Published images become available after the container
workflow succeeds for a manually pushed version tag (for example `v0.1.0`). Before then, build locally
with `docker build -t tdl-bot:local <repository>` and set
`TDL_BOT_IMAGE=tdl-bot:local` in `.env`.

Linux/macOS/WSL2:

```sh
mkdir -p data/tdl bin
```

Windows PowerShell:

```powershell
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
stop the service and run `docker compose run --rm bot login -n personal`.

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
tdl-bot setup -d /path/to/workspace
```

Edit the generated `.env`, then use the explicit `--env-file` commands above, or
change into the workspace and run `tdl-bot login` followed by `tdl-bot run`.
The workspace can be placed on a user-selected disk. It must not be created
inside uv's tool environment or cache.

Package upgrades and dependency replacement use a graceful stop/start:

```sh
tdl-bot stop -e /path/to/workspace/.env
uv tool upgrade tdl-bot
tdl-bot run -e /path/to/workspace/.env
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
uv run tdl-bot setup
# Edit .env with paths, BOT_TOKEN, and ADMIN_USER_IDS.
uv run tdl-bot login
uv run tdl-bot run
```

For an existing `.env`, skip `setup` and keep explicit `DATA_DIR` plus `BIN_DIR`
or the existing `TDL_BINARY`. The existing Telegram session location is reused.
The first upgrade to explicit configuration should use `stop` then `run`, so
an old supervisor that exported dotenv values does not keep stale settings.

Code/configuration development uses manual `uv run tdl-bot reload`. A version
upgrade that also changes dependencies should stop first, update the checkout,
run `uv sync --locked`, then start. No deployment mode watches file changes.

## CI and publishing

`checks.yaml` runs on branch pushes and pull requests, and can also be called
by the release workflow. It runs Ruff, Ty, offline tests and package checks on
Linux/macOS, including a built-wheel uv tool install outside the checkout.
Ordinary commits and pull requests do not build Docker images or publish releases.

`container.yaml` triggers **only when a `vX.Y.Z` Git tag is pushed**. It validates
that the tag matches `project.version` in `pyproject.toml`, then runs the reusable
checks. After checks and Docker smoke checks pass, it builds/pushes
`linux/amd64` and `linux/arm64` images to GHCR. A final job creates the GitHub
Release for that existing tag and attaches the wheel and source archive. The
workflow never creates Git tags or publishes to PyPI.

For a release, update `project.version`, run `uv lock`, and commit both files
before manually creating/pushing the matching tag:

```sh
# Example: pyproject.toml must already declare version = "0.1.0".
git tag v0.1.0
git push origin v0.1.0
```

Stable `vX.Y.Z` tags are supported; other tag names are not release versions.
The release workflow has no manual-dispatch or branch-push release path.
Rerunning a tagged workflow can refresh its existing release assets.

Each release produces `vX.Y.Z`, `X.Y.Z`, `latest`, and `sha-<full-commit>` image
tags. Set `TDL_BOT_IMAGE=ghcr.io/yzctzl/tdl-bot:v0.1.0` to select a version.
Publishing uses `GITHUB_TOKEN`, scoped to package writes for the image job and
repository contents writes for the release job. Public pull access may require
making the GHCR package public once in GitHub's package settings. Forks should
set `TDL_BOT_IMAGE` to their own GHCR image.

Real Telegram transfers are not part of CI. Native Windows runtime support
remains a separate future change.
