# -*- coding: utf-8 -*-
"""环境自检脚本（不依赖配置文件）：把 ADB 层 + 小天才操作层的关键能力
在真机模拟器上跑一遍，定位哪一步没通。

用法：python tools/selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adb_controller import ADBController, AdbError  # noqa: E402
from xiaotiancai import Xiaotiancai  # noqa: E402
from utils.logger import setup_logger  # noqa: E402


def main() -> int:
    log = setup_logger(level="INFO", console=True)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")

    # 1. adb 发现
    try:
        adb = ADBController()
        check("查找 adb.exe", bool(adb.adb_path), adb.adb_path)
    except AdbError as e:
        check("查找 adb.exe", False, str(e))
        return 1

    # 2. 连接
    if not adb.is_connected():
        try:
            adb.connect()
        except AdbError as e:
            check("ADB 连接", False, str(e))
            return 1
    check("ADB 连接", True, f"设备 {adb.serial}")

    # 3. 系统信息
    try:
        ver, sdk, size = adb.android_version(), adb.android_sdk(), adb.get_screen_size()
        check("系统信息", bool(ver and size),
              f"Android {ver} (SDK {sdk}) 屏幕 {size} "
              f"{'[WSA]' if adb.is_wsa() else ''} 序列号 {adb.serial}")
    except AdbError as e:
        check("系统信息", False, str(e))

    # 3.5 输入通道探测（WSA 上最容易出问题的一环）
    try:
        check("ADBKeyBoard 已安装", adb.adbkeyboard_ready(),
              "未安装：main.py 启动时会自动安装（或 python main.py --no-adbkeyboard 跳过）")
        check("当前输入法", True, f"{adb.current_ime() or '(未知)'} | 键盘显示={adb.ime_shown()}")
        check("剪贴板通道", adb.probe_clipboard(),
              "写入+回读一致才可用；WSA 上不可用时注入只走 ADBKeyBoard 广播")
    except AdbError as e:
        check("输入通道", False, str(e))

    # 4. UI dump
    try:
        root = adb.dump_ui()
        check("UI dump", True, f"{len(list(root.iter('node')))} 节点")
    except AdbError as e:
        check("UI dump", False, str(e))

    # 5. 启动小天才 + 登录检测
    xtc = Xiaotiancai(adb, logger=log)
    check("启动小天才", xtc.launch())
    logged_in = xtc.is_logged_in()
    check("登录检测（返回布尔即可）", True,
          f"已登录={logged_in}（未登录=停在欢迎页/登录页，属预期）")

    # 6. 截图
    try:
        adb.screenshot("selftest_screen.png")
        check("截图", True, "selftest_screen.png")
    except AdbError as e:
        check("截图", False, str(e))

    # 7. 读取消息（未登录时应返回 (None, None) 且不崩溃）
    contact, text = xtc.get_latest_message()
    check("读取最新消息（不崩溃即可）", True,
          f"contact={contact!r} text={text!r}（未登录时为空属预期）")

    # 8. 中文输入能力（真机验证：只在聊天页/输入框聚焦时才真正注入）
    try:
        xtc.ensure_input_clean()
        ok = adb.input_text("selftest-测试", verify=xtc.input_verifier("selftest-测试"))
        check("文本注入可用", ok,
              "失败时：确认 ADBKeyBoard 为当前输入法（adb shell ime set "
              "com.android.adbkeyboard/.AdbIME）且输入框已聚焦")
        if ok:
            adb.clear_text_field()
    except AdbError as e:
        check("文本注入可用", False, str(e))

    fails = [r for r in results if not r[1]]
    print(f"\n===== 自检完成：{len(results) - len(fails)}/{len(results)} 通过 =====")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
