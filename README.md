# Rain Bridge · 雨幕桥接 — 自托管群组消息桥接

一个自托管的群组消息桥接系统：**一条命令安装**，通过 **Web 面板** 完成全部配置，无需编辑配置文件。

> 本仓库已移除全部 QQ 相关代码。桥接的底层数据模型是平台无关的「**A 端 ↔ B 端**」：
> 每一条桥接连接 `chats` 表里的任意两个群组，不再与具体平台绑定。
> 今天内置 Telegram 适配器（Telegram 群 ↔ Telegram 群即可工作）；
> 未来决定接入的新平台，只需要加一个适配器，路由 / 权限 / 媒体 / 面板全部复用。

```
群 A ──► Adapter ──► Bridge Core（路由/去重/防环/队列/媒体） ◄──── Web 管理面板
群 B ◄── Adapter ◄── Bridge Core
```

内置适配器：**Telegram Bot**（BotFather 创建，支持自建 Bot API Server）。

## 特性

- **「高透雨天」面板 UI**：雨滴落地窗全屏背景 + 晶透毛玻璃卡片 + 深墨文字，管理后台也有审美
- **一键安装**：自动检测系统 / 安装依赖 / 创建服务 / 可选 Caddy + HTTPS，升级自动备份、失败自动回滚
- **A ↔ B 通用桥接**：一个机器人服务任意数量的群组；每条桥接 = 任意两个群组，可设置方向（双向 / 仅 A→B / 仅 B→A）
- **完整媒体支持**：文字、图片、GIF/动画、视频、音频、语音、文件、贴纸、合并转发；按目标平台原生类型发送，转换失败时保留文字并附上说明，**绝不丢消息**
- **零残留存储**：媒体只经过临时目录（TTL + 配额 + 启动/定时清理），内容哈希 → Telegram `file_id` 缓存，重复图片不重复上传
- **三层防循环 + 去重**：自身消息过滤、消息映射表反查、`(平台, 群, 消息 ID)` 唯一约束
- **回复 / 撤回 / 编辑同步**（可按桥接开关），发送者显示模式：简洁 / 标准 / 完整
- **稳定性**：优先级队列、FFmpeg 并发限制、Telegram 限流与 429 退避、1s/5s/30s 重试、断线自动指数退避重连、`systemd Restart=always`
- **安全**：Bot Token 加密存储（面板仅显示掩码）、日志自动脱敏、面板默认仅监听 `127.0.0.1` 并由 Caddy 提供 HTTPS、登录 5 次失败锁定 15 分钟、CSRF、Owner / Admin / Viewer 三级权限、首次访问需初始化令牌
- **可观测**：System Health、消息处理时间线、分类日志、一键诊断、失败消息重试

## 安装

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/YiranrumengQAQ/QQTG/main/install.sh)
```

支持 Debian / Ubuntu / CentOS / Alpine，自动完成：检测系统 → 安装 Python/FFmpeg → 创建服务用户与 systemd 服务 → 可选配置 Caddy HTTPS。

升级 / 卸载：

```bash
qqtg-install update
qqtg-install uninstall
```

面板默认监听 `127.0.0.1:8321`，建议通过 Caddy 反代到 HTTPS 域名。

## 快速开始

1. 打开面板 → 用安装脚本输出的 **初始化令牌** 创建管理员（Owner）账号（令牌丢失可在服务器执行 `qqtg setup-token` 重新生成）；
2. 「连接」页 → 填入 **Telegram Bot Token**（找 [@BotFather](https://t.me/BotFather) 创建；建议在 BotFather 中关闭 `/setprivacy`，否则机器人收不到普通群消息）→ 自动验证并连接；
3. 把机器人拉进要桥接的两个群 → 在每个群里发送 `/bridge`（或面板「群组」页手动添加 chat id）→ 新群默认**不转发**；
4. 「桥接」页 → 创建桥接：选择 A 端、B 端 → 自动检查双方权限 → 可发送测试消息 → 启用。

## 面板一览

- **总览**：今日消息 / 媒体 / 失败统计、System Health、近 7 天消息量、Issues（24h 失败聚合）、桥接卡片与快速开关
- **连接**：Telegram Bot 的配置、状态、重连与移除；以及「更多平台 · 规划中」的预留位
- **群组**：机器人所在群组的发现 / 授权 / 权限检测（发送消息、图片、语音、删除等细分权限）
- **桥接**：向导式创建（选 A 端 → 选 B 端 → 选项 → 权限检查与测试 → 启用），详情页可调整方向、消息类型、显示模式、回复/撤回/编辑/事件同步
- **消息**：每条消息的处理时间线（received → route → media → send）、状态、耗时、失败原因与手动重试
- **日志**：分类日志（系统 / 连接 / 消息 / 媒体 / 错误 / 安全），凭据自动脱敏
- **系统**：全局设置、用户与权限、备份与恢复、一键诊断

## 命令行

```
qqtg init            # 初始化目录/配置/数据库，打印初始化令牌
qqtg run             # 启动服务（systemd 使用）
qqtg status          # 连接 / 桥接 / 今日消息概览
qqtg diagnose        # 本地诊断（面板里有更全面的一键诊断）
qqtg backup|restore  # 导出 / 恢复配置备份（JSON）
qqtg db-backup       # SQLite 一致性备份
qqtg setup-token     # 重新生成初始化令牌
qqtg reset-password  # 重置面板密码
```

## 架构

```
qqtg/
├── adapters/
│   ├── base.py        # 适配器接口：BaseAdapter / OutgoingMessage / PermissionReport
│   └── telegram.py    # Telegram Bot API（长轮询、限流、全媒体收发）
├── core/
│   ├── app.py         # 应用容器：连接 / 群组 / 桥接 / 健康 / 诊断 / 备份
│   ├── engine.py      # 桥接核心：路由(A↔B) → 去重 → 防环 → 队列 → 媒体 → 发送 → 映射
│   └── formatter.py   # 发送者显示格式（简洁 / 标准 / 完整）
├── media/             # 媒体检测 / FFmpeg 转换 / 哈希缓存 / 临时存储
├── web/               # FastAPI：面板 + JSON API；static/ 为无依赖单页应用
├── db.py              # SQLite 与向前迁移（v2: QQ 移除，桥接泛化为 A↔B）
└── settings.py        # 运行期设置（面板可编辑）
```

**接入新平台**（等你决定后）：实现 `BaseAdapter`（start/stop/list_chats/check_permissions/send/download），
在 `core/app.py` 的 `_build_adapter` 注册一行工厂即可。`connections`、`chats`、`bridges` 表
对平台完全通用，面板里连接页、群组页、桥接向导都会自动工作。

## 测试

```bash
python tests/e2e_mock.py         # 端到端：mock Telegram × 2 群 A↔B 全流程断言
node  tests/ui_smoke.js          # 面板 SPA 无头冒烟（需 jsdom，配合 E2E_KEEP=1 的实例）
```

## 界面风格

雨滴落地窗全屏背景 + 极薄晶透玻璃晶片（`backdrop-filter: blur(22px) saturate(190%) brightness(1.05)` 与极细水晶倒角反光线），
深墨文字 `#111114`，点缀 `#007aff`。登录页写着一句话：

> 雨夜隔着车窗，看整座喧嚣的城市。晶片之内，是两个世界安静的呼吸。
