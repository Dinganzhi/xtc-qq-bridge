# 小天才 ↔ QQ 桥接（AstrBot 插件版）

> **温馨提示**  
> 因雷电模拟器限制，仅适用于Windows平台  
> 本项目仅供学习交流使用  
> 本项目有使用Vibe Coding  
> 分发，修改，再发布等操作，请遵守Apache-2.0许可证  
> 对于雷电模拟器9的可用性未知，建议安装雷电模拟器14  

通过 **模拟器（雷电推荐 14 / MuMu 等）+ Python ADB** 控制小天才 App，与 QQ 双向桥接消息。
QQ 侧通过 **AstrBot 插件**（常驻 AstrBot 进程）收发，已实现：
双向消息桥接、账密自动登录、登录态检测、安全验证提醒与自动恢复。

> 🚀 **安装：双击项目根目录的 `install.bat` 即可一键初始化**（详见下方「快速安装」）。

```
小天才 App(模拟器) ──ADB──▶ Python 桥脚本 ──HTTP──▶ AstrBot 插件 ──▶ QQ
       ▲                                                        │
       └──────────────── 插件转发 QQ 命令（/小天才 等）──────────┘
```

## ⚠️ 模拟器注意事项（重要，先读）

| 事项 | 说明 |
|---|---|
| **支持的模拟器** | 雷电（推荐 **14**；9 可用性未知）、**MuMu**（15/12/6，adb 自动识别）。**其他模拟器/真机也通用**——只需在 `config.yaml → adb.path` 指定 adb（或加入 PATH）、`adb.port` 填对，**无需改源码** |
| **必须开启「ADB 调试」** | 雷电：设置 → 其他设置；MuMu：设置 → 其他/高级 → ADB 调试。不开的话 `adb devices` 为空，桥接无法连接 |
| **adb 端口** | 默认 **5555**（雷电、MuMu 都是；MuMu 另有 16384 亦可用）。其他模拟器按实际情况改 `adb.port` |
| **同时装多个模拟器** | 自动探测会优先命中雷电。**请务必在 `adb.path` 显式指定**要用的那个（否则桥接操作的是被自动选中的模拟器） |
| **模拟器镜像差异** | 已做防御式处理：LDPlayer14(Android 14) 无 `cmd clipboard`（自动走 ADBKeyBoard）、`ime set` 不生效（自动双写 settings）等，一般无需干预 |
| **小天才 App 需自行安装** | 每个模拟器都要装小天才 App；界面控件 id 与模拟器无关（同一 APK），雷电上实测的适配在 MuMu 等上通用 |

## 功能总览

| 功能 | 说明 |
|---|---|
| 小天才 → QQ | 轮询读取手表发来的消息 → `[日期时间] [本地昵称] 内容` 转发（时间取 App 内该消息的时间标签） |
| QQ → 小天才 | 命令 `/小天才 <文本>` → `[日期时间] [QQ昵称] 内容` 发到手表聊天 |
| 账密登录 | 命令 `/小天才登录`：用手机号 + 密码登录（**非验证码**） |
| 自动登录 | 每 `login_check_interval`（默认 600s = 10 分钟）检测登录态，未登录自动账密登录 |
| 安全验证 | 触发"登录安全验证"时 QQ 提醒手动操作；**验证完成后自动恢复并通知，无需重启** |
| 接收白名单 | 私聊 / 群聊严格白名单，只有列表内的会话可触发命令 |
| 昵称映射 | App 联系人名 → 转发到 QQ 时显示的自定义昵称（不用 App 原始姓名） |
| 消息过滤 | 只转发手表侧消息（按 App 的发送方标注识别），自己发的/UI 文案/网络提示一律不转发 |
| 送达确认 | 转发成功自动回复「✅ 消息已转发成功」（QQ→小天才 回复发送者；小天才→QQ 在聊天内回复）；确认消息带 ✅/❌ 前缀，不会被再次转发 |

## 目录结构

```
project/
├── main.py                  # 入口（--check / --debug dump-ui / --once / 正常启动）
├── config.yaml              # 配置（含中文注释）；模板见 config.example.yaml
├── adb_controller.py        # ADB 封装：连接/点击/滑动/截图/UI 解析/中文输入策略链
├── xiaotiancai.py           # 小天才 App 操作：启动/登录检测/账密登录/打开聊天/发送/读取
├── bridge.py                # 轮询调度：去重 + 回声过滤 + 断线重连 + 转发 + 自动登录检测
├── plugin_client.py         # AstrBot 插件客户端（转发/通知）
├── qq_webhook.py            # 反向回调服务（纯 stdlib）：接插件命令/消息 → 桥
├── utils/
│   ├── logger.py            # 日志（控制台 + 滚动文件）
│   └── deduplicate.py       # LRU 去重 + 文件化回声过滤
├── tools/
│   ├── dump_ui.py           # 实机 UI 探测（填映射表用）
│   └── selftest.py          # 环境自检（不依赖配置）
├── astrbot_plugin_xtc_bridge/   # AstrBot 插件源码（已编写，安装见下文）
├── keyboardservice-debug.apk    # 捆绑的 ADBKeyBoard APK（新机器免下载）
├── install.bat             # 一键安装（新机器）
└── requirements.txt
```

## 快速安装（推荐）

> 💡 **最简单的方式：双击 `install.bat`** —— 一键完成：
> ① 安装 pyyaml ② 复制 AstrBot 插件到 `~\.astrbot\data\plugins\xtc_qq_bridge\`
> ③ 从 `config.example.yaml` 生成 `config.yaml` ④ 生成插件初始配置。
> 之后只需编辑 `config.yaml` 并按下方步骤启用插件即可。

## 快速开始

```bash
pip install -r requirements.txt        # 只需 pyyaml（无外网时可用 JSON 格式配置）

python tools\selftest.py               # ① 环境自检（adb 发现/连接/UI dump/启动/登录检测）
python main.py --check                 # ② 或使用 --check
```

然后在模拟器中登录小天才家长账号（也可用 `/小天才登录` 自动登录），再：

```bash
python tools\dump_ui.py --filter 消息   # ③ 查看界面控件，按需调整 config.yaml → xiaotiancai.ui
python main.py                          # ④ 启动桥接
```

## QQ 命令（给机器人发）

| 命令 | 作用 |
|---|---|
| `/小天才 <文本>` | 把文本发到小天才手表（如 `/小天才 晚上回家吃饭`） |
| `/小天才登录` | 用 `config.yaml → xiaotiancai.login` 的手机号+密码登录 |

发送者需命中 `webhook.allow_from`（私聊）/ `webhook.allow_groups`（群聊）白名单。

**送达确认**（`target.confirm_delivery`，默认开）：
- `/小天才` 转发成功 → 回复「✅ 消息已转发成功」；失败 → 「❌ 消息转发失败」。
- 小天才消息转发到 QQ 成功 → 在小天才聊天内回复「✅ 已转发到QQ」。
- 确认消息带 `✅/❌` 前缀（`xiaotiancai.ui.system_msg_prefixes`），**不会被当作接收消息再次转发**。

## 登录与安全验证闭环

```
掉登录 → 10 分钟检测到未登录 → 自动账密登录
  ├─ 成功 → 静默（不打扰）
  ├─ 密码错误等 → QQ 通知"❌ 登录失败"
  └─ 触发"登录安全验证" → QQ 通知"⚠️ 请手动打开模拟器完成验证"
       └─ 你手动验证完成后 → 程序 2 秒内自动感知 → 桥接恢复 + QQ 通知"✅ 已重新登录"
```

- 登录过程中风险标记（`安全验证/风险/滑块/图形验证/完成验证` 等）可在
  `config.yaml → xiaotiancai.ui.risk_markers` 调整。
- 待验证/失败期间**不会反复自动重试**，避免刷屏；恢复后自动解除。

## 消息读取策略（真机实测）

- 识别依据：消息气泡 `chat_msg_item_content` 的 content-desc 标注发送方——
  `'童武洋发的消息,内容'`（手表发，收）vs `'你发的消息,内容'`（自己发，跳过）；
  表情/语音消息 text 为空时从 desc 提取类型（如"表情"/"语音"）。
- 聊天列表模式：取最顶部（最新）聊天行的消息预览（`tv_chat_dialog_last_msg_content`）。
- 界面更新后优先调整 `config.yaml → xiaotiancai.ui`，不要改代码。

## 中文输入方案（已在真机实测）

| 方案 | 状态 | 说明 |
|---|---|---|
| **ADBKeyBoard IME**（推荐） | 自动安装 | 优先用捆绑的 `keyboardservice-debug.apk`，其次在线下载；设为默认输入法后通过广播 `ADB_INPUT_TEXT` 注入任意文本（含中文） |
| `cmd clipboard set-text` + 粘贴 | ❌ 本模拟器不可用 | LDPlayer14(Android 14) 镜像未实现该 shell 命令 |
| `input text` | ⚠️ 仅数字可用 | 字母被拼音输入法吞进组词状态 |

- 恢复原输入法：`adb shell ime set com.android.inputmethod.pinyin/.InputService`
- 手动安装：`adb install -r keyboardservice-debug.apk && adb shell ime enable com.android.adbkeyboard/.AdbIME && adb shell ime set com.android.adbkeyboard/.AdbIME`

## AstrBot 插件（v4.x，已在 4.27.4 实测）

插件位于 `astrbot_plugin_xtc_bridge/`，安装到 `C:\Users\<用户名>\.astrbot\data\plugins\xtc_qq_bridge\`（install.bat 自动完成）。

**启用步骤**：
1. 启动 AstrBot 桌面版 → WebUI「插件管理」→ 启用 `xtc_qq_bridge`。
2. 在 NapCat（AstrBot 平台适配器）里登录 QQ 机器人。
3. 插件配置与 `config.yaml` 对齐（默认值已一致，改 token 需两端同步）：
   - `http_port` 11452 ↔ `forward.plugin.base_url`
   - `token` ↔ `forward.plugin.token`
   - `python_callback_url` ↔ `webhook` 地址（http://127.0.0.1:5000/qq_callback）
   - `python_callback_token` ↔ `webhook.token`
4. 先给机器人发一条消息（让插件学到平台 ID）；若报「无法确定平台 ID」，可让机器人执行 `/sid` 查看后填 `platform_id`。
5. `python main.py` 启动。

**插件原理**：
- 小天才→QQ：Python 轮询 → 格式化 → POST `http://127.0.0.1:11452/api/forward` → 插件发 QQ。
- QQ→小天才：插件收到 `/小天才` / `/小天才登录` → POST 到 Python 侧 `qq_webhook`（5000 端口）→ ADB 操作。

**接收白名单（严格模式）**：`webhook.allow_from`（私聊 QQ 号）/ `webhook.allow_groups`（群号）；
对应列表为空 = 该类消息全部拒绝。插件侧 `allow_senders`/`allow_groups` 为可选前置过滤。

## 部署到新机器（打包分发）

整个项目目录即可打包。打包前建议删除：`config.yaml`（含本机敏感信息，install.bat 会从模板重新生成）、`data/`、`logs/`、`__pycache__/`（删掉无影响；不删也能跑）。用 git 管理时使用仓库内 `.gitignore`。

**新机器初始化清单**（按顺序）：

| # | 步骤 | 说明 |
|---|---|---|
| 1 | 安装 **Python 3.10+** | 勾选 Add to PATH |
| 2 | 安装**雷电模拟器（推荐 14）或 MuMu（或其他模拟器/真机）** + 小天才 App | 均需开启 ADB 调试；adb 端口默认 **5555**（MuMu 另有 16384 亦可用）；其他模拟器在 `config.yaml → adb.path/port` 指定 |
| 3 | 安装 **AstrBot** 并启动一次 | 桌面版或 pip 版均可（v4.26+ 已兼容），启动生成 `~\.astrbot` |
| 4 | 双击 **`install.bat`** | 装 pyyaml、复制插件、从模板生成 config.yaml 和插件配置 |
| 5 | 编辑 **`config.yaml`** | QQ 号、联系人、昵称、账密、token（与插件配置一致） |
| 6 | AstrBot WebUI | 启用插件；配置 NapCat 适配器、登录 QQ |
| 7 | 给机器人发一条消息 | 让插件学到平台 ID |
| 8 | `python main.py` | ADBKeyBoard 用捆绑 APK 自动安装（免下载） |

注：
- 请将 `config.example.yaml` 复制为 `config.yaml` 再进行编辑
- 使用 yaml 格式，请安装 pyyaml，例如 `pip install pyyaml`

**打包/不打包清单**：
- ✅ 随包：全部 `.py`、`astrbot_plugin_xtc_bridge/`（插件源码）、`keyboardservice-debug.apk`、`install.bat`、`config.example.yaml`、`requirements.txt`
- ❌ 不随包（每台机器独立）：模拟器本身、小天才账号登录态、AstrBot 里的 NapCat/QQ 登录态、`config.yaml` 的值、平台 ID（运行时自动学习）

**插件同步更新**：`astrbot_plugin_xtc_bridge/` 是唯一源码，改完重跑 `install.bat`（或手动复制到 `~\.astrbot\data\plugins\xtc_qq_bridge\`）并重载插件。

## 关键配置速览

| 配置项 | 说明 |
|---|---|
| `adb.path` | adb.exe 路径；留空自动探测（雷电/MuMu 目录 → PATH）。多模拟器并存时必须显式指定 |
| `forward.mode` | `plugin`=走 AstrBot 插件；`log`=仅打印调试 |
| `target.xtc_contact` | 小天才联系人名（打开聊天用） |
| `target.nicknames` | App 名 → 显示昵称映射（行首 `#` 是注释） |
| `target.notify_qq` | 登录/异常通知目标（留空用 qq_private 第一个） |
| `target.confirm_delivery` | 送达确认开关（默认 true：转发成功回复 ✅，失败回复 ❌） |
| `target.qq_private` / `qq_group` | 转发目标，支持单个或列表，可并存 |
| `xiaotiancai.login.phone/password` | 账密登录凭据 |
| `xiaotiancai.login_check_interval` | 自动登录检测间隔（秒，默认 600） |
| `xiaotiancai.ui.risk_markers` | 安全验证检测标记 |
| `xiaotiancai.ui.system_msg_prefixes` | 桥接系统提示前缀（✅/❌），读取时跳过不转发 |
| `webhook.allow_from` / `allow_groups` | 接收白名单（私聊/群聊） |

## 已知限制

- 仅支持 **Windows**（本项目依赖的模拟器均为 Windows 平台）；需小天才家长账号。
- 手表发**语音消息**无法转文字，转发为"语音"占位通知。
- Python 3.14 下 pyyaml 若无轮子，配置可写为 JSON 格式（loader 自动降级）。
- 轮询间隔默认 2s，去重 LRU 200 条/120s，回声过滤 60s（文件持久化，多实例/重启共享），
  防止重复转发与自我回传。
