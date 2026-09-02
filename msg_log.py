# -*- coding: utf-8 -*-
"""本地消息库：`/小天才 历史消息` 的数据源（不滚动读界面）。

记录桥接处理过的**真实对话消息**（手表侧读到的消息 + 从 QQ 发进小天才的消息），
不记录系统提示（发送成功/发送失败）、命令文本、桥接自己的回复。
文件持久化（data/msg_log.json），重启不丢；按时间追加，容量封顶。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class MessageLog:
    def __init__(self, path: str = "", cap: int = 1000):
        self.path = Path(path) if path else Path(__file__).resolve().parent / "data" / "msg_log.json"
        self.cap = max(50, int(cap))
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._entries = [e for e in data if isinstance(e, dict)][-self.cap:]
        except Exception:  # noqa: BLE001 读取失败不致命
            self._entries = []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(self.path) + ".tmp"
            Path(tmp).write_text(
                json.dumps(self._entries[-self.cap:], ensure_ascii=False), encoding="utf-8")
            Path(tmp).replace(self.path)
        except Exception:  # noqa: BLE001 写盘失败不影响运行
            pass

    def append(self, kind: str, sender: str, text: str,
               t: float | None = None) -> None:
        """追加一条消息。kind: 'xtc'（手表侧）/'qq'（QQ 发入小天才）。"""
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._entries.append({
                "t": t if t is not None else time.time(),
                "kind": kind,
                "sender": (sender or "").strip(),
                "text": text,
            })
            if len(self._entries) > self.cap:
                del self._entries[:len(self._entries) - self.cap]
            self._save()

    def recent(self, count: int = 20) -> list[dict]:
        """返回最近 count 条（时间从旧到新）。"""
        with self._lock:
            return list(self._entries[-max(1, int(count)):])

    def count(self) -> int:
        with self._lock:
            return len(self._entries)
