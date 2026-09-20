# -*- coding: utf-8 -*-
"""在真实 WSA 设备上跑一遍桥接的关键路径（**只读/可回退操作，不发真实消息**）。

检查项：连接/诊断、UI dump、登录态三态、弹窗清理、聊天页判定、打开聊天、
联系人匹配、读取最新消息、文本注入（输入后清空，不点发送）、消息库归档。
用法：python tools/live_probe.py [--contact 昵称] [--config config.yaml]
"""
from __future__ import annotations

import argparse
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from adb_controller import ADBController, AdbError  # noqa: E402
from utils.logger import setup_logger  # noqa: E402
from xiaotiancai import Xiaotiancai  # noqa: E402

RC = {"pass": 0, "fail": 0, "skip": 0}


def check(name: str, ok, detail: str = "") -> None:
    tag = "PASS" if ok is True else ("SKIP" if ok is None else "FAIL")
    RC[{"PASS": "pass", "FAIL": "fail", "SKIP": "skip"}[tag]] += 1
    print(f"[{tag}] {name}  {detail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--contact", default="")
    ap.add_argument("--adb", default="")
    ap.add_argument("--serial", default="")
    args = ap.parse_args()

    contact = args.contact
    adb_path, serial = args.adb, args.serial
    if Path(args.config).exists():
        try:
            import yaml
            cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
            contact = contact or ((cfg.get("target") or {}).get("xtc_contact") or "")
            adb_path = adb_path or ((cfg.get("adb") or {}).get("path") or "")
            serial = serial or ((cfg.get("adb") or {}).get("serial") or "")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 读配置失败: {e}")
    log = setup_logger(level="INFO", console=True)
    print(f"===== 实机探测：contact={contact!r} serial={serial or '(自动)'} =====\n")

    try:
        adb = ADBController(adb_path=adb_path, serial=serial, logger=log)
    except AdbError as e:
        check("查找 adb", False, str(e))
        return 1

    # 1) 连接与设备
    check("ADB 连接", adb.ensure_connected(), adb.device_summary())
    if not adb.is_connected():
        check("设备在线", False, "没有在线设备")
        return 1
    check("设备在线", True, f"serial={adb.serial} 屏幕={adb.get_screen_size()}")

    # 2) 窗口/屏幕/焦点（null root 场景的两个前提）
    adb.keep_awake()          # 与 main.py 启动时一致：保持常亮，避免息屏后读不到界面
    act = adb.get_current_activity()
    win = adb.get_current_focus()
    check("Activity 解析", bool(act), f"activity={act or '(空)'}")
    check("窗口焦点", adb.has_focus_window(), f"window={win or '(无焦点窗口)'}")
    if adb.screen_on() is not True:
        print("[INFO] 屏幕当前是息屏状态（已请求保持常亮；读取界面时会自动唤醒）")

    # 2.5) 与真实启动顺序一致：确保小天才 App 在前台（桥接启动时也会做这一步）
    xtc = Xiaotiancai(adb, {"ui": {}}, logger=log)
    fg = adb.is_in_foreground(xtc.package)
    check("App 在前台", True if fg else xtc.launch(),
          f"启动前={act or '(空)'} -> 现在={xtc.current_activity() or '(空)'}")

    # 3) UI dump（快路径/多目录/exec-out）
    t0 = time.time()
    root = None
    try:
        root = adb.dump_ui(retries=2, delay=0.4)
        check("UI dump", True,
              f"{len(list(root.iter('node')))} 节点，{time.time() - t0:.1f}s，策略={adb._dump_strategy}")
    except AdbError as e:
        check("UI dump", False, f"{e}")
    check("ADBKeyBoard", adb.adbkeyboard_ready(), f"ime={adb.current_ime() or '(未知)'}")
    # WSA 与宿主共享剪贴板，clipboard_ok=False 是**预期**（中文输入靠 ADBKeyBoard 广播），
    # 所以这里只作信息展示，不算失败。
    print(f"[INFO] 剪贴板通道 clipboard_ok={adb.probe_clipboard()}"
          f"（WSA 上通常为 False，属预期；不影响中文输入）")

    # 4) 登录态三态（本次修复重点）
    state = xtc.login_state(force=True)
    check("登录态三态判定", state in ("logged_in", "not_logged_in", "unknown"),
          f"state={state} activity={xtc.current_activity()}")
    check("is_logged_in 与三态一致",
          xtc.is_logged_in(force=True) == (state == "logged_in"),
          f"is_logged_in={xtc.is_logged_in(force=True)} state={state}")

    # 5) 弹窗清理（只会在识别到弹窗时点击）
    try:
        handled = xtc.settle()
        check("弹窗清理", True, f"handled={handled}")
    except AdbError as e:
        check("弹窗清理", False, str(e))

    # 6) 聊天页判定 / 打开聊天（未登录时跳过，避免把"登录页"误判成失败）
    in_chat = xtc.is_in_chat()
    check("聊天页判定", True, f"in_chat={in_chat} activity={xtc.current_activity()}")
    logged_out = (state == "not_logged_in")
    if logged_out:
        check("打开聊天", None, "小天才 App 未登录（在登录页），跳过聊天相关检查")
    elif not in_chat and contact:
        t0 = time.time()
        ok = xtc.open_chat(contact)
        check("打开聊天", ok, f"{(time.time() - t0):.1f}s -> activity={xtc.current_activity()}")

    # 7) 消息列表结构（若当前在列表页，验证联系人匹配修复）
    try:
        r2 = adb.dump_ui(retries=1, delay=0.2)
        is_list = xtc.looks_like_message_list(r2)
        names = xtc._visible_contact_names(r2)
        check("消息列表识别", True,
              f"is_list={is_list} 可见联系人={names[:5] or '(无)'}")
        if contact and not logged_out:
            node = xtc._find_contact_node(r2, contact)
            check("联系人匹配", node is not None,
                  f"找 {contact!r} -> {'命中 ' + str(node.get('bounds')) if node is not None else '未命中'}")
        elif contact:
            check("联系人匹配", None, "未登录（没有消息列表），跳过")
    except AdbError as e:
        check("消息列表识别", None, f"读界面失败: {e}")

    # 8) 读取最新消息（只读）
    if xtc.is_in_chat():
        msg = xtc.get_latest_message()
        check("读取最新消息", True, f"contact={msg[0]!r} text={str(msg[1])[:30]!r} time={msg[2]!r}")
        try:
            hist = xtc.get_chat_history(count=3)
            check("读取聊天历史(3条,会滚动)", True,
                  f"{len(hist)} 条: " + " | ".join(f"{h['is_own'] and '我' or h.get('contact') or '?'}:{h['text'][:12]}" for h in hist))
        except Exception as e:  # noqa: BLE001
            check("读取聊天历史", False, str(e))
    else:
        check("读取最新消息", None, "不在聊天页，跳过")

    # 9) 文本注入（输入 -> 校验 -> 清空；**不点发送**）
    if xtc.is_in_chat():
        probe_text = "bridge-live-probe-可忽略"
        clean = xtc.ensure_input_clean()
        check("输入框就绪", "就绪" in clean, clean)
        try:
            ok = adb.input_text(probe_text, verify=xtc.input_verifier(probe_text))
            cur = xtc.chat_input_text()
            check("文本注入", ok and probe_text in (cur or ""), f"输入框={cur[:40]!r}")
        except AdbError as e:
            check("文本注入", False, str(e))
        xtc._clear_chat_input()
        left = xtc.chat_input_text().strip()
        check("清空输入框（不留残字，不发送）", not left, f"残留={left[:30]!r}")
    else:
        check("文本注入", None, "不在聊天页，跳过（避免乱输入）")

    # 10) 消息库（本地归档，只读）
    try:
        from msg_log import MessageLog
        import runtime_paths
        ml = MessageLog(path=str(runtime_paths.data_path("msg_log.json")))
        check("本地消息库", True, f"{ml.count()} 条，来源={ml.sources()[:4]}")
    except Exception as e:  # noqa: BLE001
        check("本地消息库", False, str(e))

    print(f"\n===== 实机探测结果：{RC['pass']} 通过 / {RC['fail']} 失败 / {RC['skip']} 跳过 =====")
    return 1 if RC["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
