# -*- coding: utf-8 -*-
"""实机 UI 探测工具：打印当前界面所有控件（序号/class/resource-id/text），
用于登录家长账号后填写 config.yaml → xiaotiancai.ui 的映射表。

用法：
  python tools/dump_ui.py                      # 打印当前界面控件
  python tools/dump_ui.py --filter 发送        # 只看包含“发送”的节点
  python tools/dump_ui.py --tap 3              # 点击第 3 个节点中心（按打印序号）
  python tools/dump_ui.py --screenshot ui.png  # 同时截图
  python tools/dump_ui.py --save-xml ui.xml    # 保存 UI XML
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adb_controller import ADBController, AdbError  # noqa: E402


def run(adb: ADBController, filter_re: str = "", tap_index: int = -1,
        screenshot: str = "", save_xml: str = "") -> int:
    if screenshot:
        try:
            adb.screenshot(screenshot)
            print(f"[截图] 已保存: {screenshot}")
        except AdbError as e:
            print(f"[错误] 截图失败: {e}")
    try:
        root = adb.dump_ui()
    except AdbError as e:
        print(f"[错误] UI dump 失败: {e}")
        return 1
    if save_xml:
        ET.ElementTree(root).write(save_xml, encoding="utf-8", xml_declaration=True)
        print(f"[XML] 已保存: {save_xml}")

    pat = re.compile(filter_re) if filter_re else None
    all_nodes = list(root.iter("node"))
    nodes = []
    for n in all_nodes:
        if pat and not (pat.search(n.get("text", "") or "")
                        or pat.search(n.get("resource-id", "") or "")
                        or pat.search(n.get("content-desc", "") or "")
                        or pat.search(n.get("class", "") or "")):
            continue
        nodes.append(n)

    print(f"[UI] 当前界面 {len(all_nodes)} 个节点，过滤后 {len(nodes)} 个")
    print(f"[界面] 当前前台: {adb.get_current_focus() or '(未知)'}")
    print(f"{'#':>3}  {'class':<30} {'resource-id':<38} text/content-desc")
    for i, n in enumerate(nodes):
        label = (n.get("text", "") or n.get("content-desc", "")).replace("\n", "\\n")
        print(f"{i:>3}  {n.get('class', ''):<30} {n.get('resource-id', ''):<38} {label}")

    if tap_index >= 0:
        if 0 <= tap_index < len(nodes):
            n = nodes[tap_index]
            x, y = adb.node_center(n)
            print(f"[点击] 节点 #{tap_index} 中心 ({x}, {y}) bounds={n.get('bounds', '')}")
            adb.tap(x, y)
        else:
            print(f"[错误] 节点序号越界: {tap_index}（共 {len(nodes)} 个）")
            return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description="小天才 App 界面探测工具")
    ap.add_argument("--adb", default="", help="adb.exe 路径（留空自动探测）")
    ap.add_argument("--serial", default="", help="设备序列号（留空自动选择）")
    ap.add_argument("--filter", default="", help="正则过滤节点")
    ap.add_argument("--tap", type=int, default=-1, help="点击指定序号节点中心")
    ap.add_argument("--screenshot", default="", help="截图保存路径")
    ap.add_argument("--save-xml", default="", help="UI XML 保存路径")
    args = ap.parse_args()

    adb = ADBController(adb_path=args.adb, serial=args.serial)
    if not adb.is_connected():
        try:
            adb.connect()
        except AdbError as e:
            print(f"[错误] {e}")
            return 1
    print(f"[ADB] {adb.adb_path}")
    print(f"[ADB] 设备 {adb.serial or '(自动)'} | Android {adb.android_version()} "
          f"| 屏幕 {adb.get_screen_size()}")
    sys.exit(run(adb, args.filter, args.tap, args.screenshot, args.save_xml))


if __name__ == "__main__":
    main()
