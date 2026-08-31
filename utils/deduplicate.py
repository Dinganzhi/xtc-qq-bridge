# -*- coding: utf-8 -*-
"""消息去重与回声过滤。

- Deduplicator：LRU 集合 + TTL，用于“同一消息不被重复转发”（进程内）。
- EchoFilter：记录本桥发出过的文本（双向），防止轮询到自己发的消息又回传。
  支持文件持久化（store_path）：多实例/重启后共享回声状态，避免重复转发。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict


def _hash(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()


def normalize(text: str) -> str:
    """归一化文本用于比较：统一换行、折叠空白、去首尾。
    避免同一消息因 \r\n vs \n、多余空格等显示差异导致回声/去重失效。"""
    if not text:
        return ""
    return " ".join(str(text).replace("\r\n", "\n").replace("\r", "\n").split())


class Deduplicator:
    """容量上限 + TTL 的 LRU 去重。seen() 只查不记，mark() 记录。"""

    def __init__(self, capacity: int = 200, ttl: float = 120.0):
        self.capacity = capacity
        self.ttl = ttl
        self._data: OrderedDict[str, float] = OrderedDict()

    def _purge(self, now: float) -> None:
        expired = [k for k, ts in self._data.items() if now - ts > self.ttl]
        for k in expired:
            del self._data[k]
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def seen(self, *parts) -> bool:
        now = time.monotonic()
        self._purge(now)
        key = _hash(*[normalize(p) for p in parts])
        return key in self._data

    def mark(self, *parts) -> None:
        now = time.monotonic()
        key = _hash(*[normalize(p) for p in parts])
        self._data[key] = now
        self._data.move_to_end(key)
        self._purge(now)

    def clear(self) -> None:
        self._data.clear()


class EchoFilter:
    """记录本桥发出过的消息文本，TTL 内视为“回声”，不再转发。

    若给定 store_path，每次 mark 会写盘（JSON），多个桥接实例/重启后共享，
    防止“实例 A 发过、实例 B 又转发一遍”的重复问题。
    """

    def __init__(self, ttl: float = 60.0, capacity: int = 100, store_path: str | None = None):
        self.ttl = ttl
        self.capacity = capacity
        self._data: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._store_path = store_path
        if store_path:
            self._load()

    def _load(self) -> None:
        try:
            if not os.path.exists(self._store_path):
                return
            with open(self._store_path, "r", encoding="utf-8") as f:
                items = json.load(f)
            now = time.monotonic()
            for text, ts in items:
                if now - ts <= self.ttl:
                    self._data[text] = ts
        except Exception:  # noqa: BLE001 读取失败不致命
            self._data.clear()

    def _save(self) -> None:
        if not self._store_path:
            return
        try:
            os.makedirs(os.path.dirname(self._store_path) or ".", exist_ok=True)
            tmp = self._store_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([[t, ts] for t, ts in self._data.items()], f, ensure_ascii=False)
            os.replace(tmp, self._store_path)  # 原子替换
        except Exception:  # noqa: BLE001 写盘失败不致命
            pass

    def _purge(self, now: float) -> None:
        expired = [k for k, ts in self._data.items() if now - ts > self.ttl]
        for k in expired:
            del self._data[k]
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def mark(self, text: str) -> None:
        text = normalize(text)
        if not text:
            return
        now = time.monotonic()
        with self._lock:
            self._data[text] = now
            self._data.move_to_end(text)
            self._purge(now)
            self._save()

    def is_echo(self, text: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            return normalize(text) in self._data

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            if self._store_path:
                try:
                    os.remove(self._store_path)
                except OSError:
                    pass
