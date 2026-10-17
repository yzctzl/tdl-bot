# tdl-bot

**发一个链接，留住整个媒体组**

[English](README.md) · [用户手册](docs/manual_zh.md) · [安装指南](docs/deployment_zh.md)

收藏 Telegram 媒体组、视频和文字，不必逐个保存文件
选择接收位置，定时追踪喜欢的频道，在自己的 Telegram Bot 中用按钮完成管理

适合个人收藏、多个账号，以及少量可信协作者使用
共用一个工作空间，每位成员都能设置自己的接收位置

## 从发现到收藏，少一点操作

- **一个链接，完整媒体组** — 发送媒体组中任意一条消息的链接，一起保存图片、视频和文字，保留顺序及文字格式
- **收藏到你想要的位置** — 接收到 Bot 对话、群组、频道或论坛话题
- **不用反复查看更新** — 定时追踪新消息，管理员也能搬运之前的历史内容
- **日常操作用按钮完成** — 查看进度、取消任务、加群后重试或调整设置，不必记住命令
- **保存过的内容，更快送达** — 已保存的副本仍可用时，避免再次下载相同内容
- **和信任的人一起使用** — 邀请协作者、管理权限与追踪配额，每位成员独立选择英文或简体中文

媒体预览不再被隐藏，复制的内容不附带原发送者的转发署名
处理仍需具备来源访问权限，并遵守 Telegram 的权限限制

## 在 Linux 上使用 uv 开始

安装好 [uv](https://docs.astral.sh/uv/) 后执行：

```sh
uv tool install --python 3.12 git+https://github.com/yzctzl/tdl-bot.git
mkdir tdl-bot-workspace && cd tdl-bot-workspace
tdl-bot setup -i
tdl-bot run
```

进入向导前，先通过 **@BotFather** 创建 Bot，准备一个私有存储频道，并将 Bot
添加为有发帖权限的管理员
准备好 Bot token 和 Telegram 账号，向导会引导你登录、确认管理员并选择存储位置，缺少 tdl 时自动下载
打开自己的 Bot，发送 `/start`，选择语言，再发送消息链接即可

请保持终端运行
Docker、Windows、macOS、后台运行和更新方式见[安装指南](docs/deployment_zh.md)

## 接下来做什么

| 我想要… | 查看 |
| --- | --- |
| 保存媒体组、选择接收位置或管理重试 | [用户手册](docs/manual_zh.md#保存消息与媒体组) |
| 追踪新消息或搬运频道历史 | [定时追踪与历史搬运](docs/manual_zh.md#定时追踪与历史搬运) |
| 使用 Docker、uv tool 或 Git 源码安装 | [安装方式与平台支持](docs/deployment_zh.md) |
| 处理权限、发送失败或任务中断问题 | [常见问题](docs/manual_zh.md#常见问题) |
| 理解实现或参与贡献 | [技术架构](docs/architecture_zh.md) |

仅保存你有权访问和留存的内容
请妥善保护 Bot token 与 Telegram 登录会话
