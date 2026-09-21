# QQTG Bridge — QQ ↔ Telegram 群组双向桥接

一个自托管的 QQ 群 ↔ Telegram 群双向消息桥接系统：**一条命令安装**，通过 **Web 面板** 完成全部配置，无需编辑配置文件。

```
QQ 群 ──► QQ 接入（二选一）                Bridge Core ──► Telegram Bot API ──► Telegram 群
          ├─ 个人账号 · OneBot v11 (NapCat)        │
          └─ QQ 官方机器人 (q.qq.com)         Web 管理面板
QQ 群 ◄──────────────────────────────────── Bridge Core ◄── Telegram Bot API ◄── Telegram 群
```

## QQ 接入方式（二选一）

在面板「连接」页选择 QQ 接入类型：

### 1. 个人账号（内置 OneBot v11）— 推荐，功能最全

通过 [NapCat](https://napneko.github.io/) / LLOneBot / Lagrange 等 OneBot v11 实现登录个人 QQ 号当作机器人，支持全部消息类型（图片 / 语音 / 视频 / 文件 / 贴纸 / 合并转发等）：

- **正向模式（推荐，默认）**：在 NapCat 网络配置中新建 **WebSocket 服务器**（端口如 3001，可设 token），把 `ws://127.0.0.1:3001` 与 token 填入面板；
- **反向模式**：在 NapCat 新建 **WebSocket 客户端**，地址填 `ws://<面板地址>/onebot/v11/ws`，并在面板选择"反向 WebSocket"。

### 2. QQ 官方机器人（q.qq.com）

接入在 [QQ 开放平台](https://q.qq.com) 创建的**正式机器人**（Bot API v2，WebSocket 网关），支持两种授权方式：

- **扫码授权**：面板显示二维码 → 手机 QQ「扫一扫」→ 选择要绑定的机器人并确认 → 自动获取 AppID / AppSecret 并连接（与官方 Agent 接入的扫码绑定协议一致，二维码过期自动刷新）；
- **输入授权**：直接粘贴开放平台「开发设置」里的 AppID / AppSecret，保存时自动验证（`GET /users/@me`）。

平台限制（由 QQ 官方接口决定）：

- 群内只能收到 **@机器人** 的消息（除非群主在群设置中开启"获取全部消息"）；
- 回复需在收到消息后 **5 分钟**内（被动回复，每条消息最多回复 5 次，桥接会自动携带 `msg_id`/`msg_seq`）；窗口外的消息走主动消息，受平台配额限制；
- 富媒体（图片 / 视频 / 文件）通过官方 `/v2/groups/{openid}/files` 接口以 URL 方式上传，**需要在「系统 → 设置」配置公网媒体地址**（面板必须能被 QQ 服务器访问）；桥接会生成带签名、10 分钟有效的临时链接；
- 官方语音仅接受 silk 编码，TG 语音会以**文件**形式发送；官方接口不提供群列表，群与机器人互动后自动出现在「群组」页，也支持手动添加 `group_openid`。

两种方式的切换随时可做（保存即生效），桥接配置保留；官方机器人的群以 `group_openid` 标识，与个人账号的群号互不相通。

## 特性

- **一键安装**：自动检测系统 / 安装依赖 / 创建服务 / 可选 Caddy + HTTPS，升级自动备份、失败自动回滚
- **多群桥接**：一个 QQ 机器人 + 一个 Telegram 机器人服务任意数量的群；每条桥接 = 一个 QQ 群 ↔ 一个 TG 群，可设置方向（双向 / QQ→TG / TG→QQ）
- **完整媒体支持**：文字、图片、GIF/动画、视频、音频、语音、文件、贴纸、合并转发。媒体按 Telegram 原生类型发送（`sendPhoto / sendAnimation / sendVideo / sendAudio / sendVoice / sendDocument`），不会全部退化成"文件"
- **自动转换**：QQ 语音 → OGG/Opus（Telegram 语音气泡）；TG 语音 → WAV/MP3（QQ 语音）；TG 视频贴纸/动画 → GIF；静态贴纸 WebP → PNG；转换失败时保留文字并附上说明，**绝不丢消息**
- **零残留存储**：媒体只经过临时目录（TTL + 配额 + 启动/定时清理），内容哈希 → Telegram `file_id` 缓存，重复图片不重复上传
- **三层防循环 + 去重**：自身消息过滤、消息映射表反查、`(平台, 群, 消息 ID)` 唯一约束
- **回复 / 撤回 / 编辑同步**（可按桥接开关），发送者显示模式：简洁 / 标准 / 完整
- **稳定性**：优先级队列、FFmpeg 并发限制（默认 1，按 CPU/内存推荐）、Telegram 限流与 429 退避、1s/5s/30s 重试、断线自动指数退避重连（官方机器人支持 access_token 自动刷新与网关重连）、`systemd Restart=always`
- **安全**：Bot Token / AppSecret 加密存储（面板仅显示掩码）、日志自动脱敏、媒体临时链接 HMAC 签名 + 有效期、面板默认仅监听 `127.0.0.1` 并由 Caddy 提供 HTTPS、登录 5 次失败锁定 15 分钟、CSRF、Owner / Admin / Viewer 三级权限、首次访问需初始化令牌
- **可观测**：System Health、消息处理时间线、分类日志、一键诊断、失败消息重试

## 安装

在一台 Linux 服务器上（Debian 11+ / Ubuntu 20.04+ / RHEL 8+ / Rocky / Alma / Fedora / Arch / Alpine，x86_64 或 aarch64，root）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/YiranrumengQAQ/QQTG/main/install.sh)
```

有域名（解析到本机）并希望自动配置 HTTPS：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/YiranrumengQAQ/QQTG/main/install.sh) --domain bridge.example.com
```

非交互安装（无域名，直接 `http://IP:8321`）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/YiranrumengQAQ/QQTG/main/install.sh) --yes --port 8321
```

安装脚本会：

1. 检测系统、CPU / 内存 / 磁盘、systemd、网络（IPv4 是否可达 `api.telegram.org`）
2. 安装 `python3 (≥3.10)`、`ffmpeg`、`git` 等依赖（软件源无合适 Python 时自动通过 uv 安装独立 Python；无 FFmpeg 时尝试静态构建）
3. 创建系统用户 `qqtg`、目录 `/opt/qqtg-bridge/{app,venv,config,data,logs,tmp}`、Python 虚拟环境
4. 初始化数据库与 `config/config.env`（随机密钥，权限 600），生成一次性 **初始化令牌**
5. 创建并启动 `qqtg-bridge.service`（`Restart=always`，带 systemd 沙箱加固）
6. 可选安装 Caddy 并写入反向代理站点（自动申请 / 续期证书），放行防火墙端口

安装完成后按输出提示打开面板，用初始化令牌创建 Owner 账号。

### 安装脚本参数

| 参数 | 说明 |
| --- | --- |
| `--domain <域名>` | 配置 Caddy 反向代理 + 自动 HTTPS，面板绑定 127.0.0.1 |
| `--port <端口>` | 面板端口（默认 8321） |
| `--bind <地址>` | 监听地址（有域名默认 127.0.0.1，否则 0.0.0.0） |
| `--home <目录>` | 安装目录（默认 `/opt/qqtg-bridge`） |
| `--ref <分支/标签>` | 安装指定版本（默认 `main`） |
| `--local` | 从当前目录安装（开发 / 离线） |
| `--no-caddy` | 指定了域名但自行配置反代（模板：`deploy/nginx.conf.template`） |
| `--yes` | 全部默认值，不提问 |

### 日常运维

```bash
qqtg status              # 服务 / 连接 / 桥接概览
qqtg diagnose            # 本地环境诊断（完整诊断在面板「系统 → 一键诊断」）
qqtg backup [文件]       # 导出配置备份（默认不含 Token；--with-secrets 导出加密凭据）
qqtg restore 文件.json   # 恢复
qqtg setup-token         # 重新生成初始化令牌（仅在尚未创建管理员时）
qqtg reset-password 用户 # 重置面板密码
qqtg-install update      # 升级（自动备份数据库，失败自动回滚）
qqtg-install uninstall   # 卸载（--purge 同时删除数据）
systemctl status qqtg-bridge ; journalctl -u qqtg-bridge -f
```

## 配置向导（面板）

1. **连接 Telegram**：向 [@BotFather](https://t.me/BotFather) 创建机器人，复制 Token 到「连接」页 → 验证并保存。
   建议在 BotFather 里执行 `/setprivacy` → **Disable**，否则机器人收不到普通群消息（或把机器人设为群管理员）。
2. **连接 QQ**（「连接」页选择类型）：
   - **个人账号（内置 OneBot）**：安装任意 OneBot v11 实现（推荐 [NapCat](https://napneko.github.io/)，也支持 LLOneBot / Lagrange），登录 QQ 后：
     - 正向模式（推荐，默认）：在 NapCat 网络配置中新建 **WebSocket 服务器**（端口如 3001，可设 token），把 `ws://127.0.0.1:3001` 与 token 填入面板；
     - 反向模式：在 NapCat 新建 **WebSocket 客户端**，地址填 `ws://<面板地址>/onebot/v11/ws`，并在面板选择"反向 WebSocket"。
   - **QQ 官方机器人**：在 [q.qq.com](https://q.qq.com) 创建机器人后，选择「扫码授权」（手机 QQ 扫码即连）或「输入授权」（粘贴 AppID / AppSecret）。发媒体需在「系统」页配置公网媒体地址。
3. **发现群组**：个人账号的 QQ 群会自动从机器人的群列表读取；官方机器人的群在群内 **@机器人** 后自动出现（或手动添加 `group_openid`）；Telegram 群把机器人拉进群后在群里发送 `/bridge`（QQ 群也可以）。申请会出现在「群组」页的 **待授权** 列表。
4. **创建桥接**：「桥接 → 创建桥接」选择 QQ 群 → 选择 TG 群 → 设置方向 / 媒体类型 / 显示模式 → 自动检查双方权限 → 发送测试消息 → **启用**。
   新群、新桥接默认**不转发**，必须由管理员确认启用。

## 媒体转换规则

| 方向 | 源 | 目标 |
| --- | --- | --- |
| QQ → TG | 图片 (jpg/png/webp) | `sendPhoto`（超出 10MB / 尺寸限制自动改为文件） |
| QQ → TG | GIF / 动画表情 / mface | `sendAnimation` |
| QQ → TG | 视频 | `sendVideo`（非 mp4 自动 remux / 转码，附缩略图） |
| QQ → TG | 语音 (silk/amr → 由 NapCat 输出 mp3) | FFmpeg → OGG/Opus → `sendVoice` |
| QQ → TG | 音频文件 | `sendAudio` |
| QQ → TG | 文件 | `sendDocument`（≤50MB；自建 Bot API Server 可提高） |
| TG → QQ | 照片 | QQ 图片 |
| TG → QQ | GIF / 动画 (mp4) | FFmpeg → GIF → QQ 图片（过长 / 过大则按视频发送） |
| TG → QQ | 视频 / 圆形视频 | QQ 视频 |
| TG → QQ | 语音 (ogg/opus) | FFmpeg → WAV（可选 MP3）→ QQ 语音 |
| TG → QQ | 静态贴纸 (webp) | PNG → QQ 图片 |
| TG → QQ | 视频贴纸 (webm) | GIF → QQ 图片 |
| TG → QQ | 动画贴纸 (tgs) | 使用 Telegram 提供的预览图；无预览时降级为 `[贴纸 😀]` |
| TG → QQ | 音频 / 文件 | QQ 群文件（> 20MB 的 Telegram 文件 Bot 无法下载，发送说明文字） |

任何一步失败都会把文字部分照常发出，并附上 `[图片] 文件名 / 大小 / 原因` 的说明。

## 目录结构

```
/opt/qqtg-bridge/
├── app/        代码（git 检出）
├── venv/       Python 虚拟环境
├── config/     config.env（监听地址、端口、密钥；权限 600）
├── data/       bridge.db（SQLite，WAL）、backups/
├── logs/       bridge.log（滚动，已脱敏）
└── tmp/        媒体临时目录（自动清理）
```

运行时设置（显示模式、并发、限流、大小策略、保留策略等）全部在面板「系统」页管理并存于数据库。

## 开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
QQTG_HOME=$PWD/.dev python -m qqtg init --bind 127.0.0.1 --port 8321   # 打印初始化令牌
QQTG_HOME=$PWD/.dev python -m qqtg run

# 端到端测试（内置 Mock OneBot + Mock Telegram API，需要 ffmpeg 才会测语音转换）
pip install python-multipart
python tests/e2e_mock.py
```

代码结构：

```
qqtg/
├── config.py / settings.py     启动配置 & 运行时设置
├── db.py                       SQLite + 迁移（chats / bridges / messages / message_mapping / media_cache …）
├── models.py                   UnifiedMessage / Media / SendResult
├── security.py / logsys.py     Token 加密、密码、脱敏日志、媒体链接签名
├── qqbotqr.py                  QQ 官方机器人扫码授权协议（create_bind_task / poll / AES-GCM）+ 二维码
├── adapters/onebot.py          QQ 个人账号：OneBot v11（正向 / 反向 WS）
├── adapters/qqbot.py           QQ 官方机器人：Bot API v2（网关 WS、access_token 刷新、被动/主动回复、富媒体）
├── adapters/telegram.py        Telegram Bot API（长轮询、限流、file_id）
├── media/                      类型嗅探、FFmpeg、临时存储、转换策略
├── core/engine.py              路由、去重、防循环、队列、重试、映射
├── core/app.py                 应用容器：连接 / 群组 / 桥接管理、扫码会话、健康、诊断、备份
└── web/                        FastAPI API + 单页管理面板（无构建步骤）+ /qqbot/media 签名媒体端点
```

## 常见问题

- **Telegram 收不到普通群消息** → BotFather `/setprivacy` 设为 Disable，或把机器人设为管理员；改动后需把机器人移出再拉回群。
- **官方机器人收不到群消息 / 不回复** → 群里必须 **@机器人**；回复窗口为收到消息后 5 分钟内、每条消息最多 5 次；窗口外的主动消息受平台配额限制（一般每群每月 4 条，需在开放平台申请）。
- **官方机器人发不出图片 / 视频 / 文件** → 在「系统 → 设置」填写「公网媒体地址」（必须为 QQ 服务器可访问的 https 地址，例如 Caddy 域名）；图片 ≤10MB、视频/文件 ≤100MB；语音以文件形式发送（官方仅接受 silk 编码）。
- **扫码授权二维码刷不出来** → 检查服务器能否访问 `q.qq.com`；二维码过期会自动刷新，也可点击「取消扫码」重新生成。
- **服务器在中国大陆无法访问 api.telegram.org** → 需要自行处理网络，或在面板「连接」里填写自建 Bot API Server 地址。
- **大于 10MB 的媒体发到 QQ 失败** → 大文件通过 `file://` 路径交给 NapCat，需要 NapCat 与 Bridge 在同一台机器且能读取 `/opt/qqtg-bridge/tmp`（Docker 需挂载同路径）。
- **语音 / 贴纸 / GIF 不转换** → 面板「一键诊断」查看 FFmpeg 状态；`apt install ffmpeg` 后重启服务。
- **忘记密码** → `sudo qqtg reset-password <用户名>`；忘记所有账号 → `sudo qqtg setup-token --force`。

## License

MIT
