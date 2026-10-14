# tdl-bot

面向一个人及其多个 Telegram 账号／协作者的共享工作空间。使用 aiogram 3
将 tdl 封装成 Telegram Bot。发送一个消息 **url**，自动定位
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

初始化授权名单使用 Telegram **用户 ID**，以逗号分隔。
可在 Bot 启动后通过 `/whoami` 查询用户 ID；这个命令不需要授权。
`ADMIN_USER_IDS` 显式指定管理员；留空时只有 `ALLOWED_USER_IDS` 中**第一个**
账号为管理员（按配置顺序，不按 ID 大小）。填写时必须是授权名单子集。
升级时请检查名单顺序，或显式填写管理员；普通授权账号可管理共享任务，管理员
可管理全部任务并更新 tdl。

首次启动这版管理功能时，将 `.env` 的名单和角色初始化到 SQLite。此后数据库
是成员权限的来源：通过 Bot 添加、删除、封禁或修改角色即可即时生效，重启
不会将已删除账号从 `.env` 重新加回，也不会重置角色。`.env` 中的名单继续
保留为初始化配置；修改它不会覆盖已经初始化的成员列表。

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

## 专属执行账号（可选）

操作账号指给 Bot 发消息的账号；执行账号指下载、读取历史、入群和上传使用的
Telegram 用户会话。二者分开管理，默认所有操作账号使用 `.env` 中的共享
执行会话。有自己的已登录 tdl 会话时，在服务器配置绑定；无需让 Bot 再登录。

将 `accounts.example.toml` 复制到 `data/accounts.toml`，替换示例操作账号 ID
和已有 namespace。也可通过 `ACCOUNTS_FILE` 指定其他位置：

```toml
[[accounts]]
operator_id = 123456789
namespace = "personal"
tdl_dir = ".tdl"
# proxy = "socks5://127.0.0.1:1080"
```

只为有专属会话的操作账号添加条目；未配置的账号自动使用共享会话。每个操作
账号最多绑定一个会话，`tdl_dir` 可省略以继承 `.env` 的 `TDL_DIR`；相对路径
统一从启动时的项目工作目录解析。`proxy` 省略时继承 `TDL_PROXY`，填写空串
表示该账号直连。实际绑定配置放在被 Git 忽略的 `data/` 内，不提交真实 ID。

启动时验证会话登录状态及 Telegram 账号 ID 必须匹配 `operator_id`，校验
失败会退出并关闭所有连接，不自动退回共享身份。绑定相同授权的会话副本会
被拒绝；如需绑定共享会话自身的账号，直接使用其原目录和 namespace。
添加／修改绑定后需要重启服务。会话配置本身不会授予 Bot 访问权限：可以先
在服务器准备绑定，再由管理员 `/user_add` 授权；只有有效成员才能提交任务。
专属执行账号仍可使用共享目标，个人目标也可
使用共享执行账号；`me` 始终指该任务**执行账号**的收藏夹。

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
默认所有操作账号共用一个上传目标。仅管理员可用
`/target shared <chat> [topic_id]` 设置／修改共享目标。
`/target private <chat> [topic_id]` 设置并选择本账号的个人目标。
不带范围的 `/target <chat> [topic_id]` 对管理员设置共享目标，对普通账号
设置个人目标，普通账号无法通过任何写法修改共享目标。
`/target shared` 和 `/target private` 切换已经保存的目标；新账号默认选共享。
管理员可用 `/target for <操作账号 ID> [<chat> [topic_id]]` 查看／设置该账号
的个人目标。修改目标不会改变已排队任务或待重试任务中的目标快照。

目标范围决定管理权限，与使用哪个执行账号无关：共享目标的任务由任意授权账号
管理；个人目标的任务仅本人和管理员可见、可取消、可重试。更改范围只影响新
提交的任务，旧任务继续沿用提交时的管理范围。
所有目标、目标选择、成员角色、封禁期限、管理记录及待重试记录均存储在同一个
SQLite 数据库中。普通账号可使用／切换共享目标，但不能修改它。

每个操作账号可以连续提交多个任务，共用有界队列。相同来源消息、目标范围和
执行会话的未结束任务不会重复入队；同一媒体组的不同消息链接仍可能形成不同
任务。`/jobs [页码]` 显示可管理的任务、目标和取消按钮；也可使用
`/cancel <任务编号>`，编号可用列表中的八位前缀。进度消息仍只发给任务提交
账号，协作者通过自己的 `/jobs` 入口管理，不向所有账号广播进度。

发送消息 url 后，Bot 只回复一条引用该 url 的进度消息，并在同一条消息中
依次更新：加入队列 → 定位媒体组 → 下载 x 个媒体文件 → 上传 x 个媒体文件
→ 完成（已上传 x 个媒体及视频文字）。

进度消息下有“取消”按钮。排队、等待 tdl 更新、定位、下载和上传阶段均可
取消；下载进程退出并清理本地临时文件后，原消息会更新为“已取消”。完成、
失败、取消后移除按钮。按钮绑定具体任务，操作时验证共享／个人管理权限及
按钮来源；旧按钮不会取消后续的新任务。上传期间取消时，已发送到 Telegram
的媒体可能保留。

| 命令 | 行为 |
| --- | --- |
| `/start`、`/help` | 使用说明 |
| `/whoami` | 显示自己的 Telegram 用户 ID |
| `/target [shared\|private] <chat> [topic_id]` | 设置共享／个人目标；不带聊天时切换目标 |
| `/target for <账号 ID> [chat] [topic_id]` | 管理员查看／设置其他账号的个人目标 |
| `/status` | 查看当前目标范围、执行账号和可管理任务数量 |
| `/jobs [页码]` | 查看可管理任务及取消按钮 |
| `/cancel <任务编号>` | 取消可管理的任务并等待清理 |
| `/pending [页码]` | 查看可管理的待重试列表及移除按钮 |
| `/retry_cancel <编号>` | 取消／移除待重试记录 |
| `/join <群主页或邀请 url> [编号]` | 使用失败任务的原执行账号入群并重试 |
| `/retry [编号]` | 使用失败任务的原目标、范围和执行账号重试 |
| `/update_tdl [latest\|v0.20.4]` | 管理员检查版本，按需更新 tdl |
| `/users [页码]` | 管理员查看成员角色和封禁状态 |
| `/user_add <账号 ID>` | 添加普通账号 |
| `/user_remove <账号 ID>` | 删除账号并取消它提交的活动任务 |
| `/user_ban <账号 ID> <1d\|1w\|1m>` | 封禁一天、一周或 30 天并取消活动任务 |
| `/user_unban <账号 ID>` | 提前解除封禁 |
| `/user_role <账号 ID> <user\|admin>` | 设置普通账号／管理员角色 |
| `/user_history <账号 ID>` | 查看最近十次账号管理操作 |

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
重新写入。共享记录可协同管理；个人记录只允许本人和管理员管理。

普通账号最多保存 **自己提交的三条**有效待重试记录，入群／入频道等待审批
也沿用原记录，不另占名额。达到上限后不能再提交新链接，但可以重试和清理。
并发提交的更多任务若随后无权限，超过容量的失败不会加入列表，原进度消息会
明确提示清理后重新发送链接。管理员不限额。列表也可能包含其他账号提交的
共享记录，它们不占自己的名额；`/status` 显示自己的名额占用。
升级前的多余记录，以及管理员降为普通账号时超过三条的记录，保留为仅管理员
可见的归档；管理员可在 `/pending` 中重试或移除，不会静默丢失原链接。

命令路由和 Telegram 输入提示来自同一份注册表，启动时自动安装菜单。
普通账号显示常用命令，管理员额外显示成员管理和更新命令；角色改变后同步
菜单，封禁或删除账号仅保留 `/whoami` 提示。新成员尚未开始与 Bot 私聊时，
权限仍会生效，菜单在其首次操作时再次同步。菜单展示不能替代服务端权限检查。

## 成员管理

管理员可以直接添加、删除、封禁、解封和调整角色，无需修改 `.env` 或重启：

```text
/user_add <账号 ID>
/user_ban <账号 ID> 1d
/user_ban <账号 ID> 1w
/user_ban <账号 ID> 1m
/user_unban <账号 ID>
/user_role <账号 ID> admin
/user_remove <账号 ID>
```

`1m` 固定为 30 天。封禁期限以 UTC 时间戳持久化，到期无需定时任务或重启
即可恢复权限；成员列表按 Asia/Tokyo 显示期限。封禁／删除会撤销消息命令、
按钮和新任务提交权限，并取消该账号已经提交的活动任务，等待临时文件清理。
已经完成的上传不会撤回。删除成员不删除服务器上的 tdl 登录会话、历史个人
目标或待重试记录，管理员仍可查看／处理这些记录。

添加、删除、封禁、解封和角色改变记录到数据库，可用 `/user_history` 查看。
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
- 正常停止 Bot 会等待已接收任务和更新结束。强制终止／断电可能遗留临时
  文件，下次启动会清理本 Bot 创建的 `job-*` 目录。既有 `downloads/` 文件
  不属于这些任务，不会被启动清理处理。
- 更新先比较官方 release 和本地版本，已满足要求时不占更新锁、不下载或替换。
  需要更新才阻止新任务开始，等待活动任务完成下载、上传和清理，然后下载
  官方 GitHub release，校验 SHA-256 和可执行程序版本，最后原子替换
  `TDL_BINARY`。更新失败保留旧程序；更新结束后排队任务继续执行。
- 进程锁禁止多个 Bot 实例共享 data 目录、tdl 程序或同一 tdl namespace。
  运行期间应通过
  `/update_tdl` 更新程序；外部进程直接替换或运行同一 tdl 会话不受此锁管理。

队列在内存中，重启后不会自动恢复已接收任务；上传目标和待重试列表中的
url 则持久化到 SQLite。Telegram 上传并非事务，若中途失败，目标里可能已有
部分媒体，重试前先检查目标以避免重复。

## 配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `BOT_TOKEN` | 必填 | aiogram Bot token |
| `SESSION_SOURCE` | `tdl` | 复用已有 tdl 授权；`telethon` 为独立登录模式 |
| `TDL_DIR` | `.tdl` | 已登录的 tdl 根目录（其下包含 `data/` 或 `data.kv`） |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | 默认不需要 | 仅独立登录模式需要 |
| `ALLOWED_USER_IDS` | 首次初始化必填 | 初始成员 ID，逗号分隔，保留配置顺序 |
| `ADMIN_USER_IDS` | 第一个操作账号 | 初始管理员；初始化后用 Bot 管理角色 |
| `ACCOUNTS_FILE` | `<DATA_DIR>/accounts.toml` | 可选的专属 tdl 会话绑定配置 |
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
