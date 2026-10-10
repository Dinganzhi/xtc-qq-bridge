# -*- coding: utf-8 -*-
"""把当前聊天窗口里每个气泡的**原始字段**打出来（text / content-desc / class / bounds）。

用途：查"表情名读不到、图片被当成文字"这类问题 —— 桥接解析出来的字段和 App 实际给的
字段差在哪，一眼就能看出来。结果写 data/probe_img/bubbles.txt
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from adb_controller import ADBController  # noqa: E402
from xiaotiancai import Xiaotiancai  # noqa: E402

out: list[str] = []
cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
a = cfg.get("adb") or {}
adb = ADBController(adb_path=a.get("path", ""), host=a.get("host", "127.0.0.1"),
                    port=int(a.get("port", 5555)), serial=a.get("serial", ""),
                    wsa_port=int(a.get("wsa_port", 0) or 0))
adb.ensure_connected()
adb.wake_if_asleep()
pkg = str((cfg.get("xiaotiancai") or {}).get("package", "com.xtc.watch"))
if not adb.is_in_foreground(pkg):
    adb.launch_app(pkg, str((cfg.get("xiaotiancai") or {}).get("main_activity", ".MainActivity")),
                   wait=8.0, attempts=1)
    import time
    time.sleep(2.0)

xtc = Xiaotiancai(adb, cfg.get("xiaotiancai") or {}, logger=None)
root = adb.dump_ui(retries=2, delay=0.5)
out.append(f"state={xtc.app_state(root)} in_chat={xtc.is_in_chat(root)} "
           f"title={xtc.chat_title(root)!r}")
# 消息列表可视区：气泡贴它的边 = 被裁（抠图只有半张，见 emoji_clip_probe.py）
out.append(f"消息列表可视区={xtc.chat_view_bounds(root)}")
for it in xtc._chat_bubbles(root, include_own=True):
    if it.get("bounds"):
        out.append(f"  气泡 {it['bounds']} 被列表裁到={xtc.bubble_clipped(root, it['bounds'])} "
                   f"text={it.get('text')!r}")
out.append("--- 原始节点（chat_msg_item_content）---")
for n in root.iter("node"):
    rid = n.get("resource-id") or ""
    if not rid.endswith("chat_msg_item_content"):
        continue
    out.append("  " + str({
        "id": rid.split("/")[-1],
        "class": (n.get("class") or "").split(".")[-1],
        "text": n.get("text"),
        "desc": n.get("content-desc"),
        "bounds": n.get("bounds"),
        "clickable": n.get("clickable"),
    }))
out.append("--- 桥接解析出来的气泡 ---")
for it in xtc._chat_bubbles(root, include_own=True):
    out.append("  " + str({k: v for k, v in it.items() if k != "node"}))

Path("data/probe_img").mkdir(parents=True, exist_ok=True)
Path("data/probe_img/bubbles.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
