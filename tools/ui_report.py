# -*- coding: utf-8 -*-
"""把当前界面的控件清单写成文本文件（避免控制台中文乱码），用于定位登录按钮等控件。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yaml  # noqa: E402

from adb_controller import ADBController  # noqa: E402
from xiaotiancai import Xiaotiancai  # noqa: E402

cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
adb_cfg = cfg.get("adb") or {}
adb = ADBController(adb_path=adb_cfg.get("path", ""), serial=adb_cfg.get("serial", ""))
adb.keep_awake()
xtc = Xiaotiancai(adb, cfg.get("xiaotiancai") or {})
root = xtc._dump_fast()
out = Path("logs/ui_dump_report.txt")
lines = [f"activity={xtc.current_activity()}", f"window={xtc.current_window()}",
         f"login_state={xtc.login_state(force=True)}",
         f"_find_login_button={'FOUND' if xtc._find_login_button(root) is not None else 'NONE'}",
         ""]
for n in root.iter("node"):
    cls = (n.get("class") or "").split(".")[-1]
    rid = n.get("resource-id") or ""
    text = (n.get("text") or "").replace("\n", "\\n")
    desc = n.get("content-desc") or ""
    clickable = n.get("clickable") or "false"
    enabled = n.get("enabled") or "?"
    if not (text or desc or rid):
        continue
    lines.append(f"{cls:<14} id={rid.split('/')[-1]:<28} click={clickable:<5} en={enabled:<5} "
                 f"text={text[:30]:<32} desc={desc[:24]}")
out.write_text("\n".join(lines), encoding="utf-8")
print(f"written {out} ({len(lines)} lines)")
