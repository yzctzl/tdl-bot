# tdl-bot

面向一个人及其多个 Telegram 账号／协作者的共享工作空间。使用 aiogram 3
将 tdl 封装成 Telegram Bot。发送一个消息 **url**，自动定位
其所在的整个 **media group**，保留各媒体绑定的**视频文字**与文字格式。
媒体先进入管理员配置的内部存储，再由 Bot 复制到用户接收位置；可直接转发
或复用历史结果时不下载。默认接收位置为用户与 Bot 的私聊。

服务器临时目录仅用于下载中转，处理结束后删除。

## 运行条件

- Docker，或 Linux/macOS/WSL2 中的 Python 3.12+ 和 uv。
- 由 @BotFather 创建的 Bot token。
- 用户配置 `TDL_DIR` 中的 Telegram 会话，可通过下方登录命令创建。
- tdl 可执行文件；登录命令会在缺少时下载官方发行包并校验后安装。
- 默认直接复用 tdl 的已有授权。来源群的读取和入群、目标的写入均使用
  该账号；最终发送使用 Bot，无需让 tdl-bot 再次登录。

Bot API 没有按任意消息 url 获取历史消息／整个媒体组的接口。仅用 aiogram
不能从历史 url 补齐其余成员，所以使用 Telethon 用户会话读取消息和入群。
tdl 通过原生 clone 传输受限媒体，保留原 MIME、视频属性和预览；Telethon
使用已上传媒体引用按原顺序组建相册，恢复文字和 entities。

### Windows 兼容性

当前版本不支持 Windows 原生运行。静态检查发现：启动入口和任务引擎直接
导入 `fcntl` 并使用 `flock`；进程通信使用 asyncio Unix socket；手动热重启
依赖 `SIGHUP` 和事件循环的 Unix 信号处理接口。这些依赖尚未提供 Windows
替代实现；原生 Windows 入口会明确提示使用 Docker／WSL2。

tdl 更新器能选择 Windows 压缩包，并不代表整个 Bot 支持 Windows。
Windows 用户可使用 Docker Desktop 的 Linux 容器，或在 WSL2 的 Linux 环境中部署，使用 Linux 版 Python、uv
和 tdl，并将项目、会话及运行数据放在 WSL 的 Linux 文件系统内。
本次仅做代码静态检查，未在 Windows 或 WSL 上实测。

## 部署方式

支持 Docker Compose、`uv tool` 和 Git 源码运行，统一使用用户指定的工作目录。
`.env` 必须存在，`DATA_DIR` 和 `BIN_DIR` 显式配置；原有 `TDL_BINARY` 仍兼容。
所有相对路径均以 `.env` 所在目录为基准，程序安装与更新不改变用户数据位置。

- Docker／WSL2 是 Windows 的首选入口；原生 Windows 暂不支持。
- Docker 支持容器内 `docker compose exec bot tdl-bot reload`，当前任务继续。
- **更换 Docker 镜像属于排空后的重启，不支持跨容器热升级。**
- `uv tool` 升级依赖前同样先 `stop`，升级后 `run`；普通 `reload` 保留进行中的工作。
- 在当前目录运行 `tdl-bot setup` 即可初始化；其他目录用 `setup -d <目录>`。源码运行加 `uv run` 前缀。
- 本机推荐 `tdl-bot setup -i` 交互配置：Token → Telegram 登录 → 确认管理员 → 选择存储频道。其他目录加 `-d <目录>`；已有配置可回车保留，运行中的服务需先停止。Docker 目前继续使用下文的配置流程。
- 常用短选项：`-e` 配置文件、`-d` 工作目录、`-n` 登录 namespace、`-m` 登录方式；长选项仍支持。

完整的三平台安装、目录映射、配置与升级流程见 [部署指南](docs/deployment.md)。
仓库包含 Linux/macOS 检查与 `linux/amd64`、`linux/arm64` GHCR 构建流程；
普通提交和 PR 只运行检查；**仅推送 `vX.Y.Z` 版本 tag 才触发 Docker 构建与
版本发布**，tag 需与 `pyproject.toml` 版本一致。成功后发布版本镜像和附带
wheel／源码包的 GitHub Release。tag 由用户手动创建，工作流不自动打 tag。

## 安装与登录

在项目根目录执行：

```bash
uv sync
cp .env.example .env
uv run tdl-bot login
```

登录命令默认使用二维码，直接调用原生 tdl，读取 `.env` 中的会话目录
和 `default` namespace。新模板使用 `data/tdl`，缺少程序时下载官方发行包，
验证 SHA-256 和版本，再安装到 `BIN_DIR/<系统-架构>/tdl`。已有有效授权时仅验证并复用，
不重新登录。验证码／两步验证交互直接由 tdl 在终端处理，Bot 不收集登录凭据。

验证码登录或导入 Telegram Desktop 会话可分别使用：

```bash
uv run tdl-bot login -m code
uv run tdl-bot login -m desktop
```

登录成功会显示账号 UID。编辑 `.env`，填写 `BOT_TOKEN` 和
`ADMIN_USER_IDS`，保留默认会话配置：

```dotenv
SESSION_SOURCE=tdl
DATA_DIR=./data
BIN_DIR=./bin
TDL_DIR=./data/tdl
TDL_NAMESPACE=default
```

`ADMIN_USER_IDS` 必须显式填写，使用 Telegram 用户 UID，以逗号分隔。
首次启动仅初始化这些管理员；普通成员由管理员使用 `/user add` 添加。
`ALLOWED_USER_IDS` 已移除，不再作为初始化或授权来源。完成配置后启动：

```bash
uv run tdl-bot
```

管理员随后在 Bot 中使用 `/storage` 配置内部存储；Bot 必须加入该存储频道
并具有相应权限，执行账号也必须能够向其发送内容。

成员初始化后，SQLite 是权限的唯一来源。已有数据库不会因为 `.env` 变化而
重新添加成员或重置角色，后续启动也无需再次配置 `ADMIN_USER_IDS`。
登录命令不要求 Bot token 或管理员 UID，可以先登录再填写首次运行所需配置。

`TDL_DIR` 指 tdl 根目录，而不是其中的 `data` 子目录；相对路径均从 `.env`
所在目录解析。其他位置启动可用 `--env-file` 指定配置。也支持绝对路径、`~` 路径，
以及仅有 `data.kv` 的旧版 legacy 存储。Bot 不会自动从家目录复制登录信息。

普通的 `tdl login` 仍使用 tdl 自身的默认 `~/.tdl`。使用上述 `tdl-bot login`
即可将授权放入配置位置。已有 `TDL_DIR=.tdl`、`TDL_BINARY=bin/tdl` 的
安装会继续复用原位置；需要移动时先停止服务，再修改配置并显式迁移文件。

tdl 自身仍可能在 `~/.tdl` 创建日志和扩展目录；这不改变显式 storage 指定的
登录凭据位置。通过 `tdl-bot login` 登录时需先停止服务，进程锁会防止改写
正在使用的会话。读取、下载和转存等普通运行路径不会创建空会话或触发登录。

Bot 运行时调用 tdl 原生 `backup` 读取授权，将密钥、DC 配置和应用信息转换为
Telethon 内存会话。临时备份在权限为 0700 的目录内，读取后删除；不会生成
`data/telegram.session`，也不会在启动失败时自动登录。

共享授权需要 tdl 和 Telethon 使用相同的网络出口；配置 `TDL_PROXY` 时两者
同步使用该代理（支持 socks5、socks4、http）。运行期间避免在不同网络出口
的其他机器上同时使用这份授权。

保留了独立 Telethon 登录模式供旧配置使用：显式设置
`SESSION_SOURCE=telethon`，填写从 <https://my.telegram.org> 获取的
`TELEGRAM_API_ID` 和 `TELEGRAM_API_HASH`，再执行 `uv run tdl-bot login`。
仅此模式会生成 `data/telegram.session`，并要求其账号与 tdl 下载账号一致。
请保护 `.env` 和 `TDL_DIR` 指定的会话目录。

## 语言选择

Bot 默认使用英文，不自动跟随 Telegram 客户端的语言。每个操作账号可以
独立选择语言，设置保存在当前工作空间数据库中，重启后保留：

首次发送 `/start` 时，Bot 先显示中英双语欢迎消息和各语言按钮。点击后保存
语言选择，并将欢迎消息替换为对应语言的使用说明。已有语言设置的账号再次
使用 `/start` 时直接显示说明；也可以使用下面的命令随时修改语言：

```text
/language
/language en
/language zh
```

不带参数时显示当前语言和可用选项。选择后，帮助、命令提示、按钮、任务进度
和错误提示使用对应语言；管理员查看其他成员时使用管理员自己的界面语言。
媒体原文和视频文字保留原有内容。

### 添加其他语言

翻译目录为 `src/tdl_bot/locales/`。英文源文案作为翻译键；新增一个以语言
代码命名的 JSON 文件，例如 `fr.json`，即可将它加入 `/language` 的选项：

```json
{
  "name": "Français",
  "messages": {
    "Cancel": "Annuler",
    "Your user ID: {p0}": "Votre identifiant : {p0}"
  }
}
```

可参考 `zh.json` 补全翻译。保留源文案中的占位符名称，例如 `{p0}`、
`{name}`；程序加载时检查占位符是否一致。缺少翻译时回退到英文。
新增界面文案使用 `tr("English source", ...)`，并添加对应翻译。
修改代码或语言目录后执行 `uv run tdl-bot reload`；后台引擎在现有任务完成后
切换到新版本，具体行为见下方热重启说明。

## Execution accounts

The shared execution account is selected by `TDL_DIR` and `TDL_NAMESPACE` in
`.env`. In native tdl mode, the engine discovers other logged-in namespaces in
that same directory and verifies their Telegram identities. No `accounts.toml`,
`ACCOUNTS_FILE`, or operator-to-namespace mapping is needed.

A regular operator automatically uses the session whose verified Telegram UID
matches their own. Without one, they use the shared execution account. Personal
sessions cannot be selected or used for retries or joins by another regular
operator. Administrators can use every discovered account:

- `/account`: administrators can view the current selection and available
  accounts with usernames and UIDs. Regular operators have no account command.
- `/account <UID>`: select a logged-in account (administrator only).
- `/account shared`: use the shared account (administrator only).
- `/account auto`: restore automatic own-or-shared selection (administrator only).

An administrator's selection persists across restarts and affects only their
new tasks. Accepted tasks and retries retain their original execution account;
a missing original account never silently falls back to another identity.
Overrides stop applying when an operator loses administrator rights.

Add another account in a distinct namespace while the service is stopped:

```sh
uv run tdl-bot stop
uv run tdl-bot login -n personal
uv run tdl-bot
```

`--namespace` affects only that login and does not change the configured shared
namespace. If you use tdl directly while the Bot is running, log in to a new,
unused namespace with the same explicit storage path, then manually reload.
Do not reauthenticate an account being used by running jobs.

Manual reload discovers newly logged-in or logged-out sessions after existing
engine work finishes. No directory watcher or Bot login flow is involved.
Namespaces with no valid authorization or expired sessions are skipped; network
failures during verification remain startup errors. Multiple sessions for the
same Telegram UID use one identity: the shared session takes priority, otherwise
the first namespace in alphabetical order. Duplicate authorization keys do not
open competing connections. All discovered sessions use `TDL_PROXY`.

Discovery does not grant Bot membership: administrators still manage allowed
operators separately. Every execution account needs access to internal storage.
The Bot handles final delivery, so execution accounts do not need access to each
operator's destination. Standalone `SESSION_SOURCE=telethon` keeps its single
shared execution account.

## Channel dumps and periodic follows

`dump` copies the source history visible to the selected execution account,
from oldest to newest, processing each album as one unit. Only administrators
can use it. The upper message ID is captured when the task is created, so a busy
source does not make a dump run forever. Newer posts can be handled by a separate
follow task.

`follow` is available to every authorized member. It starts after the latest
message present at creation, checks every five minutes by default, and copies
new messages in order. It does not synchronize edits or deletions. Both modes
include albums, individual media and text, and ignore service notifications.
Text messages count as individual groups in progress counters.

```text
/dump @source_channel to @backup_channel
/dump https://t.me/source_channel
/follow @source_channel to @backup_channel every 5m
/follow @source_channel every 60s
/follow @source_channel to @backup_channel 42 every 10m
```

Sources accept usernames, Telegram homepages or message links, marked chat IDs,
and raw channel IDs. Private sources must be accessible to the execution account;
invite links are checked without automatically joining. Targets accept the same
chat IDs or usernames as `/target`, plus an optional topic ID. Prepare the target
channel/group first and give the Bot permission to post there. The Bot also
needs access to internal storage for final delivery. No channels are created.

Omitting `to` saves only to internal shared storage, with no second delivery.
Regular users see “Default storage” rather than the internal location. Existing
results are reused before any new byte transfer; an explicit destination still
receives every new source post. Starting a new dump after a completed one can
intentionally send that history again.

Ordinary members may have up to nine follows by default. Active and paused
follows both count; cancellation releases a slot. Administrators can set a
per-member limit with `/user follow <UID|@username> <0-1000>` (0 prevents
new follows and resumes). Lowering a limit preserves existing tasks, while new
follows and resumes must fit the new limit. `/user list` shows usage and limits;
`/follow` shows the caller's quota. Changes persist and appear in `/user history`.
Administrators have no count limit and can manage other members' tasks. A task retains its original
execution account and targets; later `/account`, `/target` or `/storage` changes
apply to new tasks. Removing access or banning the owner pauses affected work.
Restore the original owner's permissions/session before resuming it.

Use the same controls for both command families:

```text
/dump
/dump list 2
/dump status <id>
/dump pause <id>
/dump resume <id>
/dump cancel <id>
/follow
/follow status <id>
/follow pause <id>
/follow resume <id>
/follow cancel <id>
/follow interval <id> 10m
```

Lists show five tasks per page. Ordinary members see only their own follows;
administrators can also see dumps and other members' follows. `remove` is an
alias for `cancel`. Cancelled dump/follow tasks appear in lists for seven days,
then disappear from those lists. Their stored checkpoints and request identities
remain available for audit, duplicate-request protection and direct `status <id>`;
they cannot be resumed. Older cancellations without timestamps receive a
seven-day display window starting at upgrade.
Intervals accept seconds, minutes, hours and days (`60s`, `5m`, `1h`, `1d`), with
a minimum of 60 seconds.

Cursors advance only after a whole unit is successfully saved/delivered. Safe
network failures back off automatically, and Telegram rate limits respect the
reported delay. Backup writes are serialized and paced per execution account
(1 second per message) and per destination chat (3 seconds per message), including
all members of an album; topics share their parent chat limit. The gap is counted
after a unit finishes, avoiding bursts after long transfers. A FloodWait cools
down all backups using that account; a Bot API retry delay cools down backup
Bot deliveries globally. These conservative application limits reduce risk, but
Telegram may impose additional account- or method-specific limits. Other errors
pause the task. If a write was interrupted or its
acknowledgement was lost, check the destination and explicitly choose:

```text
/follow resume <id> retry
/follow resume <id> skip
```

The same choices work for `/dump`. `retry` attempts the unfinished unit again;
`skip` advances past that unit after your inspection. This avoids silently
repeating uncertain sends. Already delivered messages are retained when a task
is paused or cancelled; local temporary files are cleaned up.

The resident engine stores task definitions, cursors and uncertain writes in
SQLite. A frontend reload leaves current work alive. When engine code reloads,
the current unit finishes and checkpoints before handoff. A frontend health check
starts the replacement engine even without another user command, so follows
continue unattended. It does not watch source files or trigger code reloads.
An engine crash during a write pauses that task for explicit inspection.

History pages are bounded and extend through the final album boundary. Follow
backlogs are drained in further pages without waiting a full polling interval
between them. Recently published tail albums get a short settling window.
At most two backup scans run concurrently, while transfers share existing
account locks, update gates, result caches and native clone verification.
Self-copies, reading internal storage as a source, and cycles between this
workspace's follow routes are rejected.

## 旧数据库迁移

首次启动新版本时，在原数据库旁创建权限为 0600 的
`state.sqlite3.pre-workspace` 备份，并在一个事务中迁移到工作空间结构。
旧目标全部相同时迁为共享目标；有不同目标时全部保留为各账号的个人目标，
共享目标需重新设置。旧待重试任务按原目标和共享执行账号导入，旧表保留为
只读归档（程序不再写入）；缺少目标的旧重试记录留在归档中，可重新发送链接。
迁移完成后不会重复导入。成员管理升级会额外创建 `state.sqlite3.pre-members`
备份；从已有成员版本升级重试归档时创建 `state.sqlite3.pre-retry-cap`。
各迁移步骤采用事务，旧普通账号超过三条的记录归档为仅管理员可见，原链接和
任务编号保留，管理员仍可重试／取消。若迁移中断且备份已存在，程序会要求先
检查备份再重试，避免覆盖恢复资料。

## 使用

只接受授权用户的私聊消息。用户无需设置目标，直接发送 url，结果会发送到
当前 Bot 对话。可选择有权限的群／频道和话题：

```text
/target bot
/target @your_channel
/target -1001234567890 42
https://t.me/source_channel/123
```

设置其他群／频道前需将 Bot 加入，授予发送媒体权限；频道需要发布消息权限。
Bot 会验证用户的成员身份与自己的权限，接收位置只允许本人与 Bot 的私聊、
或双方均有权限的群／频道。正数 channel ID 会规范化为带 `-100` 的 ID。
`/target` 查看自己的接收位置，显示可点击的频道／群组名称；设置成功的反馈
也显示目标链接。`/status` 查看完整状态，不再提供 `/target get` 写法。
管理员可用 `/target for <UID或@用户名> [chat] [topic_id]` 查看／设置其他成员
的接收位置，或用 `/status <UID或@用户名>` 查看其状态。

管理员先将 Bot 与所有执行账号加入内部存储群／频道，再通过
`/storage <chat> [topic_id]` 配置；`/storage` 查看。内部存储不能禁止保存／转发。
普通账号的菜单、帮助、状态、任务与重试列表不展示此位置，处理进度也不透露它。
`/target` 始终配置最终接收位置，不再支持共享目标切换。旧共享目标自动作为
内部存储沿用；旧 `me` 存储需管理员重新配置。旧个人目标沿用为接收位置。

处理顺序为：定位来源 → 转发或下载上传到内部存储 → Bot 复制到接收位置。
复制保留媒体组、文字、格式和话题，去掉原发送者署名。已有有效内部结果时，
直接复用；不同执行账号也共享此缓存。内部结果失效时重新处理来源。
旧重试记录会迁移到提交者的接收位置；新任务及重试由本人／管理员管理。
修改接收位置或内部存储不改变已接收任务的快照。

每个操作账号可以连续提交多个任务，共用有界队列。相同来源消息、目标范围和
执行会话的未结束任务不会重复入队；同一媒体组的不同消息链接可能形成不同
任务，但会复用内部结果。`/jobs [页码]` 显示可管理的任务、目标和取消按钮；也可使用
`/cancel <任务编号>`，编号可用列表中的八位前缀。进度消息仍只发给任务提交
账号，协作者通过自己的 `/jobs` 入口管理，不向所有账号广播进度。

发送消息 url 后，Bot 只回复一条引用该 url 的进度消息，并在同一条消息中
依次更新：加入队列 → 定位媒体组 → 准备媒体 → 发送媒体 → 完成。
第三步按实际工作显示“正在准备 x 个媒体”“正在下载 x 个媒体”或
“正在下载并上传 x 个媒体”；原生传输未结束时不会提前显示第四步。
第四步仅在开始发送至接收位置时显示“正在发送 x 个媒体”，完成后显示
“已发送 x 个媒体及文字”。所有路径均不显示缓存、复用或存储位置等内部信息。

进度消息下有“取消”按钮。排队、等待 tdl 更新、定位、下载和上传阶段均可
取消；下载进程退出并清理本地临时文件后，原消息会更新为“已取消”。完成、
失败、取消后移除按钮。按钮绑定具体任务，操作时验证本人／管理员管理权限及
按钮来源；旧按钮不会取消后续的新任务。上传期间取消时，已发送到 Telegram
的媒体可能保留。

| 命令 | 行为 |
| --- | --- |
| `/start`、`/help` | 使用说明 |
| `/whoami` | 显示自己的 Telegram 用户 ID |
| `/language [en\|zh]` | 查看或设置自己的界面语言；默认英文 |
| `/target bot` / `/target <chat> [topic_id]` | 设置最终接收位置；默认 Bot 对话 |
| `/storage [chat] [topic_id]` | 仅管理员查看／配置内部存储 |
| `/target for <账号 ID> [chat] [topic_id]` | 管理员查看／设置其他账号的个人目标 |
| `/status [UID或@用户名]` | 查看当前目标范围、执行账号和可管理任务数量 |
| `/jobs [页码]` | 查看可管理任务及取消按钮 |
| `/cancel <任务编号>` | 取消可管理的任务并等待清理 |
| `/pending [页码]` | 查看可管理的待重试列表及移除按钮 |
| `/retry_cancel <编号>` | 取消／移除待重试记录 |
| `/join <群主页或邀请 url> [编号]` | 使用失败任务的原执行账号入群并重试 |
| `/retry [编号]` | 使用失败任务的原目标、范围和执行账号重试 |
| `/update_tdl [latest\|v0.20.4]` | 管理员检查版本，按需更新 tdl |
| `/user` | 显示用户管理子命令用法（仅管理员） |
| `/user list` | 管理员查看成员角色和封禁状态 |
| `/user add <账号 ID>` | 添加普通账号 |
| `/user remove <账号 ID>` | 删除账号并取消它提交的活动任务 |
| `/user ban <账号 ID> <1d\|1w\|1m>` | 封禁一天、一周或 30 天并取消活动任务 |
| `/user unban <账号 ID>` | 提前解除封禁 |
| `/user role <账号 ID> <user\|admin>` | 设置普通账号／管理员角色 |
| `/user history <账号 ID>` | 查看最近十次账号管理操作 |
| `/user follow <UID或@用户名> <0-1000>` | 设置普通用户的 follow 数量上限 |

支持公开消息、`/c/` 私密消息、forum topic、`/s/` 预览 url、`?thread=` 和
`?comment=` 讨论群评论 url；forum url 使用最后一段消息 ID。仅发送一个 url
即可，文字可绑定在媒体组的任意成员上。会沿消息历史向两个方向匹配相同
`grouped_id`，避免消息 ID 不连续时漏掉成员。

来源无权限时，Bot 将每个失败任务保存到待重试列表，失败记录不会互相覆盖。
先用 `/pending` 查看编号，再发送 `/join https://t.me/+邀请码 <编号>` 或
`/retry <编号>`。仅有一个可管理的待重试任务时可以省略编号，也可直接发送
群主页／邀请 url。需要群管理员审核时，先等待批准，再发送 `/retry <编号>`。
重试和入群沿用失败时的执行会话、目标及管理范围，换操作账号不会改变身份。
原执行会话绑定被删除时会明确报错，不会悄悄使用共享账号替代。

`/retry_cancel <编号>` 或待重试列表中的“移除”按钮可取消记录，不会入群或
重新传输。正在重试的记录需要先取消活动任务再移除，避免移除后又被失败任务
重新写入。记录只允许本人和管理员管理。

普通账号最多保存 **自己提交的三条**有效待重试记录，入群／入频道等待审批
也沿用原记录，不另占名额。达到上限后不能再提交新链接，但可以重试和清理。
并发提交的更多任务若随后无权限，超过容量的失败不会加入列表，原进度消息会
明确提示清理后重新发送链接。管理员不限额；`/status` 显示自己的名额占用。
升级前的多余记录，以及管理员降为普通账号时超过三条的记录，保留为仅管理员
可见的归档；管理员可在 `/pending` 中重试或移除，不会静默丢失原链接。

命令路由和 Telegram 输入提示来自同一份注册表，启动时自动安装菜单。
普通账号显示常用命令，管理员额外显示成员管理和更新命令；角色改变后同步
菜单，封禁或删除账号仅保留 `/whoami` 提示。新成员尚未开始与 Bot 私聊时，
权限仍会生效，菜单在其首次操作时再次同步。菜单展示不能替代服务端权限检查。

## 成员管理

管理员可以直接添加、删除、封禁、解封和调整角色，无需修改 `.env` 或重启：

```text
/user add <账号 ID>
/user ban <账号 ID> 1d
/user ban <账号 ID> 1w
/user ban <账号 ID> 1m
/user unban <账号 ID>
/user role <账号 ID> admin
/user remove <账号 ID>
```

`1m` 固定为 30 天。封禁期限以 UTC 时间戳持久化，到期无需定时任务或重启
即可恢复权限；成员列表按 Asia/Tokyo 显示期限。封禁／删除会撤销消息命令、
按钮和新任务提交权限，并取消该账号已经提交的活动任务，等待临时文件清理。
已经完成的上传不会撤回。删除成员不删除服务器上的 tdl 登录会话、历史个人
目标或待重试记录，管理员仍可查看／处理这些记录。

添加、删除、封禁、解封和角色改变记录到数据库，可用 `/user history` 查看。
系统拒绝封禁、删除或降级最后一个有效管理员；也拒绝在 Bot 内封禁／删除
管理员自己的账号。权限在每次操作时重新检查，旧按钮和旧菜单不会绕过封禁。
入群不能绕过封禁、历史不可见或消息已删除的限制。

## 下载、清理与更新

- 一个工作空间共用有界队列，默认两个 worker、最多 20 个等待任务，无按账号
  限制单任务的规则。多个任务可在下载／上传阶段交错执行；同一执行会话的
  tdl 和 Telethon RPC 串行协调，不同会话可并发。同一存储目录的 tdl 子进程
  也串行运行，兼容 legacy 数据库锁。所有会话共用 tdl 更新锁，
  更新等待全部执行中任务和入群操作结束，期间新任务排队。
- 默认共享 tdl 授权时，Telethon 每个 RPC 都使用自己的协议层重新初始化
  连接上下文，并与 tdl 子进程互斥；不订阅用户账号的无关群聊更新。这样避免
  tdl 下载后将协议层切换到不同版本，导致上传返回 `TypeNotFoundError`。
- 服务输出带时间戳的任务阶段日志（resolve、download、upload、complete）；
  不记录来源 url、视频文字、授权密钥或消息报文。
- 每个任务使用独立的 `data/jobs/job-*` 临时目录。按消息 ID 检查完整性后
  才上传；下载不全、大小不符时不会上传残缺媒体组。
- 成功、失败、取消均清理该任务的本地媒体。子进程超时或取消时先终止并
  等待 tdl 退出，再删除目录，防止下载进程继续写入。
- 交互进程重启／退出不会停止任务引擎或 tdl。完整停服使用 `tdl-bot stop`，
  等待已经接收的任务完成后引擎退出。断电／强制终止任务引擎可能遗留文件，
  下次启动清理本 Bot 创建的 `job-*` 目录；既有 `downloads/` 不属于任务清理。
- 更新先比较官方 release 和本地版本，已满足要求时不占更新锁、不下载或替换。
  需要更新才阻止新任务开始，等待活动任务完成下载、上传和清理，然后下载
  官方 GitHub release，校验 SHA-256 和可执行程序版本，最后原子替换
  `TDL_BINARY`。更新失败保留旧程序；更新结束后排队任务继续执行。
- 进程锁禁止多个 Bot 实例共享 data 目录、tdl 程序或同一 tdl namespace。
  运行期间应通过
  `/update_tdl` 更新程序；外部进程直接替换或运行同一 tdl 会话不受此锁管理。

## 热重启与任务状态

```bash
uv run tdl-bot                 # 启动；代码修改不会触发自动重载
uv run tdl-bot help            # 查看所有子命令
uv run tdl-bot help login      # 查看登录选项
uv run tdl-bot login help      # 同上，也支持 login -h / --help
uv run tdl-bot reload          # 自动重载唯一运行实例
uv run tdl-bot reload -a       # 重载当前系统用户的全部运行实例
uv run tdl-bot reload -e ./workspace/.env  # 指定实例
uv run tdl-bot stop            # 停止交互进程，引擎完成已接收任务后退出
```

架构分为交互进程管理器、Bot 交互进程、常驻任务引擎。管理器仅响应手动重载，
并在交互进程意外退出时恢复；不扫描目录或检测文件变化。交互通过权限为 `0600`
的本机 Unix socket 通信；任务引擎独占 tdl／Telethon 会话和子进程。
交互重启立即重连引擎，下载、上传和清理持续进行；进度消息、取消按钮、队列、
目标快照和接收消息去重记录持久化。重复投递同一 Bot 消息不会重复创建任务。
执行手动 reload 时，交互加载新代码；任务引擎在当前队列空闲、RPC 操作完成后
退出，下次操作自动启动新版本。持续有任务时，引擎保留旧代码直到空闲。

Ctrl+C 只停止交互管理器和交互，引擎仍继续运行。
从旧自动监控版本升级时，先 Ctrl+C 再运行 `uv run tdl-bot` 一次，
让管理器加载手动模式；单独 reload 只替换交互子进程，不替换旧管理器。重新运行命令即可重连。
引擎日志在 `data/engine.log`。`engine`、`frontend` 是内部子命令，一般无需手动调用。
配置、依赖或数据库结构不兼容的修改应完整 `stop` 后启动；本次首次升级旧的
单进程服务也需要先正常停止，再运行新版。数据目录、会话和 Bot token 不应
在执行任务期间替换。

任务引擎意外退出时，下一次启动恢复尚未执行远程写入的任务；若中断发生在
上传／发送过程中，程序会提示检查接收位置后重新提交，避免自动重复发送。
这类崩溃恢复不能保证 Telegram 写入恰好一次；正常热重启不终止执行进程。
数据库升级到版本 8；已有版本 5 先生成 `state.sqlite3.pre-history` 私有备份，
版本 6 先生成 `state.sqlite3.pre-availability`，补齐早期版本可能缺少的缓存可用性字段。
版本 7 升级前生成 `state.sqlite3.pre-backups`，新增 dump/follow 持久化任务表。
交互进程不迁移仍由旧引擎使用的数据库，新引擎空闲切换后负责升级。

## 配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `BOT_TOKEN` | 必填 | aiogram Bot token |
| `SESSION_SOURCE` | `tdl` | 复用已有 tdl 授权；`telethon` 为独立登录模式 |
| `TDL_DIR` | `DATA_DIR/tdl` | 已登录的 tdl 根目录（其下包含 `data/` 或 `data.kv`） |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | 默认不需要 | 仅独立登录模式需要 |
| `ADMIN_USER_IDS` | 首次初始化必填 | 显式指定初始管理员 UID；初始化后由数据库管理 |
| `DATA_DIR` | 显式配置 | SQLite、任务临时目录和日志 |
| `BIN_DIR` | 显式配置 | 按系统／架构保存托管 tdl；已有 `TDL_BINARY` 可替代此项 |
| `RUNTIME_DIR` | `DATA_DIR` | socket、PID 和交互进程锁；Compose 使用容器内 tmpfs |
| `TDL_BOT_ENV_FILE` | 当前目录 `.env` | 配置文件；CLI `--env-file` 优先 |
| `TDL_BINARY` | 可选 | 原生运行时显式覆盖托管 tdl 可执行程序路径 |
| `TDL_NAMESPACE` | `default` | tdl 登录 namespace |
| `TDL_PROXY` | 空 | tdl 和 Telethon 共享的 socks5／socks4／http 代理 |
| `WORKERS` | `2` | 后台任务 worker 数量 |
| `QUEUE_SIZE` | `20` | 最多排队任务数量 |
| `DOWNLOAD_TIMEOUT` | `7200` | 单次 tdl 下载超时，秒 |
| `UPLOAD_CONCURRENCY` | `8` | tdl clone 每文件传输线程数；独立 Telethon 模式的分片窗口，1–32 |
| `UPLOAD_TIMEOUT` | `7200` | 单次 tdl clone 转存超时（含下载和上传），秒 |

## 验证

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check
```

完整 pytest 默认统计全部 `tdl_bot` 业务代码的行与分支覆盖率，综合覆盖率
低于 **85%** 时失败。不会排除 CLI、本机 RPC 或其他低覆盖模块。
临时只跑某个文件时可用 `uv run pytest tests/test_dispatcher.py --no-cov`；
提交前仍运行完整检查，不能用局部运行替代覆盖率门槛。

测试侧重权限、状态变化和跨组件流程。`test_dispatcher.py` 使用真实 aiogram
Dispatcher、中间件、服务、SQLite 和 Unix RPC，仅替换外部 Telegram／下载
接口；`test_cli.py` 验证启动路由、进程锁、reload／stop 和交互进程退出清理。
这些均不需要真实 Telegram 凭据，不会向真实聊天发送消息。

Ruff 负责代码规范、导入排序和格式化；ty 负责类型检查与编辑器补全、跳转。
ty 固定在开发依赖中，编辑器可使用 `ty.importStrategy = "fromEnvironment"`
以保持版本一致。日常编辑建议 `ty.diagnosticMode = "openFilesOnly"`，提交前
执行上面的完整检查；无需同时启用 Pylance。

业务代码保留默认类型检查，仅对 Telethon 已确认的动态接口和不完整注解使用
带具体规则及原因的局部豁免。测试目录仅放宽 `invalid-argument-type`，允许
轻量测试替身代替真实服务，其余检查仍开启。`unused-ignore-comment` 会提醒
已经不再需要的豁免。

整个项目按需使用类型标注，不要求每个函数、参数或返回值都写标注。能确定的
类型正常标注；难以确定的框架参数（如 `handler`）可以省略，同一签名中的
`event`、`data` 等仍可保留标注。Ruff 不启用强制标注的 `ANN` 规则，ty 默认
允许省略标注，继续检查已标注或可推断的类型。通过 `isinstance`、空值判断
和局部变量推断补充信息。未知类型会降低静态检查覆盖率，因此复杂业务仍需要
测试验证，不使用全局 `Any` 或关闭业务代码的参数、属性检查来掩盖问题。

测试不需要真实 Telegram 凭据，覆盖 url、媒体组补齐、文字／格式保留、私密群
权限和入群重试、目标持久化、队列限制、下载完整性、异常／取消清理、子进程
回收，以及更新等待、校验失败保留旧版、原子安装。会话复用测试覆盖原授权
密钥、DC／IPv6／空地址配置、namespace 与应用选择、临时备份清理、无需 API
凭据、不触发重新登录，以及下载和备份使用同一存储。
交互测试覆盖单条引用消息的五阶段编辑、取消权限、旧按钮失效、排队／定位／
下载／上传／更新等待阶段取消、取消后的本地清理与 worker 继续处理任务。
真实 Telegram 的下载／上传需配置凭据和可访问的来源、目标后验收。
代码使用四空格缩进、类型标注、Google 风格 docstring 和 80 列格式。

工作空间测试覆盖旧数据库备份、迁移幂等和失败回滚、共享／个人可见性、管理员
覆盖、跨账号取消、专属执行账号路由、重试快照、账号绑定验证和连接清理。
管理测试覆盖权限即时生效、封禁到期、最后管理员保护、角色菜单、待重试移除、
并发容量限制、历史记录归档、封禁时活动任务清理，以及已是最新版时跳过更新。

接口依据：[tdl 下载](https://docs.iyear.me/tdl/guide/download/)、
[Telegram Bot API](https://core.telegram.org/bots/api)、
[Telethon 客户端](https://docs.telethon.dev/en/stable/modules/client.html)、
[Telethon 入群](https://docs.telethon.dev/en/stable/examples/chats-and-channels.html)。
会话格式参照本地 `data/tdl/core/storage/session.go`、
`data/tdl/pkg/kv/bolt.go`、`data/tdl/app/migrate/backup.go`、
`data/tdl/pkg/tclient/app.go` 及
[gotd session schema](https://raw.githubusercontent.com/gotd/td/v0.140.0/session/session.go)。

用户名、目标查询和媒体复用：

- 用户管理命令接受 UID 或 `@用户名`，例如 `/user add @example`、
  `/user ban @example 1d`、`/user role @example user`。
  用户名通过共享执行账号实时解析；频道、群和 Bot 不能作为成员。
  权限始终绑定 UID，用户名变更不会转移权限。没有用户名的账号仍使用 UID。
- `/user list` 显示用户名（未知时显示姓名或提示）、UID 和当前目标；
  管理操作反馈也包含账号身份。成员与 Bot 交互时会更新身份资料。
- `/status` 将接收位置显示为可点击的 Bot、频道或群链接，不透露内部存储。
  未配置话题时不显示“默认话题”；配置论坛群分区时显示指定话题并链接到它。
  管理员可用 `/status @example` 或 `/status 123456789` 查询其他账号，
  `/target for @example` 查询该账号个人目标。
- 媒体组优先通过 Telegram 在服务器端转发，去掉原发送者署名，保留文字、
  格式和目标话题。来源禁止转发时仍使用原来的 tdl 下载与上传路径。
- 成功处理后保存结果消息 ID；重复链接、同一媒体组的其他成员链接，或
  Telegram 媒体 ID、文字和格式一致的内容，优先转发已有结果，不重新下载。
  内部历史结果被删除时重新处理来源，发送失败后再次提交可复用已准备结果。
  记录从本次版本开始积累，旧版本没有保存结果消息 ID，无法追溯旧上传。
- 从版本 3 升级会先生成权限为 `0600` 的 `state.sqlite3.pre-results` 备份；
  从版本 4 升级生成 `state.sqlite3.pre-runtime`；数据库当前版本为 7；版本 5 升级生成 `state.sqlite3.pre-history`，
  版本 6 升级生成 `state.sqlite3.pre-availability`。


媒体显示与历史记录：

- 转发到 storage 后移除媒体剧透隐藏标记和文字 spoiler entities；只修改存储
  副本，不改动来源。上传默认关闭媒体隐藏，并保留其他文字格式。旧 storage
  结果首次复用时也会处理；完成后保存标记，后续直接复制，不重复编辑。
- `stored_results` 保留全部已知存储结果，`result_sources` 将来源链接和媒体指纹
  关联到结果；同一来源新增结果不会覆盖旧结果。`delivery_history` 保存每次
  最终发送的目标、返回消息 ID、状态与错误类型；发送前先记下尝试，失败和部分
  成功也保留记录。重启不清空历史，失效结果仅标记不可用，不删除。
- 重复内容可跨执行账号复用已有 storage 结果，也可以复用仍可访问的旧存储。
  相同链接／媒体组并发提交时只准备一份媒体；每个用户仍各自收到一次结果。
- v5 迁移保留现有缓存和原表。以前被旧版覆盖的记录、没有保存的最终消息 ID
  无法自动补回；新记录从本版开始完整保存。Telegram 写入响应丢失时仍需检查
  接收位置，不能将本地历史当作远程发送恰好一次的保证。

## Source architecture

See [the architecture guide](docs/architecture.md) for package boundaries,
the transfer flow, and the division of work between tdl and Telegram APIs.

## Upload performance

With `SESSION_SOURCE=tdl` (the default), protected media uses native
`tdl forward --mode clone` with the selected account's existing namespace,
storage and proxy. Cached results and direct server-side copies remain preferred.
Native clone preserves original document attributes and previews; Python does
not download thumbnails or rebuild video metadata. Personal bindings use the
same path. `UPLOAD_CONCURRENCY=8` sets native transfer threads per file, while
`UPLOAD_TIMEOUT=7200` bounds the complete clone, including download and upload.

The Bot requests individual clone receipts, verifies every expected member and
its document size, then assembles the album using existing Telegram media
references. This restores original order, captions and formatting, removes
spoilers, and records the final album for reuse and delivery. Telethon does not
upload file bytes again. Native temporary files are confined to the job's own
directory, which is removed after the child process exits, including cancellation.

Temporary storage posts are removed after success, failure or cancellation.
Only this task's tagged messages are eligible for removal; unrelated messages
are untouched. The execution account must be able to delete its staging posts.
Cleanup failures are logged; a forced engine crash can leave staging posts for
an administrator to remove. Frontend reloads leave the engine and tdl running.

Standalone `SESSION_SOURCE=telethon` retains its download and Telethon upload
path because it does not provide an existing native tdl login. Its large-file
upload window and native `cryptg` acceleration remain available.

Native clone logs include verified media count and duration without source
URLs or captions. Use manual reload to adopt new code after old engine tasks
finish. Actual throughput depends on Telegram, the proxy and the network.


### Link and join activity

Only two kinds of user activity are mirrored to internal storage: submitted media
links and explicit `/join` actions. A new media task posts the operator and source
link before transferring media. Repeated content adds a link to the existing
stored media instead of another media copy. Join records retain the initiating
user, execution account, supplied link, resolved chat identity when available,
and result (joined, approval pending, or failed). Other commands and background
backup units do not create activity posts.

Administrators can use `/user activity` or `/user activity <UID|@username>` to
view 20 compact records per page with pagination buttons. Both the list and
shared-target posts use `timestamp · @username · Link` (or `Join`). Failed or
cancelled links are struck through; successful rows need no status line. A
successful media row links its timestamp to the stored media, preserving reuse
navigation without an extra field. Pending approval keeps a short status suffix.
The database retains UIDs for accurate
filtering; displays prefer `@username` and only fall back to a UID when needed.
Activity records are durable across reloads. Database schema v9 adds the activity
table and backs up an existing v8 database before migrating. No past actions are
reconstructed automatically.


Bot subcommands accept unique prefixes: `/user h 123` and `/user hist 123`
mean `/user history 123`; `/follow p <id>` pauses a follow, and
`/follow res <id>` resumes it. `/target b` selects the Bot conversation;
`/account a` selects automatic routing. Full words remain supported.
Ambiguous prefixes only show candidates and never execute an operation:
`/user a` matches `add` and `activity`, `/user r` matches `remove` and `role`,
and `/follow r` matches `remove` and `resume`. Use a longer prefix to choose.
`/user help` remains explicit so `/user h` selects history. Usernames, links,
IDs and other values are preserved; only command words and option keywords
are matched.
