# -*- coding: utf-8 -*-
"""实机验证：现学现卖地把界面上那张贴纸找出来，看挑的是不是同一张。

py tools/emoji_live_probe.py [表情名，默认取界面最后一条]
输出 data/probe_sticker/live.txt（UTF-8）
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from adb_controller import ADBController  # noqa: E402
from emoji_store import EmojiStore  # noqa: E402
from utils import imgtool  # noqa: E402
from utils.pngtool import encode_png_rgb  # noqa: E402
from xiaotiancai import Xiaotiancai  # noqa: E402

name_arg = sys.argv[1] if len(sys.argv) > 1 else ""
out: list[str] = []
cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
adb_cfg = cfg.get("adb") or {}
adb = ADBController(adb_path=str(adb_cfg.get("path", "")),
                    wsa_port=int(adb_cfg.get("wsa_port", 58526) or 58526))
adb.ensure_connected()
adb.wake_if_asleep()
if not adb.is_in_foreground("com.xtc.watch"):
    adb.launch_app("com.xtc.watch", ".MainActivity", wait=8.0, attempts=1)


class Log:
    def info(self, msg):
        out.append("  [info] " + str(msg))

    def debug(self, msg):
        out.append("  [debug] " + str(msg))

    def warning(self, msg):
        out.append("  [warn] " + str(msg))

    error = warning


t0 = time.time()
root = adb.dump_ui(retries=2, delay=0.5)
xtc = Xiaotiancai(adb, cfg.get("xiaotiancai") or {}, logger=None)
item = xtc.sticker_of_latest(root, name_arg)
t_dump = time.time() - t0
item_brief = {k: v for k, v in (item or {}).items() if k != "node"}
out.append(f"dump+定位 {t_dump:.2f}s -> {item_brief!r}")
if not item:
    out.append("界面上没有表情气泡，退出")
    Path("data/probe_sticker/live.txt").write_text("\n".join(out), encoding="utf-8")
    print("no sticker")
    raise SystemExit(0)

name = (item.get("text") or "").removeprefix("表情").strip()
t1 = time.time()
ref = xtc.capture_sticker(item.get("bounds"))
t_shot = time.time() - t1
out.append(f"抠气泡截图 {t_shot:.2f}s {len(ref or b'')} 字节")

store = EmojiStore(adb, logger=Log())
aspect = None
if item.get("bounds"):
    x1, y1, x2, y2 = item["bounds"]
    aspect = (x2 - x1) / (y2 - y1) if y2 > y1 else None
near = None
t2 = time.time()
got = store.find(name, near_epoch=near, aspect=aspect, reference=ref)
t_find = time.time() - t2
out.append(f"\n名字 {name!r} 唯一={store.name_is_unique(name)} 找原图耗时 {t_find:.2f}s")
brief = {k: v for k, v in (got or {}).items() if k != "data"}
out.append(f"结果: {brief!r}")
if got:
    f = imgtool.decode_frames(got["data"])[0]
    rows = [f["rgb"][y * f["w"] * 3:(y + 1) * f["w"] * 3] for y in range(f["h"])]
    p = Path("data/probe_sticker/live_picked.png")
    p.write_bytes(encode_png_rgb(f["w"], f["h"], rows))
    out.append(f"挑中图片已导出（第一帧）: {p} {f['w']}x{f['h']}")

t3 = time.time()
got_noref = EmojiStore(adb, logger=Log()).find(name, aspect=aspect)
brief2 = {k: v for k, v in (got_noref or {}).items() if k != "data"}
out.append(f"\n不给基准图（老逻辑兜底）耗时 {time.time() - t3:.2f}s -> {brief2!r}")
out.append(f"\n总耗时 {time.time() - t0:.2f}s")

Path("data/probe_sticker/live.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
