# 小天才 <-> QQ 桥接（AstrBot 插件版）

> **温馨提示**
> 跨平台：**Windows / Linux / macOS** 均可运行，只要目标 Android 环境开放 ADB
> 本项目仅供学习交流使用
> 本项目有使用 Vibe Coding
> 分发，修改，再发布等操作，请遵守 Apache-2.0 许可证

通过 **模拟器 / Android 容器（WSA、Waydroid、雷电、MuMu 等）+ Python ADB** 控制小天才 App，
与 QQ 双向桥接消息。QQ 侧通过 **AstrBot 插件**（常驻 AstrBot 进程）收发，已实现：
双向消息桥接、账密自动登录、登录态检测、安全验证提醒与自动恢复、弹窗自动清理与界面自愈。

> **一键安装**：Windows 双击 `install.bat`；Linux / macOS 执行 `bash install.sh`（详见「一键安装」）。
> **WSA/WSABuilds 经常断网**：另开一个终端跑 `python tools/wsa_net_guard.py`
> （独立守护工具，分级修复：重连 -> 网络复位 -> 重启子系统，见「WSA 网络守护」）。

```
小天才 App (Android 环境)  --ADB-->  Python 桥脚本  --HTTP-->  AstrBot 插件  -->  QQ
        ^                                                                        |
        +---------------- 插件转发 QQ 命令（/小天才 等）-------------------------+
```

**阅读顺序建议**：本文档**配置在前、原理在后**——第一次部署只看第一、二、三部分即可；
想改代码/调策略再看第四部分「原理与实现」。

---

# 一、快速上手

## 1. 环境要求与注意事项（重要，先读）

| 事项 | 说明 |
|---|---|
| **支持的平台** | **Windows / Linux / macOS** 都能跑。桥接本身只依赖 Python + `adb`；只要目标 Android 环境能被 `adb` 连上即可 |
| **支持的 Android 环境** | Windows：雷电、MuMu、**WSA / WSABuilds**；Linux：**Waydroid**、Genymotion、Android-x86（含夜神/逍遥等提供 adb 端口的模拟器）；macOS：Genymotion、Android Studio 模拟器。**真机（USB 或网络调试）同样可用** |
| **必须开启「ADB 调试」** | 模拟器：在它的设置里打开 ADB 调试；WSA：设置 -> Advanced settings -> Developer mode；真机：开发者选项 -> USB 调试。不开的话 `adb devices` 为空，桥接无法连接 |
| **adb 端口** | 通用 **5555**（Waydroid、雷电、MuMu、Genymotion、真机网络调试）；MuMu 另有 **16384**、夜神 **62001**、逍遥 **21503**、Waydroid 多实例 **5556**；**WSA 端口随机分配**（通常 58526）。用 `adb.port` / `adb.extra_ports` 指定 |
| **设备查找顺序** | **已有在线设备 -> 环境变量 ADB_SERIAL -> WSA 端口（仅 Windows）-> 常见端口（5555/16384/62001/21503/5556）**。已有设备永远优先，不会被新连接顶掉；多设备时优先 WSA，其次 `emulator-XXXX` |
| **同时连多个设备** | 多设备并存时日志会提示；**请在 `adb.serial` 显式指定**要用的那个（如 `"127.0.0.1:5555"`） |
| **镜像差异** | 已做防御式处理：无 `cmd clipboard`（自动走 ADBKeyBoard）、`ime set` 不生效（自动双写 settings）、`mCurrentFocus` 为空（自动回退 `topResumedActivity`）等，一般无需干预 |
| **小天才 App 需自行安装** | 每个环境都要装小天才 App 并登录家长账号；界面控件 id 与运行环境无关（同一 APK） |
| **WSA 断网是环境问题** | WSA / WSABuilds 长时间运行后子系统网络栈会失效。桥接自身会重连 ADB，但**网络栈死了要靠独立守护工具救**：见「WSA 网络守护」 |

## 2. 各平台接入指引

<details open>
<summary><b>Windows</b>：雷电 / MuMu / WSA·WSABuilds</summary>

- **雷电（LDPlayer）**：设置 -> 其他设置 -> 开启 ADB 调试；adb 端口默认 `5555`。
- **MuMu**：设置 -> 其他/高级 -> 开启 ADB 调试；端口 `5555`（另有 `16384`）。
- **WSA / [WSABuilds](https://github.com/MustardChef/WSABuilds)**：设置 -> Advanced settings -> 打开
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
- **WSA / WSABuilds 隔三差五断网**：用 `python tools/wsa_net_guard.py` 常驻守护（见「WSA 网络守护」）。
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

## 3. 一键安装

| 平台 | 命令 | 一键完成的事 |
|---|---|---|
| **Windows** | 双击 `install.bat` | ① 装 pyyaml ② 复制插件到 `%USERPROFILE%\.astrbot\data\plugins\xtc_qq_bridge\` ③ 从模板生成 `config.yaml` ④ 生成插件初始配置 |
| **Linux / macOS** | `bash install.sh` | 同上，插件目录为 `~/.astrbot/data/plugins/xtc_qq_bridge/`；缺少 pyyaml 时优先装进项目 `.venv/`（绕开 PEP 668 限制），并检查 `adb` 是否在 PATH |

之后编辑 `config.yaml`，再按「快速开始」启用插件即可。脚本只做初始化，**不会覆盖已有的
`config.yaml` 与插件配置**，重复执行是安全的。

## 4. 快速开始

**方式一：用启动器（推荐）**

- Windows：双击 `start.bat`
- Linux / macOS：`bash start.sh`（仅首次需要，或先 `chmod +x start.sh install.sh`）

自动完成：找 Python 并校验版本（Linux/macOS 优先用项目 `.venv`）-> 缺 pyyaml 时尝试安装
-> `config.yaml` 不存在则从模板生成并提示填写 -> 检查项目内有没有 ADBKeyBoard 的本地 APK
-> 提示 `adb` 是否可用。随后给出菜单：

| 菜单项 | 等价命令 |
|---|---|
| 1. 启动桥接 | `python main.py` |
| 2. 环境自检 | `python main.py --check`（adb/连接/运行环境/输入法/剪贴板） |
| 3. 干跑一轮 | `python main.py --once` |
| 4. 打印当前界面控件 | `python main.py --debug dump-ui` |
| 5. 打开 config.yaml | — |
| 6. 查看日志 | `logs/bridge.log` 末尾 40 行 |
| 7. WSA 网络守护状态 | `python tools/wsa_net_guard.py --status` |
| 8. 运行 WSA 网络守护 | `python tools/wsa_net_guard.py`（常驻，Ctrl+C 退出） |

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
python tools/dump_ui.py --filter 消息    # ③ 查看界面控件，按需调整 config.yaml -> xiaotiancai.ui
python main.py                           # ④ 启动桥接
```

## 5. 目录结构

```
project/
|-- main.py                  # 入口（--check / --debug dump-ui|adb-info / --once / 正常启动）
|-- config.yaml              # 配置（含中文注释）；模板见 config.example.yaml
|-- adb_controller.py        # ADB 封装：连接（WSA/Waydroid/模拟器/真机）/点击/滑动/截图/UI 解析/启动/文本注入
|-- xiaotiancai.py           # 小天才 App 操作：启动/登录检测/账密登录/弹窗清理/打开聊天/发送/读取
|-- bridge.py                # 轮询调度：去重 + 回声过滤 + 断线重连 + 转发 + 自动登录 + 界面自愈
|-- plugin_client.py         # AstrBot 插件客户端（转发/通知）
|-- qq_webhook.py            # 反向回调服务（纯 stdlib）：接插件命令/消息 -> 桥（每次回调都有日志）
|-- msg_log.py               # 本地消息库（/小天才 历史消息 的数据源）
|-- utils/
|   |-- logger.py            # 日志（控制台 + 滚动文件；控制台编码兜底，日志不会整条丢失）
|   `-- deduplicate.py       # LRU 去重 + 文件化回声过滤
|-- tools/
|   |-- dump_ui.py           # 实机 UI 探测（填映射表用）
|   |-- selftest.py          # 环境自检（不依赖配置）
|   |-- wsa_net_guard.py     # WSA/WSABuilds 网络守护（独立程序，可单独常驻或注册计划任务）
|   |-- wsa_guard.bat        # Windows 双击启动上面的守护（纯 ASCII）
|   |-- test_wsa.py          # 离线单测：WSA 端口/解析等纯逻辑（无需设备）
|   |-- test_integration.py  # 离线集成：假 ADB 执行器跑连接/启动/注入链（无需设备）
|   `-- test_reported_bugs.py# 回归：来源/命令去重/登录判定/发送确认/弹窗/自动登录/日志（无需设备）
|-- astrbot_plugin_xtc_bridge/   # AstrBot 插件源码（安装见下文）
|-- keyboardservice-debug.apk    # 捆绑的 ADBKeyBoard APK（新机器免下载）
|-- install.bat / install.sh     # 一键安装（Windows / Linux·macOS）
|-- start.bat   / start.sh       # 启动器：环境自检 + 菜单（启动/自检/干跑/看界面/看日志/WSA 守护）
`-- requirements.txt
```

> 路径写法：下面命令里的 `\` 是 Windows 写法，Linux / macOS 换成 `/`（如 `python3 tools/selftest.py`）。
> 改完输入/启动/连接/命令去重相关代码后建议跑一遍离线测试（都不需要设备）：
> - `python tools/test_wsa.py` —— 纯逻辑单测（端口识别、activity/前台解析、端口顺序）
> - `python tools/test_integration.py` —— 假 ADB 执行器跑通连接/启动/注入策略链
> - `python tools/test_reported_bugs.py` —— 回归测试（历史来源 / 命令去重 / 登录判定 / 发送确认 / 弹窗 / 自动登录 / 日志）
> - `python tools/wsa_net_guard.py --test` —— WSA 守护的分级修复逻辑自测
>
> **`.bat` 文件必须保持纯 ASCII 英文**：中文（尤其配合 `chcp 65001`）会让 cmd 解析
> 出错并报「命令未找到」之类的错误。Shell 脚本保持 LF 行尾，`.bat` 保持 CRLF 行尾。

---

# 二、配置

## 1. 关键配置速览

| 配置项 | 说明 |
|---|---|
| `adb.path` | adb 可执行文件路径；留空自动探测（Windows 的雷电/MuMu/WindowsApps、Unix 的 `~/Android/Sdk`、`/usr/bin/adb` -> PATH）。多环境并存时必须显式指定 |
| `adb.port` | 目标 adb 端口（默认 5555；MuMu 16384、夜神 62001、逍遥 21503、Waydroid 5555/5556） |
| `adb.wsa_port` | WSA/WSABuilds 的 ADB 端口（仅 Windows）；`0`=自动读注册表（默认 58526） |
| `adb.extra_ports` | 额外尝试的端口列表，例如 `[58526, 5555, 62001]` |
| `adb.serial` | 指定设备序列号（如 `"127.0.0.1:5555"`、`emulator-5554`）；留空自动选择 |
| `adb.auto_launch_wsa` | 找不到设备时自动拉起 WSA 客户端（仅 Windows，默认 false） |
| `adb.auto_launch_emulator` | 找不到设备时自动拉起模拟器/容器（如 Linux 的 Waydroid，默认 false） |
| `adb.input_retries` | 文本注入重试轮数（默认 2） |
| `adb.dump_retries` / `adb.dump_delay` | UI dump 默认重试次数/间隔（默认 2 / 0.8s；交互路径自动用更快的参数） |
| `adb.focus_cache_ttl` | 前台组件缓存秒数（默认 1.5；减少 dumpsys，点击/发送更快） |
| `forward.mode` | `plugin`=走 AstrBot 插件；`log`=仅打印调试 |
| `target.xtc_contact` | 小天才联系人名（打开聊天用） |
| `target.nicknames` | App 名 -> 显示昵称映射（行首 `#` 是注释） |
| `target.notify_qq` | 登录/异常通知目标（留空用 qq_private 第一个） |
| `target.confirm_delivery` | 送达确认开关（默认 true：成功回复「发送成功：…」，失败回复「发送失败：…」） |
| `target.qq_private` / `qq_group` | 转发目标，支持单个或列表，可并存 |
| `xiaotiancai.login.phone/password` | 账密登录凭据 |
| `xiaotiancai.auto_login` | 自动登录总开关（默认 true；`/小天才 自动登录` 可临时切换） |
| `xiaotiancai.login_check_interval` | 登录态复查间隔（秒，默认 600） |
| `xiaotiancai.login_retry_interval` | 登录"超时/网络慢"后的重试间隔（秒，默认 120） |
| `xiaotiancai.login_retry_after_fail` | 明确账号/密码错误后的重试间隔（秒，默认 1800） |
| `xiaotiancai.login_retry_after_risk` | 触发安全验证后的重试间隔（秒，默认 900） |
| `xiaotiancai.login_state_ttl` | 登录态缓存秒数（默认 5，减少界面 dump） |
| `xiaotiancai.ui.interaction_delay` | 点击/输入之间的等待（秒，默认 0.6；设备慢可调大） |
| `xiaotiancai.ui.send_retries` | 发送确认失败时的重试轮数（默认 2；只在"输入框仍留有内容"时重发） |
| `xiaotiancai.ui.risk_markers` | 安全验证检测标记 |
| `xiaotiancai.ui.login_error_markers` | 登录失败判定标记（泛化词只在"短提示且不含网络字眼"时采纳） |
| `xiaotiancai.ui.login_progress_markers` | "登录中/正在验证/请稍候"等进度文案（**出现即不判失败**） |
| `xiaotiancai.ui.system_msg_prefixes` | 桥接系统提示前缀（发送成功/发送失败），读取时跳过不转发 |
| `webhook.allow_from` / `allow_groups` | 接收白名单（私聊/群聊） |
| `wsa_guard.*` | WSA 网络守护参数（只被 `tools/wsa_net_guard.py` 读取） |

## 2. config.yaml 详解

安装脚本会从 `config.example.yaml` 生成 `config.yaml`，逐段说明如下（改完重启桥接生效）：

```yaml
# ---------- ADB / 模拟器 / 容器 ----------
adb:
  path: ""                     # 留空自动探测；多环境并存时建议写绝对路径
  host: "127.0.0.1"
  port: 5555                   # 通用 adb 端口（MuMu 16384、夜神 62001、逍遥 21503）
  wsa_port: 0                  # WSA/WSABuilds 端口；0=自动读注册表
  extra_ports: []              # 额外尝试的端口，例如 [58526, 5555, 62001]
  serial: ""                   # 多设备时写死要用的那个
  auto_launch_wsa: false       # WSA 没开机时自动拉起
  auto_launch_emulator: false  # Waydroid 等容器自动拉起
  heartbeat_interval: 10       # ADB 心跳（秒）：掉线自动重连
  disable_animations: true     # 关系统动画（uiautomator dump 需要界面空闲）
  input_retries: 2             # 文本注入重试轮数
  dump_retries: 2              # UI dump 重试次数
  dump_delay: 0.8              # UI dump 重试间隔（秒）
  focus_cache_ttl: 1.5         # 前台组件缓存（秒）

# ---------- 转发（QQ 侧） ----------
forward:
  mode: "plugin"               # plugin=转发到 AstrBot 插件；log=仅打印调试
  plugin:
    base_url: "http://127.0.0.1:11452"   # 与插件 http_port 一致
    token: "change-me-bridge-token"      # 与插件 token 一致（建议改随机串）

# ---------- 桥接目标 ----------
target:
  xtc_contact: ""              # 小天才 App 里的联系人昵称（如“张三”）
  nicknames:                   # App名 -> 转发到 QQ 时显示的昵称（行首 # 是注释）
    # "张三": "张三"
  default_nickname: ""         # 未命中映射时的兜底；留空回退 App 原始名
  notify_qq: ""                # 登录/异常通知 QQ 号（留空用 qq_private 第一个）
  confirm_delivery: true       # 送达确认（双向，无 emoji）
  qq_private: ""               # 转发目标 QQ：单个 "10001" 或 ["10001","10002"]
  qq_group: ""                 # 转发目标群：单个或多个（可与 qq_private 并存）

# ---------- 小天才 App ----------
xiaotiancai:
  package: "com.xtc.watch"
  main_activity: ".MainActivity"
  check_interval: 2            # 消息轮询间隔（秒）
  auto_install_adbkeyboard: true
  login:
    phone: ""                  # 手机号
    password: ""               # 密码（注意：不是验证码）
  auto_login: true             # 自动登录总开关
  login_check_interval: 600    # 登录态复查间隔（秒）
  login_retry_interval: 120    # 超时/网络慢后的重试间隔（秒）
  login_retry_after_fail: 1800 # 明确的账号密码错误后的重试间隔（秒）
  login_retry_after_risk: 900  # 安全验证后的重试间隔（秒）
  login_state_ttl: 5           # 登录态缓存（秒）
  ui:
    message_tab_text: "微聊"                 # 聊天列表 Tab 文案（精确匹配）
    login_markers: ["注册/登录"]             # 出现这些文案视为未登录
    risk_markers: ["安全验证", "风险", "滑块", "拖动滑块", "图形验证", "验证码"]
    login_error_markers: ["密码错误", "账号不存在", "手机号不存在", "错误", "失败", "次数过多"]
    login_progress_markers: ["登录中", "正在登录", "正在验证", "请稍候", ...]
    login_timeout: 45                        # 等待登录结果的最长秒数
    input_resource_id: "com.xtc.watch:id/et_chat_text_content"
    send_resource_id: "com.xtc.watch:id/tv_send_view"
    send_texts: ["发送"]
    interaction_delay: 0.6                   # 点击/输入之间的等待（秒）
    send_retries: 2                          # 发送确认失败时的重试轮数
    send_fail_markers: ["发送失败", "网络异常", ...]
    popup_skip_texts: ["以后再说", "稍后更新", ...]   # 更新/活动弹窗点这些跳过
    anr_wait_texts: ["等待", "等待响应"]               # 无响应弹窗点这些（不杀 App）
    system_msg_prefixes: ["发送成功", "发送失败"]      # 送达确认前缀，读取时跳过
    badge_resource_ids: []

# ---------- 日志 ----------
logging:
  level: "INFO"                # INFO 就能看到"收到 QQ 命令/收到小天才消息/发送结果"
  file: "logs/bridge.log"

# ---------- 反向回调（QQ -> 小天才） ----------
webhook:
  enabled: true
  host: "127.0.0.1"
  port: 5000
  path: "/qq_callback"
  token: "change-me-webhook-token"       # 与插件 python_callback_token 一致
  allow_from: []               # 私聊白名单：允许触发 /小天才 的 QQ 号（空=该类全部拒绝）
  allow_groups: []             # 群聊白名单：允许触发 /小天才 的群号（空=该类全部拒绝）

# ---------- WSA / WSABuilds 网络守护（独立工具，可选） ----------
wsa_guard:
  interval: 30
  ping_hosts: ["223.5.5.5", "8.8.8.8"]
  recover_wait: 8
  light_recover: true
  allow_reboot: true
  reboot_cooldown: 600
  allow_restart_wsa: false
  log_file: "logs/wsa_guard.log"
```

> 需要 yaml 格式就装 pyyaml：`pip install pyyaml`。**完全没有 pyyaml 时**桥接主程序无法启动
> （除非把配置写成 JSON），而 `tools/wsa_net_guard.py` 会退化成"只解析 `wsa_guard` 段"，
> 不受影响。

## 3. WSA / WSABuilds 网络守护（独立工具）

WSA（含 WSABuilds / MagiskOnWSA）跑久了会"隔三差五断网"：宿主侧 adb 还在、`adb devices` 时有时无，
但 Android 子系统里的网络栈已经不通——表现就是界面读不到、消息发不出去、`uiautomator` 一直失败。
这**不是桥接程序的 bug**，靠桥接自己重连也救不回来，所以单独提供了一个守护程序
`tools/wsa_net_guard.py`：**和主程序完全分开**，各跑各的，互不依赖。

**检测 + 分级修复（由轻到重，成功后自动退回日常监测）**

| 级别 | 触发条件 | 动作 |
|---|---|---|
| 0 | 一切正常 | 只记录状态（写入 `data/wsa_guard_state.json`） |
| 1 | ADB 掉线 / 子系统网络不通 | `adb connect` 重连；网络问题则关飞行模式 + `svc wifi/data enable` |
| 2 | 连续 2 轮仍未恢复 | `adb kill-server` + `start-server` 重连；网络栈复位（飞行模式开->关、wifi 关->开） |
| 3 | 连续 3 轮仍未恢复 | `adb reboot` 重启 Android 子系统（**有冷却期**，默认 10 分钟内只做轻量修复） |
| 4 | 允许 `--restart-wsa` 且多轮失败 | 重启宿主 WSA 客户端（`WsaClient` 杀掉后重新拉起）并等新端口 |

**用法**

```bash
# 常驻守护（推荐：和桥接各开一个终端）
python tools/wsa_net_guard.py
python tools/wsa_net_guard.py --interval 20            # 20 秒检测一次
# Windows 也可以直接双击 tools\wsa_guard.bat（等价于常驻运行）

# 只想看一眼 / 修一次
python tools/wsa_net_guard.py --status                 # 打印状态（正常时退出码 0）
python tools/wsa_net_guard.py --once                   # 检测一次，必要时修
python tools/wsa_net_guard.py --dry-run                # 只诊断，不动手

# 更重/更保守的策略
python tools/wsa_net_guard.py --no-reboot              # 禁止重启子系统
python tools/wsa_net_guard.py --restart-wsa            # 允许重启宿主 WSA 客户端
python tools/wsa_net_guard.py --no-light-recover        # 禁止网络轻量修复（只保活 adb）

# 不想常驻：注册 Windows 计划任务，每 5 分钟自动跑一次 --once
python tools/wsa_net_guard.py --install-task
python tools/wsa_net_guard.py --uninstall-task

# 纯逻辑自测（不需要设备）
python tools/wsa_net_guard.py --test
```

**日志与状态**

- 控制台 + `logs/wsa_guard.log`（可用 `--quiet` 只写文件、`--log-file` 改路径）。
- 最近一次检测结果写在 `data/wsa_guard_state.json`（`problem` / `action` / `streak` …），方便其它脚本读取。
- **注意**：第 3/4 级修复会重启子系统或 WSA，桥接会短暂断连（约 30~90 秒），恢复后桥接
  会自动重连并重新打开聊天页；这是预期行为，不是桥接崩了。

**为什么不做进主程序？** 因为断网时主程序的 ADB 链路本身也不可用，把修复逻辑塞进主程序会
互相拖累；分开之后：守护只管"把 Android 环境救活"，桥接只管"消息收发"，各自重启都不影响对方。

## 4. AstrBot 插件配置（两端对齐）

插件位于 `astrbot_plugin_xtc_bridge/`，安装到 AstrBot 的插件目录（`install.bat` / `install.sh` 自动完成）：

- Windows：`%USERPROFILE%\.astrbot\data\plugins\xtc_qq_bridge\`
- Linux / macOS：`~/.astrbot/data/plugins/xtc_qq_bridge/`

**启用步骤**：
1. 启动 AstrBot 桌面版 -> WebUI「插件管理」-> 启用 `xtc_qq_bridge`。
2. 在 NapCat（AstrBot 平台适配器）里登录 QQ 机器人。
3. 插件配置与 `config.yaml` 对齐（默认值已一致，改 token 需两端同步）：
   - `http_port` 11452 <-> `forward.plugin.base_url`
   - `token` <-> `forward.plugin.token`
   - `python_callback_url` <-> `webhook` 地址（http://127.0.0.1:5000/qq_callback）
   - `python_callback_token` <-> `webhook.token`
4. 先给机器人发一条消息（让插件学到平台 ID）；若报「无法确定平台 ID」，可让机器人执行 `/sid` 查看后填 `platform_id`。
5. `python main.py` 启动。

**接收白名单（严格模式）**：`webhook.allow_from`（私聊 QQ 号）/ `webhook.allow_groups`（群号）；
对应列表为空 = 该类消息全部拒绝。插件侧 `allow_senders`/`allow_groups` 为可选前置过滤。

## 5. 部署到新机器（打包分发）

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
| 10 | （WSA 用户）启动网络守护 | 另开一个终端 `python tools/wsa_net_guard.py`，或注册计划任务 `--install-task` |

**打包/不打包清单**：
- 随包：全部 `.py`、`astrbot_plugin_xtc_bridge/`（插件源码）、`keyboardservice-debug.apk`、
  `install.bat` / `install.sh`、`start.bat` / `start.sh`、`tools/wsa_guard.bat`、
  `config.example.yaml`、`requirements.txt`
- 不随包（每台机器独立）：Android 环境本身（模拟器/WSA/Waydroid）、小天才账号登录态、
  AstrBot 里的 NapCat/QQ 登录态、`config.yaml` 的值、平台 ID（运行时自动学习）

**插件同步更新**：`astrbot_plugin_xtc_bridge/` 是唯一源码，改完重跑 `install.bat` / `install.sh`
（或手动复制到 `~/.astrbot/data/plugins/xtc_qq_bridge/`，Windows 为 `%USERPROFILE%\.astrbot\...`）并重载插件。

---

# 三、使用

## 1. QQ 命令（给机器人发）

| 命令 | 作用 |
|---|---|
| `/小天才 发送 <文本>` | 把文本发到小天才手表（如 `/小天才 发送 晚上回家吃饭`） |
| `/小天才 登录` | 手动登录小天才（`config.yaml -> xiaotiancai.login` 的手机号+密码） |
| `/小天才 自动登录` | 切换自动登录检测开关（默认开启；重启后恢复配置值） |
| `/小天才 初始化` | 检测并恢复界面状态（**按需**：弹窗/启动/登录/聊天页/输入框，缺什么补什么） |
| `/小天才 历史消息 [条数] [来源]` | 查看最近 N 条对话（1-100，默认 20），可只看某个来源。数据来自**本地消息库**（不滚动界面）：桥接启用后自动积累的真实对话，已剔除发送成功等系统提示与 `/小天才` 命令；日期为明确数字（如昨天->`09-01`），每行形如 `[09-01 21:41] [来源] 发送方: 内容`，末尾附来源统计 |
| `/小天才 命令模式` | 切换命令模式（默认开启=仅命令转发；关闭=群/私聊所有新消息都转发） |
| `/小天才`（无参数） | 显示用法 |

发送者需命中 `webhook.allow_from`（私聊）/ `webhook.allow_groups`（群聊）白名单；
被拦截时回复「无权限执行此命令（不在白名单）」，**同时桥接控制台会打印一行"未转发（来源不在白名单）"**。

**历史消息样例**（`/小天才 历史消息 3`）：

```
小天才历史消息（最近 3 条）：
[09-01 21:41] [QQ私聊 10001] 张三: 中午吃什么
[09-01 21:42] [QQ群 123456] 李四: 群里说
[09-01 21:43] [手表] 王五: 我吃了
来源统计：手表 12 条、QQ群 123456 5 条、QQ私聊 10001 3 条
```

- 来源取值：`手表`（小天才 App 内）、`QQ私聊 <QQ号>`、`QQ群 <群号>`（每行只有 `[手表]` 这类短标签）。
- 只想看某个来源：`/小天才 历史消息 30 手表`、`/小天才 历史消息 50 QQ群 123456`、
  `/小天才 历史消息 20 10001`（只写号码时私聊/群聊都能命中）。
- 日期由归档时间戳输出为明确数字（昨天 -> `09-01`，前天 -> `08-31`）；消息库自桥接启用起积累
  （重启不丢；清空 `data/msg_log.json` 即重新开始）。旧版本归档的条目没有来源字段，
  回显时会按 `kind` 兜底显示为 `手表` / `QQ`。

**送达确认**（`target.confirm_delivery`，默认开）：
- 转发成功 -> 回复「发送成功：[日期时间] [昵称] 内容」（引用+@ 发送人）；失败 -> 「发送失败：…」。
- 小天才消息转发到 QQ 成功 -> 在小天才聊天内回复「发送成功：<转发内容>」。
- 确认消息以「发送成功/发送失败」开头（`xiaotiancai.ui.system_msg_prefixes`），**不会被当作接收消息再次转发**。
- "发送失败"是**真的失败**：桥接只有在"输入框已清空且没有新的失败提示（或出现新的己方消息气泡）"
  时才回「发送成功」，见「发送结果确认」。

## 2. 小天才侧命令（在 Android 环境的小天才聊天里直接输入）

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

**注意**：每次收到这类命令，桥接控制台都会打印
`[收到小天才命令] 来源=手表/家长侧 内容=... 时间标签=...`，执行结果也会打印一行。

## 3. 功能总览

| 功能 | 说明 |
|---|---|
| 小天才 -> QQ | 轮询读取手表发来的消息 -> `[日期时间] [本地昵称] 内容` 转发（时间取 App 内该消息的时间标签） |
| QQ -> 小天才 | 命令 `/小天才 <文本>` -> `[日期时间] [QQ昵称] 内容` 发到手表聊天 |
| 账密登录 | 命令 `/小天才登录`：用手机号 + 密码登录（**非验证码**） |
| 自动登录 | 定时检测登录态；未登录自动账密登录。**登录中超时不判失败并按间隔自动重试**（见「登录与安全验证闭环」） |
| 安全验证 | 触发"登录安全验证"时 QQ 提醒手动操作；**验证完成后自动恢复并通知，无需重启** |
| 界面自愈 | 连续读不到消息时自动清弹窗、必要时启动 App、重新打开聊天页；App 已在前台时**不会重复启动** |
| 弹窗清理 | 权限 / 无响应 / 更新 / 评价 / 活动 / 网络异常 / 警告弹窗自动处理；正常页面绝不误点 |
| 接收白名单 | 私聊 / 群聊严格白名单，只有列表内的会话可触发命令 |
| 昵称映射 | App 联系人名 -> 转发到 QQ 时显示的自定义昵称（不用 App 原始姓名） |
| 消息过滤 | 只转发手表侧消息（按 App 的发送方标注识别），自己发的/UI 文案/网络提示一律不转发 |
| 送达确认 | 转发结果如实回报：成功「发送成功：…」，失败「发送失败：…」；确认消息以「发送成功/发送失败」开头，不会被再次转发 |
| 可观测 | 收到的 QQ 命令、收到的小天才命令/消息、每次发送结果都打 INFO 日志（控制台 + `logs/bridge.log`） |

## 4. 常见故障速查

| 现象 | 排查 |
|---|---|
| `adb devices` 为空 | 目标没开 ADB 调试（模拟器设置 / WSA 的 Developer mode / 真机的 USB 调试）。先跑 `python main.py --check`，它会按平台给出具体建议（含 Linux 的 udev 规则做法） |
| `adb devices` 显示 `unauthorized` | 在目标设备/子系统窗口里点「允许 USB 调试」；真机可在手机上撤销授权后重新插拔 |
| **WSA / WSABuilds 隔三差五断网** | 跑独立守护：`python tools/wsa_net_guard.py`（分级修复：重连 -> 网络复位 -> 重启子系统；见「WSA 网络守护」）。只想看一眼用 `--status` |
| WSA 报 `10061` 端口被拒 | Hyper-V 抢占端口：`netsh int ipv4 add excludedportrange protocol=tcp startport=<端口> numberofports=1` + 重启（见「Windows」小节） |
| **自动登录不生效** | ① 看日志有没有 `检测到小天才未登录，触发自动登录`；② 若提示"没有配置账密"，补 `xiaotiancai.login.phone/password`；③ 若提示超时/安全验证，程序会按 `login_retry_interval` / `login_retry_after_risk` 自动重试，不需要重启；④ 确认 `xiaotiancai.auto_login: true`（QQ 发 `/小天才 自动登录` 可切换，日志会打印当前状态） |
| **明明在登录中却提示"登录失败"** | 已修复：出现"登录中/正在验证/请稍候"等进度文案时**一律不判失败**；只有明确的账号/密码错误才算失败，网络类临时问题按"超时->稍后重试"处理。若仍误报，把该文案加进 `xiaotiancai.ui.login_progress_markers` |
| **发送提示失败但其实发不出去 / 提示成功却没发出** | 已修复：只有"输入框已清空且无新的失败提示"或"出现新的己方气泡"才回「发送成功」；读不到界面、出现"发送失败/网络异常"、输入框仍有残留 -> 如实回「发送失败」。若你的机型提示语不同，补充 `xiaotiancai.ui.send_fail_markers` |
| **按按钮/发送很慢** | 已优化：UI dump 走 `/dev/tty` 快路径、前台组件缓存、登录态缓存、交互等待变短。设备本身慢可调大 `xiaotiancai.ui.interaction_delay`（0.6 -> 1.0）；网络差可减小 |
| **控制台看不到收到的命令** | INFO 级别下应当能看到 `[QQ回调] 收到 …`、`[收到QQ命令] …`、`[收到小天才命令] …`、`[收到小天才消息] …`、`[QQ->小天才] 发送成功/失败`。看不到时：① 确认 `logging.level: INFO`；② 确认命令真的到了（QQ 侧看插件日志，小天才侧看界面）；③ 中文 Windows 控制台编码问题已兜底（不会再整条丢失） |
| **已经启动了 App 还重复启动** | 已修复：`launch()` 先判断前台，已在前台直接返回，不执行任何启动命令；`/小天才 初始化` 也改成按需执行 |
| **弹窗挡住界面导致读不到消息** | 已修复：常见弹窗（权限/无响应/更新/评价/活动/网络/警告）会自动处理；连续读不到消息会触发界面自愈。特殊弹窗可加 `xiaotiancai.ui.popup_skip_texts` / `anr_wait_texts` |
| Linux 真机看不到设备 | udev 规则/权限问题：配 `/etc/udev/rules.d/51-android.rules` 并把用户加入 `plugdev`（见「Linux」小节），再 `sudo udevadm control --reload-rules && sudo udevadm trigger` |
| Waydroid 连不上 | `waydroid session start`（X11 加 `-X`）后再 `adb connect 127.0.0.1:5555`；容器/无桌面环境需 `/dev/kvm` 与显示输出 |
| 连错设备（多个模拟器/真机） | 在 `adb.serial` 里写死要用的序列号（`python main.py --check` 会打印当前选中的是哪个） |
| 日志"启动小天才未确认" | 用 `python main.py --debug dump-ui` 看前台是不是 `com.xtc.watch`；`--debug adb-info` 看 `focus` 字段。App 未安装会直接报错 |
| 中文发不出去 / 发出去是旧内容 | `--debug adb-info` 看 `adbkeyboard_ready` 与 `ime`：必须 `com.android.adbkeyboard/.AdbIME`；`clipboard_ok=false` 时不要依赖剪贴板 |
| 输入框有残留导致内容拼接 | 已内置发送前清空；若仍出现，检查 `adb.input_retries` 与聊天页是否稳定 |
| uiautomator dump 失败 | 系统动画未关闭（`adb.disable_animations: true`）；界面有持续动画/弹窗。dump 已支持 `/dev/tty` 快路径 + 文件回退 |

---

# 四、原理与实现（进阶）

> 这一部分讲"为什么这么写"，调策略/改代码时再看即可。

## 1. 登录与安全验证闭环

```
掉登录 -> 定时检测到未登录 -> 自动账密登录
  |- 成功（ok）        -> 静默（不打扰），按 login_check_interval 复查
  |- 已经在登录中/超时 -> 不算失败：继续等待；仍不确定 -> 状态 'timeout'
  |                      -> 按 login_retry_interval（默认 120s）自动重试
  |- 密码错误等（fail）-> QQ 通知"登录失败（账号或密码错误等）"
  |                      -> 按 login_retry_after_fail（默认 30 分钟）自动重试（不会永久放弃）
  |- 触发"登录安全验证"（risk）-> QQ 通知"请手动打开对应窗口完成验证"
  |                      -> 按 login_retry_after_risk（默认 15 分钟）重试；手动完成后 2 秒内自动感知
  |                      -> 桥接恢复 + QQ 通知"已重新登录"
```

判定细节（`xiaotiancai.py`）：

- **进度优先**：界面出现 `login_progress_markers`（登录中/正在登录/正在验证/请稍候…）时，
  `_detect_login_error()` 直接返回空 —— 这就是"登录中不再提示登录失败"的实现。
- **强标记**：`密码错误 / 账号或密码错误 / 账号不存在 / 手机号未注册 / 验证码错误 / 次数过多 / 账号异常 …`
  命中即判失败。
- **弱标记**（`错误/失败/不存在` 这类泛化词）：只有出现在**短提示节点**（<=30 字）且**不含网络类词**
  时才采纳，避免"网络连接失败，请重试"被误判成密码错误。
- **超时**：等待 `login_timeout`（默认 45s，出现进度文案可延长，硬上限 +30s）仍无法确证 ->
  返回 `timeout`（稍后重试），**不返回 fail**。
- 状态机与节流在 `bridge._do_login_job()` / `bridge._login_check_loop()`：
  用 `_login_not_before` + `_login_inflight` 保证"该重试就重试、该等用户就等用户、不重复触发"。
- 登录表单输入按行精确校验；密码框是掩码（••••）时按长度校验，**不会把密码输好几遍**。

## 2. 界面自愈与弹窗处理策略

**弹窗处理**（`Xiaotiancai._dismiss_blockers()`，按优先级）：
系统权限 -> 应用无响应/崩溃（点"等待"，**不杀 App**）-> 通话面板 -> 隐私协议 ->
更新/评价/公告/活动（点"以后再说"这类跳过，**绝不点"立即更新"**）-> 网络异常（带 30s 冷却地点"重试"，否则关掉）
-> 小天才警告弹窗 -> 关闭类控件（`iv_close` 等 id）-> 通用对话框文本按钮 -> 弹窗窗口 BACK 兜底。

安全护栏：只有在"像弹窗"时才动手（独立 Dialog/PopupWindow 窗口、对话框标题控件、
或同屏出现多个对话框按钮文本），正常聊天/列表页**不会**被误点或误按返回键——
`tools/test_reported_bugs.py::test_popup_handling` 覆盖了这些场景。

**界面自愈**（`bridge._poll_loop()` + `Xiaotiancai.recover()`）：
连续若干轮读不到任何消息时，按需执行"清弹窗 -> 不在前台才启动 -> 未登录就等登录 ->
不在聊天页才导航回去"，并把结果写进日志。正常时不会打扰界面（重开聊天页有 30s 冷却，
异常时才收紧到 5s）。

**不重复操作**：`launch()` 先判断前台；`/小天才 初始化` 每一步都先判断当前状态，
只做缺的那一步（这就是"小天才明显启动了却还会再启动一次"的修复）。

## 3. 消息读取策略（真机实测）

- 识别依据：消息气泡 `chat_msg_item_content` 的 content-desc 标注发送方——
  `'王五发的消息,内容'`（手表发，收）vs `'你发的消息,内容'`（自己发，跳过）；
  表情/语音消息 text 为空时从 desc 提取类型（如"表情"/"语音"）。
- 聊天列表模式：取最顶部（最新）聊天行的消息预览（`tv_chat_dialog_last_msg_content`）。
- 只在聊天窗口内读取：列表预览无法可靠判断发送方（家长侧手动发送的消息也会出现在预览里），
  会被误当成对方消息转发。
- 界面更新后优先调整 `config.yaml -> xiaotiancai.ui`，不要改代码。

## 4. 文本注入方案（已在真机实测，每一步都校验结果）

注入的**判定标准是"输入框里真的出现了这段文本"**，而不是"广播命令执行成功"——
这正是不再出现"只能输入宿主剪贴板内容"的原因。

| 顺序 | 方案 | 说明 |
|---|---|---|
| 1 | **ADBKeyBoard `ADB_INPUT_TEXT`** | 明文广播，v2.5-dev 与旧版都兼容；设为默认输入法后注入任意文本（含中文） |
| 2 | **ADBKeyBoard `ADB_INPUT_B64`** | base64 广播；Oreo/P 之后 `am` 不再接受 UTF-8 明文参数时兜底 |
| 3 | **ADBKeyBoard `ADB_INPUT_CHARS`** | Unicode 码点数组，按 200 个码点分批 |
| 4 | **剪贴板 + 粘贴** | `cmd clipboard set-text` -> **回读校验一致** -> `KEYCODE_PASTE`。回读不一致（剪贴板与宿主共享的环境常见）就**直接放弃**，绝不粘贴宿主剪贴板里的旧内容 |
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

## 5. 发送结果确认（为什么不再"假成功"）

`Xiaotiancai.send_message()` 的判定顺序：

1. 发送前记录基线：界面上原有的"发送失败"类提示（`send_fail_markers`）与已有己方气泡计数；
2. 注入文本 -> 点发送按钮（找不到按钮就回车）；
3. 最多 3 次快速 dump 复核：
   - 出现**新的**失败提示 -> 失败（旧提示不算，避免历史残留误报）；
   - 出现**新的**、包含该文本的己方消息气泡 -> 成功（最强证据）；
   - 输入框里**仍留有**这段文本 -> 失败；
   - 输入框已清空且无新失败提示 -> 成功；
   - 界面**读不到**（dump 失败）-> 失败并如实上报（旧实现这里返回"成功"，就是假成功的来源）。

重试策略：只有"输入框仍留有内容"（点击发送没生效）才安全重发，最多 `ui.send_retries` 轮；
"读不到界面"绝不重发，避免重复消息。

速度优化：UI dump 优先 `uiautomator dump /dev/tty`（一次 shell 调用拿到 XML，失败再回退
"写文件->cat->删文件"）；交互路径用 `retries=2, delay=0.3` 的快速 dump；
前台组件与登录态都带短缓存；等待时间由 `ui.interaction_delay` 控制。

## 6. 日志与可观测性

- 级别 `logging.level`（默认 INFO）下，控制台会打印：
  - `[QQ回调] 收到 …` / `[QQ回调] 放行|未转发 …`（`qq_webhook.py`，**每次回调都留痕**）
  - `[收到QQ命令] 来源=… 内容=… （队列中 N 条待处理）`（`bridge.forward_to_xiaotiancai`）
  - `[QQ->小天才] 开始发送 …` / `发送成功|发送失败`
  - `[收到小天才命令] 来源=手表|家长侧 …`（重复命令会以 debug 记录）
  - `[收到小天才消息] 来源=… 时间=… 内容=…`
  - 登录相关：`检测到小天才未登录，触发自动登录…`、`登录进行中…`、`登录未在时限内完成…`
- **编码兜底**：中文 Windows 控制台是 GBK，日志里若出现 GBK 无法表示的字符（emoji、日文标点、
  特殊符号等），`StreamHandler` 会抛 `UnicodeEncodeError` 并**丢弃整条日志**——`utils/logger.py`
  已把标准输出设为 `errors="replace"`，不会再有"看不到命令日志"的问题。
  （项目本身也已清理掉所有 emoji，日志/提示一律纯文本。）

## 7. AstrBot 插件原理

- 小天才->QQ：Python 轮询 -> 格式化 -> POST `http://127.0.0.1:11452/api/forward` -> 插件发 QQ。
  `/api/forward` 会等实际发送结果再返回（避免"假成功"）。
- QQ->小天才：插件收到 `/小天才` / `/小天才登录` -> POST 到 Python 侧 `qq_webhook`（5000 端口）-> ADB 操作；
  长任务（登录/初始化/历史消息）通过 `request_id` 回传结果，插件在原会话**引用+@ 回复**发送人。
- 命令模式关闭时，群/私聊所有新消息都会转发到小天才（`command_mode`）。

## 8. 已知限制

- 桥接本体跨平台（Windows / Linux / macOS），但**宿主集成类能力有平台差异**：
  WSA/WSABuilds 与宿主剪贴板共享、可被自动拉起，这些只存在于 Windows；
  Linux/macOS 没有等价物（Waydroid 与宿主不共享剪贴板，因此中文输入依赖 ADBKeyBoard）。
  **WSA 的网络守护工具同理只在 Windows 有意义**（Linux/macOS 用 systemd timer / cron 调 `--once`）。
- 需要小天才家长账号；小天才 App 必须由使用者自行安装（仓库不含该 APK）。
- 手表发**语音消息**无法转文字，转发为"语音"占位通知。
- Python 3.14 下 pyyaml 若无轮子，配置可写为 JSON 格式（loader 自动降级）；不装 pyyaml 时
  `tools/wsa_net_guard.py` 仍可工作（只解析 `wsa_guard` 段）。
- 轮询间隔默认 2s，去重 LRU 200 条/120s，回声过滤 60s（文件持久化，多实例/重启共享），
  防止重复转发与自我回传。
- 剪贴板与宿主共享的环境（如 WSA）里，桥接运行期间手动复制内容会干扰"剪贴板兜底"通道
  （ADBKeyBoard 通道不受影响）。
- 无桌面环境（纯 SSH/容器）运行图形模拟器需要额外处理显示与 `/dev/kvm`；
  Android Studio 的 headless 模拟器（`emulator -no-window`）可用，但需自行确认 adb 能连上。
- WSA 守护的第 3/4 级修复会重启子系统或 WSA：期间桥接短暂断连（约 30~90 秒）属预期。
