# 小天才 ↔ QQ 桥接（AstrBot 插件版）

> **温馨提示**
> 跨平台：**Windows / Linux / macOS** 均可运行，只要目标 Android 环境开放 ADB
> 本项目仅供学习交流使用
> 本项目有使用 Vibe Coding
> 分发，修改，再发布等操作，请遵守 Apache-2.0 许可证

通过 **模拟器 / Android 容器（WSA、Waydroid、雷电、MuMu 等）+ Python ADB** 控制小天才 App，
与 QQ 双向桥接消息。QQ 侧通过 **AstrBot 插件**（常驻 AstrBot 进程）收发，已实现：
双向消息桥接、账密自动登录、登录态检测、安全验证提醒与自动恢复。

> 🚀 **一键安装**：Windows 双击 `install.bat`；Linux / macOS 执行 `bash install.sh`（详见「快速安装」）。

```
小天才 App(Android 环境) ──ADB──▶ Python 桥脚本 ──HTTP──▶ AstrBot 插件 ──▶ QQ
        ▲                                                                │
        └──────────────── 插件转发 QQ 命令（/小天才 等）──────────────────┘
```

## ⚠️ 运行环境注意事项（重要，先读）

| 事项 | 说明 |
|---|---|
| **支持的平台** | **Windows / Linux / macOS** 都能跑。桥接本身只依赖 Python + `adb`；只要目标 Android 环境能被 `adb` 连上即可 |
| **支持的 Android 环境** | Windows：雷电、MuMu、**WSA / WSABuilds**；Linux：**Waydroid**、Genymotion、Android-x86（含夜神/逍遥等提供 adb 端口的模拟器）；macOS：Genymotion、Android Studio 模拟器。**真机（USB 或网络调试）同样可用** |
| **必须开启「ADB 调试」** | 模拟器：在它的设置里打开 ADB 调试；WSA：设置 → Advanced settings → Developer mode；真机：开发者选项 → USB 调试。不开的话 `adb devices` 为空，桥接无法连接 |
| **adb 端口** | 通用 **5555**（Waydroid、雷电、MuMu、Genymotion、真机网络调试）；MuMu 另有 **16384**、夜神 **62001**、逍遥 **21503**、Waydroid 多实例 **5556**；**WSA 端口随机分配**（通常 58526）。用 `adb.port` / `adb.extra_ports` 指定 |
| **设备查找顺序** | **已有在线设备 → 环境变量 ADB_SERIAL → WSA 端口（仅 Windows）→ 常见端口（5555/16384/62001/21503/5556）**。已有设备永远优先，不会被新连接顶掉；多设备时优先 WSA，其次 `emulator-XXXX` |
| **同时连多个设备** | 多设备并存时日志会提示；**请在 `adb.serial` 显式指定**要用的那个（如 `"127.0.0.1:5555"`） |
| **镜像差异** | 已做防御式处理：无 `cmd clipboard`（自动走 ADBKeyBoard）、`ime set` 不生效（自动双写 settings）、`mCurrentFocus` 为空（自动回退 `topResumedActivity`）等，一般无需干预 |
| **小天才 App 需自行安装** | 每个环境都要装小天才 App 并登录家长账号；界面控件 id 与运行环境无关（同一 APK） |

## 🖥️ 各平台接入指引

<details open>
<summary><b>Windows</b>：雷电 / MuMu / WSA·WSABuilds</summary>

- **雷电（LDPlayer）**：设置 → 其他设置 → 开启 ADB 调试；adb 端口默认 `5555`。
- **MuMu**：设置 → 其他/高级 → 开启 ADB 调试；端口 `5555`（另有 `16384`）。
- **WSA / [WSABuilds](https://github.com/MustardChef/WSABuilds)**：设置 → Advanced settings → 打开
  **Developer mode**，记下显示的 `IP:端口`（通常 `127.0.0.1:58526`）。桥接会自动读注册表
  `HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsSubsystemForAndroid` 取端口，读不到则试
  58526/58527/58525/6520/6521。也可以直接写死：
  ```yaml
  adb:
    wsa_port: 58526              # 0（默认）= 自动读注册表
    # serial: "127.0.0.1:58526"  # 或者直接指定序列号
    auto_launch_wsa: true        # WSA 没开机时自动拉起客户端
  ```
- 准备 `adb`：`winget install Google.PlatformTools`，或把 platform-tools 加入 PATH。
- 首次连接若报 `10061`（Hyper-V 抢占端口）：管理员执行
  `netsh int ipv4 add excludedportrange protocol=tcp startport=58526 numberofports=1` 后重启电脑。
- WSA 与宿主剪贴板共享：桥接运行期间**不要复制别的内容**再手动粘贴，调试时容易被误导。
</details>

<details>
<summary><b>Linux</b>：Waydroid / Genymotion / Android-x86 / 真机</summary>

- **Waydroid**（推荐，最接近 WSA 的形态，官方文档见 <https://waydro.id>）：
  ```bash
  # 安装（Debian/Ubuntu 示例，其他发行版见官方文档）
  sudo apt install curl ca-certificates -y
  curl -s https://repo.waydro.id | sudo bash
  sudo apt install waydroid -y
  sudo waydroid init                      # 初始化（需要能访问镜像源）

  sudo systemctl enable --now waydroid-container
  waydroid session start                  # Wayland 会话；X11 会话用 waydroid session start -X
  # 另开一个终端：
  adb connect 127.0.0.1:5555              # Waydroid 的 adb 端口就是 5555
  ```
  桥接侧无需特殊配置（`adb.port: 5555` 默认即可）；也可以设 `adb.auto_launch_emulator: true`
  让桥接在找不到设备时自动 `waydroid session start`。
  装 APK：`waydroid app install keyboardservice-debug.apk` 或 `adb install -r 小天才.apk`。
  注意：Waydroid 里 Android 与宿主**不共享剪贴板**，所以"剪贴板兜底"通道通常是不可用的，
  中文输入依赖 ADBKeyBoard（`main.py --check` 会打印 `clipboard_ok`）。
- **Genymotion**：启动实例后 `adb connect 127.0.0.1:5555`（多实例端口递增）。
- **Android-x86 / 夜神 / 逍遥等**：在模拟器设置里打开 ADB 调试，用 `adb connect 127.0.0.1:<端口>`
  （夜神 `62001`、逍遥 `21503`、部分实现 `5555`），必要时写进 `adb.extra_ports`。
- **真机（USB）**：需要在手机上打开 USB 调试并允许本机；若 `adb devices` 一直为空，通常是
  **udev 规则/权限**问题：
  ```bash
  # 厂商 ID 用 lsusb 查看（如 2717 = 小米）
  echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2717", MODE="0666", GROUP="plugdev"' \
    | sudo tee /etc/udev/rules.d/51-android.rules
  sudo udevadm control --reload-rules && sudo udevadm trigger
  sudo usermod -aG plugdev "$USER"        # 重新登录后生效
  ```
- **装 adb**：Debian/Ubuntu `sudo apt install adb`；Fedora `sudo dnf install android-tools`；
  Arch `sudo pacman -S android-tools`；或下载 platform-tools 后在 `adb.path` 指定绝对路径。
- **无桌面/容器环境**：模拟器需要显示输出与 `/dev/kvm`（容器加 `--device /dev/kvm`）；
  WSA 之类的宿主集成能力在 Linux 上不存在，属预期差异。
</details>

<details>
<summary><b>macOS</b>：Genymotion / Android Studio 模拟器 / 真机</summary>

- **装 adb**：`brew install --cask android-platform-tools`。
- **Android Studio 模拟器**：直接启动即可，`adb devices` 会显示 `emulator-5554` 之类的序列号
  （桥接优先选 `emulator-XXXX`）。
- **Genymotion / 真机**：`adb connect 127.0.0.1:5555` 或 USB 直连（首次需在手机上信任本机）。
- Apple Silicon 上运行 x86 Android 镜像可能很慢，建议用 arm64 镜像或 ARM 转译兼容的模拟器。
</details>

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
| 送达确认 | 转发成功自动回复「发送成功：[日期时间] [昵称] 内容」（QQ→小天才 回复发送者；小天才→QQ 在聊天内回复）；确认消息以「发送成功/发送失败」开头，不会被再次转发。全程不用 emoji |

## 目录结构

```
project/
├── main.py                  # 入口（--check / --debug dump-ui|adb-info / --once / 正常启动）
├── config.yaml              # 配置（含中文注释）；模板见 config.example.yaml
├── adb_controller.py        # ADB 封装：连接（WSA/Waydroid/模拟器/真机）/点击/滑动/截图/UI 解析/启动/文本注入
├── xiaotiancai.py           # 小天才 App 操作：启动/登录检测/账密登录/打开聊天/发送/读取
├── bridge.py                # 轮询调度：去重 + 回声过滤 + 断线重连 + 转发 + 自动登录检测
├── plugin_client.py         # AstrBot 插件客户端（转发/通知）
├── qq_webhook.py            # 反向回调服务（纯 stdlib）：接插件命令/消息 → 桥
├── utils/
│   ├── logger.py            # 日志（控制台 + 滚动文件）
│   └── deduplicate.py       # LRU 去重 + 文件化回声过滤
├── tools/
│   ├── dump_ui.py           # 实机 UI 探测（填映射表用）
│   ├── selftest.py          # 环境自检（不依赖配置）
│   ├── test_wsa.py          # 离线单测：WSA 端口/解析等纯逻辑（无需设备）
│   ├── test_integration.py  # 离线集成：假 ADB 执行器跑连接/启动/注入链（无需设备）
│   └── test_reported_bugs.py # 回归：历史来源/命令去重/登录判定/表单输入（无需设备）
├── astrbot_plugin_xtc_bridge/   # AstrBot 插件源码（安装见下文）
├── keyboardservice-debug.apk    # 捆绑的 ADBKeyBoard APK（新机器免下载）
├── install.bat / install.sh     # 一键安装（Windows / Linux·macOS）
├── start.bat   / start.sh       # 启动器：环境自检 + 菜单（启动/自检/干跑/看界面/看日志）
└── requirements.txt
```

> 路径写法：下面命令里的 `\` 是 Windows 写法，Linux / macOS 换成 `/`（如 `python3 tools/selftest.py`）。
> 改完输入/启动/连接/命令去重相关代码后建议跑一遍离线测试（都不需要设备）：
> - `python tools/test_wsa.py` —— 纯逻辑单测（端口识别、activity/前台解析、端口顺序）
> - `python tools/test_integration.py` —— 假 ADB 执行器跑通连接/启动/注入策略链
> - `python tools/test_reported_bugs.py` —— 回归测试（历史来源 / 命令去重 / 登录判定 / 表单输入）
>
> ⚠️ **`.bat` 文件必须保持纯 ASCII 英文**：中文（尤其配合 `chcp 65001`）会让 cmd 解析
> 出错并报「命令未找到」之类的错误。Shell 脚本保持 LF 行尾，`.bat` 保持 CRLF 行尾。

## 快速安装（推荐）

| 平台 | 命令 | 一键完成的事 |
|---|---|---|
| **Windows** | 双击 `install.bat` | ① 装 pyyaml ② 复制插件到 `%USERPROFILE%\.astrbot\data\plugins\xtc_qq_bridge\` ③ 从模板生成 `config.yaml` ④ 生成插件初始配置 |
| **Linux / macOS** | `bash install.sh` | 同上，插件目录为 `~/.astrbot/data/plugins/xtc_qq_bridge/`；缺少 pyyaml 时优先装进项目 `.venv/`（绕开 PEP 668 限制），并检查 `adb` 是否在 PATH |

之后编辑 `config.yaml`，再按下方步骤启用插件即可。脚本只做初始化，**不会覆盖已有的
`config.yaml` 与插件配置**，重复执行是安全的。

## 快速开始

**方式一：用启动器（推荐）**

- Windows：双击 `start.bat`
- Linux / macOS：`bash start.sh`（仅首次需要，或先 `chmod +x start.sh install.sh`）

自动完成：找 Python 并校验版本（Linux/macOS 优先用项目 `.venv`）→ 缺 pyyaml 时尝试安装
→ `config.yaml` 不存在则从模板生成并提示填写 → 检查项目内有没有 ADBKeyBoard 的本地 APK
→ 提示 `adb` 是否可用。随后给出菜单：

| 菜单项 | 等价命令 |
|---|---|
| 1. 启动桥接 | `python main.py` |
| 2. 环境自检 | `python main.py --check`（adb/连接/运行环境/输入法/剪贴板） |
| 3. 干跑一轮 | `python main.py --once` |
| 4. 打印当前界面控件 | `python main.py --debug dump-ui` |
| 5. 打开 config.yaml | — |
| 6. 查看日志 | `logs/bridge.log` 末尾 40 行 |

也支持参数透传（不进菜单，直接跑完退出）：

```bash
# Windows
start.bat --check
start.bat --debug adb-info

# Linux / macOS
bash start.sh --check
bash start.sh --debug adb-info
```

**方式二：命令行**

```bash
pip install -r requirements.txt         # 只需 pyyaml（无外网时可用 JSON 格式配置）

python tools/selftest.py                # ① 环境自检（adb 发现/连接/UI dump/启动/登录检测）
python main.py --check                  # ② 或使用 --check
```

然后在 Android 环境里登录小天才家长账号（也可用 `/小天才登录` 自动登录），再：

```bash
python tools/dump_ui.py --filter 消息    # ③ 查看界面控件，按需调整 config.yaml → xiaotiancai.ui
python main.py                           # ④ 启动桥接
```

## QQ 命令（给机器人发）

| 命令 | 作用 |
|---|---|
| `/小天才 发送 <文本>` | 把文本发到小天才手表（如 `/小天才 发送 晚上回家吃饭`） |
| `/小天才 登录` | 手动登录小天才（`config.yaml → xiaotiancai.login` 的手机号+密码） |
| `/小天才 自动登录` | 切换十分钟自动登录检测（默认开启；重启后恢复配置值） |
| `/小天才 初始化` | 检测并恢复界面状态（登录/聊天页/文字模式/清空输入框） |
| `/小天才 历史消息 [条数] [来源]` | 查看最近 N 条对话（1-100，默认 20），可只看某个来源。数据来自**本地消息库**（不滚动界面）：桥接启用后自动积累的真实对话，已剔除发送成功等系统提示与 `/小天才` 命令；日期为明确数字（如昨天→`09-01`），每行形如 `[09-01 21:41] [来源] 发送方: 内容`，末尾附来源统计 |
| `/小天才 命令模式` | 切换命令模式（默认开启=仅命令转发；关闭=群/私聊所有新消息都转发） |
| `/小天才`（无参数） | 显示用法 |

发送者需命中 `webhook.allow_from`（私聊）/ `webhook.allow_groups`（群聊）白名单；
被拦截时回复「无权限执行此命令（不在白名单）」。

**送达确认**（`target.confirm_delivery`，默认开）：
- 转发成功 → 回复「发送成功：[日期时间] [昵称] 内容」（引用+@ 发送人）；失败 → 「发送失败：…」。
- 小天才消息转发到 QQ 成功 → 在小天才聊天内回复「发送成功：<转发内容>」。
- 确认消息以「发送成功/发送失败」开头（`xiaotiancai.ui.system_msg_prefixes`），**不会被当作接收消息再次转发**。

**历史消息（本地消息库，带来源）**：`/小天才 历史消息` 不滚动读界面，而是读 `data/msg_log.json`——
桥接在转发手表消息、把 QQ 消息发进小天才时自动归档真实对话；发送成功等系统提示与 `/小天才` 命令不入库。
**每条消息都记录来源**并在回显时标注：

```
小天才历史消息（最近 3 条）：
[09-01 21:41] [QQ私聊 10001] 小明: 中午吃什么
[09-01 21:42] [QQ群 123456] 小红: 群里说
[09-01 21:43] [手表] 宝贝: 我吃了
来源统计：手表 12 条、QQ群 123456 5 条、QQ私聊 10001 3 条
```

- 来源取值：`手表`（小天才 App 内）、`QQ私聊 <QQ号>`、`QQ群 <群号>`。
- 只想看某个来源：`/小天才 历史消息 30 手表`、`/小天才 历史消息 50 QQ群 123456`、
  `/小天才 历史消息 20 10001`（只写号码时私聊/群聊都能命中）。
- 日期由归档时间戳输出为明确数字（昨天 → `09-01`，前天 → `08-31`），格式 `[MM-DD HH:MM] [来源] 发送方: 内容`。
- 消息库自桥接启用起积累（重启不丢；清空该文件即重新开始）。旧版本归档的条目没有来源字段，
  回显时会按 `kind` 兜底显示为 `手表` / `QQ`。

## 小天才侧命令（在 Android 环境的小天才聊天里直接输入，由桥接程序执行）

> 与 QQ 命令隔离、不走 AstrBot 的命令解析：在小天才聊天窗口里输入下面命令，
> 桥接轮询检测到「自己发的、以 `/小天才` 开头」的消息后执行，并把结果写回聊天。
> 命令本身与执行结果都是家长侧消息，**不会转发到 QQ**。全程无 emoji。

| 命令 | 作用 |
|---|---|
| `/小天才 历史消息 [条数] [来源]` | 查看最近 N 条对话（1-100，默认 20），结果写回聊天；可只看某来源（`手表` / `QQ群 <群号>` / `QQ私聊 <QQ号>`），每行带来源标注并附来源统计 |
| `/小天才 搜索 <昵称>` | 在白名单 QQ 私聊/群聊中按昵称找人（附最后消息时间与距现在；无记录显示「未发送过任何消息」） |
| `/小天才 在线人数 <分钟>` | 统计最近 N 分钟内在白名单 QQ 会话发言过的去重人数（1-60，按会话分组） |
| `/小天才 提醒 <群号> <QQID> [内容]` | 机器人向该 QQ 群发消息 @ 目标用户（群号需在白名单） |
| `/小天才 帮助` | 显示上面的用法 |

数据来源：
- QQ 昵称/会话数据：插件通过 NapCat（OneBot v11）`get_friend_list` / `get_group_member_list` / `get_group_info` 实时拉取；
- 「最后消息时间 / 在线人数」：插件持续记录收到的 QQ 消息活动（从插件启动起积累，重启后清空）。
- 需要 AstrBot + 插件运行（QQ 侧命令的搜索范围 = `webhook.allow_from` + `webhook.allow_groups`）。

## 登录与安全验证闭环

```
掉登录 → 10 分钟检测到未登录 → 自动账密登录
  ├─ 成功 → 静默（不打扰）
  ├─ 密码错误等 → QQ 通知"登录失败（账号或密码错误等）"
  └─ 触发"登录安全验证" → QQ 通知"请手动打开对应窗口完成验证"
       └─ 你手动验证完成后 → 程序 2 秒内自动感知 → 桥接恢复 + QQ 通知"已重新登录"
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

## 文本注入方案（已在真机实测，每一步都校验结果）

注入的**判定标准是"输入框里真的出现了这段文本"**，而不是"广播命令执行成功"——
这正是不再出现"只能输入宿主剪贴板内容"的原因。

| 顺序 | 方案 | 说明 |
|---|---|---|
| 1 | **ADBKeyBoard `ADB_INPUT_TEXT`** | 明文广播，v2.5-dev 与旧版都兼容；设为默认输入法后注入任意文本（含中文） |
| 2 | **ADBKeyBoard `ADB_INPUT_B64`** | base64 广播；Oreo/P 之后 `am` 不再接受 UTF-8 明文参数时兜底 |
| 3 | **ADBKeyBoard `ADB_INPUT_CHARS`** | Unicode 码点数组，按 200 个码点分批 |
| 4 | **剪贴板 + 粘贴** | `cmd clipboard set-text` → **回读校验一致** → `KEYCODE_PASTE`。回读不一致（剪贴板与宿主共享的环境常见）就**直接放弃**，绝不粘贴宿主剪贴板里的旧内容 |
| 5 | `input text` | **仅 ASCII** 兜底（中文无效） |

- 仅当 1~3 都失败（输入法没附加到输入框）才降级到 4；每轮 `adb.input_retries`（默认 2）次重试。
- 发送前会校验聊天输入框内容，发现残留内容（上次失败的半截文本）会先清空再输入。
- **ADBKeyBoard 安装策略**：先检查设备上有没有，**没有才用项目目录里的本地 APK** 安装
  （`keyboardservice-debug.apk`，其次找 `ADBKeyBoard.apk`），**不做任何在线下载**。
  项目目录里没有该 APK 时会在日志里提示，把 APK 放回项目根目录重跑即可。
  Linux 上若懒得用 ADB，也可以直接给 Waydroid 装：`waydroid app install keyboardservice-debug.apk`。
- 恢复原输入法：`adb shell ime set com.android.inputmethod.pinyin/.InputService`
- 手动安装：`adb install -r keyboardservice-debug.apk && adb shell ime enable com.android.adbkeyboard/.AdbIME && adb shell ime set com.android.adbkeyboard/.AdbIME`
- 排查：`python main.py --debug adb-info` 会打印当前输入法、软键盘是否显示、剪贴板通道是否可用。

## AstrBot 插件（v4.x，已在 4.27.4 实测）

插件位于 `astrbot_plugin_xtc_bridge/`，安装到 AstrBot 的插件目录（`install.bat` / `install.sh` 自动完成）：

- Windows：`%USERPROFILE%\.astrbot\data\plugins\xtc_qq_bridge\`
- Linux / macOS：`~/.astrbot/data/plugins/xtc_qq_bridge/`

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

整个项目目录即可打包。打包前建议删除：`config.yaml`（含本机敏感信息，安装脚本会从模板重新生成）、
`data/`、`logs/`、`.venv/`、`__pycache__/`（删掉无影响；不删也能跑）。用 git 管理时使用仓库内 `.gitignore`。

**新机器初始化清单**（按顺序）：

| # | 步骤 | 说明 |
|---|---|---|
| 1 | 安装 **Python 3.10+** | Windows 安装时勾选 Add to PATH；Linux/macOS 一般自带 `python3` |
| 2 | 准备 **Android 环境 + 小天才 App** | Windows：雷电 / MuMu / WSA；Linux：Waydroid / Genymotion / Android-x86；macOS：Android Studio 模拟器 / Genymotion。都可换成真机。需开启 ADB 调试 |
| 3 | 确保 **adb 可用** | Windows `winget install Google.PlatformTools`；Linux `sudo apt install adb`；macOS `brew install --cask android-platform-tools`。也可在 `adb.path` 指定绝对路径 |
| 4 | 安装 **AstrBot** 并启动一次 | 桌面版或 pip 版均可（v4.26+ 已兼容），启动生成 `~/.astrbot` |
| 5 | 运行安装脚本 | Windows 双击 `install.bat`；Linux/macOS `bash install.sh`。装 pyyaml、复制插件、生成 config.yaml 和插件配置 |
| 6 | 编辑 **`config.yaml`** | QQ 号、联系人、昵称、账密、token（与插件配置一致）；按需设 `adb.port` / `adb.serial` |
| 7 | AstrBot WebUI | 启用插件；配置 NapCat 适配器、登录 QQ |
| 8 | 给机器人发一条消息 | 让插件学到平台 ID |
| 9 | 启动 | Windows 双击 `start.bat`；Linux/macOS `bash start.sh`（或 `python main.py`）。ADBKeyBoard 会先查设备上有没有，没有才用项目里的本地 APK 安装（不联网） |

注：
- 请将 `config.example.yaml` 复制为 `config.yaml` 再进行编辑（安装脚本已自动完成）
- 使用 yaml 格式需 pyyaml：`pip install pyyaml`（Linux 上若被 PEP 668 拦住，`install.sh` 会自动用项目 `.venv`）

**打包/不打包清单**：
- ✅ 随包：全部 `.py`、`astrbot_plugin_xtc_bridge/`（插件源码）、`keyboardservice-debug.apk`、
  `install.bat` / `install.sh`、`start.bat` / `start.sh`、`config.example.yaml`、`requirements.txt`
- ❌ 不随包（每台机器独立）：Android 环境本身（模拟器/WSA/Waydroid）、小天才账号登录态、
  AstrBot 里的 NapCat/QQ 登录态、`config.yaml` 的值、平台 ID（运行时自动学习）

**插件同步更新**：`astrbot_plugin_xtc_bridge/` 是唯一源码，改完重跑 `install.bat` / `install.sh`
（或手动复制到 `~/.astrbot/data/plugins/xtc_qq_bridge/`，Windows 为 `%USERPROFILE%\.astrbot\...`）并重载插件。

## 关键配置速览

| 配置项 | 说明 |
|---|---|
| `adb.path` | adb 可执行文件路径；留空自动探测（Windows 的雷电/MuMu/WindowsApps、Unix 的 `~/Android/Sdk`、`/usr/bin/adb` → PATH）。多环境并存时必须显式指定 |
| `adb.port` | 目标 adb 端口（默认 5555；MuMu 16384、夜神 62001、逍遥 21503、Waydroid 5555/5556） |
| `adb.wsa_port` | WSA/WSABuilds 的 ADB 端口（仅 Windows）；`0`=自动读注册表（默认 58526） |
| `adb.extra_ports` | 额外尝试的端口列表，例如 `[58526, 5555, 62001]` |
| `adb.serial` | 指定设备序列号（如 `"127.0.0.1:5555"`、`emulator-5554`）；留空自动选择 |
| `adb.auto_launch_wsa` | 找不到设备时自动拉起 WSA 客户端（仅 Windows，默认 false） |
| `adb.auto_launch_emulator` | 找不到设备时自动拉起模拟器/容器（如 Linux 的 Waydroid，默认 false） |
| `adb.input_retries` | 文本注入重试轮数（默认 2） |
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

## 常见故障速查

| 现象 | 排查 |
|---|---|
| `adb devices` 为空 | 目标没开 ADB 调试（模拟器设置 / WSA 的 Developer mode / 真机的 USB 调试）。先跑 `python main.py --check`，它会按平台给出具体建议（含 Linux 的 udev 规则做法） |
| `adb devices` 显示 `unauthorized` | 在目标设备/子系统窗口里点「允许 USB 调试」；真机可在手机上撤销授权后重新插拔 |
| Linux 真机看不到设备 | udev 规则/权限问题：配 `/etc/udev/rules.d/51-android.rules` 并把用户加入 `plugdev`（见「Linux」小节），再 `sudo udevadm control --reload-rules && sudo udevadm trigger` |
| Waydroid 连不上 | `waydroid session start`（X11 加 `-X`）后再 `adb connect 127.0.0.1:5555`；容器/无桌面环境需 `/dev/kvm` 与显示输出 |
| 连错设备（多个模拟器/真机） | 在 `adb.serial` 里写死要用的序列号（`python main.py --check` 会打印当前选中的是哪个） |
| WSA 报 `10061` 端口被拒 | Hyper-V 抢占端口：`netsh int ipv4 add excludedportrange protocol=tcp startport=<端口> numberofports=1` + 重启（见「Windows」小节） |
| 日志"启动小天才未确认" | 用 `python main.py --debug dump-ui` 看前台是不是 `com.xtc.watch`；`--debug adb-info` 看 `focus` 字段。App 未安装会直接报错 |
| 中文发不出去 / 发出去是旧内容 | `--debug adb-info` 看 `adbkeyboard_ready` 与 `ime`：必须 `com.android.adbkeyboard/.AdbIME`；`clipboard_ok=false` 时不要依赖剪贴板 |
| 输入框有残留导致内容拼接 | 已内置发送前清空；若仍出现，检查 `adb.input_retries` 与聊天页是否稳定 |
| uiautomator dump 失败 | 系统动画未关闭（`adb.disable_animations: true`）；界面有持续动画/弹窗 |

## 已知限制

- 桥接本体跨平台（Windows / Linux / macOS），但**宿主集成类能力有平台差异**：
  WSA/WSABuilds 与宿主剪贴板共享、可被自动拉起，这些只存在于 Windows；
  Linux/macOS 没有等价物（Waydroid 与宿主不共享剪贴板，因此中文输入依赖 ADBKeyBoard）。
- 需要小天才家长账号；小天才 App 必须由使用者自行安装（仓库不含该 APK）。
- 手表发**语音消息**无法转文字，转发为"语音"占位通知。
- Python 3.14 下 pyyaml 若无轮子，配置可写为 JSON 格式（loader 自动降级）。
- 轮询间隔默认 2s，去重 LRU 200 条/120s，回声过滤 60s（文件持久化，多实例/重启共享），
  防止重复转发与自我回传。
- 剪贴板与宿主共享的环境（如 WSA）里，桥接运行期间手动复制内容会干扰"剪贴板兜底"通道
  （ADBKeyBoard 通道不受影响）。
- 无桌面环境（纯 SSH/容器）运行图形模拟器需要额外处理显示与 `/dev/kvm`；
  Android Studio 的 headless 模拟器（`emulator -no-window`）可用，但需自行确认 adb 能连上。
