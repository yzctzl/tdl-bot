# tdl-bot

使用 aiogram 3 将 tdl 封装成 Telegram Bot。发送一个消息 **url**，自动定位
其所在的整个 **media group**，下载、重新上传到用户指定的聊天／频道／话题，
并保留各媒体绑定的**视频文字**与文字格式。

这里将 Agents.md 中的“目录”解释为 Telegram 上传目标（聊天／频道及其话题）。
服务器临时目录仅用于中转，上传结束后删除。

## 运行条件

- Python 3.12+、uv，Linux 或 macOS。
- 由 @BotFather 创建的 Bot token。
- 已登录 Telegram 用户账号的项目 `./.tdl` 目录。
- tdl 可执行文件；当前项目中的 `bin/tdl` 可以直接使用。
- 默认直接复用 tdl 的已有授权。来源群的读取和入群、目标的写入均使用
  该账号，Bot 负责交互与任务调度，无需让 tdl-bot 再次登录。

Bot API 没有按任意消息 url 获取历史消息／整个媒体组的接口。仅用 aiogram
不能从历史 url 补齐其余成员，所以使用 Telethon 用户会话读取消息和入群。
tdl 负责下载，Telethon 负责按原顺序重新上传完整媒体组，保留原 MIME、视频
属性、文字和 entities；通过用户账号上传也支持较大的视频。

## 安装与登录

在项目根目录执行：

```bash
uv sync
cp .env.example .env
```

编辑 `.env`，填写 `BOT_TOKEN` 和 `ALLOWED_USER_IDS`，保留默认会话配置：

```dotenv
SESSION_SOURCE=tdl
TDL_DIR=.tdl
TDL_NAMESPACE=default
```

授权名单使用 Telegram **用户 ID**，以逗号分隔。
可在 Bot 启动后通过 `/whoami` 查询用户 ID；这个命令不需要授权。
`ADMIN_USER_IDS` 留空时，所有授权用户均可更新 tdl；填写时必须是授权名单子集。

已有 `./.tdl/data/default` 会话时直接启动：

```bash
uv run tdl-bot
```

`TDL_DIR` 指 tdl 根目录，而不是其中的 `data` 子目录。Bot 显式传入
`--storage type=bolt,path=<TDL_DIR>/data`，不再依赖 tdl 默认的 `~/.tdl`。
也支持仅有 `data.kv`、尚未迁移到 `data/` 的旧版 legacy 存储。
namespace 如有调整，需同步修改 `.env` 的 `TDL_NAMESPACE`。

Bot 启动时调用 tdl 原生 `backup` 接口读取 Bolt 存储，将既有 gotd 授权密钥、
DC 配置和应用信息转换为 Telethon **内存会话**。临时备份在权限为 0700 的
目录内，读取后立即删除；不会生成 `data/telegram.session`，也不会修改
tdl 的授权密钥或要求输入验证码。读取失败时直接报错，不会回退到重新登录。
`uv run tdl-bot login` 在默认模式下只检查既有授权是否有效。

如果需要检查原 tdl 会话，可以直接执行：

```bash
bin/tdl --storage type=bolt,path=./.tdl/data --ns default chat ls
```

共享授权需要 tdl 和 Telethon 使用相同的网络出口；配置 `TDL_PROXY` 时两者
同步使用该代理（支持 socks5、socks4、http）。运行期间避免在不同网络出口
的其他机器上同时使用这份授权。

保留了独立 Telethon 登录模式供旧配置使用：显式设置
`SESSION_SOURCE=telethon`，填写从 <https://my.telegram.org> 获取的
`TELEGRAM_API_ID` 和 `TELEGRAM_API_HASH`，再执行 `uv run tdl-bot login`。
仅此模式会生成 `data/telegram.session`，并要求其账号与 tdl 下载账号一致。
请保护 `.env` 和 `.tdl` 会话目录。

## 使用

只接受授权用户的私聊消息。先设置上传目标，然后发送一个完整消息 url：

```text
/target me
/target @your_channel
/target 1234567890
/target -1001234567890 42
https://t.me/source_channel/123
```

`me` 是用户账号的收藏夹；第二个参数是目标 forum topic ID。
正数数字目标按原始 channel ID 解析，例如 `1234567890` 等同于
`-1001234567890`；带 `-100` 的频道 ID、负数群 ID 和 `@username` 仍可使用。
目标必须是该用户账号可以访问并写入的聊天，设置目标不会自动入群。
每个用户独立保存目标；已排队任务使用提交时的目标。

发送消息 url 后，Bot 只回复一条引用该 url 的进度消息，并在同一条消息中
依次更新：加入队列 → 定位媒体组 → 下载 x 个媒体文件 → 上传 x 个媒体文件
→ 完成（已上传 x 个媒体及视频文字）。

进度消息下有“取消”按钮。排队、等待 tdl 更新、定位、下载和上传阶段均可
取消；下载进程退出并清理本地临时文件后，原消息会更新为“已取消”。完成、
失败、取消后移除按钮。按钮绑定具体任务，只有任务所属用户可以操作；旧按钮
不会取消后续的新任务。上传期间取消时，已发送到 Telegram 的媒体可能保留。

| 命令 | 行为 |
| --- | --- |
| `/start`、`/help` | 使用说明 |
| `/whoami` | 显示自己的 Telegram 用户 ID |
| `/target <chat> [topic_id]` | 保存上传目标；无参数时显示当前状态 |
| `/status` | 查看目标、任务状态和待权限重试状态 |
| `/join <群主页或邀请 url>` | 加入来源群并自动重试最后一个无权限 url |
| `/retry` | 重试最后一个无权限 url |
| `/update_tdl [latest\|v0.20.4]` | 管理员更新 tdl |

支持公开消息、`/c/` 私密消息、forum topic、`/s/` 预览 url、`?thread=` 和
`?comment=` 讨论群评论 url；forum url 使用最后一段消息 ID。仅发送一个 url
即可，文字可绑定在媒体组的任意成员上。会沿消息历史向两个方向匹配相同
`grouped_id`，避免消息 ID 不连续时漏掉成员。

来源无权限时，Bot 会记住该用户最后一个失败 url 并提示提供来源群链接。
此时发送 `/join https://t.me/+邀请码` 或直接发送群主页／邀请 url，用户账号
加入后自动重试。需要群管理员审核时，先等待批准，再发送 `/retry`。
入群不能绕过封禁、历史不可见或消息已删除的限制。

## 下载、清理与更新

- 全局有界队列，默认两个 worker、最多 20 个等待任务，每个用户最多一个
  未完成任务。多个任务可以在下载／上传阶段交错执行；tdl 子进程串行运行，
  避免默认 Bolt 会话存储的数据库锁冲突。
- 默认共享 tdl 授权时，Telethon 每个 RPC 都使用自己的协议层重新初始化
  连接上下文，并与 tdl 子进程互斥；不订阅用户账号的无关群聊更新。这样避免
  tdl 下载后将协议层切换到不同版本，导致上传返回 `TypeNotFoundError`。
- 服务输出带时间戳的任务阶段日志（resolve、download、upload、complete）；
  不记录来源 url、视频文字、授权密钥或消息报文。
- 每个任务使用独立的 `data/jobs/job-*` 临时目录。按消息 ID 检查完整性后
  才上传；下载不全、大小不符时不会上传残缺媒体组。
- 成功、失败、取消均清理该任务的本地媒体。子进程超时或取消时先终止并
  等待 tdl 退出，再删除目录，防止下载进程继续写入。
- 正常停止 Bot 会等待已接收任务和更新结束。强制终止／断电可能遗留临时
  文件，下次启动会清理本 Bot 创建的 `job-*` 目录。既有 `downloads/` 文件
  不属于这些任务，不会被启动清理处理。
- 更新请求阻止新任务开始，等待活动任务完成下载、上传和清理，然后下载
  官方 GitHub release，校验 SHA-256 和可执行程序版本，最后原子替换
  `TDL_BINARY`。更新失败保留旧程序；更新结束后排队任务继续执行。
- 进程锁禁止多个 Bot 实例共享 data 目录、tdl 程序或同一 tdl namespace。
  运行期间应通过
  `/update_tdl` 更新程序；外部进程直接替换或运行同一 tdl 会话不受此锁管理。

队列在内存中，重启后不会自动恢复已接收任务；上传目标和最后一个无权限
url 则持久化到 SQLite。Telegram 上传并非事务，若中途失败，目标里可能已有
部分媒体，重试前先检查目标以避免重复。

## 配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `BOT_TOKEN` | 必填 | aiogram Bot token |
| `SESSION_SOURCE` | `tdl` | 复用已有 tdl 授权；`telethon` 为独立登录模式 |
| `TDL_DIR` | `.tdl` | 已登录的 tdl 根目录（其下包含 `data/` 或 `data.kv`） |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | 默认不需要 | 仅独立登录模式需要 |
| `ALLOWED_USER_IDS` | 必填 | 授权用户 ID，逗号分隔 |
| `ADMIN_USER_IDS` | 授权用户全体 | 可更新 tdl 的用户 ID |
| `DATA_DIR` | `data` | 会话、SQLite 和任务临时目录 |
| `TDL_BINARY` | `bin/tdl` | tdl 可执行程序路径 |
| `TDL_NAMESPACE` | `default` | tdl 登录 namespace |
| `TDL_PROXY` | 空 | tdl 和 Telethon 共享的 socks5／socks4／http 代理 |
| `WORKERS` | `2` | 后台任务 worker 数量 |
| `QUEUE_SIZE` | `20` | 最多排队任务数量 |
| `DOWNLOAD_TIMEOUT` | `7200` | 单次 tdl 下载超时，秒 |

## 验证

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check
```

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

接口依据：[tdl 下载](https://docs.iyear.me/tdl/guide/download/)、
[Telegram Bot API](https://core.telegram.org/bots/api)、
[Telethon 客户端](https://docs.telethon.dev/en/stable/modules/client.html)、
[Telethon 入群](https://docs.telethon.dev/en/stable/examples/chats-and-channels.html)。
会话格式参照本地 `data/tdl/core/storage/session.go`、
`data/tdl/pkg/kv/bolt.go`、`data/tdl/app/migrate/backup.go`、
`data/tdl/pkg/tclient/app.go` 及
[gotd session schema](https://raw.githubusercontent.com/gotd/td/v0.140.0/session/session.go)。
