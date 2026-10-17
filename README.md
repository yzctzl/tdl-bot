# tdl-bot

**Send a link. Keep the whole album.**

[简体中文](README_zh.md) · [Owner's guide](docs/manual.md) · [Installation](docs/deployment.md)

Run your own Telegram collection Bot. Save whole albums, videos and captions,
choose where they arrive, and follow channels without checking for updates yourself.
Set it up with a terminal wizard, then do everyday work through buttons in Telegram.

Built for people who want to own and manage their collection: use it alone, from
several of your accounts, or with a small circle of trusted collaborators.

## Less work between finding and keeping

- **One link, the whole album** — send a link to any member of an album to collect
  its photos, videos and captions together, preserving their order and text formatting
- **Your collection, your destination** — receive content in the Bot chat, a group,
  a channel or a forum topic
- **Keep up without checking** — follow new posts on a schedule, or run a
  history backup to collect earlier posts
- **Buttons for everyday work** — view progress, cancel a task, retry after joining
  a source, or change settings without memorizing commands
- **Previously saved content arrives sooner** — avoid downloading the same content
  again while its saved copy remains available
- **A workspace for people you trust** — invite collaborators, manage access and
  follow quotas, and let each member choose English or Simplified Chinese

Album previews are displayed without spoilers, and copied content does not carry
an original-sender forwarding label. Source access and Telegram permissions still apply.

## Start on Linux with uv

With [uv](https://docs.astral.sh/uv/) installed, run these commands on the machine
that will host your Bot:

```sh
uv tool install --python 3.12 git+https://github.com/yzctzl/tdl-bot.git
mkdir tdl-bot-workspace && cd tdl-bot-workspace
tdl-bot setup -i
tdl-bot run
```

Before the wizard, create a Bot with **@BotFather**, prepare a private storage
channel, and add the Bot there as an administrator with posting permission.
Have your Bot token and Telegram account ready. The wizard guides you through
sign-in, administrator confirmation and storage selection; tdl is downloaded
when needed. Open your Bot, send `/start`, choose a language, then send a message link.

Keep the terminal running. For Docker, Windows, macOS, background operation and
updates, see the [installation guide](docs/deployment.md).

## Find your next step

| I want to… | Read |
| --- | --- |
| Use and manage your Bot, save albums, or handle retries | [Owner's guide](docs/manual.md#first-use) |
| Follow new posts or back up channel history | [Scheduled tracking and history backups](docs/manual.md#scheduled-tracking-and-history-backups) |
| Install with Docker, uv tool, or a Git checkout | [Installation and platform support](docs/deployment.md) |
| Resolve access, sending, or interrupted-task problems | [Troubleshooting](docs/manual.md#troubleshooting) |
| Understand or contribute to the implementation | [Architecture](docs/architecture.md) |

Only save content you are allowed to access and retain. Treat Bot tokens and
Telegram login sessions as private credentials.
