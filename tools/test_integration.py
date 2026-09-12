# -*- coding: utf-8 -*-
"""集成测试：用"假 ADB 命令执行器"替换 subprocess，跑通 ADBController 的真实逻辑链，
不需要设备/模拟器/WSA，也不依赖文件系统。

覆盖：
  - 连接：没有在线设备时自动 connect WSA 端口（58526）；已有设备优先，不去抢端口
  - 启动：resolve-activity 输出带警告行时仍能解析；`am start -n` 失败后 monkey 兜底；
          前台判定兼容 Android 13 的 topResumedActivity
  - 文本注入：以输入框真实内容校验；明文被吞 → base64 救回；全失败 → 老实返回 False
  - 剪贴板：回读不一致（WSA 把宿主剪贴板内容读回来）时绝不按 KEYCODE_PASTE

用法：python tools/test_integration.py
"""
from __future__ import annotations

import base64
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import adb_controller as ac  # noqa: E402

RC = {"pass": 0, "fail": 0}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")
    RC["pass" if ok else "fail"] += 1


def chat_xml(input_text: str = "", send_visible: bool = True) -> str:
    send = ('<node class="android.widget.TextView" '
            'resource-id="com.xtc.watch:id/tv_send_view" text="发送" '
            'bounds="[920,1700][1060,1800]" />') if send_visible else ""
    return ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
            "<hierarchy rotation=\"0\">"
            "<node class=\"android.widget.FrameLayout\" bounds=\"[0,0][1080,1920]\">"
            f"<node class=\"android.widget.EditText\" "
            f"resource-id=\"com.xtc.watch:id/et_chat_text_content\" "
            f"text=\"{input_text}\" bounds=\"[40,1700][900,1800]\" focusable=\"true\" />"
            f"{send}</node></hierarchy>")


class FakeAdb:
    """把 ADBController._run 换成内存实现，并按 shell 命令文本分发。"""

    def __init__(self, connected: bool = True, broadcast: str = "ok",
                 clipboard: str = "ok", am_fail: bool = False,
                 focus: str = "com.xtc.watch/.MainActivity",
                 stale_clip: str = "STALE-HOST-CLIPBOARD",
                 listed: bool = True, sdk: int = 33):
        self.connected = connected
        self.broadcast = broadcast        # ok | dead
        self.clipboard = clipboard        # ok | broken
        self.am_fail = am_fail
        self.focus = focus
        self.stale_clip = stale_clip
        self.listed = listed              # 是否一开始就出现在 adb devices 里
        self.sdk = sdk
        self.adbkeyboard_installed = True  # 设备上是否已装 ADBKeyBoard
        self.launched = False             # 是否已成功启动过 App（决定前台是谁）
        self.connect_attempted = False    # 是否已被 connect 过
        self.calls: list[str] = []
        self.device_input = ""            # 模拟设备输入框内部状态
        self.device_clip = ""
        self.dump_text = ""               # 当前 UI dump 里输入框的内容

    # ---- 供控制器调用的接口
    def run(self, args, timeout=None, binary=False, check=True):
        self.calls.append(" ".join(args))
        args = list(args)
        if not args:
            return "", ""
        cmd = args[0]
        if cmd == "devices":
            # listed=False：设备尚未注册到 adb server（WSA 刚开机/重启后的真实情形）
            visible = self.connected and self.listed
            return ("List of devices attached\n127.0.0.1:58526\tdevice\n"
                    if visible else "List of devices attached\n"), ""
        if cmd == "connect":
            self.connect_attempted = True
            return (f"connected to {args[1]}\n" if self.connected else
                    f"cannot connect to {args[1]}: No connection could be made (10061)\n"), ""
        if cmd == "get-state":
            return "device\n", ""
        if cmd == "shell":
            return self._shell(" ".join(args[1:])), ""
        return "", ""

    def _shell(self, sh: str) -> str:
        if "uiautomator dump" in sh:
            self.dump_text = self.device_input
            return "UI hierchary dumped to: /sdcard/x.xml\n"
        if sh.startswith("cat "):
            return chat_xml(self.dump_text)
        if sh.startswith("rm -f "):
            return ""
        if "resolve-activity" in sh:
            # Android 13 真实输出：先警告行，结果在最后一行
            return ("Priority=0\nWARNING: com.xtc.watch is not exported\n"
                    "com.xtc.watch/.MainActivity\n")
        if "am start" in sh:
            if self.am_fail:
                self.calls.append("AM_FAIL")
                return "Error: Activity class {com.xtc.watch/} does not exist.\n"
            self.launched = True
            return "Starting: Intent { ... }\n"
        if sh.startswith("monkey "):
            self.launched = True
            return "Events injected: 1\n"
        if "dumpsys window" in sh or "dumpsys activity activities" in sh:
            cur = self.focus if self.launched else "com.android.systemui/.Launcher"
            return ("  mCurrentFocus=null\n  mFocusedApp=null\n"
                    f"  topResumedActivity=ActivityRecord{{d8e1 u0 {cur} t123}}\n")
        if "dumpsys input_method" in sh:
            return "  mInputShown=true\n"
        if "ADB_INPUT_TEXT" in sh:
            if self.broadcast == "dead":
                return "Broadcast completed: result=0\n"   # 静默失败：输入框收不到
            self.device_input = sh.split("--es msg", 1)[-1].strip().strip("'")
            return "Broadcast completed: result=0\n"
        if "ADB_INPUT_B64" in sh:
            if self.broadcast == "dead":
                return "Broadcast completed: result=0\n"
            raw = sh.split("--es msg", 1)[-1].strip()
            self.device_input = base64.b64decode(raw).decode("utf-8")
            return "Broadcast completed: result=0\n"
        if "ADB_INPUT_CHARS" in sh:
            if self.broadcast == "dead":
                return "Broadcast completed: result=0\n"
            raw = sh.split("--eia chars", 1)[-1].strip().strip("'")
            self.device_input = "".join(chr(int(c)) for c in raw.split(",") if c.strip())
            return "Broadcast completed: result=0\n"
        if "cmd clipboard set-text" in sh:
            if self.clipboard == "ok":
                self.device_clip = sh.split("set-text", 1)[-1].strip().strip("'").strip('"')
            return ""
        if "cmd clipboard get-text" in sh:
            return self.device_clip if self.clipboard == "ok" else self.stale_clip
        if sh.startswith("input keyevent 279") or "keyevent 279" in sh:
            # 模拟 KEYCODE_PASTE：把剪贴板内容粘进输入框
            self.device_input = self.device_clip
            return ""
        if "ime list -s" in sh:
            return ("com.android.adbkeyboard/.AdbIME\n"
                    if self.adbkeyboard_installed else "com.android.inputmethod.pinyin/.InputService\n")
        if "default_input_method" in sh or "enabled_input_methods" in sh:
            return ("com.android.adbkeyboard/.AdbIME\n"
                    if self.adbkeyboard_installed else "com.android.inputmethod.pinyin/.InputService\n")
        if "pm list packages" in sh:
            if "com.xtc.watch" in sh:
                return "package:com.xtc.watch\n"
            if "adbkeyboard" in sh:
                return ("package:com.android.adbkeyboard\n"
                        if self.adbkeyboard_installed else "")
            return ""
        if "wm size" in sh:
            return "Physical size: 1080x1920\n"
        if "getprop" in sh:
            return f"{self.sdk}\n"
        if sh.startswith("input text"):
            self.device_input = sh.split("input text", 1)[-1].strip().strip("'").replace("%s", " ")
            return ""
        return ""

    # ---- 断言辅助
    def did(self, needle: str) -> bool:
        return any(needle in c for c in self.calls)

    def count(self, needle: str) -> int:
        return sum(1 for c in self.calls if needle in c)


def make_controller(**kw) -> tuple[ac.ADBController, FakeAdb]:
    fake = FakeAdb(**{k: v for k, v in kw.items() if k in
                      ("connected", "broadcast", "clipboard", "am_fail", "focus",
                       "stale_clip", "listed", "sdk")})
    # ADBController.__init__ 会校验 adb 可执行文件存在，这里指到一个一定存在的程序；
    # 真正执行被 FakeAdb.run 顶掉了，不会真的启动它。
    import os
    os.environ["ADB_PATH"] = sys.executable or "python"
    ctl = ac.ADBController(adb_path=os.environ["ADB_PATH"], port=5555,
                           input_retries=int(kw.get("input_retries", 1)))
    ctl._run = fake.run  # type: ignore[method-assign]
    return ctl, fake


# ------------------------------------------------------------------ 用例
def test_connect_attempted_when_devices_empty() -> None:
    """adb devices 为空时，连接候选端口必须从 WSA 端口 58526 开始。"""
    ctl, fake = make_controller(connected=True, listed=False)
    ctl.serial = ""
    ok = ctl.connect()
    check("connect 成功并采用目标设备", ok and ctl.serial == "127.0.0.1:58526",
          f"serial={ctl.serial}")
    conns = [c for c in fake.calls if c.startswith("connect")]
    check("首个 connect 目标是 58526", conns and conns[0].endswith("58526"), str(conns[:3]))


def test_adopt_already_listed_device() -> None:
    """设备已在 adb devices 里（模拟器常见）→ 直接采用，不做多余 connect。"""
    ctl, fake = make_controller(connected=True, listed=True)
    ctl.serial = ""
    ok = ctl.ensure_connected(retries=1)
    check("采用已在线设备", ok and ctl.serial == "127.0.0.1:58526", ctl.serial)
    check("没有多余 connect", not fake.did("connect "), str(fake.calls))


def test_existing_device_wins() -> None:
    """已显式指定设备（config serial）时不得再去 connect 抢端口。"""
    ctl, fake = make_controller()
    ctl.serial = "emulator-5554"
    ok = ctl.ensure_connected(retries=1)
    check("已有设备直接采用", ok and ctl.serial == "emulator-5554", ctl.serial)
    check("没有发出 connect", not fake.did("connect "), str(fake.calls))


def test_no_device_error_hint() -> None:
    ctl, _ = make_controller(connected=False)
    ctl.serial = ""
    try:
        ctl.ensure_connected(retries=1)
        check("无设备时抛 AdbError", False, "没有抛异常")
    except ac.AdbError as e:
        check("无设备时报错含端口提示", "已尝试端口" in str(e), str(e).splitlines()[0])
    check("候选端口包含 WSA 与模拟器端口",
          58526 in ctl._port_candidates() and 5555 in ctl._port_candidates())


def test_focus_android13() -> None:
    ctl, fake = make_controller()
    ctl.serial = "127.0.0.1:58526"
    fake.launched = True
    check("前台识别（topResumedActivity）",
          ctl.get_current_focus() == "com.xtc.watch/.MainActivity")
    check("is_in_foreground", ctl.is_in_foreground("com.xtc.watch"))


def test_launch_activity_parse_and_confirm() -> None:
    ctl, _ = make_controller(am_fail=False)
    ctl.serial = "127.0.0.1:58526"
    act = ctl.resolve_launcher_activity("com.xtc.watch")
    check("resolve-activity 跳过警告行", act == ".MainActivity", repr(act))
    used = ctl.launch_app("com.xtc.watch", "", wait=3, attempts=1)
    check("am start -n 启动并确认前台", used == ".MainActivity", repr(used))


def test_launch_monkey_fallback() -> None:
    """WSA 上 am start 失败是"启动失败"的主因之一，monkey 必须能救回来。"""
    ctl, fake = make_controller(am_fail=True)
    ctl.serial = "127.0.0.1:58526"
    used = ctl.launch_app("com.xtc.watch", "", wait=3, attempts=1)
    check("am start 失败后 monkey 兜底成功", bool(used), f"used={used!r}")
    check("确实尝试过 monkey", fake.did("monkey -p com.xtc.watch"))
    check("确实尝试过 am start", fake.did("am start -n com.xtc.watch/.MainActivity"))


def test_launch_missing_package() -> None:
    ctl, fake = make_controller()
    ctl.serial = "127.0.0.1:58526"
    orig = fake._shell

    def shell(sh: str) -> str:
        if "pm list packages" in sh:
            return ""
        return orig(sh)

    fake._shell = shell  # type: ignore[assignment]
    try:
        ctl.launch_app("com.not.installed", "", wait=1, attempts=1)
        check("未安装包时抛 AdbError", False, "没有抛异常")
    except ac.AdbError as e:
        check("未安装包时给出明确错误", "没有安装" in str(e), str(e)[:60])


def test_input_verified_by_inputbox() -> None:
    ctl, fake = make_controller(broadcast="ok")
    ctl.serial = "127.0.0.1:58526"
    ok = ctl.input_text("你好世界", verify=lambda: fake.device_input == "你好世界")
    check("广播注入成功（以输入框内容为准）", ok is True)
    check("设备输入框内容正确", fake.device_input == "你好世界", repr(fake.device_input))


def test_input_plain_swallowed_b64_rescue() -> None:
    ctl, fake = make_controller(broadcast="dead")
    ctl.serial = "127.0.0.1:58526"
    ok = ctl.input_text("你好世界", verify=lambda: fake.device_input == "你好世界")
    check("明文被吞后 base64 兜底成功", ok is True)
    check("最终输入框内容正确", fake.device_input == "你好世界", repr(fake.device_input))
    check("明文与 base64 都试过",
          fake.did("ADB_INPUT_TEXT") and fake.did("ADB_INPUT_B64"))


def test_input_all_channels_dead() -> None:
    """全部广播失败 + 剪贴板回读不一致 → 必须返回 False，且绝不粘贴宿主旧内容。"""
    ctl, fake = make_controller(broadcast="dead", clipboard="broken")
    ctl.serial = "127.0.0.1:58526"
    ok = ctl.input_text("你好世界", verify=lambda: fake.device_input == "你好世界")
    check("全部通道失败时返回 False（不再谎报成功）", ok is False)
    check("回读不一致时不发 KEYCODE_PASTE", not fake.did("keyevent 279"),
          str(fake.calls[-6:]))
    check("做了剪贴板回读校验", fake.did("cmd clipboard get-text"))
    check("输入框没有被塞进宿主剪贴板的旧内容",
          fake.stale_clip not in fake.device_input, repr(fake.device_input))


def test_input_clipboard_confirmed_then_paste() -> None:
    ctl, fake = make_controller(broadcast="dead", clipboard="ok")
    ctl.serial = "127.0.0.1:58526"
    ok = ctl.input_text("hello-wsa", verify=lambda: fake.device_input == "hello-wsa")
    check("剪贴板确认一致后允许粘贴", ok is True)
    check("剪贴板写入了目标文本", fake.device_clip == "hello-wsa", repr(fake.device_clip))
    check("确实执行了粘贴", fake.did("keyevent 279"))


def test_input_ascii_only_fallback() -> None:
    """广播/剪贴板都不可用时，ASCII 走 input text 兜底。"""
    ctl, fake = make_controller(broadcast="dead", clipboard="broken")
    ctl.serial = "127.0.0.1:58526"
    ok = ctl.input_text("hello world", verify=lambda: fake.device_input == "hello world")
    check("ASCII 走 input text 兜底", ok is True)
    check("使用了 input text 且空格转 %s",
          fake.did("input text 'hello%sworld'"), str(fake.calls[-3:]))


def test_input_skips_clipboard_when_unsupported() -> None:
    """设备剪贴板不可用（SDK<29）时不应尝试剪贴板，也不应粘贴。"""
    ctl, fake = make_controller(broadcast="dead", sdk=28)
    ctl.serial = "127.0.0.1:58526"
    ctl.input_text("你好", verify=lambda: False)
    check("低版本不尝试剪贴板", not fake.did("cmd clipboard set-text"))
    check("低版本不粘贴", not fake.did("keyevent 279"))


def test_clear_residue_before_send() -> None:
    """聊天输入框有残留时应能被读到（xiaotiancai 层据此清空后再输入）。"""
    ctl, fake = make_controller(broadcast="ok")
    ctl.serial = "127.0.0.1:58526"
    fake.device_input = "残留内容"
    root = ET.fromstring(chat_xml(fake.device_input))
    edits = ctl.find_elements(root, class_name="EditText")
    check("能读到残留输入框内容", bool(edits) and edits[0].get("text") == "残留内容",
          edits[0].get("text") if edits else "(无)")


def test_diagnose_fields() -> None:
    ctl, _ = make_controller(clipboard="broken")
    ctl.serial = "127.0.0.1:58526"
    info = ctl.diagnose()
    check("诊断包含关键字段",
          {"adb", "serial", "connected", "ime", "clipboard_ok"} <= set(info),
          str(sorted(info)))
    check("识别剪贴板不可用", info.get("clipboard_ok") is False)
    check("识别当前输入法", "adbkeyboard" in (info.get("ime") or ""))


def test_adbkeyboard_already_installed_skips_install() -> None:
    """设备上已有 ADBKeyBoard → 不装 APK，只确保它是默认输入法。"""
    ctl, fake = make_controller()
    ctl.serial = "127.0.0.1:58526"
    fake.adbkeyboard_installed = True
    ok = ctl.install_adbkeyboard()
    check("已安装时返回 True", ok is True)
    check("没有执行 adb install", not fake.did("install -r"), str(fake.calls))
    check("没有联网下载动作（无 urllib/网络）",
          not any("http" in c for c in fake.calls), str(fake.calls[:3]))


def test_adbkeyboard_installs_from_local_apk() -> None:
    """设备上没有 ADBKeyBoard → 只用项目目录里的本地 APK 安装。"""
    ctl, fake = make_controller()
    ctl.serial = "127.0.0.1:58526"
    fake.adbkeyboard_installed = False
    installed_paths: list[str] = []
    ctl.install_apk = lambda p: (installed_paths.append(p), True)[1]  # type: ignore
    ctl._find_bundled_apk = lambda: str(Path("keyboardservice-debug.apk"))  # type: ignore
    ok = ctl.install_adbkeyboard()
    check("未安装时用本地 APK 安装成功", ok is True)
    check("安装的是项目内的本地 APK",
          installed_paths and installed_paths[0].endswith("keyboardservice-debug.apk"),
          str(installed_paths))
    check("安装后设为默认输入法", fake.did("ime set com.android.adbkeyboard/.AdbIME"))


def test_adbkeyboard_missing_local_apk_no_remote() -> None:
    """设备没装、项目里也没 APK → 返回 False 并提示放本地文件，绝不联网。"""
    ctl, fake = make_controller()
    ctl.serial = "127.0.0.1:58526"
    fake.adbkeyboard_installed = False
    ctl._find_bundled_apk = lambda: ""  # type: ignore
    ok = ctl.install_adbkeyboard()
    check("缺本地 APK 时返回 False（不联网）", ok is False)
    check("没有执行 adb install", not fake.did("install -r"), str(fake.calls))
    check("没有 _download_adbkeyboard 方法", not hasattr(ctl, "_download_adbkeyboard"))


def main() -> int:
    for fn in (test_connect_attempted_when_devices_empty, test_adopt_already_listed_device,
               test_existing_device_wins,
               test_no_device_error_hint, test_focus_android13,
               test_launch_activity_parse_and_confirm, test_launch_monkey_fallback,
               test_launch_missing_package, test_input_verified_by_inputbox,
               test_input_plain_swallowed_b64_rescue, test_input_all_channels_dead,
               test_input_clipboard_confirmed_then_paste, test_input_ascii_only_fallback,
               test_input_skips_clipboard_when_unsupported, test_clear_residue_before_send,
               test_diagnose_fields, test_adbkeyboard_already_installed_skips_install,
               test_adbkeyboard_installs_from_local_apk,
               test_adbkeyboard_missing_local_apk_no_remote):
        print(f"--- {fn.__name__} ---")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            check(f"{fn.__name__} 未抛异常", False, f"{type(e).__name__}: {e}")
    print(f"\n===== 集成测试：{RC['pass']} 通过 / {RC['fail']} 失败 =====")
    return 1 if RC["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
