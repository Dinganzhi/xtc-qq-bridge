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
   缓存里同时躺着聊天照片、头像、**别的贴纸** —— 实测按时间/形状"猜"会把
   小猫流汗的贴纸猜成弹吉他的乌龟。

所以挑图分三层，**像不像由像素说话**：

1. 表情包目录里名字**唯一**命中 -> 直接用（确定性）；
2. 名字重名（实测 99 个名字里 55 个在多套包里重名）或查不到 -> 拿**界面上气泡的
   截图**当基准，逐个候选（表情包文件 + 缓存文件）比像素，够像才用；
3. 没有任何候选够像 -> 返回 None，调用方退回"就发那张气泡截图"（静止但一定是对的）。

`find(name, near_epoch, aspect, reference)` 就是这个入口。
"""
from __future__ import annotations

import json
import struct
import time

from utils import imgtool

# 表情包解包目录（相对 App 的外部数据根目录）
EMOJI_SUBDIR = "files/xtcdata/telwatch/weichat/emoji"
# 图片缓存目录（Glide 会把原图写在这两个下面，文件名是 hash + .cnt）
CACHE_SUBDIRS = ("cache/big_image", "cache/small_image")

IMAGE_KINDS = ("gif", "png", "webp", "jpeg")

# 相似度门槛：实测"同一张"能到 0.95 上下，而缓存里最像的无关图片只有 0.63
# （小猫流汗 vs 弹吉他的乌龟 = 0.50），0.80 留了很宽的余量。
MATCH_OK = 0.80


class EmojiStore:
    """按名字/像素比对从小天才 App 的数据目录里取表情原图。"""

    def __init__(self, adb, package: str = "com.xtc.watch", logger=None,
                 recent_secs: float = 45.0, index_ttl: float = 600.0,
                 max_px: int = 600, max_bytes: int = 512 * 1024,
                 near_window: float = 300.0, fresh_window: float = 1800.0,
                 match_ok: float = MATCH_OK, max_reads: int = 16,
                 strict_secs: float = 120.0, match_stop: float = 0.90):
        self.adb = adb
        self.package = package
        self.logger = logger
        self.recent_secs = max(5.0, float(recent_secs))   # 缓存"刚写进来"的时间窗
        self.near_window = max(10.0, float(near_window))  # 按消息时间找缓存时允许的偏差
        # 补发/重渲染时，App 会重新写缓存文件（mtime 与消息时间能差十几分钟），
        # 所以"最近写过"的窗口给到 30 分钟；挑哪张靠像素核对，不靠时间
        self.fresh_window = max(10.0, float(fresh_window))
        self.index_ttl = max(0.0, float(index_ttl))       # 名字索引缓存时长
        self.max_px = max(64, int(max_px))                # 太大的图不当表情（多半是照片）
        # 贴纸文件都很小（实测表情包 4~70KB、缓存里的贴纸 0.5~80KB）；限制字节数是为了
        # **不去读、更不去解**那些 1~2MB 的聊天照片（纯 Python 解一张 2000x3000 的 JPEG
        # 要十几秒，实测曾把"找一张表情"拖到 80 秒以上）。
        self.max_bytes = max(1024, int(max_bytes))
        self.match_ok = min(0.99, max(0.3, float(match_ok)))
        self.match_stop = min(0.999, max(self.match_ok, float(match_stop)))
        self.max_reads = max(1, int(max_reads))           # 每张表情最多核对多少个候选
        self.strict_secs = max(20.0, float(strict_secs))  # 没有基准图时"时间上必须很近"
        self._index: dict[str, list[str]] = {}            # desc 名字 -> [big/<code> ...]
        self._index_ts = float("-inf")
        # path -> (mtime, info, [网格...])：核对过的候选不必重复解码
        self._grids: dict[str, tuple] = {}

    # ------------------------------------------------------------------ 对外
    @property
    def root(self) -> str:
        return f"/sdcard/Android/data/{self.package}"

    def name_is_unique(self, name: str) -> bool:
        """这个名字在本地表情包里是不是只有一套包有（有就无需像素核对）。"""
        hits = self._pack_hits(name)
        return len(hits) == 1

    def find(self, name: str = "", near_epoch: float | None = None,
             aspect: float | None = None, reference: bytes | None = None) -> dict | None:
        """找这张表情的原图，返回 {data, kind, w, h, animated, path, source, score}。

        name：界面 content-desc 里的表情名（如 '流汗'）。
        near_epoch：这条消息**自己的时间**（找不到像素依据时按它挑"当时写进缓存的"）。
        aspect：屏幕上那个表情气泡的宽高比（没有基准图时用来排除照片）。
        reference：**界面上那张气泡的截图**（PNG 字节）。给了它就逐个候选比像素，
        只有真的像才敢发 —— 这是"货不对板"（小猫发成乌龟）的根治办法。

        拿不到可信的原图时返回 None，调用方退回"就发这张气泡截图"。
        """
        ref_grid = None
        if reference:
            try:
                ref_grid = imgtool.image_grid(reference)
                if not ref_grid[0]:
                    ref_grid = None
            except Exception as e:  # noqa: BLE001 解不出来就当没有基准
                self._log("debug", f"基准截图解码失败: {e}")
                ref_grid = None
        hits = self._pack_hits(name)
        if hits and len(hits) == 1:
            hit = self._read_hit(hits[0])
            if hit:
                # 名字在本地表情包里唯一 -> 确定性最高，直接用（也省一次界面截图）
                hit["score"] = None if ref_grid is None else 1.0
                return hit
        if hits and ref_grid is None:
            # 重名又没基准图：只能按老行为信第一套（日志里会写明）
            hit = self._read_hit(hits[0])
            if hit:
                self._log("debug", f"表情 {name!r} 在 {len(hits)} 套包里重名、"
                                  "又没有基准截图，按第一套发送")
                hit["score"] = None
                return hit
        best = None
        for path in hits:
            hit = self._read_hit(path)
            if not hit:
                continue
            hit["score"] = self._score(ref_grid, hit["data"], path, hit.get("mtime"))
            if best is None or hit["score"] > best["score"]:
                best = hit
        if best is not None and best["score"] >= self.match_ok:
            return best
        if hits and best is not None:
            self._log("info", f"表情 {name!r} 在表情包里找到的原图都不像界面上的那张"
                              f"（最像的 {best['score']:.2f} < {self.match_ok:.2f}），改去缓存里找")
        got = self._from_cache(near_epoch, aspect, ref_grid)
        if got:
            return got
        return None

    def warm(self) -> int:
        """预热表情包名字索引（可放后台线程调用，避免第一次收到表情时才建索引卡一下）。"""
        return len(self._pack_index())

    # ------------------------------------------------------------------ 缓存
    def _cache_listing(self) -> tuple[list[tuple[float, int, str]], float]:
        """缓存目录里的文件 ([(mtime, size, path)], 设备当前时间)。

        实测整个缓存也就几十个文件，一次 `find` 全列出来比按时间窗反复筛更省事，
        也不会因为"文件被重新渲染过（mtime 被刷新）"而漏掉目标；挑哪张靠像素核对。
        """
        try:
            listing = self.adb.shell(
                f"date +%s; find {self.root}/cache -type f "
                f"-exec stat -c '%Y %s %n' {{}} \\; 2>/dev/null", timeout=60)
        except Exception as e:  # noqa: BLE001 取不到就走别的路
            self._log("debug", f"读图片缓存失败: {e}")
            return [], time.time()
        now = 0.0
        rows: list[tuple[float, int, str]] = []
        for line in (listing or "").splitlines():
            line = line.strip()
            if not now and line.isdigit():
                now = float(line)
                continue
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                size = int(parts[1])
                if 0 < size <= self.max_bytes:
                    rows.append((float(parts[0]), size, parts[2].strip()))
        return rows, (now or time.time())

    def _score(self, ref_grid, data: bytes, path: str, mtime: float | None) -> float:
        """候选图片与基准网格的最佳相似度（解不出来返回 -1）。"""
        grids = self._grids_of(path, mtime, data)
        if not grids:
            return -1.0
        return max(imgtool.similarity(ref_grid, g) for g in grids)

    def _grids_of(self, path: str, mtime: float | None, data: bytes | None = None) -> list:
        """候选图片每帧的网格（带缓存）；解不出来的格式返回 []。"""
        ent = self._grids.get(path)
        if ent and mtime is not None and ent[0] == mtime:
            return ent[2]
        if data is None:
            data = self._read(path)
        if not data:
            return []
        frames = imgtool.decode_frames(data, max_frames=imgtool.FRAME_LIMIT)
        if not frames:
            return []
        grids = [imgtool.frame_grid(f) for f in frames]
        info = self.sniff(data)
        self._grids[path] = (mtime, info, grids)
        return grids

    def _from_cache(self, near_epoch: float | None = None, aspect: float | None = None,
                    ref_grid=None) -> dict | None:
        """缓存里那张表情图。

        有基准图：**按像素相似度选**（够像才返回；都不像返回 None，让调用方退回截图）。
        没有基准图：按"刚写过 + 形状对 + 是动图"猜，而且只敢用**时间上非常确定**的候选。
        """
        rows, now = self._cache_listing()
        if not rows:
            return None
        ordered = []
        for mtime, size, path in rows:
            fresh = (now - mtime) <= self.fresh_window
            near = near_epoch is not None and abs(mtime - near_epoch) <= self.near_window
            prio = 0 if fresh else (1 if near else 2)
            ordered.append((prio, -mtime, mtime, size, path))
        ordered.sort()

        reads = 0
        best: tuple | None = None
        for prio, _neg, mtime, size, path in ordered:
            if ref_grid is None:
                # 没有基准图：不解码（解不开的格式也不影响），只按"时间非常确定 + 形状 + 动图"猜。
                # 而且**老消息一律不猜**：补发一条几天前的消息时，缓存里可能有别的图
                # 刚被重新渲染过（mtime 很新），猜它就会"货不对板"（小猫发成乌龟）。
                if near_epoch is not None and abs(now - near_epoch) > self.strict_secs:
                    continue
                if not ((now - mtime) <= self.strict_secs
                        or (near_epoch is not None and abs(mtime - near_epoch) <= 60)):
                    continue
                data = self._read(path)
                if not data:
                    continue
                info = self.sniff(data)
                if not info or info.get("kind") not in IMAGE_KINDS:
                    continue
                grids = []
            else:
                ent = self._grids.get(path)
                cached = bool(ent) and ent[0] == mtime
                if cached:
                    info, grids = ent[1], ent[2]
                else:
                    if reads >= self.max_reads:
                        break
                    data = self._read(path)
                    reads += 1
                    if not data:
                        continue
                    info = self.sniff(data)
                    if not info or info.get("kind") not in IMAGE_KINDS:
                        continue
                    w0, h0 = info.get("w") or 0, info.get("h") or 0
                    if max(w0, h0) > self.max_px:
                        continue      # 先按**文件头**尺寸筛掉照片，别花时间去解码大图
                    grids = self._grids_of(path, mtime, data)
                    if not grids:
                        continue                              # WebP 之类解不了 -> 不能核对
            w, h = info.get("w") or 0, info.get("h") or 0
            if max(w, h) > self.max_px:
                continue
            anim = 1 if info.get("animated") else 0
            area = w * h
            if ref_grid is not None:
                score = max(imgtool.similarity(ref_grid, g) for g in grids)
            else:
                d = abs(mtime - near_epoch) if near_epoch else max(0.0, now - mtime)
                pen_shape = 0
                if aspect and w and h:
                    ratio = w / h
                    pen_shape = 0 if abs(ratio - aspect) <= 0.25 * max(1.0, aspect) else 1
                pen_kind = 0 if info.get("animated") else (0.5 if info.get("kind") != "jpeg" else 1)
                score = max(0.0, 1.0 - min(1.0, d / max(1.0, self.fresh_window)))
                score -= 0.05 * pen_shape + 0.03 * pen_kind
            key = (score, anim, area)
            if best is None or key > best[0]:
                best = (key, path, mtime, info, score)
            if ref_grid is not None and score >= self.match_stop:
                break                       # 已经非常像了，不必再翻后面的候选（省时间）
        if best is None:
            return None
        _key, path, mtime, info, score = best
        if ref_grid is not None and score < self.match_ok:
            self._log("info", f"[表情包] 缓存里没有像界面这张的原图"
                              f"（最像的 {score:.2f} < {self.match_ok:.2f}），改用气泡截图")
            return None
        data = self._read(path)
        if not data:
            return None
        out = dict(info)
        out.update({"data": data, "path": path, "source": "cache",
                    "mtime": mtime, "score": score if ref_grid is not None else None})
        return out

    # ------------------------------------------------------------------ 表情包目录
    def _pack_index(self) -> dict:
        """desc 名字 -> [表情文件路径...]（带 TTL 缓存；同名的包都留着，供"重名"判断）。"""
        if self._index and time.monotonic() - self._index_ts < self.index_ttl:
            return self._index
        index: dict[str, list[str]] = {}
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
                    if not desc or not code:
                        continue
                    pack = jp.rsplit("/", 1)[0]
                    path = f"{pack}/big/{code}"                # big 优先（清晰）
                    lst = index.setdefault(desc, [])
                    if path not in lst:
                        lst.append(path)
            except Exception as e:  # noqa: BLE001 单个包坏了不影响其它
                self._log("debug", f"解析表情包索引失败 {jp}: {e}")
        if index:
            self._index = index
            self._index_ts = time.monotonic()
            dup = sum(1 for v in index.values() if len(v) > 1)
            self._log("info", f"表情包索引已建立：{len(index)} 个名字（其中 {dup} 个重名）")
        return self._index or index

    def _pack_hits(self, name: str) -> list[str]:
        name = (name or "").strip()
        if not name:
            return []
        return list(self._pack_index().get(name) or [])

    def _read_hit(self, path: str) -> dict | None:
        data = self._read(path)
        if not data:
            return None
        info = self.sniff(data)
        if not info or info.get("kind") not in IMAGE_KINDS:
            return None
        out = dict(info)
        out.update({"data": data, "path": path, "source": "pack",
                    "mtime": -1.0})            # -1：表情包文件没有"写入时间"概念
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
