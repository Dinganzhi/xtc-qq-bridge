# -*- coding: utf-8 -*-
"""验证"气泡被列表裁掉 -> 自动滚进可视区 -> 取到原图 -> 滚回原位"。

步骤：先故意把列表滚到让表情气泡贴顶边（被裁），确认 bubble_clipped=True，
再走桥接真实入口 _capture_sticker，最后核对气泡是否回到原来的位置。
结果写 data/probe_img/clip.txt
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
import bridge as bridge_mod  # noqa: E402
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


class Log:
    def info(self, m):
        out.append("  INFO  " + str(m))

    def debug(self, m):
        out.append("  debug " + str(m))

    warning = error = info


class Dummy:
    def send_detail(self, *a, **k):
        return True, ""

    def send_image(self, *a, **k):
        return True, ""


xtc = Xiaotiancai(adb, cfg.get("xiaotiancai") or {}, logger=None)
br = bridge_mod.MessageBridge(cfg, adb=adb, xtc=xtc, forwarder=Dummy(), logger=Log())


def sticker(root):
    return xtc.sticker_of_latest(root, "表情开心") or xtc.sticker_of_latest(root, "")


root = adb.dump_ui(retries=2, delay=0.5)
vb = xtc.chat_view_bounds(root)
item = sticker(root)
out.append(f"列表可视区={vb} 起始气泡={item and item.get('bounds')}")

# 故意把气泡拖到贴顶边（=被列表裁掉）
for _ in range(3):
    it = sticker(root)
    if it and it["bounds"][1] <= vb[1] + 2:
        break
    xtc.scroll_chat_by(vb, -int((vb[3] - vb[1]) * 0.3))
    root = adb.dump_ui(retries=2, delay=0.5)
item = sticker(root)
before = item and item["bounds"]
out.append(f"制造被裁后：气泡={before} clipped={xtc.bubble_clipped(root, before) if before else None}")
if before:
    # 直接看半张图的比对分数（复现用户看到的 0.7x）
    half = xtc.capture_sticker(before)
    out.append(f"（对照）被裁时直接抠图 {len(half) if half else 0} 字节")

got = br._capture_sticker(root, "表情开心", near_epoch=None)
if got:
    out.append(f"桥接取图: source={got.get('source')} kind={got.get('kind')} score={got.get('score')} "
               f"{got.get('w')}x{got.get('h')} {len(got['data'])} 字节")
else:
    out.append("桥接取图: None")

after_root = adb.dump_ui(retries=2, delay=0.5)
after = sticker(after_root)
out.append(f"取图后气泡位置={after and after.get('bounds')}（应与制造被裁后一致 = 已滚回原位）")

Path("data/probe_img").mkdir(parents=True, exist_ok=True)
Path("data/probe_img/clip.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
