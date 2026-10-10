# -*- coding: utf-8 -*-
"""量一下"压缩 dump"能不能用于**发送确认**（确认只需要气泡文本 + 输入框，不需要时间标签）。

结果写 data/probe_img/dump_cmp.txt：两种策略的耗时、节点数、是否含输入框/气泡。
"""
import sys
import time
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

for tag, fn in (("完整 dump", lambda: adb._dump_via_file_combined(False)),
                ("压缩 dump", lambda: adb._dump_via_file_combined(True))):
    t0 = time.time()
    xml, why = fn()
    dt = time.time() - t0
    if not xml:
        out.append(f"{tag}: 失败（{why}）耗时 {dt:.2f}s")
        continue
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    nodes = list(root.iter("node"))
    texts = [n.get("text") for n in nodes if (n.get("text") or "").strip()]
    inputs = [n for n in nodes if (n.get("class") or "").endswith("EditText")]
    dates = [n for n in nodes if (n.get("resource-id") or "").endswith("tv_chat_msg_item_date")]
    out.append(f"{tag}: {dt:.2f}s 节点={len(nodes)} 有文本节点={len(texts)} "
               f"输入框={len(inputs)} 时间标签={len(dates)}")
    out.append(f"    文本样例={texts[:4]}")
    for n in inputs:
        out.append(f"    输入框 text={n.get('text')!r} id={(n.get('resource-id') or '').split('/')[-1]}")
    out.append(f"    能解析出气泡: {len(xtc._chat_bubbles(root, include_own=True))} 个")

Path("data/probe_img").mkdir(parents=True, exist_ok=True)
Path("data/probe_img/dump_cmp.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
