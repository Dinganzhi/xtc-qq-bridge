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
> **WSA/WSABuilds 经常断网（仅 Windows）**：另开一个终端跑 `python tools/wsa_net_guard.py`
> （独立守护工具，分级修复：重连 -> 网络复位 -> 重启子系统，见「WSA 网络守护」）。
> WSA 是 Windows 独有组件，所以这个守护**只在 Windows 上有意义**，也不会为其它平台提供产物。
> **不想装 Python**：可用 Nuitka 编成机器码单文件（Windows / Linux，x86_64 / arm64）——
> `build.bat` 或 `bash build.sh` 一条命令出产物，详见「五、编译成单文件可执行程序」。

```
小天才 App (Android 环境)  --ADB-->  Python 桥脚本  --HTTP-->  AstrBot 插件  -->  QQ
        ^                                                                        |
        +---------------- 插件转发 QQ 命令（/小天才 等）-------------------------+
```

**阅读顺序建议**：本文档**配置在前、原理在后**——第一次部署只看第一、二、三部分即可；
想改代码/调策略看第四部分「原理与实现」；要编译单文件分发看第五部分。

> **分支与发布约定**
> - `dev`：日常开发全部推这里（推送后 CI 自动跑离线测试）。
> - `main`：只保留**确认发布**的正式版。确认某个版本要发布时，才把 `dev` 整体合并进 `main`。
> - 合并到 `main` 也**不会**自动产出 Release：要发布必须去 Actions 页面手动
>   `workflow_dispatch`，并显式把 `publish` 勾成真。

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
| **WSA 断网是环境问题（仅 Windows）** | WSA / WSABuilds 长时间运行后子系统网络栈会失效。桥接自身会重连 ADB，但**网络栈死了要靠独立守护工具救**：见「WSA 网络守护」。WSA 是 Windows 独有组件，别的平台没有这个问题 |

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
- **WSA / WSABuilds 隔三差五断网（仅 Windows）**：用 `python tools/wsa_net_guard.py` 常驻守护（见「WSA 网络守护」）。
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
| **Windows** | 双击 `install.bat` | (1) 装 pyyaml (2) 复制插件到 `%USERPROFILE%\.astrbot\data\plugins\xtc_qq_bridge\` (3) 从模板生成 `config.yaml` (4) 生成插件初始配置 |
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
| 7. WSA 网络守护状态（仅 Windows） | `python tools/wsa_net_guard.py --status` |
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

python tools/selftest.py                # (1) 环境自检（adb 发现/连接/UI dump/启动/登录检测）
python main.py --check                  # (2) 或使用 --check
```

然后在 Android 环境里登录小天才家长账号（也可用 `/小天才登录` 自动登录），再：

```bash
python tools/dump_ui.py --filter 消息    # (3) 查看界面控件，按需调整 config.yaml -> xiaotiancai.ui
python main.py                           # (4) 启动桥接
```

## 5. 目录结构

```
project/
|-- main.py                  # 入口（--check / --debug dump-ui|adb-info / --once / --paths / --verify / --install-plugin）
|-- version.py               # 版本号唯一来源（编译脚本/CI 也读它）
|-- runtime_paths.py         # 运行路径解析：源码运行与 Nuitka 单文件都要用（配置/日志/数据放哪）
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
|   |-- ui_report.py         # 把当前界面控件导出成文本文件（排查控件 id / 中文不乱码）
|   |-- live_probe.py        # 实机一键体检：连接/界面/登录态/联系人/读取/注入（不发真实消息）
|   |-- selftest.py          # 环境自检（不依赖配置）
|   |-- wsa_net_guard.py     # WSA/WSABuilds 网络守护（独立程序，可单独常驻或注册计划任务；仅 Windows）
|   |-- wsa_guard.bat        # Windows 双击启动上面的守护（纯 ASCII）
|   |-- build_nuitka.py      # 编译驱动：一键编出单文件机器码（Windows/Linux/macOS 通用）
|   |-- build_in_docker.sh   # 在 Docker 里编译 Linux 版（免装本机工具链）
|   |-- test_wsa.py          # 离线单测：WSA 端口/解析等纯逻辑（无需设备）
|   |-- test_integration.py  # 离线集成：假 ADB 执行器跑连接/启动/注入链（无需设备）
|   |-- test_paths.py        # 离线测试：运行路径解析（源码/冻结、配置与日志落点）
|   `-- test_reported_bugs.py# 回归：来源/命令去重/登录判定/发送确认/弹窗/自动登录/日志（无需设备）
|-- .github/workflows/build-release.yml  # CI：矩阵编译 Windows/Linux/macOS x86_64+arm64
|-- astrbot_plugin_xtc_bridge/   # AstrBot 插件源码（安装见下文）
|-- keyboardservice-debug.apk    # 捆绑的 ADBKeyBoard APK（新机器免下载）
|-- install.bat / install.sh     # 一键安装（Windows / Linux·macOS）
|-- start.bat   / start.sh       # 启动器：环境自检 + 菜单（启动/自检/干跑/看界面/看日志/WSA 守护）
|-- build.bat   / build.sh       # 一键编译单文件可执行程序（Nuitka）
|-- pyproject.toml               # pip 安装元数据与命令入口（pip install . 后可直接用命令）
`-- requirements.txt / requirements-build.txt
```

> 路径写法：下面命令里的 `\` 是 Windows 写法，Linux / macOS 换成 `/`（如 `python3 tools/selftest.py`）。
> 改完输入/启动/连接/命令去重相关代码后建议跑一遍离线测试（都不需要设备）：
> - `python tools/test_wsa.py` —— 纯逻辑单测（端口识别、activity/前台解析、端口顺序）
> - `python tools/test_integration.py` —— 假 ADB 执行器跑通连接/启动/注入策略链
> - `python tools/test_reported_bugs.py` —— 回归测试（历史来源 / 命令去重 / 登录判定 / 发送确认 / 弹窗 / 自动登录 / 日志）
> - `python tools/test_paths.py` —— 运行路径解析（源码/冻结、配置与日志落点、捆绑 APK 识别）
> - `python tools/wsa_net_guard.py --test` —— WSA 守护的分级修复逻辑自测
>
> 想在**真机/模拟器**上快速体检一遍（不发真实消息）：`python tools/live_probe.py`
> —— 依次检查 adb 连接、界面读取、登录态、弹窗清理、聊天页判定、联系人匹配、读取消息、
> 文本注入（输入后自动清空、**不点发送**）。排查控件 id 可用 `python tools/ui_report.py`，
> 它会把当前界面的控件清单写到 `logs/ui_dump_report.txt`（避免控制台中文乱码）。
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
| `adb.dump_retries` / `adb.dump_delay` | UI dump 默认重试次数/间隔（默认 2 / 0.8s，逐轮递增等待界面空闲；交互路径自动用更快的参数） |
| `adb.dump_timeout` | 单次 `uiautomator dump` 超时（秒，默认 60） |
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
| `xiaotiancai.catchup_missed` | 是否补发漏掉的消息（默认 `true`）：从最新一条往回走，撞到库里已有的就停 |
| `xiaotiancai.catchup_max` | 单次最多补几条（默认 0 = 不限制；设了上限就分批补，下一轮继续） |
| `msg_log.cap` | 本地消息库最多保留多少条（默认 5000；`0` = 不限制） |
| `msg_log.shard_size` | 每多少条新建一个分库文件（默认 2000；`0` = 不分库，单文件 `msg_log.json`） |
| `msg_log.read_shards` | 启动时加载最新几个分库做判定（默认 1；判定"库里有没有"只需要最新那个） |
| `xiaotiancai.ui.message_tab_texts` | 找不到联系人时依次尝试切换的 Tab 文案（默认 微聊/消息/聊天） |
| `xiaotiancai.ui.contact_name_ids` | 消息列表里"联系人名"控件 id（末段）；按它匹配最可靠 |
| `xiaotiancai.ui.contact_preview_ids` | 消息列表行"预览"控件 id；用来判断当前页到底是不是列表 |
| `xiaotiancai.ui.chat_id_hints` | "这是聊天页"的 id 包含特征（判定在不在聊天页，防漏判） |
| `xiaotiancai.ui.activity_chat_exclude` | Activity 名含这些词就不算聊天页（防把列表页当聊天页） |
| `xiaotiancai.ui.interaction_delay` | 点击/输入之间的等待（秒，默认 0.6；设备慢可调大） |
| `xiaotiancai.ui.send_retries` | 发送确认失败时的重试轮数（默认 2；只在"输入框仍留有内容"时重发） |
| `xiaotiancai.ui.risk_markers` | 安全验证检测标记 |
| `xiaotiancai.ui.login_error_markers` | 登录失败判定标记（泛化词只在"短提示且不含网络字眼"时采纳） |
| `xiaotiancai.ui.login_progress_markers` | "登录中/正在验证/请稍候"等进度文案（**出现即不判失败**） |
| `xiaotiancai.ui.system_msg_prefixes` | 桥接系统提示前缀（发送成功/发送失败），读取时跳过不转发 |
| `webhook.allow_from` / `allow_groups` | 接收白名单（私聊/群聊） |
| `wsa_guard.*` | WSA 网络守护参数（只被 `tools/wsa_net_guard.py` 读取；仅 Windows） |
| `wsa_guard.ping_command` | 自定义 ICMP 探测命令（`{host}` 占位）；留空自动尝试多种 ping 写法 |
| `wsa_guard.tcp_targets` / `tcp_timeout` | TCP 探测目标（`host:port`）与超时；**WSA 上靠它判断断网**（ICMP 被屏蔽） |

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
  dump_delay: 0.8              # UI dump 重试间隔（秒，逐轮递增）
  dump_timeout: 60             # 单次 uiautomator dump 超时（秒）
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
    popup_skip_texts: ["以后再说", "不更新", ...]      # 弹窗点这些跳过（含更新/活动/自研弹窗）
    popup_block_texts: ["立即安装", "立即更新", ...]   # 危险按钮，自动关弹窗时绝不点
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

# ---------- WSA / WSABuilds 网络守护（独立工具，可选，仅 Windows） ----------
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

## 3. WSA / WSABuilds 网络守护（独立工具，仅 Windows）

WSA（含 WSABuilds / MagiskOnWSA）跑久了会"隔三差五断网"：宿主侧 adb 还在、`adb devices` 时有时无，
但 Android 子系统里的网络栈已经不通——表现就是界面读不到、消息发不出去、`uiautomator` 一直失败。
这**不是桥接程序的 bug**，靠桥接自己重连也救不回来，所以单独提供了一个守护程序
`tools/wsa_net_guard.py`：**和主程序完全分开**，各跑各的，互不依赖。

> **仅 Windows**：WSA（Windows Subsystem for Android）是 Windows 独有组件，Linux / macOS 上
> 既没有 WSA 也没有对应的宿主网络，这个守护在那里没有意义——编译脚本会自动跳过它
> （非 Windows 平台只编主程序），本项目的 Release 也只为 Windows 提供守护产物。

**检测 + 分级修复（由轻到重，成功后自动退回日常监测）**

> **为什么不用 ping 判断断网**：WSA 的 NAT **不转发 ICMP** —— 实测镜像里 `/system/bin/ping`
> 存在、IP 与默认网络都正常，但 ping 公共地址**永远 100% 丢包**。所以守护按这个顺序探测：
> ① `nc` 发起 TCP 连接（`wsa_guard.tcp_targets`，默认几个公共 DNS 的 53 端口）→ 成功即判定正常；
> ② 失败再看 `ping`（模拟器/Waydroid/真机有效）；③ 再看有没有非回环 IPv4；
> ④ 最后看 `dumpsys connectivity` 的默认网络状态。**只有"没有 IP"或"系统明确说没有默认网络"
> 才判定断网**，其余情况（典型就是 WSA 上 ping 不通）判为"无法判断"，只保活 adb、绝不误触发重启。
> 顺带一提：探测命令的退出码为 1 时也要保留输出（`ping` 丢包、`nc` 连不上都是 rc=1），
> 这也是早期版本"一直提示无法判断"的原因之一。

**根因提醒：国内网络下 `PARTIAL_CONNECTIVITY`（App 说没网，其实能上网）**

WSA 里 TCP/DNS 都通、但各种 App 仍然报"网络异常"，通常不是网真的断了，而是 Android
自己的**联网验证探针**失败：默认探针地址 `connectivitycheck.gstatic.com` 在国内连不上，
系统于是把网络标成 `PARTIAL_CONNECTIVITY`（有 `INTERNET` 但没有 `VALIDATED`），
App 一律当作没网。守护现在会：
① 在状态里显示"联网验证: validated / partial / unknown"；
② 发现"能上网但没验证通过"时**自动改写验证探针**（换成实测可达的
`connectivitycheck.platform.hicloud.com` / `connect.rom.miui.com` / `wifi.vivo.com.cn`
的 `generate_204`，并关掉 DoT），而不是去重启子系统——重启治不了这个病，还会打断桥接。

```bash
python tools/wsa_net_guard.py --fix-validation   # 立刻改写探针（幂等，写入 /data）
```

设置写在 `/data` 里，重启子系统后依然有效；但 Android 要等**下一轮验证**（或子系统重启）
才会把状态翻成 `VALIDATED`，所以改完当场看到 `partial` 属正常。

> **必须和主程序共用一个 adb**：守护默认读取 `config.yaml` 的 `adb.path`（也可用 `--adb` 指定）。
> 两个**不同版本**的 `adb.exe` 会互相杀掉对方在 5037 上的 server，表现就是设备一会儿在线一会儿掉线。

| 级别 | 触发条件 | 动作 |
|---|---|---|
| 0 | 一切正常 | 只记录状态（写入 `data/wsa_guard_state.json`，含**探测证据**文本） |
| 0.5 | 能上网但 `PARTIAL_CONNECTIVITY` | 改写联网验证探针 + 关 DoT（10 分钟内最多一次） |
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
**编译版**（单文件 exe）自带 `--install-plugin`，见「五、编译成单文件可执行程序」。

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
| **自动登录不生效** | (1) 看日志有没有 `检测到小天才未登录，触发自动登录`；(2) 若提示"没有配置账密"，补 `xiaotiancai.login.phone/password`；(3) 若提示超时/安全验证，程序会按 `login_retry_interval` / `login_retry_after_risk` 自动重试，不需要重启；(4) 确认 `xiaotiancai.auto_login: true`（QQ 发 `/小天才 自动登录` 可切换，日志会打印当前状态） |
| **明明在登录中却提示"登录失败"** | 已修复：出现"登录中/正在验证/请稍候"等进度文案时**一律不判失败**；只有明确的账号/密码错误才算失败，网络类临时问题按"超时->稍后重试"处理。若仍误报，把该文案加进 `xiaotiancai.ui.login_progress_markers` |
| **发送提示失败但其实发不出去 / 提示成功却没发出** | 已修复：只有"输入框已清空且无新的失败提示"或"出现新的己方气泡"才回「发送成功」；读不到界面、出现"发送失败/网络异常"、输入框仍有残留 -> 如实回「发送失败」。若你的机型提示语不同，补充 `xiaotiancai.ui.send_fail_markers` |
| **按按钮/发送很慢** | 已优化：UI dump 一次 shell 完成"落盘+读回+删文件"（交互路径 retries=2/delay=0.3）、发送时复用注入校验的快照、前台组件与登录态缓存、交互等待变短。设备本身慢可调大 `xiaotiancai.ui.interaction_delay`（0.6 -> 1.0）；网络差可减小 |
| **控制台看不到收到的命令** | INFO 级别下应当能看到 `[QQ回调] 收到 …`、`[收到QQ命令] …`、`[收到小天才命令] …`、`[收到小天才消息] …`、`[QQ->小天才] 发送成功/失败`。看不到时：(1) 确认 `logging.level: INFO`；(2) 确认命令真的到了（QQ 侧看插件日志，小天才侧看界面）；(3) 中文 Windows 控制台编码问题已兜底（不会再整条丢失） |
| **已经启动了 App 还重复启动** | 已修复：`launch()` 先判断前台，已在前台直接返回，不执行任何启动命令；`/小天才 初始化` 也改成按需执行 |
| **明明在聊天页，却提示"在消息列表找不到联系人"** | 已修复：聊天页判定不再只看 Activity 名（`endswith("chatactivity")`），改为"Activity 名含 chat 且不含 list/main/watchmsg"**或**界面出现消息气泡/输入栏；另外 `open_chat` 进来会先确认一次界面特征，已经在聊天页就直接返回，不再去列表里找联系人 |
| **在主页确实有该联系人，却一直说找不到** | 现在：① 联系人名匹配会忽略空格差异、支持"张三(爸爸)"这类别名（取括号前部分）；② 优先按列表行的联系人名控件（`ui.contact_name_ids`）匹配，并返回**可点击的整行**；③ 找不到时会依次切换 Tab（`ui.message_tab_texts`）、在确认是消息列表时向上滑动查找；④ 失败日志会写明**当前可见的联系人**，例如`找不到联系人 '张三'：消息列表；当前可见联系人: 李四、王五`，一眼看出是名字不一致还是页面不对 |
| **WSA 守护一直提示"无法判断子系统网络"** | 根因是 **WSA 的 NAT 不转发 ICMP**：ping 在 WSA 上永远 100% 丢包（镜像里其实有 ping），旧版把"ping 失败/输出被退出码吞掉"当成了无法判断。现在守护优先用 **`nc` TCP 连接**判断（`wsa_guard.tcp_targets`），再退回 ping / IP / `dumpsys connectivity`；**只有"没有 IP"或"系统明确说没有默认网络"才判定断网**，其余判为无法判断（只保活 adb，不误重启）。`--status` 会打印"探测证据"一行说明这次的判断依据 |
| **有时"已经登录了却提示未登录"** | 已修复三处：① 登录态**以界面为准**（聊天页/消息列表/微聊·我的 等主界面特征 → 已登录；密码框/验证码/登录页文案 → 未登录），Activity 名只作兜底，不再因为名字里带 `login`（如 `AccountVerifyLoginActivity`）就误判；② "App 是否在前台"改看 **Activity** 而不是窗口焦点 —— 输入法一弹出就抢走 `mCurrentFocus`，旧实现会因此误判"App 不在前台/未登录"；③ 新增**三态**登录判定：读不到界面 / App 不在前台 = `unknown`，此时既不打印"未登录"、也**不会触发自动登录**（旧实现会误触发，甚至去点登录页控件） |
| **日志反复出现 `null root node returned by UiTestAutomationBridge` 或 `mCurrentFocus=null`** | 说明 **WSA 窗口被最小化/关闭、或虚拟显示未点亮** —— 此时 Android 侧没有任何窗口获得焦点，`uiautomator` 必然失败（**与小天才 App 无关**，手动 `uiautomator dump` 同样会失败）。办法：让 WSA 窗口保持打开（可以挪到屏幕边上，但别最小化）。桥接检测到"没有焦点窗口"会**自动唤醒屏幕 + 重新拉起 App**，窗口恢复后自动继续；单条 dump 报错已压缩成一行可读信息，完整原因见 `--debug adb-info` 的 `last_dump_error` |
| **弹窗挡住界面导致读不到消息** | 已修复：常见弹窗（权限/无响应/更新/评价/活动/网络/警告）会自动处理；**自研自定义弹窗**（如"升级提醒" `com.xtc.widget.phone.popup.activity.CustomActivity14`）也按结构识别并自动关闭（点负向按钮/返回键，绝不点"立即安装"）；状态机会报 `弹窗遮挡界面`，轮询每轮都会清它。实在认不出的弹窗，把它的跳过按钮文案加进 `xiaotiancai.ui.popup_skip_texts` 即可 |
| **消息时间不对（时间总是"当前时间"）** | 已修复：WSA 上旧版默认用的 `--compressed` dump 会把时间标签节点（`tv_chat_msg_item_date`）整片裁掉（实机同一屏：压缩版 0 个标签，完整版 3 个 `11:19`/`19:18`/`20:46`），于是每条消息的时间都退化成"当前时间"。现在默认用**完整 dump**，并且万一只拿到压缩版也会从 `ll_chat_top_layout` 的 content-desc 还原时间（`十1点十9分` -> `11:19`） |
| **QQ 侧 `/小天才 历史消息` 没反应 / 提示"群不在白名单"** | 已修复：动作类回调（历史消息 / 登录 / 初始化 / 自动登录）在 `qq_webhook` 里曾被"空消息"分支**提前 return 掉**（插件转发带 `source=astrbot` 且 `message` 为空），于是这些命令永远不执行，日志还写成"未转发（空消息或来源不在白名单）"，看着像白名单没配好。现在动作先分派、日志分开写；白名单确实不含该群时会明确说"不在白名单（webhook.allow_from / allow_groups）"。插件侧也会在桥接未受理时直接回话（不再干等 150 秒超时） |
| Linux 真机看不到设备 | udev 规则/权限问题：配 `/etc/udev/rules.d/51-android.rules` 并把用户加入 `plugdev`（见「Linux」小节），再 `sudo udevadm control --reload-rules && sudo udevadm trigger` |
| Waydroid 连不上 | `waydroid session start`（X11 加 `-X`）后再 `adb connect 127.0.0.1:5555`；容器/无桌面环境需 `/dev/kvm` 与显示输出 |
| 连错设备（多个模拟器/真机） | 在 `adb.serial` 里写死要用的序列号（`python main.py --check` 会打印当前选中的是哪个） |
| 日志"启动小天才未确认" | 用 `python main.py --debug dump-ui` 看前台是不是 `com.xtc.watch`；`--debug adb-info` 看 `focus` 字段。App 未安装会直接报错 |
| 中文发不出去 / 发出去是旧内容 | `--debug adb-info` 看 `adbkeyboard_ready` 与 `ime`：必须 `com.android.adbkeyboard/.AdbIME`；`clipboard_ok=false` 时不要依赖剪贴板 |
| 输入框有残留导致内容拼接 | 已内置发送前清空；若仍出现，检查 `adb.input_retries` 与聊天页是否稳定 |
| **uiautomator dump 失败 / 反复出现 `cat: /sdcard/xtc_dump_*.xml: No such file or directory`** | 根因是 `uiautomator` 没写出文件（最常见是 `ERROR: could not get idle state.`：界面一直不空闲，如转场/加载动画、弹窗、键盘光标闪烁；其次是 `/sdcard` 未挂载或无写权限）。现已：① 优先"完整 dump 落盘并一次 shell 读回"（要完整版才拿得到消息时间标签），失败再回退 `/dev/tty` 与 `--compressed`；② 落盘自动换 `/sdcard` -> `/data/local/tmp` -> `/storage/emulated/0` 三个目录；③ 读取改用 `exec-out cat`；④ 重试逐轮递增等待，并自动重设动画缩放；⑤ **报错里带 uiautomator 的真实原因 + 当前前台组件**（不再只报 cat）。仍出现时：调大 `adb.dump_retries` / `adb.dump_delay`，确认 `adb.disable_animations: true`，用 `/小天才 初始化` 清理界面，或看 `python main.py --debug adb-info` 里的 `last_dump_error` |

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
- 登录表单输入按行精确校验；密码框显示为掩码（圆点）时按长度校验，**不会把密码输好几遍**。

## 2. 界面自愈与弹窗处理策略

**弹窗处理**（`Xiaotiancai._dismiss_blockers()`，按优先级）：
系统权限 -> 应用无响应/崩溃（点"等待"，**不杀 App**）-> 通话面板 -> 隐私协议 ->
更新/评价/公告/活动（点"以后再说"/"不更新"这类跳过，**绝不点"立即安装/立即更新"**）-> 网络异常（带 30s 冷却地点"重试"，否则关掉）
-> 小天才警告弹窗 -> 关闭类控件（`iv_close` 等 id）-> 通用对话框文本按钮 ->
**通用弹窗兜底**（认结构不认文案，见下）-> 弹窗窗口 BACK 兜底。

**通用弹窗兜底**（"升级提醒"这类自定义 Activity 弹窗，实机：
`com.xtc.watch/com.xtc.widget.phone.popup.activity.CustomActivity14`，
按钮 `btn_left="不更新"` / `btn_right="立即安装"`）：

- 它既不是 PopupWindow 也不是 Dialog，光看文案认不出来，以前**永远不会被自动关掉**，
  弹窗盖住界面后消息一直读不到（用户报的"读不到消息 / 老提示找不到联系人"）。
- 现在按**结构**判定是否弹窗：Activity 名含 `popup/dialog/alert`、或同时存在
  `btn_left`+`btn_right`、或"弹窗容器 + 对话按钮/标题+说明"。判定刻意保守，
  正常聊天页不会被误判。
- 关闭顺序：**先点负向按钮**（`btn_left`/`btn_cancel`… 或 `popup_skip_texts` 里的文案），
  找不到就**按返回键**；`popup_block_texts` 里的危险按钮（立即安装/立即更新…）**绝不点**。
- 同一个弹窗最多试 2 轮（点一次 + 返回一次），仍关不掉就停止动作并每 10 分钟提醒一次，
  提示你去 `popup_skip_texts` 补上它的按钮文案——不会反复点、也不会刷屏。
- 状态机会把它判成 `popup`（日志："小天才状态: 弹窗遮挡界面"），轮询层**不设冷却**地清它。

安全护栏：只有在"像弹窗"时才动手（独立 Dialog/PopupWindow 窗口、弹窗 Activity 名、
双按钮结构、对话框标题控件），正常聊天/列表页**不会**被误点或误按返回键——
`tools/test_reported_bugs.py::test_popup_handling` / `test_custom_popup_auto_close` 覆盖了这些场景。
其他弹窗（系统权限、无响应等）同样通用；若是没见过的自研弹窗，加
`xiaotiancai.ui.popup_skip_texts` 一个文案即可，不需要改代码。

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
- **每条消息的时间**（`_time_labels()` + `_label_for_bubble()`）：小天才只在一个"时间组"的
  第一条消息上方画一个时间标签，所以标签**只归它下面紧挨着的那条消息**，组内其它消息
  用自己的标签；没有标签时用"当前时间"（对刚收到的消息就是它的到达时间，各条不同）。
  标签节点首选 `tv_chat_msg_item_date`（形如 `11:19` / `昨天 23:42`）；
  压缩 dump 里没有它，就从 `ll_chat_top_layout` 的 content-desc 还原（`十1点十9分` -> `11:19`）。
- **漏消息补发（撞库即停）**：每轮读完最新一条后，会从最新往回逐条检查——
  和消息库里已有的那条一样就**停**；不一样就转发，然后继续往上看，直到撞上库里已有的一条。
  这样弹窗挡住界面、界面一时读不到、以及桥接启动前积压在聊天里的消息都会按时间顺序补齐，
  不会像"只取最新一条"那样把中间几条永久漏掉。
  消息库见 `config.yaml -> msg_log`（条数上限 / 分库 / 只读最新分库）。

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

速度优化：UI dump 一次 shell 调用完成"落盘 -> `cat` 读回 -> 删文件"（失败再回退 `/dev/tty`
或换目录）；交互路径用 `retries=2, delay=0.3` 的快速 dump；发送时"注入校验"的界面快照
会复用给"找发送按钮"（省一次 dump）；前台组件与登录态都带短缓存；等待时间由
`ui.interaction_delay` 控制。

**UI dump 的三种方案与可读报错**（`adb_controller._dump_strategies` / `_dump_ui_locked`）：

| 方案 | 动作 | 作用 |
|---|---|---|
| `file-full`（默认首选） | 一次 shell 完成"完整 dump 落盘 -> `cat` 读回 -> 删文件" | 节点最全：**消息时间标签 `tv_chat_msg_item_date` 只有完整 dump 里才有** |
| `tty-full` | `uiautomator dump /dev/tty` | 不落盘，一次 shell 拿到 XML（绕开 `/sdcard` 写不进去） |
| `file-compressed` / `tty-compressed` | 加 `--compressed` 的兜底 | 某些镜像上完整 dump 会被系统 Killed；失败后自动落到这里并**记住**能用的那个 |
| `file` | 落盘 `/sdcard` -> `/data/local/tmp` -> `/storage/emulated/0` | `/sdcard` 未挂载/无权限时自动换目录；读文件用 `exec-out cat` |

**为什么默认不再用 `--compressed`**（实机 WSA 实测的坑）：`--compressed` 会把时间标签节点
整片裁掉——同一屏压缩版 **19 个节点 / 0 个时间标签**，完整版 **46 个节点 / 3 个时间标签**
（`11:19` / `19:18` / `20:46`）。时间标签一丢，转发出去的消息时间就退化成"当前时间"
（用户报的"消息时间还是不对"）。完整版实测成功率 5/5、平均只慢约 0.4s（3.5s vs 3.1s）。
万一某个镜像只给压缩版，`Xiaotiancai._time_labels()` 会退化成从时间标签父容器
`ll_chat_top_layout` 的 content-desc 还原时间（`十1点十9分` -> `11:19`），
所以再怎么退化也不会把时间丢成"当前时间"。

失败时抛出的错误形如：
`UI dump 失败: file-full: ERROR: could not get idle state.；tty-full: 没有 XML 输出 | ...（界面一直不空闲：...可调大 adb.dump_retries / adb.dump_delay，或用 /小天才 初始化 清理界面）｜当前前台=com.xtc.watch/.ChatActivity`
——即**真实原因 + 当前前台 + 怎么处理**，而上层（打开聊天/读消息/发送）遇到读不到界面时
只记一条节流后的 warning 并稍后重试，不再整轮报 ERROR、也不再影响后续轮询。

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
- **WSA 窗口被最小化/关闭时 Android 侧没有窗口焦点**（`mCurrentFocus=null`），此时任何
  界面读取都会失败（`uiautomator` 报 `null root node`，手动 dump 同样失败）——这不是桥接的问题。
  请让 WSA 窗口保持打开；桥接检测到"没有焦点窗口"会自动唤醒屏幕并重新拉起 App，日志也会给出同样的提示。
- 单条 UI dump 报错已压缩成一行（原因归类 + 处置建议 + 当前前台），完整原因在
  `python main.py --debug adb-info` 的 `last_dump_error` 字段里，排障时优先看它。

---

---

# 五、编译成单文件可执行程序（Nuitka）

把整个项目编译成**机器码单文件**：目标机器**不需要装 Python**，双击/直接运行即可。
用 [Nuitka](https://nuitka.net/) 编译（真编译成 C 再编成原生可执行文件）。

产物（主程序所有平台都有；守护只在 Windows 上编）：

| 产物 | 入口 | 说明 |
|---|---|---|
| `xtc-qq-bridge-<版本>-<系统>-<架构>[.exe]` | `main.py` | 桥接主程序（Windows / Linux / macOS） |
| `xtc-wsa-guard-<版本>-windows-<架构>.exe` | `tools/wsa_net_guard.py` | WSA 网络守护（**仅 Windows**） |

命名规范示例：`xtc-qq-bridge-1.0.0-windows-x86_64.exe`、`xtc-wsa-guard-1.0.0-windows-x86_64.exe`
（版本号不带 `v` 前缀，与 tag 一致，例如 tag `1.0.0-alpha.1`）。

## 1. 本地一键编译

```bash
# Windows：双击 build.bat，或
build.bat --mode onefile                 # 单文件（默认，两个目标都编）
build.bat --mode standalone              # 目录模式（启动更快，便于调试）
build.bat --target bridge                # 只编主程序
build.bat --check-env                    # 只检查编译环境
build.bat --dry-run                      # 只打印将要执行的 nuitka 命令行

# Linux / macOS
bash build.sh                            # 同上，参数一致
bash build.sh --mode standalone

# 任何平台都可以直接调驱动脚本
python tools/build_nuitka.py             # onefile + bridge + guard -> dist/
python tools/build_nuitka.py --out dist --jobs 8 --lto yes
```

编译产物在 `dist/`，同时生成 `.sha256` 校验文件。**先用 `--standalone` 验证、再切 `--onefile`**
（目录模式不用每次解包，启动快、排错容易）；本项目的 `onefile` 已实测可跑。

## 2. 编译前置条件

| 平台 | 需要什么 |
|---|---|
| 通用 | Python 3.10+（本项目在 3.14 上实测）、`python -m pip install -U -r requirements-build.txt`（Nuitka ≥4.1 + pyyaml；**Python 3.14 必须 Nuitka 4.1+**） |
| Windows | **Visual Studio 2022+**，安装时勾选"使用 C++ 的桌面开发"。**MinGW64 不支持 Python 3.13+** |
| Linux | `gcc` / `g++` 与 `python3-dev`（Debian/Ubuntu：`sudo apt install gcc g++ python3-dev`） |
| macOS | `xcode-select --install`（clang） |

`pyyaml` 必须装：配置是 YAML，编译时用 `--include-package=yaml` 打进产物，否则运行时会退化成只认 JSON。
`tools/build_nuitka.py --check-env` 会把 Python 版本、pyyaml、Nuitka 版本、C 编译器一次性列出来，
缺什么直接给解决办法；编译器缺失时不会白等几十分钟。

## 3. 编译产物怎么用（和源码版完全不同，先看这段）

单文件运行时会把自身解包到临时目录，所以**"程序在哪"和"数据写哪"是两件事**，本项目已按此设计
（`runtime_paths.py`）：

```
把 exe 放到任意目录（例如 D:\xtc\），第一次运行：

D:\xtc\
|-- xtc-qq-bridge-1.0.0-windows-x86_64.exe   # 你的程序
|-- config.yaml        # 首次运行自动从模板生成（编辑它：QQ 号/联系人/账密/token）
|-- logs\bridge.log    # 日志（写在 exe 旁边，不会随临时目录被删）
`-- data\             # 消息库/去重状态（重启不丢）
```

```bash
xtc-qq-bridge.exe --verify           # 校验产物完整性：依赖/捆绑资源/配置解析（不连设备）
xtc-qq-bridge.exe --paths            # 打印 配置在哪、日志在哪、数据目录在哪
xtc-qq-bridge.exe --check            # 环境自检（adb/连接/输入法/剪贴板）
xtc-qq-bridge.exe --install-plugin   # 把内置的 AstrBot 插件装到 ~/.astrbot/data/plugins/
xtc-qq-bridge.exe --debug dump-ui    # 打印当前界面控件
xtc-qq-bridge.exe                    # 启动桥接（Ctrl+C 退出）

xtc-wsa-guard.exe --status           # WSA 守护：看状态
xtc-wsa-guard.exe                    # WSA 守护：常驻
xtc-wsa-guard.exe --test             # 守护自身逻辑自测（不需要设备）
```

- 配置：把 `config.yaml` 放在 exe 旁边即可（也可 `--config D:\path\config.yaml`）。
  模板、ADBKeyBoard 的 APK、AstrBot 插件源码都已经打进 exe，**新机器不需要再下载任何东西**。
- exe 所在目录不可写时（例如放进 `C:\Program Files`），数据会自动落到
  `%LOCALAPPDATA%\xtc-qq-bridge`（Linux：`~/.local/share/xtc-qq-bridge`），`--paths` 里会写明。
- 升级：直接换新 exe 即可，`config.yaml` / `logs/` / `data/` 不受影响。

## 4. 跨平台与多架构：不能交叉编译

**Nuitka 不支持交叉编译**（要目标平台的原生 C 编译器与系统库）。所以：

| 想要的产物 | 必须在哪编 |
|---|---|
| Windows x86_64 / arm64 | Windows（x64 用普通机器；arm64 用 Arm 版 Windows 机器/CI runner） |
| Linux x86_64 / arm64 | Linux（arm64 用 Arm 机器，或 QEMU 模拟，慢） |
| macOS x86_64 / arm64 | macOS |

三条可行路线：

**(a) GitHub Actions（推荐，本项目已配好）**：推一个 tag 会为 6 个平台矩阵编译并把产物传到 Actions 附件：

```bash
git tag v1.1.0 && git push origin v1.1.0      # 触发 .github/workflows/build-release.yml
```

工作流做的事：跑离线测试 -> 矩阵编译（Windows/Linux/macOS × x86_64/arm64）-> 每个产物跑
`--version` 和 `--verify` 冒烟测试 -> 重命名带平台后缀 -> 上传 Actions 产物。

发布（创建 GitHub Release）**不会**自动发生：只有到 Actions 页面手动 `workflow_dispatch`，
并显式勾选 `publish` 为真，才会创建/更新 Release。这样打 tag 永远不会变成正式发布。

矩阵里的 runner 与注意点：

| runner | 产物 |
|---|---|
| `windows-latest` | `windows-x86_64`（自带 Visual Studio 2022） |
| `ubuntu-22.04` | `linux-x86_64`（glibc 2.35，兼容面较广） |
| `ubuntu-24.04-arm` | `linux-arm64`（GitHub 原生 Arm runner；**私有仓库需 Arm 配额**，没有就删掉这一项） |
| `windows-11-arm` | `windows-arm64`（公开预览 runner，不可用时删掉该矩阵项） |
| `macos-14` / `macos-13` | `macos-arm64` / `macos-x86_64`（不需要可删） |

**(b) Docker（本地编 Linux 版，宿主机不用装工具链）**：

```bash
bash tools/build_in_docker.sh                  # python:3.12-slim + 临时装 gcc
IMAGE=androsh7/nuitka-compiler:latest bash tools/build_in_docker.sh   # glibc 2.17 基线，兼容老系统
PLATFORM=linux/arm64 bash tools/build_in_docker.sh                    # arm64（需 QEMU/binfmt）
```

**(c) 各自平台上直接编**：在 Windows 机器上跑 `build.bat`，在 Linux 机器上跑 `bash build.sh`。

## 5. 编译常见问题

| 现象 | 原因 / 解决 |
|---|---|
| `The '--output-dir' option requires an argument with '--output-dir='` | Nuitka 4.2 要求 `--opt=value` 写法（驱动脚本已统一用等号，老版本会自动退回 `--onefile`） |
| `failed to create cache directory` | Nuitka 缓存目录不可写（受限环境）：驱动脚本会自动改用项目内 `.nuitka-cache`，也可手动设 `NUITKA_CACHE_DIR` |
| 构建崩在 `AssertionError: ...\dist\main.build\module.__main__.c` | 上次编译被打断（Ctrl+C / 崩溃）或两个编译并行，残留的中间目录会让 Nuitka 的断言失败。驱动脚本现在会在编译前**自动清掉** `<目标>.build` / `.onefile-build` / `.dist`，失败时也会清掉半成品；如果你是自己手敲 nuitka 命令，删掉 `dist/main.build` 再编即可 |
| `[中止] 另一个编译正在进行（pid=...）` | 同一个输出目录已经有编译在跑（并行会互相踩，表现就是上面那条断言）。等它结束再编；确认那个进程已经死了，删掉 `dist/.build.lock` 重试（pid 已消失的残留锁会自动接管，不会永久卡住） |
| Windows 报找不到 C 编译器 | 装 Visual Studio 2022+ 的"使用 C++ 的桌面开发"；**Python 3.13/3.14 不能用 MinGW64** |
| 编译过了但运行报 `No module named yaml` | 编译时没装 pyyaml 或漏了 `--include-package=yaml`（驱动已默认加，`--check-env` 会提前报错） |
| 运行 `--verify` 报"捆绑资源缺失" | 编译时漏了 `--include-data-*`（驱动已按清单打包；插件是**逐文件**打进包的，因为 `--include-data-dir` 会剔除 `.py`） |
| onefile 启动慢（1~2 秒） | 单文件每次要解包到临时目录。追求启动速度改用 `--mode standalone`（目录分发） |
| 杀毒软件报毒 | Nuitka 单文件被启发式误报属常见现象：提交厂商白名单，或改用 `standalone` 目录模式 |
| Linux 产物在老发行版跑不起来 | glibc 版本过高：在更老的系统/`ubuntu-22.04`/Nuitka 编译镜像里构建 |
| 产物体积偏大（本机实测 8MB 左右） | 属正常（含 Python 运行时 + pyyaml）；`--lto=yes` 可再优化一点，代价是构建更慢 |

## 6. 也可以 pip 安装（不编译）

```bash
pip install .            # 或 pip install xtc-qq-bridge
xtc-qq-bridge --check    # 与编译产物同名的命令
xtc-wsa-guard --status
```

`pyproject.toml` 里声明了 `xtc-qq-bridge` / `xtc-wsa-guard` 两个入口点，源码安装与编译产物用法一致。
