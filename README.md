# 小天才 ↔ QQ 桥接（AstrBot 插件版）

> **温馨提示**  
> 因雷电模拟器限制，仅适用于Windows平台  
> 本项目仅供学习交流使用  
> 本项目有使用Vibe Coding  
> 分发，修改，再发布等操作，请遵守Apache-2.0许可证  
> 对于雷电模拟器9的可用性未知，建议安装雷电模拟器14  

通过 **雷电模拟器 + Python ADB** 控制小天才 App，与 QQ 双向桥接消息。
QQ 侧发送走 **AstrBot 插件**（插件常驻 AstrBot 进程，替代 HTTP API），
目前插件部分待 AstrBot 安装完成后补充，当前阶段已实现并实机验证**小天才侧**：
读取消息、发送消息、轮询调度、去重/回声过滤、断线重连。

```
小天才 App(雷电模拟器) ──ADB──▶ Python 桥脚本 ──HTTP──▶ AstrBot 插件 ──▶ QQ
       ▲                                                        │
       └──────────────────── 反向回调（NapCat 上报 / 插件转发，待就绪） ──┘
```

## 目录结构

```
project/
├── main.py                  # 入口（--check / --debug dump-ui / --once / 正常启动）
├── config.yaml              # 配置（含中文注释）
├── adb_controller.py        # ADB 封装：连接/点击/滑动/截图/UI 解析/中文输入策略链
├── xiaotiancai.py           # 小天才 App 操作：启动/登录检测/打开聊天/发送/读取
├── bridge.py                # 轮询调度：去重 + 回声过滤 + 断线重连 + 转发
├── plugin_client.py         # AstrBot 插件客户端（备用，插件就绪后启用）
├── qq_webhook.py            # 反向回调服务（纯 stdlib，NapCat/插件就绪后启用）
├── utils/
│   ├── logger.py            # 日志（控制台 + 滚动文件）
│   └── deduplicate.py       # LRU 去重 + 回声过滤
├── tools/
│   ├── dump_ui.py           # 实机 UI 探测（填映射表用）
│   └── selftest.py          # 环境自检（不依赖配置）
├── astrbot_plugin_xtc_bridge/   # ⏳ AstrBot 插件（待 AstrBot 就绪后编写）
└── requirements.txt
```

## 快速开始（当前阶段）

```bash
pip install -r requirements.txt        # 只需 pyyaml（无外网时可用 JSON 格式配置）

python tools\selftest.py               # ① 环境自检（adb 发现/连接/UI dump/启动/登录检测）
python main.py --check                 # ② 或使用 --check
```

然后在雷电模拟器中**登录小天才家长账号**，再：

```bash
python tools\dump_ui.py --filter 消息   # ③ 查看界面控件，把 resource-id 填入 config.yaml → xiaotiancai.ui
python main.py                          # ④ 启动桥接（当前转发模式=log，仅打印）
```

> `forward.mode: log` 为调试占位：收到的消息只打印不发送。
> AstrBot 插件就绪后改为 `plugin` 并填写 `forward.plugin.base_url/token`。

## 常用命令

| 命令 | 作用 |
|---|---|
| `python tools\selftest.py` | 环境自检（8 项，逐项 PASS/FAIL） |
| `python main.py --check` | 自检后退出 |
| `python tools\dump_ui.py` | 打印当前界面控件清单 |
| `python tools\dump_ui.py --filter 发送 --tap 3` | 只看含"发送"的节点，并点击第 3 个 |
| `python main.py --once` | 轮询一轮后退出（测试读取链路） |
| `python main.py --debug dump-ui` | 等价 dump_ui |

## 中文输入方案（已在真机实测）

| 方案 | 状态 | 说明 |
|---|---|---|
| **ADBKeyBoard IME**（推荐） | 自动安装 | `auto_install_adbkeyboard: true` 时 main.py 启动自动下载安装并设为默认输入法；装好后通过广播 `ADB_INPUT_TEXT` 注入任意文本（含中文） |
| `cmd clipboard set-text` + 粘贴 | ❌ 本模拟器不可用 | LDPlayer14(Android 14) 镜像未实现该 shell 命令（"No shell command implementation"） |
| `input text` | ⚠️ 仅数字可用 | 实测字母被**拼音输入法吞进组词状态**（上不了屏），中文更不行 |

- 恢复原输入法：`adb shell ime set com.android.inputmethod.pinyin/.InputService`
- 手动安装：下载 [ADBKeyBoard.apk](https://github.com/senzhk/ADBKeyBoard) 后
  `adb install -r ADBKeyBoard.apk && adb shell ime enable com.android.adbkeyboard/.AdbIME && adb shell ime set com.android.adbkeyboard/.AdbIME`

## 消息读取策略（启发式，登录后微调）

- **聊天窗口内**：取输入框上方最靠下的一条文本（最新气泡）；
  忽略 `chat_junk_texts` 里的系统文案。
- **聊天列表**：找联系人行，取行内最后一条预览文本（需填 `ui.watch_contact`）。

小天才界面更新后，优先调整 `config.yaml → xiaotiancai.ui`（resource-id/文案），
不要改代码。定位控件统一走 `uiautomator dump` 动态解析，无硬编码坐标。

## AstrBot 插件（已编写并验证，v4.27.4）

插件位于 `astrbot_plugin_xtc_bridge/`，已安装到 `C:\Users\<用户名>\.astrbot\data\plugins\xtc_qq_bridge\`。
已在 AstrBot 自带 Python 运行时中通过冒烟测试（模块导入 / /api/ping / 鉴权 / 转发入队）。

**启用步骤**：
1. 启动 AstrBot 桌面版 → WebUI「插件管理」→ 启用 `xtc_qq_bridge`。
2. 在 NapCat（AstrBot 平台适配器）里登录 QQ 机器人。
3. 打开插件配置，确认与 `config.yaml` 一致（默认值已对齐，可不改）：
   - `http_port` 11452 ↔ `forward.plugin.base_url`
   - `token` ↔ `forward.plugin.token`
   - `python_callback_url` ↔ `webhook` 地址（http://127.0.0.1:5000/qq_callback）
   - `python_callback_token` ↔ `webhook.token`
4. 先给机器人发一条 QQ 消息（让插件学到平台 ID），日志确认「本地端点已启动」。
5. 填 `config.yaml` 的 `target.qq_private`（或 `qq_group`，支持单个或多个，如
   `["10001","10002"]`）与 `target.xtc_contact`；`target.nicknames` 里把 App 联系人名
   映射成转发到 QQ 时显示的昵称（行首 `#` 是注释，启用时删掉）。
6. `python main.py` 启动桥接。

**插件工作原理**：
- 小天才→QQ：Python 轮询到消息 → 格式化为 `[时间] [本地配置昵称] 消息`（昵称取自
  `config.yaml → target.nicknames` 映射，不使用 App 里的原始姓名）→ POST
  `http://127.0.0.1:11452/api/forward` → 插件 `context.send_message(...)` 发 QQ。
- QQ→小天才：给机器人发命令 **`/小天才 <文本>`**（如 `/小天才 晚上回家吃饭`）→
  插件格式化为 `[时间] [QQ发送人昵称] 文本` → POST 到 Python 侧 `qq_webhook`（5000 端口）→
  ADB 操作小天才发送。

**接收白名单（严格模式，config.yaml → webhook）**：
- `allow_from`：私聊白名单，只有这些 QQ 号能触发 `/小天才`；
- `allow_groups`：群聊白名单，只有这些群号能触发（群聊里按群号判断，不看个人）；
- 对应列表为空 = 该类消息全部拒绝；启动时日志会提醒。
- 插件侧的 `allow_senders`/`allow_groups` 为可选的额外前置过滤（默认空=不限制，交给桥侧白名单）。

> 注意：主动发送需要平台 ID，插件默认自动取"最近收到过消息的平台"；
> 若日志报「无法确定平台 ID」，先让 QQ 给机器人发条消息，或在插件配置里填 `platform_id`
> （可让机器人执行 `/sid` 查看）。

## 部署到新机器（打包分发）

整个项目目录即可打包（含 AstrBot 插件源码、ADBKeyBoard APK、一键安装脚本）。
打包前建议删除：`config.yaml`（含本机敏感信息，install.bat 会从模板重新生成）、`data/`、`logs/`、
`__pycache__/`（Python 自动生成的字节码缓存，删掉无影响、避免版本混乱；不删也能跑）。
用 git 管理时直接使用仓库内的 `.gitignore`。

**新机器初始化清单**（按顺序）：

| # | 步骤 | 说明 |
|---|---|---|
| 1 | 安装 **Python 3.10+** | 勾选 Add to PATH |
| 2 | 安装**雷电模拟器** + 小天才 App | 登录家长账号；adb 端口 5555 |
| 3 | 安装 **AstrBot** 并启动一次 | 桌面版或 pip 版均可，启动生成 `~\.astrbot` |
| 4 | 双击 **`install.bat`** | 装 pyyaml、复制插件到 `~\.astrbot\data\plugins\xtc_qq_bridge\`、从模板生成 config.yaml 和插件配置 |
| 5 | 编辑 **`config.yaml`** | QQ 号、联系人、昵称、token（与插件配置一致） |
| 6 | AstrBot WebUI | 启用插件 `xtc_qq_bridge`；确认 token 与 config.yaml 一致；配置 NapCat 适配器、登录 QQ |
| 7 | 给机器人发一条消息 | 让插件学到平台 ID |
| 8 | `python main.py` | ADBKeyBoard 用捆绑 APK 自动安装（免下载） |

注：
- 请将config.example.yaml复制为config.yaml再进行编辑
- 使用yaml格式，请安装pyyaml，例如`pip install pyyaml`

**打包/不打包清单**：
- ✅ 随包：全部 `.py`、`astrbot_plugin_xtc_bridge/`（插件源码）、`keyboardservice-debug.apk`（捆绑安装）、`install.bat`、`config.example.yaml`、`requirements.txt`
- ❌ 不随包（每台机器独立）：模拟器本身、小天才账号登录态、AstrBot 里的 NapCat/QQ 登录态、`config.yaml` 的值、平台 ID（运行时自动学习）

**插件同步更新**：项目里 `astrbot_plugin_xtc_bridge/` 是唯一源码，改完重新跑一次 `install.bat`（或手动复制 4 个文件到 `~\.astrbot\data\plugins\xtc_qq_bridge\`）即可，然后 AstrBot WebUI 重载插件。

## 已知限制

- **必须先登录家长账号**（当前模拟器停在登录页 `AccountVerifyLoginActivity`，读取/发送均不可用）。
- 登录页实测控件：`et_verify_account`（手机号）、`tv_verify_code`（获取验证码）、
  `cb_protocol`（协议）——登录需手机号+验证码，或 `rv_login_modes` 其他方式。
- Python 3.14 下 pyyaml 若无轮子，配置可写为 JSON 格式（loader 自动降级）。
- 轮询间隔默认 2s，去重 LRU 200 条/120s，回声过滤 60s，防止重复转发与自我回传。

## ⏳ 待办（AstrBot 就绪后）

1. ✅ 插件已编写并验证（见上节），已安装到 `~/.astrbot/data/plugins/xtc_qq_bridge/`。
2. ✅ `config.yaml` 已切到 `forward.mode: plugin` 并启用 `webhook`。
3. 待你在 AstrBot 中启用插件、登录 NapCat 机器人、登录小天才家长账号并实测双向转发。
