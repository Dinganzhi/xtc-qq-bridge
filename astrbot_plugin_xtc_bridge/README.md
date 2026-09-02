# 小天才 ↔ QQ 桥接插件（AstrBot v4.x）

配合仓库根目录的 Python 桥脚本使用（`main.py` / `bridge.py` / `xiaotiancai.py`）。
已在 AstrBot v4.27.4（桌面版）实测通过。

## 安装

1. 把整个 `xtc_qq_bridge` 目录放到 AstrBot 插件目录：
   - 桌面版（本机）：`C:\Users\<你的用户名>\.astrbot\data\plugins\xtc_qq_bridge\`
   - 或 pip 版：`<AstrBot 根目录>/data/plugins/xtc_qq_bridge/`
   - 仓库根目录的 `install.bat` 会自动完成复制。
2. 启动 AstrBot，在 WebUI「插件管理」中启用 `xtc_qq_bridge`。
3. 打开插件配置，与 Python 侧 `config.yaml` 对齐：
   - `http_port`（默认 11452）↔ `forward.plugin.base_url`
   - `token` ↔ `forward.plugin.token`
   - `python_callback_url` ↔ `webhook` 服务地址（默认 http://127.0.0.1:5000/qq_callback）
   - `python_callback_token` ↔ `webhook.token`
   - `platform_id`：留空时插件自动取最近收到消息的平台（建议先让 QQ 给机器人发条消息）；
     也可让机器人执行 `/sid` 查看后手动填写。

## 支持的命令

| 命令 | 作用 |
|---|---|
| `/小天才 <文本>` | 把文本发到小天才手表（插件格式化为 `[时间] [QQ昵称] 文本` 后转发给 Python 桥） |
| `/小天才登录` | 用 Python 侧 `config.yaml → xiaotiancai.login` 的手机号+密码登录小天才（回复"正在登录..."） |
| `/小天才 历史消息 <条数>` | 让 Python 桥读取小天才最近对话（1-100，默认 20），结果引用+@ 回复 |

## QQ 活动记录与查询端点（xtc 侧「搜索 / 在线人数 / 提醒」数据源）

- 插件**无论命令模式开/关**，都会记录收到的 QQ 消息活动（时间/私聊群聊/会话/QQ号/昵称，
  内存环形缓冲，最多 2000 条）；
- 另暴露三个本地端点给 Python 桥（xtc 侧命令在小天才聊天里触发后调用）：
  - `POST /api/qq_search`  `{keyword, allow_from[], allow_groups[], limit}` → 白名单内按昵称搜人
    （好友/群成员经 NapCat `get_friend_list` / `get_group_member_list` / `get_group_info` 实时拉取，
    附活动记录里的最后消息时间）；
  - `POST /api/qq_online`  `{minutes, allow_from[], allow_groups[]}` → 最近 N 分钟白名单会话发言去重人数；
  - `POST /api/qq_remind`  `{group_id, qq_id, text}` → 向群发送 `@QQID`（可带内容）。
- 这些查询通过 aiocqhttp 适配器的 `bot.call_action(...)` 调 OneBot v11 API；
  需要 NapCat 在线且机器人至少收到过一条消息（用于学习平台 ID）。

## 工作原理

```
Python 桥(小天才侧) ──POST /api/forward──▶ 插件(本目录) ──context.send_message()──▶ QQ
        ▲                                                                        │
        └──────────── 插件收到 /小天才 命令 ──POST python_callback_url ───────────┘
```

- **Python → QQ**：Python 轮询到手表消息 → POST 插件本地端点 `/api/forward` → 插件发 QQ。
- **QQ → Python**：QQ 消息 → 插件命令处理（`/小天才` `/小天才登录`）→ POST 到 Python 桥的
  webhook（`qq_webhook.py`，5000 端口）→ ADB 操作小天才（发送消息 / 账密登录）。
- 本地端点鉴权：请求头 `X-Bridge-Token`，与插件配置 `token` 一致。

## 注意

- 命令白名单：插件配置 `allow_senders` / `allow_groups` 为可选前置过滤；
  最终闸门在 Python 侧 `config.yaml → webhook.allow_from`（私聊）/ `allow_groups`（群聊）。
- 主动发送依赖平台 ID：若插件日志报「无法确定平台 ID」，按上面第 3 步处理。
- 插件更新：仓库根目录 `astrbot_plugin_xtc_bridge/` 是唯一源码，改完复制本目录 4 个文件
  （`main.py` / `metadata.yaml` / `_conf_schema.json` / `README.md`）到插件目录并重载插件。
