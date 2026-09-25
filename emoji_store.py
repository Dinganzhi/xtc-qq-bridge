# -*- coding: utf-8 -*-
"""表情原图读取：从小天才 App 的**可读外部数据目录 / 图片缓存**里拿贴纸原文件。

为什么不继续用截图：
- 截图只有一帧 —— **动图表情**（实测缓存里就是多帧循环 GIF）发到 QQ 会变成静止图；
- 还依赖气泡正好在屏幕上、位置准确。

实测（Android 13 / WSA，**不需要 root**：App 的*外部*数据目录 adb shell 可读；
内部 `/data/data/...` 不可读，`run-as` 也因 App 非 debuggable 不可用）：

1. **表情包解包目录**
   `<ext>/files/xtcdata/telwatch/weichat/emoji/newEmoji/<包>/<序>/<名>/big/<code>`
   名字索引是同一目录里的 `desc.json`（**UTF-16** 编码）：`{"code":"tiancaituQ_003","desc":"爱你"}`
2. **图片缓存**（Glide）
   `<ext>/cache/big_image/<pkg>/<ver>/<bucket>/<hash>.cnt`
   扩展名统一是 `.cnt`，但**内容是原始格式**（GIF / PNG / JPEG / WebP）。
   表情在消息显示时被写进缓存，所以"最近几十秒内新增的那张图片"就是刚收到的表情，
   而且是原始文件 —— 动图 GIF 的动画得以保留。

`find(name)` 的顺序：缓存里刚写的 -> 表情包目录按名字精确匹配；都拿不到返回 None，
调用方再退回按气泡截图、最后退回发文字。
"""
from __future__ import annotations

import json
import struct
import time

# 表情包解包目录（相对 App 的外部数据根目录）
EMOJI_SUBDIR = "files/xtcdata/telwatch/weichat/emoji"
# 图片缓存目录（Glide 会把原图写在这两个下面，文件名是 hash + .cnt）
CACHE_SUBDIRS = ("cache/big_image", "cache/small_image")

IMAGE_KINDS = ("gif", "png", "webp", "jpeg")


class EmojiStore:
    """按名字/时间从小天才 App 的数据目录里取表情原图。"""

    def __init__(self, adb, package: str = "com.xtc.watch", logger=None,
                 recent_secs: float = 45.0, index_ttl: float = 600.0,
                 max_px: int = 600, max_bytes: int = 3 * 1024 * 1024,
                 near_window: float = 300.0, fresh_window: float = 1800.0):
        self.adb = adb
        self.package = package
        self.logger = logger
        self.recent_secs = max(5.0, float(recent_secs))   # 缓存"刚写进来"的时间窗
        self.near_window = max(10.0, float(near_window))  # 按消息时间找缓存时允许的偏差
        # 补发/重渲染时，App 会重新写缓存文件（mtime 与消息时间能差十几分钟），
        # 所以"最近写过"的窗口给到 30 分钟；挑哪张靠形状+动图判定，不靠时间
        self.fresh_window = max(10.0, float(fresh_window))
        self.index_ttl = max(0.0, float(index_ttl))       # 名字索引缓存时长
        self.max_px = max(64, int(max_px))                # 太大的图不当表情（多半是照片）
        self.max_bytes = max(1024, int(max_bytes))
        self._index: dict[str, str] = {}                  # desc 名字 -> big/<code> 路径
        self._index_ts = float("-inf")

    # ------------------------------------------------------------------ 对外
    @property
    def root(self) -> str:
        return f"/sdcard/Android/data/{self.package}"

    def find(self, name: str = "", near_epoch: float | None = None,
             aspect: float | None = None) -> dict | None:
        """找这张表情的原图，返回 {data, kind, w, h, animated, path, source}；没有则 None。

        name：界面 content-desc 里的表情名（如 '啊啊啊'）；只用于"表情包目录"精确匹配。
        near_epoch：这条消息**自己的时间**（能找到"消息显示时写进缓存"的那张时最准）。
        aspect：屏幕上那个表情气泡的宽高比。缓存里同时会有聊天里的**照片**，
        用"形状对得上 + 是动图"来挑，比只看时间可靠得多（实测 App 会在重新渲染时
        重写缓存文件，mtime 与消息时间可以差十几分钟）。

        顺序刻意是**先按名字查表情包、再查缓存**：名字命中是确定性的，
        而"缓存里最近的图片"只是启发式。缓存只用来兜底"名字查不到"的表情
        （例如需要联网取回、不在本地表情包里的贴纸）。
        """
        hit = self._from_pack(name)
        if hit:
            return hit
        return self._from_cache(near_epoch, aspect)

    def warm(self) -> int:
        """预热表情包名字索引（可放后台线程调用，避免第一次收到表情时才建索引卡一下）。"""
        return len(self._pack_index())

    # ------------------------------------------------------------------ 缓存
    def _from_cache(self, near_epoch: float | None = None,
                    aspect: float | None = None) -> dict | None:
        """缓存里那张表情图：按"形状对得上 + 是动图 + 时间接近"挑最像的一张。

        为什么不纯按时间：实测 App 会在**重新渲染聊天时重写**缓存文件，mtime 与消息时间
        能差十几分钟（实例：15:24 的消息，缓存文件的 mtime 是 15:41），纯按时间全落空。
        贴纸与照片天然不同：贴纸多是**动图 GIF/WebP**、画面是**正方形**（与气泡同形状），
        照片多是 JPEG、长宽比也不同 —— 用这些特征挑稳得多。
        """
        # find 的 -mmin 只支持分钟：先按分钟粗筛（避免遍历整个缓存目录），精筛在下面
        if near_epoch:
            age_min = max(2, int(max(0.0, time.time() - near_epoch) // 60) + 2)
        else:
            age_min = max(1, int(self.recent_secs // 60) + 1)
        age_min = min(age_min, max(1, int(self.fresh_window // 60) + 1))
        try:
            listing = self.adb.shell(
                f"date +%s; find {self.root}/cache -type f -mmin -{age_min} "
                f"-exec stat -c '%Y %s %n' {{}} \\; 2>/dev/null", timeout=30)
        except Exception as e:  # noqa: BLE001 取不到就走别的路
            self._log("debug", f"读图片缓存失败: {e}")
            return None
        now = 0.0
        rows: list[tuple[float, int, str]] = []
        for line in (listing or "").splitlines():
            line = line.strip()
            if not now and line.isdigit():
                now = float(line)
                continue
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                rows.append((float(parts[0]), int(parts[1]), parts[2].strip()))
        if not rows or not now:
            return None
        cands: list[tuple] = []
        for mtime, size, path in rows:
            if size <= 0 or size > self.max_bytes:
                continue
            d_msg = abs(mtime - near_epoch) if near_epoch else None
            d_now = now - mtime
            if not (d_now <= self.fresh_window
                    or (d_msg is not None and d_msg <= self.near_window)):
                continue
            data = self._read(path)
            if not data:
                continue
            info = self.sniff(data)
            if not info or info.get("kind") not in IMAGE_KINDS:
                continue
            w, h = info.get("w") or 0, info.get("h") or 0
            if max(w, h) > self.max_px:
                continue                                  # 太大 -> 多半是聊天里的照片
            # ① 形状：与屏幕上的气泡形状一致（贴纸是方的，气泡也是方的）
            pen_shape = 0
            if aspect and w and h:
                ratio = w / h
                pen_shape = 0 if abs(ratio - aspect) <= 0.25 * max(1.0, aspect) else 1
            # ② 动图优先（表情基本是动图），JPEG 多半是照片 -> 排最后
            pen_kind = 0 if info.get("animated") else (0.5 if info.get("kind") != "jpeg" else 1)
            score = min([d for d in (d_msg, d_now) if d is not None] or [0.0])
            cands.append((pen_shape, pen_kind, score,
                          {**info, "data": data, "path": path, "source": "cache"}))
        if not cands:
            return None
        cands.sort(key=lambda c: (c[0], c[1], c[2]))
        return cands[0][3]

    # ------------------------------------------------------------------ 表情包目录
    def _pack_index(self) -> dict:
        """desc 名字 -> 表情文件路径（带 TTL 缓存；只在缓存里找不到时才建）。"""
        if self._index and time.monotonic() - self._index_ts < self.index_ttl:
            return self._index
        index: dict[str, str] = {}
        try:
            jsons = self.adb.shell(
                f"find {self.root}/{EMOJI_SUBDIR} -name desc.json 2>/dev/null", timeout=30)
        except Exception as e:  # noqa: BLE001
            self._log("debug", f"列表情包索引失败: {e}")
            return self._index
        for jp in (jsons or "").splitlines():
            jp = jp.strip()
            if not jp:
                continue
            try:
                raw = self._read(jp)
                if not raw:
                    continue
                text = (raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff")
                        else raw.decode("utf-8", errors="replace"))
                for e in (json.loads(text).get("emojis") or []):
                    desc = str(e.get("desc") or "").strip()
                    code = str(e.get("code") or "").strip()
                    if not desc or not code or desc in index:
                        continue
                    pack = jp.rsplit("/", 1)[0]
                    index[desc] = f"{pack}/big/{code}"    # big 优先（清晰）
            except Exception as e:  # noqa: BLE001 单个包坏了不影响其它
                self._log("debug", f"解析表情包索引失败 {jp}: {e}")
        if index:
            self._index = index
            self._index_ts = time.monotonic()
            self._log("info", f"表情包索引已建立：{len(index)} 个名字")
        return self._index or index

    def _from_pack(self, name: str) -> dict | None:
        name = (name or "").strip()
        if not name:
            return None
        path = self._pack_index().get(name)
        if not path:
            return None
        data = self._read(path)
        if not data:
            return None
        info = self.sniff(data)
        if not info or info.get("kind") not in IMAGE_KINDS:
            return None
        out = dict(info)
        out.update({"data": data, "path": path, "source": "pack"})
        return out

    # ------------------------------------------------------------------ 工具
    def _read(self, path: str) -> bytes:
        try:
            return self.adb.read_file(path)
        except Exception as e:  # noqa: BLE001 读不到就当没有
            self._log("debug", f"读文件失败 {path}: {e}")
            return b""

    def _log(self, level: str, msg: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(msg)

    @staticmethod
    def sniff(data: bytes) -> dict:
        """只读文件头判断格式/尺寸/是否动图（不解码，纯标准库）。

        动图判断对表情包很关键：GIF 看帧数/NETSCAPE 循环块，PNG 看 acTL（APNG），
        WebP 看 ANIM 块。
        """
        if not data:
            return {}
        if data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", data[6:10])
            return {"kind": "gif", "w": w, "h": h,
                    "animated": data.count(b"\x21\xf9\x04") > 1 or b"NETSCAPE2.0" in data[:2048]}
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return {"kind": "png", "w": w, "h": h, "animated": b"acTL" in data[:4096]}
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            w, h = EmojiStore._webp_size(data)
            return {"kind": "webp", "w": w, "h": h, "animated": b"ANIM" in data[:4096]}
        if data[:3] == b"\xff\xd8\xff":
            w, h = EmojiStore._jpeg_size(data)
            return {"kind": "jpeg", "w": w, "h": h, "animated": False}
        return {}

    @staticmethod
    def _webp_size(data: bytes) -> tuple[int, int]:
        fourcc = data[12:16]
        try:
            if fourcc == b"VP8X" and len(data) >= 30:
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if fourcc == b"VP8L" and len(data) >= 25:
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if fourcc == b"VP8 " and len(data) >= 30:
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return w, h
        except Exception:  # noqa: BLE001 解析失败就当尺寸未知
            pass
        return 0, 0

    @staticmethod
    def _jpeg_size(data: bytes) -> tuple[int, int]:
        i = 2
        n = len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return w, h
            i += 2 + seg
        return 0, 0
