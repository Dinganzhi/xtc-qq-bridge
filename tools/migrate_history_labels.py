"""一次性迁移：把历史去重表里"相对时间标签"的旧键，补成"绝对时间"的新键。

背景：App 的时间标签会随日期变化（当天 `16:18` -> 第二天 `昨天 16:18`），旧代码直接拿
原始标签当"消息身份"。改成绝对时间后旧键不再匹配，已转发过的消息会被再发一次；
这里把界面上**可见的、且历史表里已有旧键**的消息补登记成绝对键，并删掉旧键。

py tools/migrate_history_labels.py
"""
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from adb_controller import ADBController  # noqa: E402
from bridge import MessageBridge  # noqa: E402
from utils.deduplicate import _hash  # noqa: E402
from xiaotiancai import Xiaotiancai  # noqa: E402

cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
adb_cfg = cfg.get("adb") or {}
adb = ADBController(adb_path=str(adb_cfg.get("path", "")),
                    wsa_port=int(adb_cfg.get("wsa_port", 58526) or 58526))
adb.ensure_connected()
br = MessageBridge.__new__(MessageBridge)          # 只用它的纯函数

adb.wake_if_asleep()
if not adb.is_in_foreground("com.xtc.watch"):
    adb.launch_app("com.xtc.watch", ".MainActivity", wait=8.0, attempts=1)
root = adb.dump_ui(retries=2, delay=0.5)
xtc = Xiaotiancai(adb, cfg.get("xiaotiancai") or {}, logger=None)
bubbles = xtc._chat_bubbles(root, include_own=False)
print(f"界面上对方发来的消息 {len(bubbles)} 条：")

hp = Path("data/history_cache.json")
data = json.loads(hp.read_text(encoding="utf-8"))
have = {x[0] for x in data}

contacts = ["", "屑猹不喝茶"]
adds: set = set()
dels: set = set()
for it in bubbles:
    text = (it.get("text") or "").strip()
    raw = (it.get("time_label") or "").strip()
    absl = br._abs_time_label(raw)
    if not text or not absl:
        print(f"  {text[:24]!r}: 标签 {raw!r} 解析不出绝对时间，跳过")
        continue
    hhmm = re.search(r"(\d{1,2}:\d{2})", raw)
    legacy = {raw, ""} | ({hhmm.group(1)} if hhmm else set())
    forwarded = any(_hash("xtc", c, text, lv) in have for c in contacts for lv in legacy)
    print(f"  {text[:24]!r}: 绝对={absl} 旧键在表里={forwarded} 原始标签={raw!r}")
    if not forwarded:
        continue
    for c in contacts:
        dels |= {_hash("xtc", c, text, lv) for lv in legacy}
        adds.add(_hash("xtc", c, text, absl))

new_keys = {k for k in adds if k not in have}
if new_keys or dels:
    shutil.copy(hp, str(hp) + ".bak-before-label-migration")
    kept = [x for x in data if x[0] not in dels]
    have2 = {x[0] for x in kept}
    add = [k for k in adds if k not in have2]
    kept += [[k, datetime.now().timestamp()] for k in add]
    hp.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
    print(f"history: {len(data)} -> {len(kept)}（新增绝对键 {len(add)}，删除旧键 {len(data) - len(kept) + len(add)}）")
    print("备份: data/history_cache.json.bak-before-label-migration")
else:
    print("无需迁移")
