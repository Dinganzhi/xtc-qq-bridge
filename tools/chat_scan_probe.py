# -*- coding: utf-8 -*-
"""看看当前聊天窗口里到底有什么：气泡 + ImageView 节点（找照片气泡用）。结果写 data/probe_img/scan.txt"""
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
xtc = Xiaotiancai(adb, cfg.get("xiaotiancai") or {}, logger=None)


def snap(tag: str) -> None:
    root = adb.dump_ui(retries=2, delay=0.5)
    out.append(f"===== {tag} =====")
    out.append(f"state={xtc.app_state(root)} in_chat={xtc.is_in_chat(root)} "
               f"title={xtc.chat_title(root)!r}")
    items = xtc._chat_bubbles(root, include_own=True)
    out.append(f"气泡 {len(items)} 个:")
    for it in items:
        out.append("  " + str({k: v for k, v in it.items() if k != "node"}))
    ids: dict[str, int] = {}
    for n in root.iter("node"):
        cls = (n.get("class") or "").split(".")[-1]
        if cls in ("ImageView", "android.widget.ImageView") or "image" in cls.lower():
            ids[cls] = ids.get(cls, 0) + 1
    out.append(f"图片类节点: {ids}")


snap("当前屏")
w, h = adb.get_screen_size()
for i in range(3):
    adb.swipe(w // 2, int(h * 0.25), w // 2, int(h * 0.85), 350)
    import time
    time.sleep(1.2)
    snap(f"往上翻 {i + 1} 屏")

Path("data/probe_img").mkdir(parents=True, exist_ok=True)
Path("data/probe_img/scan.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
