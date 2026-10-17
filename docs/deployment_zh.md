# 安装与维护

[English](deployment.md) · [首页](../README_zh.md) · [用户手册](manual_zh.md)

## 选择安装方式

| 方式 | 适合场景 | 需要准备 |
| --- | --- | --- |
| uv tool | 个人服务器或桌面，使用向导配置 | uv 与可交互终端 |
| Docker Compose | 长期运行，并映射持久化目录 | Docker Engine + Compose，或使用 Linux 容器的 Docker Desktop |
| Git 源码 | 从源码运行或修改项目 | Git 与 uv |

原生安装需要 Python 3.12 或更新版本，示例会让 uv 使用 3.12
Docker 已包含 Python，不需要另行安装 Go

| 平台 | 原生安装 | Docker |
| --- | --- | --- |
| Linux x64 / ARM64 | 支持，提供手动热重载 | 提供 amd64、arm64 Linux 镜像 |
| macOS Intel / Apple Silicon | 支持，提供手动热重载 | 通过 Docker Desktop 运行 Linux 镜像 |
| Windows | 原生单进程运行，不支持热重载及 Bot 崩溃自动重启 | 使用 Linux 容器的 Docker Desktop，或在 WSL2 中按 Linux 方式安装 |

Windows 原生同样提供收藏、接收位置、重试、历史搬运、定时追踪及成员管理功能
更新代码或刷新已登录账号时，需要停止并重新启动
CI 在三个系统上检查应用行为，不执行真实 Telegram 传输，也不代表验证了所有系统与架构组合

## 开始前准备

1. 通过 **@BotFather** 创建 Bot，妥善保管 token
2. 准备一个存储频道或群组，推荐私有频道，便于整理工作空间
3. 将 Bot 加入存储并授予发帖权限，准备登录的 Telegram 账号也需要能在其中发帖
4. 选择一个可写且打算长期保留的工作目录，放在 uv 安装和缓存目录之外，用来保存配置、登录会话及任务状态

登录的 Telegram 账号负责读取来源和准备内容，Bot 负责发送到最终接收位置，两者都需要访问存储
默认 tdl 登录方式不需要填写 `TELEGRAM_API_ID` 和 `TELEGRAM_API_HASH`

## 使用 uv tool 安装

```sh
uv tool install --python 3.12 git+https://github.com/yzctzl/tdl-bot.git
mkdir tdl-bot-workspace
cd tdl-bot-workspace
tdl-bot setup -i
tdl-bot run
```

向导会确认供 Docker 使用的时区，验证 Bot token，引导 Telegram 登录，确认管理员并选择存储
可以直接将登录账号确认为管理员，无需另外查询 UID
存储直接输入 ID 或 `@用户名`；需要查看自己拥有的频道或群组时才输入 `list`，不会列出所有会话

登录或启动时缺少 tdl，会自动下载、校验并安装，首次安装请确保能访问 GitHub
已有有效登录会直接使用
向导检查权限时不会发送测试消息，再次运行向导可以回车保留原值，已完成步骤会保存
修改配置前请先停止服务

这些命令也适用于 PowerShell
安装后找不到 `tdl-bot` 时，按 uv 提示配置 PATH 并重新打开终端

从其他目录操作时，显式选择配置：

```sh
tdl-bot setup -i -d /path/to/workspace
tdl-bot run -e /path/to/workspace/.env
```

Windows 请使用自己的路径，例如 `C:\Users\you\tdl-bot-workspace`，含空格的路径加引号
请保持运行终端打开，也可以用系统服务管理器，以相同工作目录和系统用户运行同一条命令
CLI 不会替你安装系统服务

## 使用 Docker Compose 安装

宿主机不需要安装 Python、uv 或 tdl
准备一个空工作目录，从**同一发行版本或 Git 修订**下载：

- [`compose.yaml`](../compose.yaml)
- [`env.example`](../src/tdl_bot/resources/env.example)，保存为 `.env`

打包资源中的模板是唯一维护的配置模板
镜像在对应版本发布流程成功后可用，请查看仓库 Releases 和容器包页面
若要构建尚未发布的代码，克隆仓库，执行 `docker build -t tdl-bot:local .`，并在工作目录 `.env` 中设置 `TDL_BOT_IMAGE=tdl-bot:local`

在工作目录中创建映射目录：

```sh
mkdir -p data/tdl bin
```

PowerShell 使用：

```powershell
New-Item -ItemType Directory -Force data/tdl, bin
```

编辑 `.env`，保留默认路径，并填写：

| 设置 | 内容 |
| --- | --- |
| `BOT_TOKEN` | @BotFather 提供的 token |
| `TZ` | 所在地区的时区名称，例如 `Asia/Taipei` 或 `Europe/London` |
| `ADMIN_USER_IDS` | 初始管理员的 Telegram 用户 ID，多个用逗号分隔，下方登录命令会显示 UID |
| `TDL_BOT_IMAGE` | 可选的版本镜像，例如 `ghcr.io/yzctzl/tdl-bot:v0.1.0`，否则使用 `latest` |

Linux/WSL2 上，映射目录所有者不是 1000/1000 时，还需设置 `PUID` 和 `PGID`
使用 `id -u` 与 `id -g` 获取，Docker Desktop 由系统处理宿主机文件共享
WSL2 推荐将工作目录放在 Linux 文件系统内，而不是 Windows 挂载目录

在工作目录执行：

```sh
docker compose run --rm bot login
# Fill ADMIN_USER_IDS in .env using the UID printed by login, then:
docker compose up -d
docker compose logs -f bot
```

打开 Bot，在 **Bot 管理 → 修改存储**中设置，或执行 `/storage @your_storage_channel`
Bot 和执行账号都需要权限
`setup -i` 交互向导用于原生安装，配套 Compose 的配置目录只读，使用上述文件配置与登录流程

### 映射的文件

| 宿主机位置 | 容器位置 | 内容 |
| --- | --- | --- |
| 工作目录 | `/workspace`，只读 | `.env` 和 Compose 配置 |
| `DATA_DIR`，默认 `./data` | `/data` | 数据库、任务状态、日志和临时文件 |
| `BIN_DIR`，默认 `./bin` | `/tdl-bin` | 托管 tdl 程序 |
| `TDL_DIR`，默认 `./data/tdl` | `/data/tdl` | Telegram 登录会话 |

自定义宿主机目录需先创建
宿主机路径由 Compose 解析，应用在容器内使用固定 Linux 路径，宿主机的 `TDL_BINARY` 覆盖值在 Compose 中会被忽略
不要让原生实例与容器同时操作同一工作空间

Compose 必须设置 `TZ`
在宿主机执行 `tdl-bot setup` 会自动填写系统时区，下载空白模板时需自己填写
请使用 IANA 地区/城市名称，不要使用含义不确定的 `CST`
Docker Desktop 不会自动把桌面系统时区提供给应用
镜像内置时区数据，无需挂载 `/etc/localtime`
修改 `TZ` 后执行 `docker compose up -d` 重建容器

## 使用 Git 源码运行

```sh
git clone https://github.com/yzctzl/tdl-bot.git
cd tdl-bot
uv sync --locked --python 3.12
uv run tdl-bot setup -i
uv run tdl-bot run
```

默认将项目目录作为工作目录
需要放在其他位置时，给 `setup -i` 增加 `-d /path/to/workspace`，运行命令增加 `-e /path/to/workspace/.env`
已有工作空间不需要重新初始化，向导可修改已有值并保留高级设置
不使用向导时，`setup` 仅生成 `.env`，需自行编辑后再执行 `login` 和 `run`

## 工作目录与高级设置

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

向导仍会将配置保存到生成的 `.env`，只是替你完成填写
所有相对路径都以 **`.env` 所在目录**为基准，与运行命令的目录无关
可以用 `-e <file>` 或 `TDL_BOT_ENV_FILE` 选择配置，否则读取当前目录 `.env`
系统环境变量优先于文件值，应用读取 `.env` 时不展开 shell 变量

| 设置 | 默认值 / 用途 |
| --- | --- |
| `DATA_DIR`、`BIN_DIR` | 必须显式设置，生成模板使用 `./data`、`./bin` |
| `TDL_DIR`、`TDL_NAMESPACE` | 模板中为 `./data/tdl`、`default`，表示公共登录位置和命名空间 |
| `BOT_TOKEN`、`ADMIN_USER_IDS` | Bot token 与首次初始化必填的管理员 UID |
| `TDL_PROXY` | 可选 SOCKS5、SOCKS4 或 HTTP 代理，用于执行会话 |
| `WORKERS`、`QUEUE_SIZE` | 2 个链接任务 worker、20 个等待任务，备份另用同样大小的 worker 池 |
| `DOWNLOAD_TIMEOUT`、`UPLOAD_TIMEOUT` | 分别为 7200 秒，clone 超时包含下载和上传全过程 |
| `UPLOAD_CONCURRENCY` | 每文件传输线程/窗口为 8，可用范围 1–32 |
| `TDL_BINARY` | 原生运行时可显式指定程序，替代托管二进制 |
| `RUNTIME_DIR` | 原生默认为 `DATA_DIR`，Unix socket 路径过长时改用较短的本地路径 |
| `SESSION_SOURCE` | 默认 `tdl`，兼容的 `telethon` 模式还需 `TELEGRAM_API_ID` 和 `TELEGRAM_API_HASH` |

增加 worker 不代表一定增加带宽，同一执行会话的操作需要协调，仍受 Telegram 与传输后端限制
没有明确需求时，建议保留默认值

`ADMIN_USER_IDS` 只用于初始化空工作空间，之后在 Bot 内管理成员，修改变量不会重置数据库角色

### 复用与增加登录

`tdl-bot login` 将授权保存在配置的 `TDL_DIR`，不是独立运行 tdl 时默认的家目录
已有 tdl 登录时，将 `TDL_DIR` 指向其**根目录**，不要指向内部的 `data` 子目录
程序不会自动从家目录复制授权

使用验证码或 Telegram Desktop 登录：

```sh
tdl-bot login -m code
tdl-bot login -m desktop
```

增加个人执行账号时，先停止服务，再在同一会话目录中使用其他命名空间登录：

```sh
tdl-bot stop
tdl-bot login -n personal
tdl-bot run
```

这不会改变公共命名空间
账号通过 Telegram 验证的 UID 与 Bot 操作用户匹配，每个执行账号都需要内部存储权限
同一授权请使用相同网络出口，避免同时在其他机器或不同代理下使用，并保护整个会话目录

## 重载、停止与更新

| 操作 | Linux / macOS / Linux 容器 | Windows 原生 |
| --- | --- | --- |
| `reload` | Bot 重新连接，活动传输继续，引擎改动等待空闲交接 | 不支持 |
| `stop` | 等待已接收任务完成后停止服务 | 等待任务完成后停止单进程 |
| 替换程序、依赖或镜像 | 先停止，再升级和启动 | 先停止，再升级和启动 |

```sh
tdl-bot reload                  # Select the only running instance
tdl-bot reload -a               # Reload all instances owned by this OS user
tdl-bot reload -e ./work/.env    # Select one workspace
tdl-bot stop -e ./work/.env
```

多个运行实例需使用 `-a` 或 `-e`，有歧义时不会重载任何实例
`stop` 使用所选工作空间，不采用 reload 的自动发现方式
程序不监控目录变化，reload 不会更新安装包、搬迁数据，也不能改变 Docker 挂载及容器环境变量

Unix 上 Ctrl+C 停止前台 Bot 管理器，引擎可能继续传输，完整停服请使用 `stop`
Windows 原生按一次 Ctrl+C 会请求单进程等待任务结束，请保持终端打开直到完成

更新 uv tool 安装：

```sh
tdl-bot stop
uv tool upgrade tdl-bot
tdl-bot run
```

源码安装：先停止，更新仓库，执行 `uv sync --locked` 后启动
Compose 使用：

```sh
docker compose pull
docker compose stop
docker compose up -d
```

容器内手动重载使用 `docker compose exec bot tdl-bot reload`
**替换镜像不属于热重载**，默认停止等待期限为 24 小时，可用 `TDL_STOP_GRACE_PERIOD` 调整，超过期限会被 Docker 强制结束
容器停止或被杀死后，无法继续在后台传输

tdl 通过 **Bot 管理 → 更新 tdl** 或 `/update_tdl` 单独更新
更新先比较版本，等待当前使用结束后下载、校验并替换程序，目前使用官方 tdl 发行版

## 备份与迁移工作空间

停止服务后，备份 `.env`、整个数据目录，以及单独配置在其他位置的会话目录
保留权限并保护备份隐私，复制完成后再启动
程序文件可以重装，登录会话和数据库才是重要状态
数据库结构升级前保留备份，旧程序会拒绝打开新版数据库

迁移路径时，先使用**旧配置**停止，移动文件，修改 `.env` 及 Docker 挂载后再启动
不要跨机器、容器或原生安装共用正在运行的数据库与会话

## 常见问题

| 问题 | 处理方式 |
| --- | --- |
| 找不到配置文件 | 进入工作目录，或指定 `-e /path/to/.env` |
| setup 提示配置已存在 | 使用 `setup -i` 修改，普通 `setup` 不覆盖文件 |
| 缺少 tdl 且无法下载 | 检查 GitHub 网络/代理及二进制目录写入权限 |
| 独立运行 tdl 登录后仍提示未登录 | 对齐 `TDL_DIR` 与命名空间，或使用 `tdl-bot login` 登录 |
| 资源被其他实例占用 | 停止旧实例，不要删除锁文件绕过正在使用的资源 |
| 容器无法写入映射目录 | 确认目录已创建，Linux 检查 `PUID`/`PGID` |
| 代理在宿主机可用，容器内不可用 | 容器的 `127.0.0.1` 指向自身，需要改成容器可访问的地址 |
| 时区缺失或不正确 | 原生检查系统时区，Compose 设置 `TZ` 后重建容器 |
| Windows 原生无法 reload | 使用 `stop` 后 `run`，Docker/WSL2 提供 Unix 运行方式 |
| Unix socket 路径过长 | 先停止旧实例，再将 `RUNTIME_DIR` 改为较短本地路径 |

`tdl-bot health` 检查本地进程存活，不代表 Telegram 网络可用
`tdl-bot help`、`tdl-bot help login` 和 `tdl-bot login help` 无需配置即可使用
日志可能含有账号标识，分享诊断信息前请移除凭据和私密内容

Bot 内的操作问题，见[用户手册](manual_zh.md#常见问题)
