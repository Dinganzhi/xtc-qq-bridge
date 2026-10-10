# -*- coding: utf-8 -*-
"""找未使用的 import / 只赋值不读的实例属性（供人工确认后删除）。"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".nuitka-cache", "dist", "__pycache__", "data", "logs"}
rows: list[str] = []

for p in sorted(ROOT.rglob("*.py")):
    if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
        continue
    src = p.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    body_lines = src.splitlines()
    imported: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.append(((a.asname or a.name.split(".")[0]), node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    imported.append(((a.asname or a.name), node.lineno))
    for name, line in imported:
        uses = len(re.findall(r"\b" + re.escape(name) + r"\b", src))
        if uses <= 1:
            rows.append(f"{p.relative_to(ROOT)}:{line}: unused import {name}")
    # 只赋值、从没被读过的 self._xxx
    assigned = set(re.findall(r"self\.(_\w+)\s*=", src))
    read = set(re.findall(r"self\.(_\w+)\b(?!\s*=)", src))
    for name in sorted(assigned - read):
        rows.append(f"{p.relative_to(ROOT)}: self.{name} 只赋值、从未读取")

out = "\n".join(rows) or "(clean)"
dest = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "unused.txt"
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text(out, encoding="utf-8")
print(out)
