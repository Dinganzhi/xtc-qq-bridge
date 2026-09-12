# -*- coding: utf-8 -*-
"""离线单元测试：不需要设备，验证 WSA 适配相关的**纯逻辑**
（序列号识别 / activity 解析 / 前台解析 / 端口顺序 / shell 转义）。

设备行为相关的策略链测试见 tools/test_integration.py（用假 ADB 执行器）。
用法：python tools/test_wsa.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 控制台默认 GBK，中文断言信息会乱码/报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import adb_controller as ac  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")
    if not ok:
        FAILED.append(name)


def _stub(port: int = 5555, wsa_port: int = 0, extra_ports=None) -> ac.ADBController:
    """不调用 __init__（避免找 adb.exe），只造一个够测纯逻辑的实例。"""
    ctl = object.__new__(ac.ADBController)
    ctl.adb_path = "adb"
    ctl.host = "127.0.0.1"
    ctl.port = port
    ctl.serial = ""
    ctl.timeout = 5.0
    ctl.logger = ac._silent_logger()
    ctl.extra_ports = list(extra_ports or [])
    ctl.wsa_port = wsa_port
    ctl.input_retries = 1
    return ctl


# ---------------------------------------------------------------- 纯函数
def test_wsa_serial() -> None:
    check("WSA 序列号识别", ac._is_wsa_serial("127.0.0.1:58526"))
    check("WSA 序列号识别(localhost)", ac._is_wsa_serial("localhost:58526"))
    check("WSA 序列号识别(IPv6)", ac._is_wsa_serial("[::1]:58526"))
    check("普通模拟器端口不算 WSA", not ac._is_wsa_serial("127.0.0.1:5555"))
    check("MuMu 端口不算 WSA", not ac._is_wsa_serial("127.0.0.1:16384"))
    check("emulator-XXXX 不算 WSA", not ac._is_wsa_serial("emulator-5554"))
    check("空序列号不算 WSA", not ac._is_wsa_serial(""))


def test_parse_resolved_activity() -> None:
    ctl = _stub()
    out = ("Priority=0\n"
           "WARNING: Activity not exported\n"
           "com.xtc.watch/.MainActivity\n")
    act = ctl._parse_resolved_activity(out, "com.xtc.watch")
    check("解析 resolve-activity（跳过警告行）", act == ".MainActivity", repr(act))

    out2 = "Error: Activity class {com.xtc.watch/} does not exist.\n"
    act2 = ctl._parse_resolved_activity(out2, "com.xtc.watch")
    check("解析失败时返回空串", act2 == "", repr(act2))

    out3 = ("priority=0 preferredOrder=0 match=0x108000 specificIndex=-1\n"
            "  com.xtc.watch/com.xtc.watch.ui.SplashActivity\n")
    act3 = ctl._parse_resolved_activity(out3, "com.xtc.watch")
    check("解析带缩进的输出", act3 == "com.xtc.watch.ui.SplashActivity", repr(act3))

    other = "com.other.app/.MainActivity\n"
    check("不匹配的包名不采纳", ctl._parse_resolved_activity(other, "com.xtc.watch") == "",
          repr(ctl._parse_resolved_activity(other, "com.xtc.watch")))


def test_parse_focus() -> None:
    old = ("  mCurrentFocus=Window{3f2e1 u0 com.xtc.watch/com.xtc.watch.MainActivity}\n"
           "  mFocusedApp=AppWindowToken{abc token=Token{xyz ActivityRecord{1 u0 "
           "com.xtc.watch/.MainActivity t42}}}\n")
    focus = ac.ADBController._parse_focus(old)
    check("Android 12- mCurrentFocus 解析", focus == "com.xtc.watch/com.xtc.watch.MainActivity",
          repr(focus))

    new = ("  mCurrentFocus=null\n"
           "  mFocusedApp=null\n"
           "  topResumedActivity=ActivityRecord{d8e1 u0 com.xtc.watch/.MainActivity t123}\n")
    focus2 = ac.ADBController._parse_focus(new)
    check("Android 13+ topResumedActivity 解析（WSA 关键路径）",
          focus2 == "com.xtc.watch/.MainActivity", repr(focus2))

    check("空 dump 返回空串", ac.ADBController._parse_focus("") == "")
    check("全是 null 返回空串",
          ac.ADBController._parse_focus("mCurrentFocus=null\nmFocusedApp=null\n") == "")


def test_pm_dump_fallback() -> None:
    ctl = _stub()
    dump = ("  Activity Resolver Table:\n"
            "    com.xtc.watch/.MainActivity filter 1234\n"
            "      Action: \"android.intent.action.MAIN\"\n"
            "      Category: \"android.intent.category.LAUNCHER\"\n")
    ctl.try_shell = lambda cmd, timeout=None: dump if "resolve-activity" not in cmd else ""  # type: ignore
    act = ctl.resolve_launcher_activity("com.xtc.watch")
    check("pm dump 兜底解析 launcher activity", act == ".MainActivity", repr(act))


def test_shell_quote() -> None:
    q = ac.ADBController._sh_quote("a'b")
    check("shell 单引号转义", q == "'a'\\''b'", q)
    check("含空格文本整体带引号", ac.ADBController._sh_quote("hello world") == "'hello world'")


def test_port_candidates_order() -> None:
    ctl = _stub(port=5555, wsa_port=58526)
    cands = ctl._port_candidates()
    check("WSA 端口优先于模拟器端口",
          cands.index(58526) < cands.index(5555), str(cands))
    check("候选端口去重", len(cands) == len(set(cands)), str(cands))

    ctl2 = _stub(port=5555, wsa_port=0, extra_ports=[62001])
    cands2 = ctl2._port_candidates()
    check("extra_ports 生效且排在常见模拟器端口前",
          cands2.index(62001) < cands2.index(16384), str(cands2))


def test_pick_serial_priority() -> None:
    ctl = _stub()
    check("多设备优先 WSA",
          ctl._pick_serial(["emulator-5554", "127.0.0.1:58526"]) == "127.0.0.1:58526")
    check("无 WSA 时优先 emulator-XXXX",
          ctl._pick_serial(["127.0.0.1:5555", "emulator-5554"]) == "emulator-5554")
    check("单设备直接采用", ctl._pick_serial(["127.0.0.1:5555"]) == "127.0.0.1:5555")
    try:
        ctl._pick_serial(["127.0.0.1:5555", "127.0.0.1:16384"])
        check("多个非 WSA 设备时报错提示指定 serial", False, "没有抛异常")
    except ac.AdbError as e:
        check("多个非 WSA 设备时报错提示指定 serial", "指定" in str(e), str(e)[:40])


def main() -> int:
    for fn in (test_wsa_serial, test_parse_resolved_activity, test_parse_focus,
               test_pm_dump_fallback, test_shell_quote, test_port_candidates_order,
               test_pick_serial_priority):
        print(f"--- {fn.__name__} ---")
        fn()
    print(f"\n===== 离线测试：{len(FAILED)} 失败 =====")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
