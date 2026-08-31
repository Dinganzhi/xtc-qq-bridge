# 小天才 ↔ QQ 桥接插件（AstrBot v4.x）

配合仓库根目录的 Python 桥脚本使用（`main.py` / `bridge.py` / `xiaotiancai.py`）。

## 安装

1. 把整个 `xtc_qq_bridge` 目录放到 AstrBot 插件目录：
   - 桌面版（本机）：`C:\Users\<你的用户名>\.astrbot\data\plugins\xtc_qq_bridge\`
   - 或 pip 版：`<AstrBot 根目录>/data/plugins/xtc_qq_bridge/`
2. 启动 AstrBot，在 WebUI「插件管理」中启用 `xtc_qq_bridge`。
3. 打开插件配置，与 Python 侧 `config.yaml` 对齐：
   - `http_port`（默认 11452）↔ `forward.plugin.base_url`
   - `token` ↔ `forward.plugin.token`
   - `python_callback_url` ↔ `webhook` 服务地址（默认 http://127.0.0.1:5000/qq_callback）
   - `python_callback_token` ↔ `webhook.token`
   - `platform_id`：留空时插件自动取最近收到消息的平台（建议先让 QQ 给机器人发条消息）；
     也可让机器人执行 `/sid` 查看后手动填写。

## 工作原理

```
Python 桥(小天才侧) ──POST /api/forward──▶ 插件(本目录) ──context.send_message()──▶ QQ
        ▲                                                                        │
        └────────────────── 插件监听所有消息事件 ──POST python_callback_url──┘
```

- Python → QQ：小天才 App 收到消息 → Python 轮询 → POST 到插件本地端点 → 插件发 QQ。
- QQ → Python：QQ 消息 → 插件事件 → POST 到 Python 桥的 webhook → ADB 操作小天才发送。

## 注意

- 事件过滤默认转发**所有**消息，可在插件配置 `allow_senders` / `allow_groups` 收窄。
- 主动发送依赖平台 ID：若插件日志报「无法确定平台 ID」，按上面第 3 步处理。
