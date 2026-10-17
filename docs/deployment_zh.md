# 安装与维护指南

[English](deployment.md) · [首页](../README_zh.md) · [使用手册](manual_zh.md)

目标是让你拥有一个能长期使用的 Telegram Bot
完成下面的流程后，你会有自己的 Bot、已经登录的 Telegram 执行账号，以及固定保存配置和数据的工作目录
日常收藏、追踪和管理都在 Telegram 中完成

## 选择你的路线

第一次部署，已经装好 uv 且不需要 Docker，建议从 **uv tool + 交互向导**开始
只想长期运行服务、习惯管理容器，就选择 Docker Compose
准备修改代码或直接跟随仓库运行，再选 Git 源码方式
三种方式选一种即可，不需要全部安装

| 方式 | 宿主机需要准备 | 配置方式 | 安装步骤 |
| --- | --- | --- | --- |
| uv tool | uv，能访问 GitHub 的网络 | 终端向导逐项引导 | [使用 uv tool](#使用-uv-tool) |
| Docker Compose | Docker Engine + Compose，或使用 Linux 容器的 Docker Desktop | 编辑配置文件后登录 | [使用 Docker Compose](#使用-docker-compose) |
| Git 源码 | Git、uv，能访问 GitHub 的网络 | 同样使用终端向导 | [使用 Git 源码](#使用-git-源码) |

平台差异、配置参考和维护操作可在需要时再看：
[平台支持](#平台支持) · [工作目录](#工作目录与配置) · [停止与升级](#停止重载与升级) · [排错](#常见问题)

## 开始前准备

### 1. 创建你自己的 Bot

在 Telegram 中打开 **@BotFather**，发送 `/newbot`，按提示设置 Bot 名称和用户名
完成后保存它给你的 **Bot token**，后面会粘贴到配置向导或 `.env` 中
同时记下 Bot 的 `@用户名`，方便把它加入存储频道，之后打开它的私聊

token 相当于 Bot 的管理凭据，不要发到群里、截图公开或提交进仓库

### 2. 准备内部存储频道

推荐在 Telegram 新建一个私有频道，例如自己容易识别的“收藏存储”
你登录的账号需要能在其中发帖
进入频道的管理员设置，将刚创建的 Bot 添加为管理员，并开启发帖权限
关闭频道的内容保护/限制保存选项，否则 Bot 无法从这里复制内容

这个频道是工具用来保存内容和必要操作记录的地方，不是必须展示给协作者的收藏频道
你最后想把内容发往其他频道或群组，可以在 Bot 启动后单独设置
第一次验证时，最终接收位置保留为 Bot 对话即可

如果没有存储频道的 ID，不用为了向导提前查询：

- 公开频道可以直接输入 `@用户名`
- 私有频道可以在向导第四步输入 `list`，从自己拥有的频道/群组列表中找到它并复制显示的 ID
- Docker 手动流程没有这个向导列表，私有频道 ID 可按下面的方法取得，不需要公开频道

需要私有频道 ID 时，在频道里自己发一条消息并复制链接
例如 `https://t.me/c/1234567890/1` 中，`1234567890` 是频道编号，在前面加 `-100` 得到 `-1001234567890`，末尾的 `1` 是消息编号，不属于频道 ID
这只是格式示例，请使用自己链接中的编号

### 3. 选择 Telegram 登录账号

准备一个能在存储频道发帖的 Telegram 用户账号
默认流程用这个账号扫描二维码，登录状态会保存在你自己的机器上
它负责读取来源和准备内容；Bot 负责与你交互并发送结果

最简单的做法是：用你自己的账号登录，并在下一步将同一个账号确认为管理员
这样无需另外查询自己的 UID，也不需要填写 `TELEGRAM_API_ID`、`TELEGRAM_API_HASH`

### 4. 选定工作目录

工作目录保存 `.env`、任务数据库、登录会话和托管程序
选择你有写权限、打算长期保留的位置，不要放进 uv 的缓存或工具安装目录
以后更新程序不会要求你重新建立工作目录或登录

## 使用 uv tool

以下操作在你打算运行 Bot 的机器终端中完成
Linux/macOS 可使用 shell，Windows 可使用 PowerShell
示例创建当前目录下的 `tdl-bot-workspace`，可自行改名

### 1. 安装并进入工作目录

```sh
uv tool install --python 3.12 git+https://github.com/yzctzl/tdl-bot.git
mkdir tdl-bot-workspace
cd tdl-bot-workspace
```

命令执行完成后，后续配置和运行都在这个目录进行
如果提示找不到 `tdl-bot`，按 uv 安装提示配置 PATH，重新打开终端，再进入这个目录
不需要自己安装 Go 或编译 tdl

### 2. 启动交互向导

```sh
tdl-bot setup -i
```

向导会先确认供 Docker 使用的时区，原生运行仍跟随系统时区
随后按四步配置：

| 步骤 | 你需要输入或操作 | 成功后会看到什么 |
| --- | --- | --- |
| 1/4 Bot | 粘贴 @BotFather 给你的 token，输入不会显示在屏幕上 | Bot 的 `@用户名`，表示 token 已验证 |
| 2/4 Telegram 登录 | 用准备好的 Telegram 账号扫描终端二维码 | 账号登录完成；已有有效会话会直接使用 |
| 3/4 管理员 | 第一次且使用登录账号管理时，核对显示账号并回车确认；其他管理员需填写数字 UID | 管理员设置保存 |
| 4/4 内部存储 | 输入存储频道的 `@用户名` 或 ID；需要查看自己拥有的位置时输入 `list` | 存储权限验证通过，提示配置完成及启动命令 |

缺少 tdl 时自动下载、校验并安装，首次配置需要能访问 GitHub
配置完成的步骤会保存，中途退出后可重新运行 `setup -i` 继续，已有值回车保留
权限验证不会发送测试媒体
如果第四步失败，先确认 Bot 已被添加为频道管理员，而不是只让你的账号加入

### 3. 启动服务

```sh
tdl-bot run
```

保持这个终端运行，Bot 才能持续响应
打开 Telegram，进入自己的 Bot 私聊，发送 `/start`，确认管理员入口并完成[第一次保存](manual_zh.md#第一次使用)
仅终端没有报错还不算完整验证，建议亲自保存一条小视频或图片

想从别的位置配置或启动，可以显式选择路径：

```sh
tdl-bot setup -i -d /path/to/workspace
tdl-bot run -e /path/to/workspace/.env
```

Windows 将路径换成例如 `C:\Users\you\tdl-bot-workspace`，含空格时加引号
需要关掉终端后仍长期运行，请使用系统的服务管理器托管相同命令，并保持相同系统用户和工作目录
tdl-bot 不会自动安装系统服务

## 使用 Docker Compose

宿主机不需要 Python、uv 或 tdl
这是手动配置路线，和上面的交互向导不同，按顺序完成每一步即可

### 1. 准备目录与两个配置文件

创建并进入自己的工作目录，从仓库**同一发行版本或 Git 修订**下载：

- [`compose.yaml`](../compose.yaml)，保持文件名
- [`env.example`](../src/tdl_bot/resources/env.example)，重命名为 `.env`，不要保留 `.example` 后缀

在工作目录创建数据目录：

```sh
mkdir -p data/tdl bin
```

PowerShell 使用：

```powershell
New-Item -ItemType Directory -Force data/tdl, bin
```

此时目录应包含 `.env`、`compose.yaml`、`data/tdl/` 和 `bin/`

### 2. 填写最初的配置

用文本编辑器打开 `.env`，保留模板路径，并填写：

| 配置 | 怎么填写 |
| --- | --- |
| `BOT_TOKEN` | @BotFather 给你的 token |
| `TZ` | 所在地区/城市，例如 `Asia/Taipei`、`Europe/London` |
| `ADMIN_USER_IDS` | 先留空，下一步登录会显示你的 UID，再回来填写 |
| `TDL_BOT_IMAGE` | 可选，填写已发布的版本镜像；不填时使用 `latest` |

Linux/WSL2 上，还需确认目录所有者：执行 `id -u` 和 `id -g`
如果不是默认的 1000/1000，在 `.env` 增加对应的 `PUID`、`PGID`
Docker Desktop 使用它自己的宿主机文件共享，WSL2 推荐将工作目录放在 Linux 文件系统内

首次安装前，确认仓库 Releases 对应版本已发布镜像
需要运行尚未发布的源码时，见下方[镜像与映射目录](#镜像与映射目录)

### 3. 登录 Telegram，并填写管理员 UID

在含 `compose.yaml` 的工作目录运行：

```sh
docker compose run --rm bot login
```

用准备好的 Telegram 账号扫描二维码，完成后记下终端显示的账号 UID
回到 `.env`，把这个数字填入 `ADMIN_USER_IDS`，保存文件
多个初始管理员可以用逗号分隔，这里填写数字 ID，不是 `@用户名`

### 4. 启动并配置存储

```sh
docker compose up -d
docker compose logs -f bot
```

`up -d` 在后台启动服务，查看日志时退出跟踪不会停止容器
打开 Telegram 的 Bot 私聊，发送 `/start` 并选择语言
进入 **Bot 管理 → 修改存储**，输入你准备的频道 `@用户名` 或 ID
没有配置内部存储前，Bot 不能正常处理内容

配套 Compose 将配置目录只读挂载，因此不使用容器内 `setup -i` 向导
现在按[使用手册](manual_zh.md#第一次使用)验证第一条内容，成功后再配置自己的接收频道

### 镜像与映射目录

发布镜像为 Linux amd64/arm64，在 Docker Desktop 中同样使用 Linux 容器
可在 `.env` 增加 `TDL_BOT_IMAGE=ghcr.io/yzctzl/tdl-bot:v0.1.0` 固定到对应的已发布版本，版本号按实际发行替换
尚未发布的代码可以克隆仓库并在仓库根目录执行 `docker build -t tdl-bot:local .`，再设置 `TDL_BOT_IMAGE=tdl-bot:local`

| 宿主机位置 | 容器位置 | 保存什么 |
| --- | --- | --- |
| 工作目录 | `/workspace`，只读 | 配置文件 |
| `DATA_DIR`，默认 `./data` | `/data` | 数据库、任务、日志及临时文件 |
| `BIN_DIR`，默认 `./bin` | `/tdl-bin` | 自动安装的 tdl 程序 |
| `TDL_DIR`，默认 `./data/tdl` | `/data/tdl` | Telegram 登录状态 |

自定义位置先在宿主机创建，再修改 `.env` 的对应值
Docker 使用容器内 Linux 程序，宿主机 `TDL_BINARY` 设置不会用来运行容器
不要让原生实例和容器同时使用一份工作空间

容器时区使用 `.env` 的 `TZ`，不自动读取桌面系统设置
填写地区/城市名称，不要使用含义不明确的 `CST`
镜像已包含时区数据，无需挂载系统时区文件，修改 `TZ` 后执行 `docker compose up -d` 重建容器

## 使用 Git 源码

适合希望直接管理仓库文件的部署方式
先安装依赖，再使用同一个向导：

```sh
git clone https://github.com/yzctzl/tdl-bot.git
cd tdl-bot
uv sync --locked --python 3.12
uv run tdl-bot setup -i
uv run tdl-bot run
```

向导的四步和 [uv tool](#使用-uv-tool) 完全相同，默认将项目目录作为工作目录
之后所有终端中的 `tdl-bot` 命令，使用 `uv run tdl-bot` 执行
工作目录放到其他位置时，给 setup 加 `-d`，运行、登录和停止命令加 `-e`
不使用向导时，普通 `setup` 只生成 `.env`，你需要自己编辑配置，再登录、启动

## 平台支持

原生安装需要 Python 3.12 或更新版本，示例让 uv 使用 3.12
Docker 已包含 Python，三种安装方式都不需要 Go 环境

| 平台 | 原生运行 | Docker |
| --- | --- | --- |
| Linux x64 / ARM64 | 支持，提供手动热重载 | amd64/arm64 Linux 镜像 |
| macOS Intel / Apple Silicon | 支持，提供手动热重载 | Docker Desktop 运行 Linux 镜像 |
| Windows | 支持收藏、重试、追踪、历史搬运和管理；不支持热重载或 Bot 崩溃自动重启 | Docker Desktop 使用 Linux 容器，或在 WSL2 按 Linux 安装 |

Windows 原生需要停止并重新启动，才能加载更新代码或新登录账号
请将文件放在当前用户可写的本地目录，并通过 Windows 文件权限保护会话
CI 检查三个系统的应用行为，但不执行真实 Telegram 传输，也不代表所有架构组合均已真机验证

## 工作目录与配置

### 哪些文件需要保留

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

`.env` 保存配置，`state.sqlite3` 保存权限、目标、任务与历史，`tdl/` 保存登录授权
`jobs/` 是处理期间的临时媒体，完成后会清理
`engine.log` 用于 Unix 任务引擎排错，`bin/` 中的程序可重新安装
不要把整个 `data/` 当作临时下载目录删除

向导替你填写 `.env`，不代表不再需要这个文件
相对路径以 **`.env` 所在目录**为基准，不随终端当前位置改变
找不到配置时，在工作目录运行，或指定 `-e /path/to/.env`
环境变量 `TDL_BOT_ENV_FILE` 也可选择配置，直接设置的系统环境变量优先于文件值
应用不展开 `.env` 里的 shell 变量

### 复用或增加登录

独立执行 `tdl login` 通常把授权放在其自己的家目录位置
`tdl-bot login` 使用 `.env` 的 `TDL_DIR`，新工作空间默认为 `./data/tdl`
因此两个命令的登录位置不一定相同，Bot 不会自动从家目录复制授权
复用已有会话时，停止旧服务，将 `TDL_DIR` 指向 tdl 根目录，不要指向其中的 `data` 子目录

需要验证码登录或导入 Telegram Desktop 会话时：

```sh
tdl-bot login -m code
tdl-bot login -m desktop
```

为另一个账号创建单独登录时，原生使用：

```sh
tdl-bot stop
tdl-bot login -n personal
tdl-bot run
```

Docker 使用：

```sh
docker compose stop
docker compose run --rm bot login -n personal
docker compose up -d
```

登录时使用另一个 Telegram 账号，`personal` 可替换成唯一、容易记忆的名字
这不会改掉公共登录账号，账号如何自动选择见[执行账号管理](manual_zh.md#管理执行账号)
同一授权不要同时在不同机器或不同网络出口运行，每个账号都需要内部存储权限

### 需要调整参数时再看

没有具体需求时，保留向导生成的默认值即可

| 设置 | 默认值 / 用途 |
| --- | --- |
| `DATA_DIR`、`BIN_DIR` | 显式路径，模板为 `./data`、`./bin` |
| `TDL_DIR`、`TDL_NAMESPACE` | 模板为 `./data/tdl`、`default`，公共登录位置与名称 |
| `ADMIN_USER_IDS` | 首次初始化管理员的数字 UID，之后在 Bot 内管理角色 |
| `TDL_PROXY` | 执行账号可选的 SOCKS5/SOCKS4/HTTP 代理 |
| `WORKERS`、`QUEUE_SIZE` | 默认 2 个链接任务 worker、20 个等待任务，备份另有相同大小的 worker 池 |
| `DOWNLOAD_TIMEOUT`、`UPLOAD_TIMEOUT` | 各 7200 秒，clone 超时包含下载与上传全过程 |
| `UPLOAD_CONCURRENCY` | 默认 8，单文件传输线程/窗口，范围 1–32 |
| `TDL_BINARY` | 原生可显式指定现有程序，替代托管程序位置 |
| `RUNTIME_DIR` | 原生默认 `DATA_DIR`，socket 路径过长时可另选较短本地路径 |
| `SESSION_SOURCE` | 默认 `tdl`，兼容的 `telethon` 模式才需要另外填写 API ID/hash |

增加 worker 不保证占满带宽，仍受账号协调、网络和 Telegram 限制
`TDL_PROXY` 不等于改变宿主机或整个容器的所有网络设置

## 停止、重载与升级

### 完整停止

在工作目录使用相应命令：

| 安装方式 | 停止命令 | 再次启动 |
| --- | --- | --- |
| uv tool | `tdl-bot stop` | `tdl-bot run` |
| Git 源码 | `uv run tdl-bot stop` | `uv run tdl-bot run` |
| Docker | `docker compose stop` | `docker compose up -d` |

停止会等待已接收任务完成收尾，不一定立即返回
请等待完成后再更新文件、搬迁目录或关闭机器
Unix 上 Ctrl+C 只停止前台 Bot 管理器，引擎可能继续工作，完整停服请用 `stop`
Windows 原生按一次 Ctrl+C 会请求整个进程正常收尾，保持终端打开直到完成

### 手动重载

Linux、macOS 和 Linux 容器可以手动 reload，让 Bot 界面重新启动，同时保留正在执行的传输
Windows 原生请使用停止再启动

```sh
tdl-bot reload
tdl-bot reload -a
tdl-bot reload -e /path/to/workspace/.env
```

只有一个实例时自动选择，多个实例时 `-a` 全部重载，`-e` 指定一个
源码方式给命令加 `uv run`，Docker 使用 `docker compose exec bot tdl-bot reload`
没有目录监控，文件变化不会自动触发重载
reload 不负责升级安装包、替换镜像、搬迁数据或改变容器环境，任务引擎更新等待现有工作空闲后交接

### 升级程序

升级前先备份，尤其是有数据库结构变化的版本
uv tool 在工作目录执行：

```sh
tdl-bot stop
uv tool upgrade tdl-bot
tdl-bot run
```

源码安装：停止后更新仓库，执行 `uv sync --locked`，再运行
Docker 执行：

```sh
docker compose pull
docker compose stop
docker compose up -d
```

**更换 Docker 镜像不是热重载**
容器正常停止等待默认为 24 小时，可用 `TDL_STOP_GRACE_PERIOD` 调整，超过期限会被 Docker 强制结束
强制关闭的进程不会继续传输，重启后遇到未确认的发送请先检查目标再处理

Bot 管理中的“更新 tdl”仅更新传输程序，不会升级 tdl-bot

## 备份与迁移

1. 用原配置完整停止服务，确认正常结束
2. 复制 `.env`、整个数据目录，以及单独放在其他位置的会话目录，保留文件权限
3. 将备份放在受保护的位置，其中包含 Bot token 和 Telegram 登录授权
4. 备份完成后再启动服务

更换机器或目录时，停止后再移动文件，修改 `.env` 路径及必要的 Docker 挂载，然后启动
不要让两个实例同时使用同一份数据或会话
旧程序可能无法读取升级后的数据库，回退版本前保留并使用匹配的升级前备份

## 常见问题

| 现象 | 先检查什么 | 处理方式 |
| --- | --- | --- |
| 找不到配置 | 当前终端是否在工作目录 | 进入该目录，或显式传 `-e` |
| setup 提示配置存在 | 是否已经生成过 `.env` | 用 `setup -i` 继续配置，普通 setup 不覆盖 |
| 找不到管理员按钮 | 操作账号是否与首次管理员一致 | 用初始管理员账号打开 Bot，已有数据库用用户管理调整 |
| 下载 tdl 失败 | GitHub 连通性、程序目录权限 | 处理网络/代理或写权限，再重新执行登录或启动 |
| 已登录却仍提示未登录 | 是否使用相同 `TDL_DIR` 和登录名称 | 用同一配置执行 `tdl-bot login`，或指向已有目录 |
| 容器目录不可写 | 目录是否存在，Linux UID/GID 是否对应 | 创建目录并修正 `PUID`/`PGID` |
| 宿主机代理可用，容器不可用 | 是否填了容器自身的 `127.0.0.1` | 使用容器能访问的代理地址 |
| 时区不正确 | 原生系统时区或 Compose 的 `TZ` | 修正系统设置，或填写 `TZ` 后重建容器 |
| 资源被占用 | 是否仍有旧实例运行 | 先停止旧实例，不删除锁文件绕过占用 |
| Unix socket 路径过长 | 工作目录路径是否过深 | 停止后设置较短的本地 `RUNTIME_DIR` |

`tdl-bot health` 用于检查本地服务是否存活，不代表已经验证 Telegram 网络
`tdl-bot help`、`tdl-bot help login`、`tdl-bot login help` 可在未配置时查看用法
请求协助时，说明系统、安装方式、操作步骤和错误日志，先移除 token、登录凭据及私密内容
Bot 内发送失败与重试问题见[使用手册](manual_zh.md#常见问题)
