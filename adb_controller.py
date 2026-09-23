# -*- coding: utf-8 -*-
"""ADB 控制层：封装 adb（subprocess），提供连接/点击/滑动/中文输入/截图/
UI 树解析等能力。

支持环境（Windows / Linux / macOS 均可）：
- Windows：雷电（LDPlayer）、MuMu 模拟器、WSA / WSABuilds（MagiskOnWSA）
- Linux：Waydroid、Genymotion、Android-x86 / 夜神 / 逍遥等提供 adb 端口的模拟器
- macOS：Genymotion、Android Studio 模拟器、真机
- 任何能通过 `adb connect` 接入的 Android 实例（含真机 USB / 网络调试）

设计要点：
- adb 查找顺序：config 指定路径 -> 环境变量 ADB_PATH -> 平台常见安装路径 -> PATH。
- 设备查找顺序：**已有的在线设备** -> 环境变量 ADB_SERIAL -> WSA 端口（Windows）
  -> 常见端口（5555 / 16384 / 62001 …）-> 多次重试。任何情况下都不会顶掉已连接的
  设备去抢端口。
- 启动 App：解析 launcher activity（cmd/pm resolve-activity）-> `am start -n`
  -> `monkey -p <pkg>`（WSA 上最稳）-> `cmd package` 兜底，并轮询前台确认。
- 前台判定：兼容 Android 13+ 的 `topResumedActivity`/`mFocusedApp`，不再只认
  `mCurrentFocus`（WSA/新镜像上 mCurrentFocus 经常为空，会把"启动成功"误判成失败）。
- 中文输入策略链（每一步都做**结果校验**，失败才降级）：
    1) ADBKeyBoard IME 广播 ADB_INPUT_TEXT（明文，兼容 v2.5-dev 与旧版）
    2) ADB_INPUT_B64（base64，Oreo/P 之后 am 不接受 UTF-8 明文时用）
    3) ADB_INPUT_CHARS（Unicode 码点数组）
    4) 剪贴板 set-text + KEYCODE_PASTE（Android 10+；剪贴板与宿主共享的环境里
       必须校验内容是否真的写进去了，否则会粘出宿主剪贴板里的旧内容）
    5) input text（仅 ASCII 兜底）
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

try:  # 仅 Windows 有；用于读取 WSA 的 ADB 端口
    import winreg  # type: ignore
except Exception:  # noqa: BLE001 非 Windows / 受限环境
    winreg = None  # type: ignore


class AdbError(RuntimeError):
    pass


# ---------------------------------------------------------------- 常见安装路径
# 常见 LDPlayer 安装目录（Windows 模拟器，内含 adb.exe）
_LDPLAYER_DIRS = [
    "D:\\leidian\\LDPlayer14", "D:\\leidian\\LDPlayer9", "D:\\leidian\\LDPlayer",
    "C:\\leidian\\LDPlayer14", "C:\\leidian\\LDPlayer9", "C:\\leidian\\LDPlayer",
    "D:\\LDPlayer\\LDPlayer14", "D:\\LDPlayer\\LDPlayer9",
    "C:\\LDPlayer\\LDPlayer14", "C:\\LDPlayer\\LDPlayer9",
]
# MuMu 模拟器 adb 位置（MuMu 15: nx_main\adb.exe；MuMu 12: shell\adb.exe；MuMu 6: vmonitor\bin）
_MUMU_ADB_CANDIDATES = [
    "D:\\Program Files\\Netease\\MuMu\\nx_main\\adb.exe",
    "D:\\Program Files\\Netease\\MuMu\\emulator\\nemu\\vmonitor\\bin\\adb_server.exe",
    "D:\\Program Files\\Netease\\MuMuPlayer-12.0\\shell\\adb.exe",
    "D:\\Program Files\\Netease\\MuMuPlayer-12.0\\vmonitor\\bin\\adb_server.exe",
    "C:\\Program Files\\Netease\\MuMu\\nx_main\\adb.exe",
    "C:\\Program Files\\Netease\\MuMuPlayer-12.0\\shell\\adb.exe",
]
# Windows 上常见的 platform-tools / WSA 自带 adb 位置
_WIN_ADB_CANDIDATES = [
    "%LOCALAPPDATA%\\Microsoft\\WindowsApps\\adb.exe",   # winget 安装的 platform-tools
    "%LOCALAPPDATA%\\Android\\Sdk\\platform-tools\\adb.exe",
    "%USERPROFILE%\\AppData\\Local\\Android\\Sdk\\platform-tools\\adb.exe",
    "%USERPROFILE%\\platform-tools\\adb.exe",
    "%USERPROFILE%\\scoop\\shims\\adb.exe",
    "%USERPROFILE%\\scoop\\apps\\adb\\current\\adb.exe",
    "C:\\platform-tools\\adb.exe",
    "C:\\adb\\adb.exe",
    "C:\\ProgramData\\chocolatey\\bin\\adb.exe",
]
# Linux / macOS 上常见的 adb 位置（~ 与 $HOME 会被展开）
_UNIX_ADB_CANDIDATES = [
    "~/Android/Sdk/platform-tools/adb",
    "~/android-sdk/platform-tools/adb",
    "~/Library/Android/sdk/platform-tools/adb",          # macOS
    "/usr/lib/android-sdk/platform-tools/adb",           # Debian/Ubuntu 包
    "/usr/local/lib/android/sdk/platform-tools/adb",
    "/opt/android-sdk/platform-tools/adb",
    "/opt/android/platform-tools/adb",
    "/usr/bin/adb",
    "/usr/local/bin/adb",
    "/snap/bin/adb",
]

# WSA 相关（Windows 专有；WSABuilds / MagiskOnWSA 同样适用）
WSA_PACKAGE_DEFAULT = "MicrosoftCorporationII.WindowsSubsystemForAndroid_8wekyb3d8bbwe"
WSA_ADB_DEFAULT_PORT = 58526
# WSA 的 ADB 端口是随机分配的；依次从注册表 / 常见端口探测
_WSA_REG_PATHS = [
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsSubsystemForAndroid",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\WSA",
]
_WSA_COMMON_PORTS = [58526, 58527, 58525, 6520, 6521]
# 模拟器/容器常见的 adb 端口（放在 WSA 之后，避免抢不到 WSA 时误连别的环境）
#   5555   = 通用 TCP/IP 调试（Genymotion、Android-x86、真机网络调试、雷电/MuMu）
#   16384  = MuMu；7555 = 部分雷电实例；21503 = 逍遥；62001/62025 = 夜神
#   5556   = Waydroid 多实例；6520 = WSA 旧端口
_EMULATOR_COMMON_PORTS = [5555, 16384, 7555, 21503, 62001, 62025, 5556]

# uiautomator dump 的落盘目录候选（按可写性排序）。
#   /sdcard 未挂载或 scoped storage 拦截时会写不进去（表现为 "cat: ... No such file"），
#   /data/local/tmp 对 shell 用户一定可写，作为兜底。
_DUMP_DIRS = ("/sdcard", "/data/local/tmp", "/storage/emulated/0")

# 抢到窗口焦点但**不代表 App 退到后台**的叠加层（输入法、系统弹窗等）。
# 判断"App 是否占着屏幕"时要放行这些包，否则键盘一弹就误判未登录/不在前台。
_SYSTEM_OVERLAY_MARKERS = (
    "inputmethod", "adbkeyboard", "com.android.systemui", "permissioncontroller",
    "packageinstaller", "android.system", "com.android.internal",
)

# 没有在线设备时可尝试自动拉起的模拟器/容器（Linux 优先 Waydroid）：
#   命令 -> (可执行文件候选, 参数列表)
_LAUNCHERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # Linux（Waydroid 官方建议 wayland；会话是 X11 时加 -X）
    "waydroid": (("waydroid",), ("session", "start")),
    "waydroid-x": (("waydroid",), ("session", "start", "-X")),
    "genymotion": (("genymotion-player", "genyshell"), ("-n",)),
    # Windows
    "ldplayer": (("dnplayer.exe",), ()),
    "mumu": (("MuMuPlayer.exe",), ()),
    "bluestacks": (("HD-Player.exe",), ()),
}

ADBKEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
ADBKEYBOARD_PKG = "com.android.adbkeyboard"
# APK 只从项目目录里的捆绑文件安装（keyboardservice-debug.apk / ADBKeyBoard.apk），
# 不做任何联网下载。
ADBKEYBOARD_APK_NAMES = ("keyboardservice-debug.apk", "ADBKeyBoard.apk")

# 文本注入相关常量
_CODEPOINT_CHUNK = 200          # ADB_INPUT_CHARS 每批码点数（命令行长度限制）
_TEXT_RETRY_SLEEP = 0.35        # 广播后等待 IME 提交文本
CLIPBOARD_PKG = "com.android.clipper"   # Android 10 及以下可用（若已安装）
WSA_APPS_URI = "shell:AppsFolder\\MicrosoftCorporationII.WindowsSubsystemForAndroid_8wekyb3d8bbwe!App"

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------- 工具函数
def _no_window_kwargs() -> dict:
    """子进程参数：Windows 隐藏控制台窗口，类 Unix 起独立会话（避免随终端退出被杀）。"""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def find_adb(explicit: str = "") -> str:
    if explicit and Path(explicit).exists():
        return explicit
    env = os.environ.get("ADB_PATH", "")
    if env and Path(env).exists():
        return env
    if IS_WINDOWS:
        for d in _LDPLAYER_DIRS:
            p = Path(d) / "adb.exe"
            if p.exists():
                return str(p)
        for p in _MUMU_ADB_CANDIDATES:
            if os.path.exists(p):
                return p
        raw_candidates = _WIN_ADB_CANDIDATES
    else:
        raw_candidates = _UNIX_ADB_CANDIDATES
    for raw in raw_candidates:
        p = Path(os.path.expandvars(raw)).expanduser()
        try:
            if p.exists():
                return str(p)
        except OSError:
            continue
    w = shutil.which("adb")
    if w:
        return w
    if IS_WINDOWS:
        raise AdbError("未找到 adb.exe：请在 config.yaml 的 adb.path 指定，或设置环境变量 ADB_PATH，"
                       "或将 adb 加入 PATH（可用 winget install Google.PlatformTools，"
                       "WSA/WSABuilds 也可用其自带的 adb）")
    raise AdbError("未找到 adb：请在 config.yaml 的 adb.path 指定，或设置环境变量 ADB_PATH，"
                   "或安装 platform-tools（Debian/Ubuntu: sudo apt install adb；"
                   "macOS: brew install --cask android-platform-tools；"
                   "通用: 下载 platform-tools 并把 adb 加入 PATH）")


def _is_wsa_serial(serial: str) -> bool:
    """判断序列号是否像 WSA：任意 host:port，且端口不是已知模拟器端口。

    典型值：127.0.0.1:58526 / localhost:58526 / [::1]:58526。
    """
    if not serial:
        return False
    m = re.match(r"^(?:\[[0-9a-fA-F:]+\]|[A-Za-z0-9_.\-]+):(\d{2,5})$", serial.strip())
    if not m:
        return False
    port = int(m.group(1))
    return port not in _EMULATOR_COMMON_PORTS


def platform_tag() -> str:
    """当前平台标签，用于日志/提示。"""
    if IS_WINDOWS:
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    return "Linux"


def _unauthorized_tip(serial: str) -> str:
    if serial.startswith(("emulator-", "127.0.0.1", "localhost")):
        return "请在该模拟器/子系统窗口里点击『允许 USB 调试』"
    if IS_WINDOWS:
        return "请在手机上允许 USB 调试（或 adb kill-server 后重试）"
    return ("请在手机上允许 USB 调试；若列表里根本看不到设备，"
            "多半需要配置 udev 规则（如 sudo usermod -aG plugdev $USER + "
            "/etc/udev/rules.d/51-android.rules 后重新插拔）")


def _environment_hints() -> list[str]:
    """没有在线设备时，按平台给出针对性的排查建议。"""
    hints: list[str] = []
    if IS_WINDOWS:
        if wsa_installed():
            info = wsa_connection_info()
            hints.append(
                f"检测到本机安装了 WSA：请在 WSA 设置 -> Advanced settings -> Developer mode "
                f"里确认端口，然后设置 adb.port={info['port']} 或 "
                f"adb.serial=\"{info['ip']}:{info['port']}\""
                f"（若报 10061 端口被占用，管理员执行 netsh int ipv4 add excludedportrange "
                f"protocol=tcp startport={info['port']} numberofports=1 后重启）")
        else:
            hints.append("未检测到 WSA；若用 WSABuilds，请确认已开启 Developer mode 并把 "
                         "adb.port 设为它显示的端口（默认 58526）")
        hints.append("模拟器（雷电/MuMu 等）请确认已开启 ADB 调试；adb 端口通常为 5555"
                     "（MuMu 另有 16384）")
        return hints

    # Linux / macOS
    if sys.platform == "darwin":
        hints.append("macOS：adb 端口通常为 5555（Genymotion / 真机网络调试），"
                     "Android Studio 模拟器会显示为 emulator-5554 之类的序列号")
        hints.append("macOS：装 adb 用 `brew install --cask android-platform-tools`；"
                     "真机需先在手机上允许 USB 调试")
        return hints

    hints.append("请确认目标已启动且开启了 ADB 调试；adb 端口通常为 5555"
                 "（夜神 62001、逍遥 21503、Waydroid 5555/5556、Genymotion 5555）")
    if waydroid_present():
        hints.append("检测到 Waydroid：先 `waydroid session start`（Wayland 会话）或 "
                     "`waydroid session start -X`（X11 会话），再 "
                     "`adb connect 127.0.0.1:5555`；也可设 adb.auto_launch_emulator: true "
                     "让桥接自动拉起")
    hints.append("Linux：装 adb 用 `sudo apt install adb`（或 android-tools-adb）；"
                 "真机若 `adb devices` 为空，多半要配 udev 规则："
                 "`echo 'SUBSYSTEM==\"usb\", ATTR{idVendor}==\"<厂商ID>\", MODE=\"0666\", "
                 "GROUP=\"plugdev\"' | sudo tee /etc/udev/rules.d/51-android.rules`，"
                 "再 `sudo udevadm control --reload-rules && sudo udevadm trigger`；"
                 "用户需在 plugdev 组（sudo usermod -aG plugdev $USER 后重新登录）")
    hints.append("若通过 SSH/容器运行，注意 X11/Wayland 显示与 /dev/kvm 权限"
                 "（容器内运行模拟器需要 --device /dev/kvm）")
    return hints


def wsa_adb_port() -> int:
    """读取 WSA 的 ADB 端口：优先注册表（Developer mode 实际分配值），失败返回常见端口。

    非 Windows 平台直接返回默认端口（无注册表可读）。
    """
    if winreg is not None:
        for path in _WSA_REG_PATHS:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                    i = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(k, i)
                        except OSError:
                            break
                        i += 1
                        if not isinstance(value, str):
                            continue
                        if "LocalHostIPAddress" in name or ":" in value:
                            m = re.search(r":(\d{2,5})\s*$", value.strip())
                            if m:
                                return int(m.group(1))
            except OSError:
                continue
    return WSA_ADB_DEFAULT_PORT


def wsa_connection_info() -> dict:
    """读取 WSA 的连接凭据（connect 用 IP/端口），供配置/serial 使用。"""
    info = {"ip": "127.0.0.1", "port": wsa_adb_port()}
    if winreg is not None:
        for path in _WSA_REG_PATHS:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                    i = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(k, i)
                        except OSError:
                            break
                        i += 1
                        if not isinstance(value, str):
                            continue
                        m = re.match(r"^(.+?):(\d{2,5})$", value.strip())
                        if m:
                            info["ip"], info["port"] = m.group(1), int(m.group(2))
                            return info
            except OSError:
                continue
    return info


def wsa_installed() -> bool:
    if not IS_WINDOWS:
        return False
    paths = wsa_known_paths()
    for p in paths:
        try:
            if Path(p).exists():
                return True
        except OSError:
            continue
    # 通过 Appx 包目录判断（LS 盘符可能变化，用包目录名兜底）
    base = os.path.expandvars(r"%LOCALAPPDATA%\Packages")
    try:
        if Path(base).exists():
            for entry in Path(base).iterdir():
                if "WindowsSubsystemForAndroid" in entry.name:
                    return True
    except OSError:
        pass
    return False


def wsa_known_paths() -> list[str]:
    """WSA 安装目录候选（WSABuilds 通常装在 C/D/E 盘根目录或 Program Files）。"""
    return [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\WsaClient.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wsaclient.exe"),
    ]


def waydroid_present() -> bool:
    """本机是否装了 Waydroid（Linux 上的 Android 容器，最接近 WSA 的形态）。"""
    if IS_WINDOWS or sys.platform != "linux":
        return False
    return bool(shutil.which("waydroid"))


def available_launchers() -> list[str]:
    """本机可用的模拟器/容器启动器（供“没有在线设备时自动拉起”使用）。"""
    found = []
    for name, (bins, _args) in _LAUNCHERS.items():
        if not IS_WINDOWS and name in ("ldplayer", "mumu", "bluestacks"):
            continue
        if IS_WINDOWS and name.startswith("waydroid"):
            continue
        for b in bins:
            if shutil.which(b):
                found.append(name)
                break
    return found


def launch_emulator(name: str = "") -> bool:
    """尝试拉起模拟器/Android 容器（只在用户显式开启 adb.auto_launch_emulator 时调用）。

    name 留空则按可用性顺序挑第一个（Linux 优先 Waydroid）。
    """
    order = [name] if name else (["waydroid", "waydroid-x", "genymotion"] if not IS_WINDOWS
                                 else ["ldplayer", "mumu", "bluestacks"])
    for key in order:
        bins, args = _LAUNCHERS.get(key, ((), ()))
        for b in bins:
            exe = shutil.which(b)
            if not exe:
                continue
            try:
                subprocess.Popen([exe, *args], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, **_no_window_kwargs())
                return True
            except OSError:
                continue
    return False


def set_clipboard(adb: "ADBController", text: str) -> bool:
    """写入设备剪贴板并**回读校验**。

    部分环境（如 WSA）的剪贴板与宿主机剪贴板是共享/桥接的：`cmd clipboard set-text`
    若静默失败，随后的 KEYCODE_PASTE 会粘出宿主机剪贴板里的旧内容。
    因此这里必须回读确认，写不进去就直接放弃剪贴板方案。
    """
    quoted = ADBController._sh_quote(text)
    cmds = [f"cmd clipboard set-text {quoted}", f"cmd clipboard set {quoted}"]
    # Android 10 及以下没有 cmd clipboard，若用户装了 Clipper 可走它的广播
    if CLIPBOARD_PKG in adb.try_shell(f"pm list packages {CLIPBOARD_PKG}"):
        cmds.append(f"am broadcast -a clipper.set -e text {quoted}")
    for cmd in cmds:
        try:
            adb.shell(cmd, timeout=15)
        except AdbError:
            continue
        time.sleep(0.2)
        cur = adb.get_clipboard()
        if cur is not None and cur == text:
            return True
    return False


def launch_wsa() -> bool:
    """尝试拉起 WSA 客户端（仅在用户显式开启 adb.auto_launch_wsa 时调用，Windows 专有）。"""
    if not IS_WINDOWS:
        return False
    try:
        subprocess.Popen(["explorer.exe", WSA_APPS_URI],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **_no_window_kwargs())
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 控制器
class ADBController:
    def __init__(self, adb_path: str = "", host: str = "127.0.0.1", port: int = 5555,
                 serial: str = "", timeout: float = 30.0, logger=None,
                 extra_ports: list[int] | None = None, wsa_port: int = 0,
                 input_retries: int = 2, dump_retries: int = 2, dump_delay: float = 0.8,
                 focus_ttl: float = 1.5, dump_timeout: float = 60.0):
        self.adb_path = find_adb(adb_path)
        self.host = host
        self.port = port
        self.serial = serial
        self.timeout = timeout
        self.logger = logger or _silent_logger()
        self.extra_ports = [int(p) for p in (extra_ports or []) if int(p) > 0]
        self.wsa_port = int(wsa_port or 0)
        self.input_retries = max(1, int(input_retries or 1))
        # UI dump 默认重试参数：默认值偏保守（2 次 / 0.8s），交互路径会显式用更快的
        # 参数（retries=2, delay=0.3），避免"按个按钮要好几秒"。
        self.dump_retries = max(1, int(dump_retries or 1))
        self.dump_delay = max(0.0, float(dump_delay or 0.0))
        # 单次 uiautomator dump 的超时（界面卡住时不至于把整条流程挂死）
        self.dump_timeout = max(5.0, float(dump_timeout or 60.0))
        # 最近一次 dump 失败的详细原因（供 --check / 诊断输出；便于排障）
        self._last_dump_detail = ""
        # 前台 component 缓存：dumpsys 每次要 0.3~1s，短时间内的连续判断直接复用，
        # 命中点击/按键后立即失效（界面已变化）。
        self.focus_ttl = max(0.0, float(focus_ttl or 0.0))
        self._focus_cache: tuple[float, str] = (0.0, "")
        self._activity_cache: tuple[float, str] = (0.0, "")
        # 曾经出现过"屏幕未点亮 -> null root"时置位：此后每次 dump 前先确认/唤醒屏幕，
        # 避免每次都先用 3~8 秒去撞 uiautomator 失败（实机 WSA 上单次 dump 从 45s 降到 2s）。
        self._screen_suspect = False
        # 上次成功的 dump 策略名（见 _dump_strategies）：记住后不再每次都先撞失败
        self._dump_strategy = ""
        self._sdk: int | None = None
        self._clipboard_ok: bool | None = None
        self._adbkeyboard_ok: bool | None = None
        self._adbkeyboard_b64_ok: bool | None = None
        self._input_verify_supported: bool | None = None
        self._is_wsa: bool | None = None
        # 显示旋转缓存（模拟器可能被旋转成竖屏，UI 逻辑坐标 != input 物理坐标）
        self._rotation: int = 0
        self._rotation_ts: float = 0.0
        self._phys: tuple[int, int] | None = None
        # uiautomator 同一时间只允许一个连接：跨线程串行化 dump，
        # 避免轮询线程与发送线程同时 dump 导致 "already registered"。
        self._dump_lock = threading.Lock()

    # ------------------------------------------------------------------ 基础
    def _base(self) -> list[str]:
        return [self.adb_path, "-s", self.serial] if self.serial else [self.adb_path]

    def _run(self, args: list, timeout: float | None = None, binary: bool = False,
             check: bool = True):
        cmd = self._base() + list(args)
        kwargs: dict = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout or self.timeout)
        kwargs.update(_no_window_kwargs())
        try:
            proc = subprocess.run(cmd, **kwargs)
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb 命令超时: {' '.join(cmd)}") from e
        out = proc.stdout if binary else proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        if check and proc.returncode != 0:
            raise AdbError(f"adb 命令失败({proc.returncode}): {' '.join(cmd)}\n"
                           f"{err.strip()}\n{str(out)[:500]}")
        return out, err

    def shell(self, cmd: str, timeout: float | None = None) -> str:
        out, _ = self._run(["shell", cmd], timeout=timeout)
        return out

    def try_shell(self, cmd: str, timeout: float | None = None) -> str:
        """执行失败返回 ''（不抛异常），用于纯探测类命令。"""
        try:
            return self.shell(cmd, timeout=timeout)
        except AdbError:
            return ""

    # ------------------------------------------------------------------ 连接
    def devices(self) -> list[str]:
        out, _ = self._run(["devices"], timeout=15)
        serials = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials

    def device_states(self) -> dict[str, str]:
        """全部设备及其状态（含 offline/unauthorized），用于排障提示。"""
        out, _ = self._run(["devices"], timeout=15)
        states: dict[str, str] = {}
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                states[parts[0]] = parts[1]
        return states

    def _port_candidates(self) -> list[int]:
        """连接候选端口：WSA 端口 -> 配置端口 -> WSA 常见端口 -> 模拟器常见端口。"""
        cands: list[int] = []
        for p in ([self.wsa_port] if self.wsa_port else []) + [wsa_adb_port()] + \
                 [self.port] + self.extra_ports + _WSA_COMMON_PORTS + _EMULATOR_COMMON_PORTS:
            p = int(p or 0)
            if p > 0 and p not in cands:
                cands.append(p)
        return cands

    def connect(self, ports: list[int] | None = None, timeout: float = 8.0) -> bool:
        """依次尝试候选端口连接；**已有的在线设备永远优先**（不会被顶掉）。

        WSA 的 ADB 无线调试端口默认 58526（WSABuilds 同），雷电/MuMu 为 5555/16384。
        """
        if self.is_connected():
            return True
        if self.serial:
            return self._connect_one(self.serial, timeout)

        last_err = ""
        for p in (ports or self._port_candidates()):
            for host in self._hosts():
                target = f"{host}:{p}"
                try:
                    out, _ = self._run(["connect", target], timeout=timeout, check=False)
                except AdbError as e:
                    last_err = str(e)
                    continue
                if "connected" in out.lower():
                    if self._adopt_serial(target):
                        return True
                else:
                    last_err = out.strip().splitlines()[-1] if out.strip() else ""
        if not self.serial:
            # 兜底：也许设备是被别的 adb server 注册的，或已存在于 devices 列表
            if self._adopt_serial(""):
                return True
        if last_err:
            self.logger.warning(f"ADB 连接失败（已尝试 {ports or self._port_candidates()}）: {last_err}")
        return False

    def _hosts(self) -> list[str]:
        hosts = [self.host or "127.0.0.1"]
        for h in ("127.0.0.1", "localhost"):
            if h not in hosts:
                hosts.append(h)
        return hosts

    def _connect_one(self, target: str, timeout: float = 8.0) -> bool:
        try:
            out, _ = self._run(["connect", target], timeout=timeout, check=False)
        except AdbError:
            return False
        if "connected" in out.lower():
            return self._adopt_serial(target)
        return False

    def _adopt_serial(self, prefer: str = "") -> bool:
        """从在线设备里挑一个（WSA/已有设备优先），成功后写入 self.serial。"""
        serials = self.devices()
        if prefer and prefer in serials:
            self.serial = prefer
            return True
        if serials:
            self.serial = self._pick_serial(serials)
            return True
        # 列表暂时为空（WSA 刚 connect、枚举有延迟）也接受刚连上的目标：
        # 它在本次 `adb connect` 里已被明确报告为 connected。
        if prefer:
            self.serial = prefer
            return True
        return False

    def _pick_serial(self, serials: list[str] | None = None) -> str:
        serials = list(serials if serials is not None else self.devices())
        if not serials:
            raise AdbError("没有在线设备：请先启动模拟器 / WSA（并确认 ADB 调试已开启）")
        if len(serials) == 1:
            return serials[0]
        # 多设备：优先 WSA（127.0.0.1:<非模拟器端口>），其次 adb 原生 emulator-XXXX
        for s in serials:
            if _is_wsa_serial(s):
                return s
        for s in serials:
            if s.startswith("emulator-"):
                return s
        raise AdbError(f"检测到多个在线设备 {serials}，请在 config.yaml 的 adb.serial 指定一个")

    def is_connected(self) -> bool:
        try:
            if self.serial:
                out, _ = self._run(["get-state"], timeout=10)
                state = out.strip().lower()
                if state == "device":
                    return True
                # serial 失效（WSA 重启后端口会变）：清掉重新选
                self.logger.warning(f"设备 {self.serial} 状态为 {state or 'unknown'}，重新选择设备")
                self.serial = ""
            serials = self.devices()
            if serials:
                self.serial = self._pick_serial(serials)  # 自动选定设备，避免后续命令多设备报错
                return True
            return False
        except AdbError:
            return False

    def ensure_connected(self, retries: int = 3, delay: float = 2.0,
                         auto_launch_wsa: bool = False,
                         auto_launch_emulator: bool = False) -> bool:
        for i in range(retries):
            if self.is_connected():
                return True
            try:
                self.logger.info(f"尝试连接 (第 {i + 1}/{retries} 次)："
                                 f"候选端口 {self._port_candidates()}")
                if self.connect():
                    return True
            except AdbError as e:
                self.logger.warning(f"连接失败: {e}")
            # 最后一轮仍失败：按平台尝试拉起目标环境后再等一会儿
            if i == retries - 2:
                if auto_launch_wsa and wsa_installed():
                    self.logger.info("未发现在线设备，尝试启动 WSA ...")
                    launch_wsa()
                    time.sleep(15)
                elif auto_launch_emulator:
                    names = available_launchers()
                    if names:
                        self.logger.info(f"未发现在线设备，尝试启动模拟器/容器: {names[0]} ...")
                        launch_emulator()
                        time.sleep(15)
            if i < retries - 1:
                time.sleep(delay)
        raise AdbError(self._connect_hint())

    def _connect_hint(self) -> str:
        states = {}
        try:
            states = self.device_states()
        except AdbError:
            pass
        hints = [f"[{platform_tag()}] ADB 连接失败（已尝试端口 {self._port_candidates()}），"
                 f"请确认目标已启动"]
        if states:
            hints.append(f"adb devices 当前状态: {states}")
            for s, st in states.items():
                if st == "unauthorized":
                    hints.append(f"设备 {s} 未授权：{_unauthorized_tip(s)}")
                elif st == "offline":
                    hints.append(f"设备 {s} offline：可尝试 adb disconnect {s} 后重连")
        hints.extend(_environment_hints())
        return "\n".join(hints)

    # ------------------------------------------------------------------ 系统信息
    def getprop(self, key: str) -> str:
        try:
            return self.shell(f"getprop {key}").strip()
        except AdbError:
            return ""

    def android_sdk(self) -> int:
        if self._sdk is None:
            try:
                self._sdk = int(self.getprop("ro.build.version.sdk") or 0)
            except ValueError:
                self._sdk = 0
        return self._sdk

    def android_version(self) -> str:
        return self.getprop("ro.build.version.release")

    def _device_blob(self) -> str:
        return " ".join([
            self.getprop("ro.product.brand"), self.getprop("ro.product.manufacturer"),
            self.getprop("ro.product.model"), self.getprop("ro.build.characteristics"),
            self.getprop("ro.product.name"), self.getprop("ro.product.device"),
            self.serial or "",
        ]).lower()

    def is_wsa(self) -> bool:
        """是否运行在 WSA / WSABuilds 上（用于日志与策略选择）。"""
        if self._is_wsa is None:
            blob = self._device_blob()
            self._is_wsa = ("windows" in blob or "subsystem" in blob or "wsa" in blob
                            or _is_wsa_serial(self.serial))
        return self._is_wsa

    def is_waydroid(self) -> bool:
        """是否运行在 Waydroid（Linux 上的 Android 容器）里。"""
        return "waydroid" in self._device_blob()

    def runtime_tag(self) -> str:
        """运行环境标签：WSA / Waydroid / 模拟器 / 真机（仅用于日志与提示）。"""
        blob = self._device_blob()
        if self.is_wsa():
            return "WSA"
        if "waydroid" in blob:
            return "Waydroid"
        if "genymotion" in blob:
            return "Genymotion"
        if self.serial.startswith("emulator-") or _is_wsa_serial(self.serial):
            return "模拟器"
        if "sdk_gphone" in blob or "google_sdk" in blob:
            return "Android 模拟器镜像"
        return "设备"

    def device_summary(self) -> str:
        parts = [f"序列号 {self.serial or '(自动)'}", self.runtime_tag()]
        ver = self.android_version()
        if ver:
            parts.append(f"Android {ver}")
        return " | ".join(parts)

    def _phys_size(self) -> tuple[int, int]:
        """物理屏幕尺寸（wm size 报告值，如 1920x1080）。"""
        if self._phys is None:
            try:
                out = self.shell("wm size")
                m = re.search(r"(\d+)x(\d+)", out)
                if m:
                    self._phys = int(m.group(1)), int(m.group(2))
            except AdbError:
                pass
            if self._phys is None:
                self._phys = 1920, 1080
        return self._phys

    def _current_rotation(self) -> int:
        """当前显示旋转：0/90/180/270（缓存 3 秒，dumpsys 查询较慢）。"""
        now = time.monotonic()
        if now - self._rotation_ts < 3.0:
            return self._rotation
        try:
            out = self.shell("dumpsys window displays", timeout=20)
            m = re.search(r"mCurrentRotation=ROTATION_(\d+)", out)
            self._rotation = int(m.group(1)) if m else 0
        except (AdbError, AttributeError, ValueError):
            self._rotation = 0
        self._rotation_ts = now
        return self._rotation

    def get_screen_size(self) -> tuple[int, int]:
        """当前应用的逻辑屏幕尺寸（宽, 高）——uiautomator bounds 与 input 坐标所在空间。

        模拟器被旋转成竖屏（ROTATION_90/270）时逻辑尺寸与物理尺寸互换，
        例如物理 1920x1080 -> 逻辑 1080x1920。发送/读取的左右判定与滑动
        计算都应使用逻辑尺寸。"""
        pw, ph = self._phys_size()
        return (pw, ph) if self._current_rotation() in (0, 180) else (ph, pw)

    # ------------------------------------------------ 前台 / 焦点（兼容 Android 13+）
    @staticmethod
    def _activity_from_dump(dump: str) -> str:
        """只从**Activity 级**信息里解析当前界面（用于"App 在不在前台"这类判断）。

        为什么要和窗口焦点分开：输入法（ADBKeyBoard）、系统弹窗、Toast 出现时
        `mCurrentFocus` 会是那些窗口（如 com.android.adbkeyboard/.AdbIME），
        而 App 的 Activity 仍是 resumed 状态。旧实现只看 mCurrentFocus，于是
        "键盘一弹出来就以为 App 不在前台/未登录"。这里优先取 Activity 记录。
        """
        if not dump:
            return ""
        for key in ("topResumedActivity", "mResumedActivity", "mFocusedApp",
                    "topResumedState", "mCurrentFocus"):
            for line in dump.splitlines():
                if key not in line or "null" in line.lower():
                    continue
                m = re.search(r"\s([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)", line)
                if m:
                    return m.group(1)
        return ""

    @staticmethod
    def _parse_focus(dump: str) -> str:
        """从 dumpsys window 输出里解析**最上层窗口**（兼容多种系统版本）。

        用于弹窗/遮挡判断（需要知道当前是不是 IME/权限弹窗/Dialog）。
        App 是否在前台请用 get_current_activity()（只看 Activity 级信息）。

        - Android 12-：`mCurrentFocus=Window{... u0 com.pkg/com.pkg.Act}`
        - Android 13+ / WSA：`topResumedActivity=ActivityRecord{... u0 com.pkg/.Act t123}`
          （此时 mCurrentFocus 可能为空或是输入法/弹窗窗口）
        """
        if not dump:
            return ""
        for key in ("mCurrentFocus", "mFocusedApp", "topResumedActivity",
                    "mResumedActivity", "topResumedState"):
            for line in dump.splitlines():
                if key not in line or "null" in line.lower():
                    continue
                m = re.search(r"\s([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)", line)
                if m:
                    return m.group(1)
        return ""

    def _window_dump(self) -> str:
        for cmd in ("dumpsys window", "dumpsys activity activities"):
            try:
                out = self.shell(cmd, timeout=25)
            except AdbError:
                continue
            if out:
                return out
        return ""

    def get_current_activity(self, use_cache: bool = True) -> str:
        """当前 **Activity** 组件（忽略输入法/弹窗等窗口）。

        与 get_current_focus() 的区别见 _activity_from_dump 的说明。
        """
        if use_cache and self.focus_ttl > 0:
            ts, cached = self._activity_cache
            if cached and (time.monotonic() - ts) < self.focus_ttl:
                return cached
        out = self._window_dump()
        act = self._activity_from_dump(out)
        if not act:                       # dumpsys window 没有 Activity 信息时退回焦点
            act = self._parse_focus(out)
        if act:
            self._activity_cache = (time.monotonic(), act)
        return act

    def get_current_focus(self, use_cache: bool = True) -> str:
        """返回当前**最上层窗口**组件（弹窗/IME 也会返回），如 'com.xtc.watch/...'；无则 ''。

        use_cache=True（默认）时复用 focus_ttl 秒内的结果：一次点击/滑动会连续触发
        多轮前台判断，每次都 dumpsys 会显著拖慢操作；tap/swipe/keyevent 会主动失效缓存。
        """
        if use_cache and self.focus_ttl > 0:
            ts, cached = self._focus_cache
            if cached and (time.monotonic() - ts) < self.focus_ttl:
                return cached
        out = self._window_dump()
        focus = self._parse_focus(out)
        if focus:
            self._focus_cache = (time.monotonic(), focus)
            return focus
        return ""

    def invalidate_focus(self) -> None:
        """丢弃前台缓存（界面刚被点击/按键改变后调用）。"""
        self._focus_cache = (0.0, "")
        self._activity_cache = (0.0, "")

    def is_in_foreground(self, package: str, use_cache: bool = True) -> bool:
        """App 是否真的"占着屏幕"（可以对着它操作界面）。

        这里要同时看**两层信息**，缺一不可（两个坑都是实机踩出来的）：
        * 只看窗口焦点（mCurrentFocus）：输入法一弹出就抢走焦点，会误判"App 不在前台/未登录"；
        * 只看 Activity：WSA 主屏（com.microsoft.windows.homeapp）抢到窗口时，
          Activity 仍是小天才的，于是误判"已在前台"，不去把它拉到前台——
          结果 uiautomator 只能 dump 到 WSA 主屏（4 个节点），随后报"找不到联系人"。

        规则：Activity 必须是目标包；窗口要么也是目标包、要么是**输入法/系统弹窗**这类
        叠加层（此时仍算在前台，弹窗清理也正需要读到它）；窗口是**别的 App** 则不算。
        """
        act = self.get_current_activity(use_cache=use_cache)
        win = self.get_current_focus(use_cache=use_cache)
        if act and not act.startswith(package):
            return bool(win) and win.startswith(package)
        if not act:
            return bool(win) and win.startswith(package)
        # Activity 是本 App：再看窗口
        if not win or win.startswith(package):
            return True
        win_pkg = win.split("/", 1)[0].lower()
        return any(m in win_pkg for m in _SYSTEM_OVERLAY_MARKERS)

    def screen_on(self):
        """屏幕是否亮着：True / False / None（无法判断）。

        `uiautomator dump` 在息屏/锁屏时会报
        "ERROR: null root node returned by UiTestAutomationBridge."（实测 WSA 上就是这样），
        所以 dump 失败时要先确认并唤醒屏幕。
        """
        out = self.try_shell("dumpsys power", timeout=20) or \
            self.try_shell("dumpsys display", timeout=20)
        if not out:
            return None
        low = out.lower()
        if "mwakefulness=asleep" in low or "mscreenon=false" in low or "state=off" in low:
            return False
        if "mwakefulness=awake" in low or "mscreenon=true" in low:
            return True
        return None

    def wake_up(self) -> bool:
        """唤醒屏幕并解除锁屏（WSA/模拟器息屏后 dump 会一直失败）。返回是否已亮屏。"""
        try:
            self.shell("input keyevent 224", timeout=15)      # KEYCODE_WAKEUP
            time.sleep(0.8)
            self.try_shell("wm dismiss-keyguard", timeout=15)  # 无锁屏时是 no-op
            time.sleep(0.4)
            self.invalidate_focus()
        except AdbError:
            return False
        return self.screen_on() is not False

    def keep_awake(self) -> bool:
        """让子系统**保持常亮**（桥接运行期间）。

        实机（WSA）测到：虚拟屏会在闲置后进入 Asleep，随后 `uiautomator` 一直返回
        "null root node"，每次读取都要先唤醒（单次 dump 从 1s 变成 20~45s）。
        这里用两条最通用的设置把屏幕钉住：
          * `svc power stayon true` —— 充电/连接时常亮（WSA 恒为"已连接"）
          * `settings put system screen_off_timeout` 设成极大值 —— 永不自动息屏
        并用一次 WAKEUP 立即点亮。
        """
        ok = False
        for cmd, timeout in (("svc power stayon true", 15),
                            ("settings put system screen_off_timeout 2147483647", 15),
                            ("settings put system screen_dim_timeout 2147483647", 15)):
            out = self.try_shell(cmd, timeout=timeout)
            low = (out or "").lower()
            if out is not None and "exception" not in low and "error" not in low:
                ok = True
        if self.screen_on() is not True:
            self.wake_up()
        self._screen_suspect = self.screen_on() is not True
        return ok

    # ------------------------------------------------------------------ 操作
    # 注：`input tap/swipe` 与 uiautomator bounds 处于同一逻辑坐标系
    # （旋转竖屏时二者同步变成 1080x1920），因此直接透传，不需要坐标变换。
    def tap(self, x: int | float, y: int | float) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")
        self.invalidate_focus()

    def swipe(self, x1: int | float, y1: int | float,
              x2: int | float, y2: int | float, duration_ms: int = 300) -> None:
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")
        self.invalidate_focus()

    def keyevent(self, code: int) -> None:
        self.shell(f"input keyevent {int(code)}")
        self.invalidate_focus()

    def clear_text_field(self) -> None:
        """清空当前输入框：优先 ADBKeyBoard 的 ADB_CLEAR_TEXT，其次全选+删除。"""
        if self._adbkeyboard_ready() and self._adbkeyboard_active():
            try:
                self.shell("am broadcast -a ADB_CLEAR_TEXT")
                time.sleep(0.2)
                return
            except AdbError:
                pass
        try:
            self.shell("input keycombination 113 29")  # CTRL+A
            time.sleep(0.2)
            self.keyevent(67)  # DEL
            time.sleep(0.2)
        except AdbError:
            pass

    # --------------------------------------------------------- App 启动 / 前台确认
    def package_installed(self, package: str) -> bool:
        out = self.try_shell(f"pm list packages {package}", timeout=20)
        return f"package:{package}" in out

    def _parse_resolved_activity(self, out: str, package: str) -> str:
        """从 resolve-activity 输出里提取 activity 名。

        注意：Android 13+ 的 `cmd package resolve-activity` 会先打印
        `Priority=... / ...`、`WARNING: ...` 之类的行，直接取最后一行会拿错值。
        """
        for line in reversed((out or "").splitlines()):
            line = line.strip()
            if "/" not in line or " " in line or line.startswith(("Error", "WARNING", "Priority")):
                continue
            pkg, _, act = line.partition("/")
            if pkg == package:
                return act
        return ""

    def resolve_launcher_activity(self, package: str) -> str:
        """解析 launcher activity：cmd package -> pm dump（monkey 兜底由启动逻辑负责）。"""
        for cmd in (f"cmd package resolve-activity --brief {package}",
                    f"pm resolve-activity --brief {package}"):
            out = self.try_shell(cmd, timeout=20)
            act = self._parse_resolved_activity(out, package)
            if act:
                return act
        # pm dump 里找 android.intent.action.MAIN + LAUNCHER 的 Activity 名
        dump = self.try_shell(f"pm dump {package}", timeout=30)
        if dump:
            blocks = re.split(r"\n\s*(?=Activity|Receiver|Service|Provider)", dump)
            for blk in blocks:
                if "android.intent.action.MAIN" not in blk or "LAUNCHER" not in blk:
                    continue
                m = re.search(r"^\s*([\w.$]+/[\w.$]+)", blk, re.M)
                if m and m.group(1).startswith(package):
                    return m.group(1).split("/", 1)[1]
                m = re.search(r"([\w.$]+/[\w.$]+)", blk)
                if m and m.group(1).startswith(package):
                    return m.group(1).split("/", 1)[1]
        return ""

    def _resolve_launcher_activity(self, package: str) -> str:  # 兼容旧调用名
        return self.resolve_launcher_activity(package)

    def launch_app(self, package: str, activity: str = "", wait: float = 25.0,
                   attempts: int = 3) -> str:
        """启动 App 并确认其到达前台，返回最终使用的 activity（空串表示启动失败）。

        多策略（WSA 上 `am start -n` 偶发失败/被系统吞掉，`monkey` 最稳）：
          1) am start -n <pkg>/<activity>（resolve-activity 解析得到时）
          2) monkey -p <pkg> -c android.intent.category.LAUNCHER 1
          3) am start -a android.intent.action.MAIN -c ...LAUNCHER <pkg>
        每次启动后轮询前台确认；未到前台则退避重试整轮。
        返回值为 activity 名或命中的策略名（调用方只需判断非空）。
        """
        if not self.package_installed(package):
            raise AdbError(f"设备上没有安装 {package}（请先在模拟器/WSA 里安装并登录小天才 App）")
        if self.is_in_foreground(package):
            return activity or "already-foreground"

        resolved = activity or self.resolve_launcher_activity(package)
        attempts_cmds: list[tuple[str, str]] = []
        if resolved:
            attempts_cmds.append(("am start -n", f"am start -n {package}/{resolved}"))
        attempts_cmds += [
            ("monkey", f"monkey -p {package} -c android.intent.category.LAUNCHER 1"),
            ("am start -a MAIN/LAUNCHER",
             f"am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER {package}"),
        ]
        last_err = ""
        for i in range(max(1, attempts)):
            for name, cmd in attempts_cmds:
                try:
                    out = self.shell(cmd, timeout=30)
                except AdbError as e:
                    last_err = f"{name}: {e}"
                    continue
                # 只认真正的启动失败行；成功时的一些警告里也会出现 "error" 字样
                if re.search(r"(?mi)^\s*(Error|Exception)\b|\bError type \d|does not exist",
                             out or ""):
                    last_err = f"{name}: {(out or '').strip()[:200]}"
                    self.logger.debug(f"启动策略 {name} 返回: {(out or '').strip()[:200]}")
                    continue
                if self.wait_for_activity(package, timeout=wait):
                    return resolved or name
                last_err = f"{name}: 命令已执行但未检测到 {package} 前台"
            # 三套策略都没成功：等一下再来（WSA 冷启动/上一个进程未完全退出时常见）
            time.sleep(2.0 + i * 2.0)
        self.logger.warning(f"启动 {package} 失败：{last_err}")
        return ""

    def wait_for_activity(self, package: str, timeout: float = 20.0,
                          interval: float = 1.0) -> bool:
        """等待 package 到达前台（typo 名保留：wait_for_activity）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_in_foreground(package, use_cache=False):
                return True
            time.sleep(interval)
        return False

    def wait_for_focus(self, package: str, timeout: float = 20.0) -> bool:
        return self.wait_for_activity(package, timeout=timeout)

    def screenshot(self, path: str | None = None) -> bytes:
        out, _ = self._run(["exec-out", "screencap", "-p"], timeout=60, binary=True)
        if not out:
            raise AdbError("screencap 返回空数据")
        if path:
            Path(path).write_bytes(out)
        return out

    # ------------------------------------------------------------------ UI 解析
    def dump_ui(self, retries: int | None = None, delay: float | None = None) -> ET.Element:
        """uiautomator dump 并解析为 XML 树。线程安全（串行化），失败自动重试。

        retries/delay 省略时用实例默认值（config -> adb.dump_retries/dump_delay）。
        需要"快一点"的交互路径（点击/发送前后）显式传 retries=2, delay=0.3。
        """
        with self._dump_lock:
            return self._dump_ui_locked(
                self.dump_retries if retries is None else max(1, int(retries)),
                self.dump_delay if delay is None else max(0.0, float(delay)))

    @staticmethod
    def _extract_xml(out: str) -> str:
        """从 uiautomator 输出里截出 XML（/dev/tty 方案前面可能混有提示行）。"""
        if not out:
            return ""
        start = out.find("<?xml")
        if start < 0:
            return ""
        end = out.rfind("</hierarchy>")
        if end < 0:
            return ""
        return out[start:end + len("</hierarchy>")]

    @staticmethod
    def _short_reason(text: str, limit: int = 200) -> str:
        """把 ADB/uiautomator 的多行输出压成一行短原因（写进异常信息用）。"""
        return " ".join(str(text or "").split())[:limit]

    def _parse_dump(self, xml: str) -> ET.Element:
        try:
            return ET.fromstring(xml)
        except ET.ParseError as e:
            raise AdbError(f"UI dump XML 解析失败: {e}") from e

    def _read_dump_file(self, path: str) -> tuple[str, str]:
        """读取设备上的 dump 文件，返回 (内容, 失败说明)。

        用 `exec-out cat`（二进制直出，不经过 shell 的换行/编码转换）；不可用时回退
        `shell cat`。**失败说明里会带上真实原因**（例如 "No such file or directory"），
        这样上层日志不再只出现误导性的 "cat: ... No such file"。
        """
        out, err = self._run(["exec-out", "cat", path], timeout=30, binary=True, check=False)
        data = out.decode("utf-8", errors="replace") if isinstance(out, bytes) else str(out)
        if "<?xml" in data:
            return data, ""
        alt = self.try_shell(f"cat {path}", timeout=30)
        if "<?xml" in alt:
            return alt, ""
        detail = (err or "").strip() or (alt or "").strip() or data.strip()
        return "", f"读取 {path} 失败: {self._short_reason(detail) or '文件不存在'}"

    def _dump_via_tty(self, compressed: bool = False) -> tuple[str, str]:
        """快路径：`uiautomator dump /dev/tty` 直接把 XML 打到 stdout（一次 shell 调用，不落盘）。"""
        flag = " --compressed" if compressed else ""
        try:
            out = self.shell(f"uiautomator dump{flag} /dev/tty 2>&1", timeout=self.dump_timeout)
        except AdbError as e:
            return "", f"/dev/tty{flag}: {self._short_reason(str(e))}"
        xml = self._extract_xml(out)
        if xml:
            return xml, ""
        return "", f"/dev/tty{flag}: {self._short_reason(out) or '没有 XML 输出'}"

    def _dump_via_file_combined(self, compressed: bool = True) -> tuple[str, str]:
        """一次 shell 调用完成"dump 落盘 -> 读回 -> 删文件"。

        比"rm -> dump -> exec-out cat -> rm"少 3 次 adb 进程开销（每次约 0.2~0.3s），
        在 WSA 上实测量级从 ~3.5s 降到 ~1.8s。文件名仍然唯一，不会读到旧文件。
        """
        flag = " --compressed" if compressed else ""
        details = []
        for base in _DUMP_DIRS:
            path = f"{base}/xtc_dump_{os.getpid()}_{int(time.time() * 1000)}.xml"
            try:
                out = self.shell(f"uiautomator dump{flag} {path} 2>&1; cat {path} 2>&1; rm -f {path}",
                                 timeout=self.dump_timeout + 30)
            except AdbError as e:
                details.append(f"{base}: {self._short_reason(str(e))}")
                continue
            xml = self._extract_xml(out)
            if xml:
                return xml, ""
            details.append(f"{base}: {self._short_reason(out) or '没有 XML 输出'}")
        return "", " | ".join(details)

    def _dump_strategies(self) -> list:
        """按"上次成功的先试"的顺序返回 dump 策略。

        为什么**优先完整 dump**（实机 WSA 实测）：`--compressed` 会把消息时间标签
        节点（tv_chat_msg_item_date）整片裁掉——同一屏压缩版 19 个节点 / 0 个时间标签，
        完整版 46 个节点 / 3 个时间标签；时间标签一丢，每条消息的时间就退化成
        "当前时间"（用户报的"消息时间还是不对"就是这么来的）。
        实测完整版成功率 5/5、平均只慢 ~0.4s（3.5s vs 3.1s），所以默认先用它。

        某些镜像上完整 dump 可能被系统 Killed：那就自动落到下一策略，并**记住**
        能用的那个（见 _dump_strategy），下次直接用它先试，不会每次都白等。
        """
        strategies = [
            ("file-full", lambda: self._dump_via_file_combined(False)),
            ("tty-full", lambda: self._dump_via_tty(False)),
            ("file-compressed", lambda: self._dump_via_file_combined(True)),
            ("tty-compressed", lambda: self._dump_via_tty(True)),
            ("file", lambda: self._dump_via_file(False)),
        ]
        if self._dump_strategy:
            strategies.sort(key=lambda s: 0 if s[0] == self._dump_strategy else 1)
        return strategies

    def _dump_via_file(self, compressed: bool = False) -> tuple[str, str]:
        """文件方案：依次在多个可写目录里尝试落盘（/sdcard 不可用时自动换目录）。"""
        flag = " --compressed" if compressed else ""
        details: list[str] = []
        for base in _DUMP_DIRS:
            path = f"{base}/xtc_dump_{os.getpid()}_{int(time.time() * 1000)}.xml"
            try:
                self.try_shell(f"rm -f {path}", timeout=15)
                out = self.try_shell(f"uiautomator dump{flag} {path} 2>&1",
                                     timeout=self.dump_timeout)
                data, why = self._read_dump_file(path)
                self.try_shell(f"rm -f {path}", timeout=15)
                if data:
                    return data, ""
                details.append(f"{base}: {why or self._short_reason(out) or '未生成文件'}")
            except AdbError as e:
                details.append(f"{base}: {self._short_reason(str(e))}")
                self.try_shell(f"rm -f {path}", timeout=15)
        return "", " | ".join(details)

    def _looks_like_idle_error(self, details: list) -> bool:
        return any("idle" in d.lower() for d in details)

    def _looks_like_screen_off(self, details: list) -> bool:
        """是否像"息屏/锁屏/安全窗口"导致的 dump 失败。

        实测报错：`ERROR: null root node returned by UiTestAutomationBridge.`
        （WSA 息屏后必然出现；也可能出现在 FLAG_SECURE 窗口上）。
        """
        joined = " ".join(details).lower()
        return ("null root node" in joined or "screen is off" in joined
                or "no root node" in joined)

    def _reapply_animations(self) -> None:
        """重设动画缩放（有些镜像/重启后会恢复默认，导致界面永不"空闲"）。"""
        for key in ("window_animation_scale", "transition_animation_scale",
                    "animator_duration_scale"):
            self.try_shell(f"settings put global {key} 0", timeout=15)
        self.logger.debug("已重新关闭系统动画（UI dump 空闲性重试）")

    def has_focus_window(self) -> bool:
        """当前是否有**任何**获得焦点的窗口。

        `mCurrentFocus=null`（没有任何窗口有焦点）是 WSA 上的典型状态：
        WSA 窗口被最小化/关闭、或虚拟显示未点亮时，uiautomator 会一直返回
        "ERROR: null root node returned by UiTestAutomationBridge."。
        调用方据此决定"唤醒屏幕 / 重新拉起 App"来把窗口找回来。
        """
        try:
            return bool(self.get_current_focus(use_cache=False))
        except Exception:  # noqa: BLE001
            return False

    def _dump_failure_message(self, details: list) -> str:
        """组装**简短、可行动**的失败原因（完整细节进 _last_dump_detail / debug 日志）。

        旧版本把三个目录的完整报错全塞进异常信息，单条日志能到 2000+ 字符、完全没法看，
        这里只保留"原因归类 + 关键一行 + 前台状态 + 处置建议"。
        """
        joined = "；".join(d for d in details if d)
        self._last_dump_detail = joined
        if joined:
            try:
                self.logger.debug(f"UI dump 详细失败原因: {joined}")
            except Exception:  # noqa: BLE001
                pass
        low = joined.lower()
        focus = ""
        try:
            focus = self.get_current_focus(use_cache=False)
        except Exception:  # noqa: BLE001
            focus = ""
        # 归类：只取最有代表性的一条原因
        key_detail = next((d for d in details if self._short_reason(d)), "")
        if "null root node" in low or "no root node" in low:
            reason = "uiautomator 拿不到根节点（屏幕未点亮 / 窗口被最小化或关闭）"
            hint = ("请让 WSA 窗口保持打开（最小化或关闭 WSA 窗口时 Android 侧没有窗口焦点，"
                    "任何界面读取都会失败）；桥接会自动尝试唤醒屏幕并重新拉起 App")
        elif not focus:
            reason = "没有任何窗口获得焦点（前台为空）"
            hint = "让 WSA 窗口保持打开，或等窗口恢复后桥接会自动继续"
        elif "idle" in low:
            reason = "界面一直不空闲（转场/加载动画、弹窗或键盘光标）"
            hint = "可调大 adb.dump_retries / adb.dump_delay，或用 /小天才 初始化 清理界面"
        elif "no such file" in low or "not exist" in low or "文件不存在" in low:
            reason = "uiautomator 没有写出文件（界面未空闲或 /sdcard 不可写）"
            hint = "已自动改用 /data/local/tmp 等目录重试"
        else:
            reason = self._short_reason(key_detail, 120) or "未知原因"
            hint = ""
        parts = [f"UI dump 失败: {reason}"]
        if key_detail and reason != self._short_reason(key_detail, 120):
            parts.append(f"（{self._short_reason(key_detail, 100)}）")
        if hint:
            parts.append(f"；{hint}")
        parts.append(f"｜前台={focus or '(无焦点窗口)'}")
        return "".join(parts)

    def _dump_ui_locked(self, retries: int, delay: float) -> ET.Element:
        """dump 当前窗口 UI 为 XML（多策略 + 可读报错）。

        顺序：
          1) 快路径 `uiautomator dump /dev/tty`（一次 shell 调用，不依赖文件系统）；
          2) 文件方案：`/sdcard` -> `/data/local/tmp` -> `/storage/emulated/0`，
             先删后写再读；读取用 `exec-out cat`；
          3) 重试之间递进等待（等转场/动画结束），并重设一次动画缩放；
          4) 最后再试一次 `--compressed`。
        失败时抛出的信息包含 uiautomator 的**真实报错**与当前前台组件，
        不会再只显示 "cat: ...: No such file or directory" 这种误导性原因。
        """
        details: list[str] = []
        woke = False
        thin: ET.Element | None = None
        # 已知这块屏会睡着：先确认/唤醒，别拿 uiautomator 去试错（省下好几秒）
        if self._screen_suspect and self.screen_on() is False:
            self.logger.info("屏幕处于息屏状态，先唤醒再读取界面")
            self.wake_up()
        for i in range(retries):
            for name, fn in self._dump_strategies():
                xml, why = fn()
                if xml:
                    root = self._parse_dump(xml)
                    n = len(list(root.iter("node")))
                    if n >= 3:                      # 正常界面
                        if self._dump_strategy != name:
                            self.logger.debug(f"UI dump 使用策略: {name}")
                        self._dump_strategy = name
                        return root
                    # 只有 0~2 个节点的"空树"：多数是页面还在切换/刚登录完的过渡帧，
                    # 直接照着它判断会得出"不在聊天页/找不到联系人"的错结论（实机踩过）
                    thin = root
                    details.append(f"{name}: 空树（仅 {n} 节点，界面可能还在切换）")
                    continue
                details.append(f"{name}: {why}")
                if self._looks_like_screen_off([why]):
                    # 根节点拿不到：换目录/换压缩都没用，先唤醒屏幕（本轮回退到下一策略）
                    self._screen_suspect = True
                    if not woke:
                        woke = True
                        self.logger.info("界面读取失败像是屏幕未点亮（null root node），正在唤醒屏幕...")
                        if self.wake_up():
                            self.logger.info("已唤醒屏幕，重试读取界面")
                    break
            if i == 0 and self._looks_like_idle_error(details):
                self._reapply_animations()
            if i < retries - 1:
                time.sleep(max(0.3, delay) * (i + 1))   # 递进等待：界面越不稳等越久
        if thin is not None:
            self.logger.warning("界面反复只读到 0~2 个节点（页面仍在切换/加载），先按空界面处理")
            return thin
        raise AdbError(self._dump_failure_message(details))

    @staticmethod
    def _node_matches(node, resource_id=None, text=None, class_name=None,
                      content_desc=None, text_contains=False,
                      content_desc_contains=True) -> bool:
        if resource_id:
            rid = node.get("resource-id", "")
            if rid != resource_id and not rid.endswith("/" + resource_id.lstrip("/")):
                return False
        if text is not None:
            t = node.get("text", "")
            if text_contains:
                if text not in t:
                    return False
            elif t != text:
                return False
        if class_name:
            c = node.get("class", "")
            if c != class_name and not c.endswith("." + class_name):
                return False
        if content_desc:
            d = node.get("content-desc", "")
            # 默认子串匹配（历史行为：弹窗/按钮文案常带前后缀）；
            # 但"精确等于"的场景必须显式关掉子串匹配——例如发送按钮的"发送"
            # 会命中"发送失败/发送中"这类消息状态图标。
            if content_desc_contains:
                if content_desc not in d:
                    return False
            elif d != content_desc:
                return False
        return True

    def find_elements(self, root: ET.Element | None = None, **kw) -> list[ET.Element]:
        root = root if root is not None else self.dump_ui()
        return [n for n in root.iter("node") if self._node_matches(n, **kw)]

    def find_element(self, root: ET.Element | None = None, index: int = 0, **kw):
        els = self.find_elements(root=root, **kw)
        return els[index] if len(els) > index else None

    @staticmethod
    def node_bounds(node) -> tuple[int, int, int, int] | None:
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        if not m:
            return None
        x1, y1, x2, y2 = map(int, m.groups())
        return x1, y1, x2, y2

    @staticmethod
    def node_center(node) -> tuple[int, int]:
        b = ADBController.node_bounds(node)
        if not b:
            return 0, 0
        x1, y1, x2, y2 = b
        return (x1 + x2) // 2, (y1 + y2) // 2

    def tap_element(self, node) -> None:
        x, y = self.node_center(node)
        self.tap(x, y)

    # ------------------------------------------------------------------ 文本注入
    def input_text(self, text: str, verify=None, retries: int = 0,
                   ensure_ime: bool = True) -> bool:
        """向当前聚焦的输入框注入文本，返回是否**确认写入成功**。

        策略链（每一步都用 verify() 校验结果，失败才降级）：
          1) ADBKeyBoard: ADB_INPUT_TEXT（明文，v2.5-dev 不解码 URL，旧版同样兼容）
          2) ADBKeyBoard: ADB_INPUT_B64（base64，绕开 am 的 UTF-8 参数问题）
          3) ADBKeyBoard: ADB_INPUT_CHARS（Unicode 码点）
          4) 剪贴板 set-text + KEYCODE_PASTE（写入后回读校验，避免粘出宿主剪贴板旧内容）
          5) input text（仅 ASCII）

        verify: 可选回调 `() -> bool`，返回输入框里是否已出现目标文本。为 None 时
                尽量用 UI dump 自动校验（找不到输入框则视为成功，保持向后兼容）。
        """
        if text is None or text == "":
            return True
        if verify is None:
            verify = self._default_input_verifier(text)
        rounds = max(1, int(retries or self.input_retries))
        for attempt in range(rounds):
            if attempt > 0:
                time.sleep(0.6 * attempt)
            if self._input_via_adbkeyboard(text, verify, ensure_ime=ensure_ime):
                return True
        if self._input_via_clipboard(text, verify):
            return True
        if text.isascii():
            try:
                self.shell(f"input text {self._sh_quote(text.replace(' ', '%s'))}")
                time.sleep(0.4)
                if verify():
                    return True
            except AdbError:
                pass
        self.logger.warning(
            "文本注入失败（输入框未收到内容）。请检查：ADBKeyBoard 是否为当前输入法"
            "（adb shell ime set com.android.adbkeyboard/.AdbIME）、输入框是否已获得焦点；"
            f"若设备上没装 ADBKeyBoard，请把 {' 或 '.join(ADBKEYBOARD_APK_NAMES)} "
            "放到项目目录后重跑（本项目不做在线安装）")
        return False

    # ---------------------------------------------------- 各注入通道
    def input_text_plain(self, text: str, ensure_ime: bool = True) -> bool:
        """只把文本广播给输入框（**不做** UI 校验），返回广播是否发出。

        用途：发送快路径。校验要 dump 一次界面（WSA 上 ~3 秒），而"点发送之后"
        那次确认 dump 本身是更强的证据（气泡里出现这段文本 = 注入成功 + 发送成功）。
        所以快路径先不校验，直接点发送，再一次 dump 统一确认；没确认成功时调用方
        会退回 `input_text()`（带校验的完整策略链，含重新聚焦）重试。

        走上次成功的通道（`_adbkeyboard_b64_ok`）：明文广播在个别镜像上会被 am 的
        UTF-8 参数问题吞掉，那时改用 base64 通道。
        """
        if not text:
            return True
        try:
            if not self._adbkeyboard_ready():
                return False
            if ensure_ime and not self._adbkeyboard_active():
                self.logger.info("ADBKeyBoard 不是当前输入法，正在切换...")
                self.set_default_ime(ADBKEYBOARD_IME)
                time.sleep(0.8)
            if self._adbkeyboard_b64_ok:
                b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
                self.shell(f"am broadcast -a ADB_INPUT_B64 --es msg {b64}", timeout=20)
            else:
                self.shell(f"am broadcast -a ADB_INPUT_TEXT --es msg {self._sh_quote(text)}",
                           timeout=20)
            time.sleep(_TEXT_RETRY_SLEEP)
            return True
        except AdbError as e:
            self.logger.debug(f"纯注入广播失败: {e}")
            return False

    def _input_via_adbkeyboard(self, text: str, verify, ensure_ime: bool = True) -> bool:
        if not self._adbkeyboard_ready():
            return False
        if ensure_ime and not self._adbkeyboard_active():
            self.logger.info("ADBKeyBoard 不是当前输入法，正在切换...")
            self.set_default_ime(ADBKEYBOARD_IME)
            time.sleep(0.8)
        # 1) 明文广播（最快，绝大多数设备可用）
        try:
            self.shell(f"am broadcast -a ADB_INPUT_TEXT --es msg {self._sh_quote(text)}",
                       timeout=20)
            time.sleep(_TEXT_RETRY_SLEEP)
            if verify():
                return True
        except AdbError as e:
            self.logger.debug(f"ADB_INPUT_TEXT 失败: {e}")
        # 2) base64 广播（Oreo/P 之后 am 不再接受 UTF-8 明文参数时使用）
        try:
            b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
            self.shell(f"am broadcast -a ADB_INPUT_B64 --es msg {b64}", timeout=20)
            time.sleep(_TEXT_RETRY_SLEEP)
            if verify():
                self._adbkeyboard_b64_ok = True
                return True
        except AdbError as e:
            self.logger.debug(f"ADB_INPUT_B64 失败: {e}")
        # 3) Unicode 码点数组
        try:
            codes = [ord(ch) for ch in text]
            for i in range(0, len(codes), _CODEPOINT_CHUNK):
                chunk = ",".join(str(c) for c in codes[i:i + _CODEPOINT_CHUNK])
                self.shell(f"am broadcast -a ADB_INPUT_CHARS --eia chars '{chunk}'", timeout=20)
                time.sleep(_TEXT_RETRY_SLEEP)
            if verify():
                return True
        except AdbError as e:
            self.logger.debug(f"ADB_INPUT_CHARS 失败: {e}")
        self.logger.warning("ADBKeyBoard 广播已发送但输入框未收到文本"
                            "（输入法可能未附加到当前输入框，将尝试剪贴板方案）")
        return False

    def _input_via_clipboard(self, text: str, verify) -> bool:
        """剪贴板方案：**先确认剪贴板写成功**，再粘贴；粘完校验输入框内容。"""
        if self.android_sdk() < 29:      # Android 10 以下没有 cmd clipboard
            self._clipboard_ok = False
            return False
        if not self.probe_clipboard():
            return False
        if not set_clipboard(self, text):
            self.logger.warning("剪贴板写入失败/回读不一致（WSA 常见），跳过剪贴板注入，"
                                "避免粘出宿主剪贴板里的旧内容")
            return False
        try:
            self.keyevent(279)  # KEYCODE_PASTE
            time.sleep(0.6)
        except AdbError:
            return False
        return verify()

    # ---------------------------------------------------- 状态探测
    def _adbkeyboard_ready(self) -> bool:
        if self._adbkeyboard_ok is None:
            try:
                out = self.shell("ime list -s")
                self._adbkeyboard_ok = ADBKEYBOARD_IME in out
                if not self._adbkeyboard_ok:
                    # 有的镜像只列出包名或以 \n 分隔不完整，再用 pm list 兜底
                    pkgs = self.try_shell(f"pm list packages {ADBKEYBOARD_PKG}")
                    self._adbkeyboard_ok = f"package:{ADBKEYBOARD_PKG}" in pkgs
            except AdbError:
                self._adbkeyboard_ok = False
        return self._adbkeyboard_ok

    def adbkeyboard_ready(self) -> bool:
        return self._adbkeyboard_ready()

    def _adbkeyboard_active(self) -> bool:
        """ADBKeyBoard 是否为当前默认输入法（仅启用不够——广播需要它是活动 IME）。"""
        try:
            out = self.shell("settings get secure default_input_method")
            if ADBKEYBOARD_IME in out:
                return True
            # 某些实现会写成 com.android.adbkeyboard/com.android.adbkeyboard.AdbIME
            return ADBKEYBOARD_PKG in out and "adbkeyboard" in out.lower()
        except AdbError:
            return False

    def current_ime(self) -> str:
        return self.try_shell("settings get secure default_input_method").strip()

    def ime_shown(self) -> bool:
        """软键盘当前是否真的显示（WSA 上 ADBKeyBoard 的输入视图高度为 0，需靠这个判断）。"""
        out = self.try_shell("dumpsys input_method", timeout=20)
        if not out:
            return False
        if "mInputShown=true" in out or "mIsInputViewShown=true" in out:
            return True
        return bool(re.search(r"mInputShown\s*=\s*true", out))

    def _clipboard_supported(self) -> bool:
        return self._clipboard_ok if self._clipboard_ok is not None else True

    def get_clipboard(self) -> str | None:
        """读取设备剪贴板文本（读不到返回 None）。"""
        out = self.try_shell("cmd clipboard get-text", timeout=15)
        if out:
            text = out.strip()
            if text and "Exception" not in text and "not found" not in text.lower():
                return text
        return None

    def probe_clipboard(self) -> bool:
        """探测剪贴板方案是否可用（写入+回读一致）。"""
        if self._clipboard_ok is None:
            if self.android_sdk() < 29:
                self._clipboard_ok = False
            else:
                probe = "xtc-clipboard-probe"
                self._clipboard_ok = set_clipboard(self, probe)
        return self._clipboard_ok

    def _clipboard_ready(self) -> bool:
        return self.probe_clipboard()

    # ---------------------------------------------------- 自动校验
    def _default_input_verifier(self, text: str):
        """默认校验：UI dump 里任意可编辑控件包含目标文本即视为成功。

        找不到任何可编辑控件（页面/镜像差异）时返回 True，避免把成功误判为失败。
        """
        needle = (text or "").strip()

        def _verify() -> bool:
            if self._input_verify_supported is False:
                return True
            try:
                root = self.dump_ui(retries=1, delay=0.0)
            except AdbError:
                self._input_verify_supported = False
                return True
            found_editable = False
            for n in root.iter("node"):
                cls = n.get("class", "")
                if cls.endswith("EditText") or n.get("focusable") == "true":
                    found_editable = True
                    cur = n.get("text", "") or ""
                    if needle and needle in cur:
                        return True
            if not found_editable:
                self._input_verify_supported = False
                return True
            return False

        return _verify

    @staticmethod
    def _sh_quote(s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"

    # ---------------------------------------------------- IME / APK 管理
    def install_apk(self, apk_path: str) -> bool:
        out, _ = self._run(["install", "-r", apk_path], timeout=180)
        return "success" in out.lower()

    def enable_ime(self, ime_id: str) -> None:
        self.shell(f"ime enable {ime_id}")

    def set_default_ime(self, ime_id: str) -> None:
        """设为默认输入法：ime set + 直接写 settings（部分镜像 ime set 不生效）。"""
        try:
            self.shell(f"ime set {ime_id}")
        except AdbError:
            pass
        try:
            self.shell(f"settings put secure default_input_method {ime_id}")
        except AdbError:
            pass
        # 部分镜像还需要把输入法加进 enabled_input_methods
        try:
            cur = self.shell("settings get secure enabled_input_methods").strip()
            if ime_id not in cur:
                joined = f"{cur}:{ime_id}" if cur and cur != "null" else ime_id
                self.shell(f"settings put secure enabled_input_methods {joined}")
        except AdbError:
            pass

    def install_adbkeyboard(self, apk_path: str = "") -> bool:
        """确保 ADBKeyBoard 可用：**先检查设备上有没有**，没有才用项目目录里的本地 APK 安装。
        全程不联网。失败返回 False，不抛出。

        返回 True 时保证：ADBKeyBoard 已安装且为当前默认输入法。
        """
        if self._adbkeyboard_ready():
            self.logger.info("ADBKeyBoard 已安装，跳过安装步骤")
            if not self._adbkeyboard_active():
                self.set_default_ime(ADBKEYBOARD_IME)
            return True
        if not apk_path:
            apk_path = self._find_bundled_apk()
        if not apk_path:
            self.logger.warning(
                "设备上没有 ADBKeyBoard，且项目目录里找不到本地 APK（"
                + " / ".join(ADBKEYBOARD_APK_NAMES) +
                "）。请把该 APK 放到项目根目录后重跑，或手动安装："
                f"adb install -r <APK路径> && adb shell ime enable {ADBKEYBOARD_IME} && "
                f"adb shell ime set {ADBKEYBOARD_IME}"
            )
            return False
        try:
            self.logger.info(f"设备上未安装 ADBKeyBoard，使用本地 APK 安装: {apk_path}")
            if not self.install_apk(apk_path):
                self.logger.error("ADBKeyBoard 安装失败")
                return False
            self.enable_ime(ADBKEYBOARD_IME)
            self.set_default_ime(ADBKEYBOARD_IME)
            self._adbkeyboard_ok = True
            self.logger.info("ADBKeyBoard 已安装并设为默认输入法（恢复原输入法：adb shell ime set com.android.inputmethod.pinyin/.InputService）")
            return True
        except AdbError as e:
            self.logger.error(f"ADBKeyBoard 配置失败: {e}")
            return False

    def _find_bundled_apk(self) -> str:
        """打包资源目录 / 项目目录 / 当前目录下捆绑的 APK（整包分发，新机器免下载）。

        冻结（Nuitka）后 `__file__` 位于解包目录，`--include-data-files` 打进包里的 APK
        就在那里，所以两种模式都能找到；这里统一走 runtime_paths 以免遗漏 exe 旁边。
        （旧实现用"体积 > 50KB"判断，而捆绑的 APK 只有约 18KB，会误报"找不到 APK"。）
        """
        try:
            import runtime_paths
            p = runtime_paths.bundled_apk()
            if p is not None:
                return str(p)
            for name in ADBKEYBOARD_APK_NAMES:
                for base in (Path(__file__).resolve().parent, Path.cwd()):
                    cand = Path(base) / name
                    if runtime_paths.looks_like_apk(cand):
                        return str(cand)
        except Exception:  # noqa: BLE001 退回原始扫描
            pass
        for name in ADBKEYBOARD_APK_NAMES:
            for base in (Path(__file__).resolve().parent, Path.cwd()):
                p = Path(base) / name
                try:
                    if p.exists() and p.stat().st_size > 1024:
                        return str(p)
                except OSError:
                    continue
        return ""

    # ---------------------------------------------------- 自检辅助
    def diagnose(self) -> dict:
        """收集一份排障信息（供 --check / selftest 打印）。按平台给出对应字段。"""
        info: dict = {
            "platform": platform_tag(),
            "adb": self.adb_path,
            "serial": self.serial,
            "connected": False,
        }
        if IS_WINDOWS:
            info["wsa_installed"] = wsa_installed()
            info["wsa_port_registry"] = wsa_adb_port()
        else:
            info["waydroid"] = waydroid_present()
            info["launchers"] = available_launchers()
        if not self.is_connected():
            info["hint"] = self._connect_hint()
            return info
        info.update({
            "connected": True,
            "runtime": self.runtime_tag(),
            "is_wsa": self.is_wsa(),
            "android": self.android_version(),
            "sdk": self.android_sdk(),
            "screen": self.get_screen_size(),
            "focus": self.get_current_focus(),
            "ime": self.current_ime(),
            "ime_shown": self.ime_shown(),
            "adbkeyboard_ready": self._adbkeyboard_ready(),
            "clipboard_ok": self.probe_clipboard(),
            "clipboard_text": self.get_clipboard(),
        })
        if self._last_dump_detail:
            info["last_dump_error"] = self._last_dump_detail
        return info

    def dump_diagnostics(self) -> str:
        info = self.diagnose()
        lines = ["---- ADB 诊断 ----"]
        for k, v in info.items():
            lines.append(f"{k}: {v}")
        lines.append("-------------------")
        return "\n".join(lines)


def _silent_logger():
    import logging
    return logging.getLogger("adb-silent")
