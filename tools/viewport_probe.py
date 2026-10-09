# -*- coding: utf-8 -*-
"""看聊天窗口里"消息列表容器"的边界（判断气泡是不是被列表裁掉了一半）。结果写 data/probe_img/viewport.txt"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from adb_controller import ADBController  # noqa: E402

cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
a = cfg.get("adb") or {}
adb = ADBController(adb_path=a.get("path", ""), host=a.get("host", "127.0.0.1"),
                    port=int(a.get("port", 5555)), serial=a.get("serial", ""),
                    wsa_port=int(a.get("wsa_port", 0) or 0))
adb.ensure_connected()
adb.wake_if_asleep()
root = adb.dump_ui(retries=2, delay=0.5)
rows = []
for n in root.iter("node"):
    cls = (n.get("class") or "").split(".")[-1]
    rid = (n.get("resource-id") or "").split("/")[-1]
    if any(k in cls for k in ("Recycler", "ListView", "ScrollView", "ViewPager")) or \
            ("chat" in rid.lower() and "item" not in rid.lower()):
        rows.append("{:16} id={:36} bounds={}".format(cls, rid[:36], n.get("bounds")))
w, h = adb.get_screen_size()
rows.append(f"屏幕 {w}x{h}")
Path("data/probe_img/viewport.txt").write_text("\n".join(rows), encoding="utf-8")
print("ok")
