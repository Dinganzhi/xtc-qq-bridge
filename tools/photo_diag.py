# -*- coding: utf-8 -*-
"""照片（图片消息）转发失败的现场诊断：把"取原图"这条链上每一道门都打印出来。

为什么需要它：用户报"图片只发出去两个字"，而代码里照片有多道把关
（气泡是否找到 -> 缓存候选是否够大/形状对不对/mtime 水位是否够新 -> 气泡截图是否空白），
不看现场就只能猜。这里把每张缓存的判定过程全部落到 data/probe_img/diag.txt。

用法：
    py tools/photo_diag.py            # 只诊断当前屏幕上的图片消息
    py tools/photo_diag.py --scroll 3 # 先往上翻屏再找（图片在屏幕外时用）
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
import bridge as bridge_mod  # noqa: E402
from adb_controller import ADBController  # noqa: E402
from emoji_store import EmojiStore  # noqa: E402
from utils import imgtool  # noqa: E402
from xiaotiancai import IMAGE_TEXT, Xiaotiancai  # noqa: E402

out_dir = Path("data/probe_img")
out_dir.mkdir(parents=True, exist_ok=True)
out: list[str] = []
cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
adb_cfg = cfg.get("adb") or {}
adb = ADBController(adb_path=adb_cfg.get("path", ""),
                    host=adb_cfg.get("host", "127.0.0.1"),
                    port=int(adb_cfg.get("port", 5555)),
                    serial=adb_cfg.get("serial", ""),
                    wsa_port=int(adb_cfg.get("wsa_port", 0) or 0))
adb.ensure_connected()
adb.wake_if_asleep()
pkg = str((cfg.get("xiaotiancai") or {}).get("package", "com.xtc.watch"))
if not adb.is_in_foreground(pkg):
    adb.launch_app(pkg, str((cfg.get("xiaotiancai") or {}).get("main_activity", ".MainActivity")),
                   wait=8.0, attempts=1)
    time.sleep(2.0)


class Log:
    def info(self, m):
        out.append("  [info] " + str(m))

    def debug(self, m):
        out.append("  [debug] " + str(m))

    warning = error = info


class DummyFwd:
    def send_detail(self, *a, **k):
        return True, ""

    def send_image(self, *a, **k):
        return True, ""


xtc = Xiaotiancai(adb, cfg.get("xiaotiancai") or {}, logger=None)
contact = str((cfg.get("target") or {}).get("xtc_contact") or "")

scroll = 0
if "--scroll" in sys.argv:
    try:
        scroll = int(sys.argv[sys.argv.index("--scroll") + 1])
    except (IndexError, ValueError):
        scroll = 0

root = adb.dump_ui(retries=2, delay=0.5)
if not xtc.is_in_chat(root) and contact:
    xtc.open_chat(contact)
    time.sleep(1.5)
    root = adb.dump_ui(retries=2, delay=0.5)

item = None
for page in range(max(1, scroll + 1)):
    root = adb.dump_ui(retries=2, delay=0.5)
    item = xtc.image_of_latest(root, IMAGE_TEXT)
    out.append(f"第 {page} 屏: 图片气泡={ {k: v for k, v in (item or {}).items() if k != 'node'}!r}")
    if item or page >= scroll:
        break
    w, h = adb.get_screen_size()
    adb.swipe(w // 2, int(h * 0.25), w // 2, int(h * 0.85), 350)
    time.sleep(1.2)

if not item:
    out.append("界面上没找到图片消息（可以先手动滑到那张照片再跑一次）")
    (out_dir / "diag.txt").write_text("\n".join(out), encoding="utf-8")
    print("no image bubble")
    raise SystemExit(0)

b = item["bounds"]
bw, bh = b[2] - b[0], b[3] - b[1]
aspect = (bw / bh) if bh else 0.0
label = (item.get("time_label") or "").strip()
own = (item.get("own_label") or "").strip()
br = bridge_mod.MessageBridge(cfg, adb=adb, xtc=xtc, forwarder=DummyFwd(), logger=Log())
label_epoch = br._label_epoch(label)
own_epoch = br._label_epoch(own)
out.append(f"气泡 bounds={b} 尺寸={bw}x{bh} 形状={aspect:.3f}")
out.append(f"App 标签={label!r} own_label={own!r} -> epoch={label_epoch}/{own_epoch}")
out.append(f"emoji 配置: forward_photo={br._emoji_photo} store="
           f"{type(br._emoji_store).__name__ if br._emoji_store else None}")

store = br._emoji_store
if store is not None:
    rows, dev_now = store._cache_listing(max_bytes=store.photo_max_bytes,
                                        min_bytes=store.photo_min_bytes)
    out.append(f"设备当前时间={dev_now:.0f}，缓存里 >= {store.photo_min_bytes} 字节的文件 "
               f"{len(rows)} 个；photo_window={store.photo_window}s")
    targets = [t for t in (label_epoch, own_epoch, dev_now) if t]
    for mtime, size, path in sorted(rows, reverse=True):
        info = store.sniff(store._read_head(path))
        w, h = (info or {}).get("w") or 0, (info or {}).get("h") or 0
        water = min(abs(mtime - t) for t in targets) if targets else -1
        asp = (w / h) if h else 0.0
        reasons = []
        if not info or info.get("kind") not in ("gif", "png", "webp", "jpeg"):
            reasons.append("格式认不出")
        if max(w, h) <= max(bw, bh, store.max_px):
            reasons.append(f"不比气泡大({w}x{h})")
        if aspect and h and abs(asp - aspect) > 0.12 * max(1.0, aspect):
            reasons.append(f"形状不符({asp:.2f} vs {aspect:.2f})")
        if water > store.photo_window:
            reasons.append(f"水位太老({water:.0f}s)")
        out.append(f"  {Path(path).name} {size}B {w}x{h} 水位{water:.0f}s -> "
                   + ("**通过**" if not reasons else "、".join(reasons)))
    t0 = time.time()
    got = store.find_photo(near_epoch=label_epoch or own_epoch or None, aspect=aspect,
                           min_px=max(bw, bh))
    out.append(f"find_photo -> {'None' if not got else {k: v for k, v in got.items() if k != 'data'}}"
               f"（{time.time() - t0:.2f}s）")

ref = None
try:
    ref = xtc.capture_sticker(b)
except Exception as e:  # noqa: BLE001
    out.append(f"capture_sticker 异常: {type(e).__name__}: {e}")
if ref:
    dom, std = imgtool.content_stats(ref)
    out.append(f"气泡截图 {len(ref)} 字节，looks_blank={imgtool.looks_blank(ref)}"
               f"（主色占比 {dom:.2f}、亮度标准差 {std:.0f}）")
else:
    out.append("气泡截图 = None（screencap 没取到）")

t1 = time.time()
picked = br._capture_sticker(root, IMAGE_TEXT, near_epoch=label_epoch or own_epoch or None)
brief = {k: v for k, v in (picked or {}).items() if k != "data"}
out.append(f"_capture_sticker（桥接真实入口）{time.time() - t1:.2f}s -> {brief!r}")
if picked:
    ext = {"jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp"}.get(picked.get("kind"), "bin")
    p = out_dir / f"diag_picked_{picked.get('source')}.{ext}"
    p.write_bytes(picked["data"])
    out.append(f"已导出 {p}（{len(picked['data'])} 字节）")

(out_dir / "diag.txt").write_text("\n".join(out), encoding="utf-8")
print("ok -> data/probe_img/diag.txt")
