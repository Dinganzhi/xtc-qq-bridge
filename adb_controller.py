# -*- coding: utf-8 -*-
"""ADB 控制层：封装 LDPlayer 自带 adb.exe（subprocess），提供连接/点击/滑动/
中文输入/截图/UI 树解析等能力。

设计要点：
- adb 查找顺序：config 指定路径 → 环境变量 ADB_PATH → 常见 LDPlayer 安装目录
  （D:/C: 盘的 leidian/LDPlayer 系列）→ PATH 中的 adb。
- Windows 下隐藏控制台窗口（CREATE_NO_WINDOW）。
- 中文输入策略链：
    1) ADBKeyBoard IME（广播 ADB_INPUT_TEXT，最可靠，可自动下载安装）
    2) cmd clipboard set-text + KEYCODE_PASTE（Android 10+，部分设备/镜像不可用）
    3) input text（仅 ASCII 兜底）
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


class AdbError(RuntimeError):
    pass


# 常见 LDPlayer 安装目录（内含 adb.exe）
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

ADBKEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
# APK 现在托管在 GitHub Releases（v2.5-dev，文件名 keyboardservice-debug.apk）。
# 依次尝试：官方直链 → 镜像站 → 旧仓库路径兜底。
ADBKEYBOARD_APK_URLS = [
    "https://github.com/senzhk/ADBKeyBoard/releases/download/v2.5-dev/keyboardservice-debug.apk",
    "https://gh.beimengvv.xyz/https://github.com/senzhk/ADBKeyBoard/releases/download/v2.5-dev/keyboardservice-debug.apk",
    "https://raw.githubusercontent.com/senzhk/ADBKeyBoard/master/ADBKeyBoard.apk",
    "https://raw.githubusercontent.com/senzhk/ADBKeyBoard/main/ADBKeyBoard.apk",
]
ADBKEYBOARD_MANUAL_URL = "https://github.com/senzhk/ADBKeyBoard/releases"


def find_adb(explicit: str = "") -> str:
    if explicit and Path(explicit).exists():
        return explicit
    env = os.environ.get("ADB_PATH", "")
    if env and Path(env).exists():
        return env
    for d in _LDPLAYER_DIRS:
        p = Path(d) / "adb.exe"
        if p.exists():
            return str(p)
    for p in _MUMU_ADB_CANDIDATES:
        if os.path.exists(p):
            return p
    w = shutil.which("adb")
    if w:
        return w
    raise AdbError("未找到 adb.exe：请在 config.yaml 的 adb.path 指定，或设置环境变量 ADB_PATH，或将 adb 加入 PATH")


class ADBController:
    def __init__(self, adb_path: str = "", host: str = "127.0.0.1", port: int = 5555,
                 serial: str = "", timeout: float = 30.0, logger=None):
        self.adb_path = find_adb(adb_path)
        self.host = host
        self.port = port
        self.serial = serial
        self.timeout = timeout
        self.logger = logger or _silent_logger()
        self._sdk: int | None = None
        self._clipboard_ok: bool | None = None
        self._adbkeyboard_ok: bool | None = None
        # uiautomator 同一时间只允许一个连接：跨线程串行化 dump，
        # 避免轮询线程与发送线程同时 dump 导致 "already registered"。
        self._dump_lock = threading.Lock()

    # ------------------------------------------------------------------ 基础
    def _base(self) -> list[str]:
        return [self.adb_path, "-s", self.serial] if self.serial else [self.adb_path]

    def _run(self, args: list, timeout: float | None = None, binary: bool = False):
        cmd = self._base() + args
        kwargs: dict = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout or self.timeout)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.run(cmd, **kwargs)
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb 命令超时: {' '.join(cmd)}") from e
        out = proc.stdout if binary else proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise AdbError(f"adb 命令失败({proc.returncode}): {' '.join(cmd)}\n"
                           f"{err.strip()}\n{str(out)[:500]}")
        return out, err

    def shell(self, cmd: str, timeout: float | None = None) -> str:
        out, _ = self._run(["shell", cmd], timeout=timeout)
        return out

    # ------------------------------------------------------------------ 连接
    def connect(self) -> bool:
        out, _ = self._run(["connect", f"{self.host}:{self.port}"], timeout=15)
        ok = "connected" in out.lower()
        if ok and not self.serial:
            self.serial = self._pick_serial()
        return ok

    def devices(self) -> list[str]:
        out, _ = self._run(["devices"], timeout=15)
        serials = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials

    def _pick_serial(self) -> str:
        serials = self.devices()
        if not serials:
            raise AdbError("没有在线设备：请先启动模拟器（并确认 ADB 调试已开启）")
        # 优先 adb server 原生注册的 emulator-XXXX（MuMu/雷电都会出现
        # emulator-5554 与 127.0.0.1:5555 之类的重复连接，选原生即可）
        for s in serials:
            if s.startswith("emulator-"):
                return s
        if len(serials) > 1:
            raise AdbError(f"检测到多个在线设备 {serials}，请在 config.yaml 的 adb.serial 指定一个")
        return serials[0]

    def is_connected(self) -> bool:
        try:
            if self.serial:
                out, _ = self._run(["get-state"], timeout=10)
                return out.strip().lower() == "device"
            serials = self.devices()
            if serials:
                self.serial = self._pick_serial()  # 自动选定设备，避免后续命令多设备报错
                return True
            return False
        except AdbError:
            return False

    def ensure_connected(self, retries: int = 3, delay: float = 2.0) -> bool:
        for i in range(retries):
            if self.is_connected():
                return True
            try:
                self.logger.info(f"尝试连接 {self.host}:{self.port} (第 {i + 1} 次)")
                self.connect()
            except AdbError as e:
                self.logger.warning(f"连接失败: {e}")
            if i < retries - 1:
                time.sleep(delay)
        raise AdbError(f"ADB 连接失败（{self.host}:{self.port}），请确认模拟器已启动")

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

    def get_screen_size(self) -> tuple[int, int]:
        out = self.shell("wm size")
        m = re.search(r"(\d+)x(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
        raise AdbError(f"无法获取屏幕尺寸: {out.strip()}")

    def get_current_focus(self) -> str:
        """返回当前前台组件，如 'com.xtc.watch/com.xtc.watch.MainActivity'；无则 ''。"""
        try:
            out = self.shell("dumpsys window", timeout=20)
        except AdbError:
            return ""
        for line in out.splitlines():
            if "mCurrentFocus" in line and "null" not in line:
                m = re.search(r"Window\{[^}]+\s+u\d\s+([^\s}]+)", line)
                if m:
                    return m.group(1)
        return ""

    # ------------------------------------------------------------------ 操作
    def tap(self, x: int | float, y: int | float) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")

    def swipe(self, x1: int | float, y1: int | float,
              x2: int | float, y2: int | float, duration_ms: int = 300) -> None:
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    def keyevent(self, code: int) -> None:
        self.shell(f"input keyevent {int(code)}")

    def launch_app(self, package: str, activity: str = "") -> str:
        if not activity:
            activity = self._resolve_launcher_activity(package)
        self.shell(f"am start -n {package}/{activity}")
        return activity

    def _resolve_launcher_activity(self, package: str) -> str:
        try:
            out = self.shell(f"cmd package resolve-activity --brief {package}")
            for line in reversed(out.splitlines()):
                line = line.strip()
                if "/" in line and line.split("/", 1)[0] == package:
                    return line.split("/", 1)[1]
        except AdbError:
            pass
        return ".MainActivity"

    def wait_for_activity(self, package: str, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.get_current_focus().startswith(package):
                return True
            time.sleep(1)
        return False

    def screenshot(self, path: str | None = None) -> bytes:
        out, _ = self._run(["exec-out", "screencap", "-p"], timeout=60, binary=True)
        if not out:
            raise AdbError("screencap 返回空数据")
        if path:
            Path(path).write_bytes(out)
        return out

    # ------------------------------------------------------------------ UI 解析
    def dump_ui(self, retries: int = 3, delay: float = 2.0) -> ET.Element:
        """uiautomator dump 并解析为 XML 树。线程安全（串行化），首次 dump 偶发失败自动重试。"""
        with self._dump_lock:
            return self._dump_ui_locked(retries, delay)

    def _dump_ui_locked(self, retries: int, delay: float) -> ET.Element:
        last: Exception | None = None
        for i in range(retries):
            try:
                self.shell("uiautomator dump /sdcard/xtc_dump.xml", timeout=60)
                out = self.shell("cat /sdcard/xtc_dump.xml", timeout=30)
                if out.strip():
                    return ET.fromstring(out)
                last = AdbError("dump 输出为空")
            except (AdbError, ET.ParseError) as e:
                last = e
            if i < retries - 1:
                time.sleep(delay)
        raise AdbError(f"UI dump 失败: {last}")

    @staticmethod
    def _node_matches(node, resource_id=None, text=None, class_name=None,
                      content_desc=None, text_contains=False) -> bool:
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
            if content_desc not in node.get("content-desc", ""):
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

    # ------------------------------------------------------------------ 中文输入
    def input_text(self, text: str) -> bool:
        """策略链：ADBKeyBoard 广播（明文直发）→ cmd clipboard+粘贴 → input text(ASCII)。

        实测 ADBKeyBoard v2.5-dev（keyboardservice-debug.apk）不对 msg 做 URL 解码，
        必须明文直发；旧版 ADBKeyBoard.apk 会解码，明文同样兼容。
        """
        if not text:
            return True
        if self._adbkeyboard_ready():
            # 必须确保 ADBKeyBoard 是当前（默认）输入法，否则广播无人接收
            if not self._adbkeyboard_active():
                self.logger.info("ADBKeyBoard 不是当前输入法，正在切换...")
                self.set_default_ime(ADBKEYBOARD_IME)
                time.sleep(1.0)
            self.shell(f"am broadcast -a ADB_INPUT_TEXT --es msg {self._sh_quote(text)}")
            return True
        if self._clipboard_ready():
            self.shell(f"cmd clipboard set-text {self._sh_quote(text)}")
            self.keyevent(279)  # KEYCODE_PASTE
            return True
        if text.isascii():
            self.shell(f"input text {self._sh_quote(text.replace(' ', '%s'))}")
            return True
        self.logger.warning("当前设备不支持中文注入：请先安装/启用 ADBKeyBoard（自动安装失败时可手动安装）")
        return False

    @staticmethod
    def _sh_quote(s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"

    def _adbkeyboard_ready(self) -> bool:
        if self._adbkeyboard_ok is None:
            try:
                out = self.shell("ime list -s")
                self._adbkeyboard_ok = ADBKEYBOARD_IME in out
            except AdbError:
                self._adbkeyboard_ok = False
        return self._adbkeyboard_ok

    def _adbkeyboard_active(self) -> bool:
        """ADBKeyBoard 是否为当前默认输入法（仅启用不够——广播需要它是活动 IME）。"""
        try:
            out = self.shell("settings get secure default_input_method")
            return ADBKEYBOARD_IME in out
        except AdbError:
            return False

    def _clipboard_ready(self) -> bool:
        if self._clipboard_ok is None:
            if self.android_sdk() < 29:
                self._clipboard_ok = False
            else:
                try:
                    self.shell("cmd clipboard set-text probe-xtc", timeout=10)
                    self._clipboard_ok = True
                except AdbError:
                    self._clipboard_ok = False
        return self._clipboard_ok

    def install_apk(self, apk_path: str) -> bool:
        out, _ = self._run(["install", "-r", apk_path], timeout=120)
        return "success" in out.lower()

    def enable_ime(self, ime_id: str) -> None:
        self.shell(f"ime enable {ime_id}")

    def set_default_ime(self, ime_id: str) -> None:
        """设为默认输入法：ime set + 直接写 settings（部分镜像 ime set 不生效）。"""
        try:
            self.shell(f"ime set {ime_id}")
        except AdbError:
            pass
        self.shell(f"settings put secure default_input_method {ime_id}")

    def install_adbkeyboard(self, apk_path: str = "") -> bool:
        """安装并启用 ADBKeyBoard（若已启用则确保其为默认输入法）。失败返回 False，不抛出。
        APK 来源优先级：本地捆绑包 → 在线下载。"""
        if self._adbkeyboard_ready():
            if not self._adbkeyboard_active():
                self.set_default_ime(ADBKEYBOARD_IME)
            return True
        if not apk_path:
            apk_path = self._find_bundled_apk()
        if not apk_path:
            apk_path = self._download_adbkeyboard()
        if not apk_path:
            self.logger.warning(
                f"未获取到 ADBKeyBoard.apk（网络不可用或下载源失效）。"
                f"手动安装：访问 {ADBKEYBOARD_MANUAL_URL} 下载 APK 后执行 "
                f"adb install -r ADBKeyBoard.apk && adb shell ime enable {ADBKEYBOARD_IME} && "
                f"adb shell ime set {ADBKEYBOARD_IME}"
            )
            return False
        try:
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
        """项目目录/当前目录下捆绑的 APK（方便整包分发，新机器免下载）。"""
        for name in ("keyboardservice-debug.apk", "ADBKeyBoard.apk"):
            for base in (Path(__file__).resolve().parent, Path.cwd()):
                p = Path(base) / name
                try:
                    if p.exists() and p.stat().st_size > 50_000:
                        return str(p)
                except OSError:
                    continue
        return ""

    def _download_adbkeyboard(self) -> str:
        tmp = Path(os.environ.get("TEMP", ".")) / "ADBKeyBoard.apk"
        for url in ADBKEYBOARD_APK_URLS:
            try:
                self.logger.info(f"下载 ADBKeyBoard: {url}")
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    tmp.write_bytes(r.read())
                if tmp.stat().st_size > 100_000:
                    return str(tmp)
            except Exception as e:
                self.logger.warning(f"下载失败({url}): {e}")
        return ""


def _silent_logger():
    import logging
    return logging.getLogger("adb-silent")
