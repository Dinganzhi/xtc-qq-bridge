# -*- coding: utf-8 -*-
"""找出"发得晚"和"发重复"的转发：把转发成功日志按目标列出，标出内容时间与日志时间的差。

结果写 data/_late.txt
"""
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

lines = Path("logs/bridge.log").read_text(encoding="utf-8", errors="replace").splitlines()
rows = []
for l in lines:
    m = re.match(r"^(2026-\d\d-\d\d \d\d:\d\d:\d\d).*\[转发成功\] (group:\d+|private:\d+) <- "
                 r"\[(\d\d-\d\d \d\d:\d\d)\] \[(.*?)\] (.*?)(?:（\+|$)", l)
    if m:
        rows.append({"log": m.group(1), "tgt": m.group(2), "ct": m.group(3),
                     "who": m.group(4), "body": m.group(5)[:30]})
out = [f"转发成功总数 {len(rows)}"]
# 只看最近这几天（新代码上线后）
recent = [r for r in rows if r["log"][5:] >= "10-10"]
out.append(f"10-10 起的转发: {len(recent)}")
late = []
for r in recent:
    try:
        lt = datetime.strptime(r["log"], "%Y-%m-%d %H:%M:%S")
        ct = datetime.strptime(f"{lt.year}-{r['ct']}", "%Y-%m-%d %H:%M")
        gap = (lt - ct).total_seconds() / 60.0
    except ValueError:
        continue
    if gap > 3:
        late.append((gap, r))
out.append(f"内容时间比日志时间早 3 分钟以上的（=发晚了）: {len(late)}")
for gap, r in sorted(late, key=lambda x: -x[0])[:20]:
    out.append(f"  晚 {gap:6.1f} 分钟 | {r['log'][5:]} | {r['tgt']:18} | {r['ct']} {r['body']}")

cnt = Counter((r["tgt"], r["ct"], r["body"]) for r in recent)
dups = {k: v for k, v in cnt.items() if v > 1}
out.append(f"（10-10 起）同一目标+同一内容时间+同一内容 被转发多次的: {len(dups)}")
for k, v in list(dups.items())[:15]:
    out.append(f"  x{v} {k}")

# 群里的顺序：内容时间是否单调
out.append("--- 群里最近 25 条（应单调递增）---")
prev = None
for r in [x for x in rows if x["tgt"].startswith("group")][-25:]:
    flag = ""
    if prev and r["ct"] < prev:
        flag = "   <<< 顺序反了"
    out.append(f"  {r['log'][5:]} | {r['ct']} | {r['body']}{flag}")
    prev = r["ct"]

Path("data/_late.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
