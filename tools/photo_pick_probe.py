# -*- coding: utf-8 -*-
"""实时核对照片原图挑选：列出照片目录候选 + 走一遍 find_photo（结果写 data/probe_img/photo.txt）。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from adb_controller import ADBController  # noqa: E402
from emoji_store import EmojiStore  # noqa: E402

out: list[str] = []
cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
a = cfg.get("adb") or {}
adb = ADBController(adb_path=a.get("path", ""), host=a.get("host", "127.0.0.1"),
                    port=int(a.get("port", 5555)), serial=a.get("serial", ""),
                    wsa_port=int(a.get("wsa_port", 0) or 0))
adb.ensure_connected()


class L:
    def info(self, m):
        out.append("INFO  " + str(m))

    def debug(self, m):
        out.append("DEBUG " + str(m))

    warning = error = info


st = EmojiStore(adb, package="com.xtc.watch", logger=L())
rows, dev = st._photo_dir_listing(max_bytes=st.photo_max_bytes, min_bytes=st.photo_min_bytes)
out.append(f"照片目录候选（设备时间={dev:.0f}）:")
for mt, sz, p in sorted(rows, reverse=True):
    info = st.sniff(st._read_head(p)) or {}
    out.append("  {} {}B {}x{} 水位{:.0f}s".format(
        Path(p).name, sz, info.get("w"), info.get("h"), dev - mt))

for aspect, note in ((136 / 180, "按 22:12 那条气泡的形状 136x180"),
                     (None, "不给形状")):
    t0 = time.time()
    got = st.find_photo(near_epoch=dev - 3700, aspect=aspect, min_px=180)
    brief = ({k: v for k, v in got.items() if k != "data"} if got else None)
    out.append(f"find_photo({note}) {time.time() - t0:.2f}s -> {brief}")

Path("data/probe_img").mkdir(parents=True, exist_ok=True)
Path("data/probe_img/photo.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
