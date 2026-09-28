# -*- coding: utf-8 -*-
"""实机验证：把聊天里那条"图片消息"按桥接的真实路径取出来看看。

py tools/image_live_probe.py
输出 data/probe_img/live.txt + 取到的图（原图/截图各存一份）
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
import bridge as bridge_mod  # noqa: E402
from adb_controller import ADBController  # noqa: E402
from xiaotiancai import IMAGE_TEXT, Xiaotiancai  # noqa: E402

out_dir = Path("data/probe_img")
out_dir.mkdir(parents=True, exist_ok=True)
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
    def info(self, m):
        out.append("  [info] " + str(m))

    def debug(self, m):
        out.append("  [debug] " + str(m))

    def warning(self, m):
        out.append("  [warn] " + str(m))

    error = warning


class DummyFwd:
    def send_detail(self, *a, **k):
        return True, ""

    def send_image(self, *a, **k):
        return True, ""


xtc = Xiaotiancai(adb, cfg.get("xiaotiancai") or {}, logger=None)
contact = str(((cfg.get("target") or {}).get("xtc_contact")) or "")
for _ in range(3):
    root = adb.dump_ui(retries=2, delay=0.5)
    if xtc.is_in_chat(root):
        break
    if contact:
        xtc.open_chat(contact)
    time.sleep(2.0)

# 往上翻，直到看见图片消息
item = None
for page in range(6):
    root = adb.dump_ui(retries=2, delay=0.5)
    item = xtc.image_of_latest(root, IMAGE_TEXT)
    brief = {k: v for k, v in (item or {}).items() if k != "node"}
    out.append(f"第 {page} 屏: 图片气泡={brief!r}")
    if item:
        break
    h = adb.get_screen_size()[1]
    adb.swipe(adb.get_screen_size()[0] // 2, int(h * 0.25),
              adb.get_screen_size()[0] // 2, int(h * 0.85), 350)
    time.sleep(1.2)

if not item:
    out.append("界面上没找到图片消息")
    (out_dir / "live.txt").write_text("\n".join(out), encoding="utf-8")
    print("no image bubble")
    raise SystemExit(0)

br = bridge_mod.MessageBridge(cfg, adb=adb, xtc=xtc, forwarder=DummyFwd(), logger=Log())
label_epoch = br._label_epoch(item.get("time_label") or "")
out.append(f"气泡: {item['bounds']} 时间标签={item.get('time_label')!r} -> epoch={label_epoch}")

t0 = time.time()
got = br._capture_sticker(root, IMAGE_TEXT, near_epoch=label_epoch)
brief_got = {k: v for k, v in (got or {}).items() if k != "data"}
out.append(f"取图耗时 {time.time() - t0:.2f}s -> {brief_got!r}")
if got:
    ext = {"jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp"}.get(got.get("kind"), "bin")
    p = out_dir / f"live_picked_{got.get('source')}.{ext}"
    p.write_bytes(got["data"])
    out.append(f"已导出 {p}（{len(got['data'])} 字节）")

# 关掉"照片取原图"开关 -> 应退回气泡截图
br._emoji_photo = False
t1 = time.time()
fallback = br._capture_sticker(root, IMAGE_TEXT, near_epoch=label_epoch)
brief_fb = {k: v for k, v in (fallback or {}).items() if k != "data"}
out.append(f"关掉 forward_photo 后 {time.time() - t1:.2f}s -> {brief_fb!r}")
if fallback:
    p = out_dir / "live_picked_screenshot_fallback.png"
    p.write_bytes(fallback["data"])
    out.append(f"已导出 {p}（{len(fallback['data'])} 字节）")

(out_dir / "live.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
