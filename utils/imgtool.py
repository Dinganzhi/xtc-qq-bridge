# -*- coding: utf-8 -*-
"""纯标准库图片解码 + 相似度比对。

**为什么需要**：小天才 App 的贴纸只有一部分能在本地表情包里按名字查到。查不到时
只能去 Glide 图片缓存里"猜"，而缓存里同时躺着聊天照片、头像、别的贴纸 —— 按时间/
形状猜经常猜错（实测：小猫流汗的贴纸被转发成了弹吉他的乌龟）。所以这里把
**界面上气泡的截图**当作基准，逐张比对缓存候选，"像不像"由像素说话。

支持：
- PNG：灰度 / 调色板 / RGB / 带 alpha，位深 1/2/4/8，非隔行（贴纸都是这种）；
- GIF：LZW + 多帧合成（贴纸动图的主要格式）；
- JPEG：只解**基线**、只取 DC 系数 -> 1/8 缩略图（够比对用，比全解码省一个数量级）；
- WebP：不支持（返回空，调用方按"无法核对"处理）。

对外主要接口：`decode_frames()`、`frame_grid()`、`similarity()`、`image_grid()`。
"""
from __future__ import annotations

import struct
import zlib

FRAME_LIMIT = 6          # 一张图最多取多少帧来比对（动图取前几帧就够）


# --------------------------------------------------------------------------- 对外
def decode_frames(data: bytes, max_frames: int = FRAME_LIMIT) -> list[dict]:
    """解码出前几帧，返回 [{w, h, rgb, cover}]；不支持的格式返回 []。

    rgb：`w*h*3` 字节 RGB；cover：`w*h` 字节不透明度（没有 alpha 通道时为 None）。
    JPEG 走"只解 DC"的通道，返回的是 1/8 缩略图（w/h 已按缩略图算）。
    """
    if not data:
        return []
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        f = _png_frame(data)
        return [f] if f else []
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return _gif_frames(data, max_frames)
    if data[:2] == b"\xff\xd8":
        f = _jpeg_thumb(data)
        return [f] if f else []
    return []


def frame_grid(frame: dict, gw: int = 10, gh: int = 10,
               crop: float = 0.06) -> tuple[list, list]:
    """把一帧压成 gw*gh 的网格（每格平均 RGB）+ 每格权重（0~1，透明格为 0）。

    crop：先裁掉四周这一点比例再取网格 —— 屏幕上的气泡截图会带一点背景/圆角，
    原图也可能多出些留白，裁边能让两者的构图对齐。
    """
    w, h = int(frame.get("w") or 0), int(frame.get("h") or 0)
    rgb, cov = frame.get("rgb") or b"", frame.get("cover")
    cells: list[tuple[float, float, float]] = []
    weights: list[float] = []
    if w <= 0 or h <= 0 or len(rgb) < w * h * 3:
        return cells, weights
    pad_x = int(w * crop)
    pad_y = int(h * crop)
    x0, x1 = pad_x, max(pad_x + 1, w - pad_x)
    y0, y1 = pad_y, max(pad_y + 1, h - pad_y)
    cw = (x1 - x0) / gw
    ch = (y1 - y0) / gh
    for gy in range(gh):
        for gx in range(gw):
            sx, ex = x0 + int(gx * cw), max(x0 + int(gx * cw) + 1, x0 + int((gx + 1) * cw))
            sy, ey = y0 + int(gy * ch), max(y0 + int(gy * ch) + 1, y0 + int((gy + 1) * ch))
            ex, ey = min(ex, w), min(ey, h)
            step_x = max(1, (ex - sx) // 6)
            step_y = max(1, (ey - sy) // 6)
            rs = gs = bs = 0.0
            wt = 0.0
            cnt = 0
            for yy in range(sy, ey, step_y):
                base = yy * w
                for xx in range(sx, ex, step_x):
                    i = base + xx
                    a = (cov[i] / 255.0) if cov is not None else 1.0
                    cnt += 1
                    if a <= 0:
                        continue
                    o = i * 3
                    rs += rgb[o] * a
                    gs += rgb[o + 1] * a
                    bs += rgb[o + 2] * a
                    wt += a
            if wt <= 0 or cnt == 0:
                cells.append((255.0, 255.0, 255.0))
                weights.append(0.0)
            else:
                cells.append((rs / wt, gs / wt, bs / wt))
                weights.append(wt / cnt)
    return cells, weights


def similarity(a: tuple[list, list], b: tuple[list, list]) -> float:
    """两组网格的相似度 0~1：颜色差（越小越像）+ 亮度结构相关性（越大越像）。"""
    ca, wa = a
    cb, wb = b
    n = min(len(ca), len(cb))
    if n == 0:
        return 0.0
    mad = 0.0
    wsum = 0.0
    la: list[float] = []
    lb: list[float] = []
    lw: list[float] = []
    for i in range(n):
        w = min(wa[i] if i < len(wa) else 1.0, wb[i] if i < len(wb) else 1.0)
        if w <= 0:
            continue
        p, q = ca[i], cb[i]
        mad += w * (abs(p[0] - q[0]) + abs(p[1] - q[1]) + abs(p[2] - q[2])) / 3.0
        wsum += w
        la.append(0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2])
        lb.append(0.299 * q[0] + 0.587 * q[1] + 0.114 * q[2])
        lw.append(w)
    if wsum <= 0:
        return 0.0
    mad /= wsum * 255.0
    # 亮度相关性（对整体明暗差异免疫，只看结构）
    pear = 0.0
    k = len(la)
    if k >= 4:
        mw = sum(lw)
        ma = sum(x * w for x, w in zip(la, lw)) / mw
        mb = sum(x * w for x, w in zip(lb, lw)) / mw
        va = sum(w * (x - ma) ** 2 for x, w in zip(la, lw))
        vb = sum(w * (x - mb) ** 2 for x, w in zip(lb, lw))
        if va > 1e-6 and vb > 1e-6:
            cov = sum(w * (x - ma) * (y - mb) for x, y, w in zip(la, lb, lw))
            pear = cov / ((va * vb) ** 0.5)
    return max(0.0, min(1.0, 0.65 * (1.0 - mad) + 0.35 * max(0.0, pear)))


def image_grid(data: bytes, gw: int = 10, gh: int = 10,
               max_frames: int = FRAME_LIMIT) -> tuple[list, list]:
    """图片字节 -> 网格（动图取"最像自己第一帧"的那一帧？不，取所有帧合起来的最优交给调用方）。

    这里只取第一帧，多帧的最优比对用 `best_grid_similarity()`。
    """
    frames = decode_frames(data, max_frames=1)
    return frame_grid(frames[0], gw, gh) if frames else ([], [])


def best_grid_similarity(ref: tuple[list, list], data: bytes, gw: int = 10, gh: int = 10,
                         max_frames: int = FRAME_LIMIT) -> float:
    """候选图片（可能是动图）与基准网格的最佳相似度；解不出来返回 -1。"""
    frames = decode_frames(data, max_frames=max_frames)
    if not frames:
        return -1.0
    best = -1.0
    for f in frames:
        best = max(best, similarity(ref, frame_grid(f, gw, gh)))
    return best


# --------------------------------------------------------------------------- PNG
def _png_frame(data: bytes) -> dict | None:
    pos = 8
    idat = bytearray()
    plte = b""
    trns = b""
    w = h = depth = color = interlace = 0
    while pos + 8 <= len(data):
        ln = int.from_bytes(data[pos:pos + 4], "big")
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR" and len(body) >= 13:
            w, h, depth, color, _c, _f, interlace = struct.unpack(">IIBBBBB", body[:13])
        elif typ == b"PLTE":
            plte = body
        elif typ == b"tRNS":
            trns = body
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
    if not w or not h or interlace or depth not in (1, 2, 4, 8):
        return None
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if channels is None:
        return None
    if color in (2, 4, 6) and depth != 8:
        return None
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    bpp = max(1, channels * depth // 8)
    stride = (w * channels * depth + 7) // 8
    rows = bytearray()
    prev = bytearray(stride)
    p = 0
    for _y in range(h):
        if p >= len(raw):
            return None
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if len(line) < stride:
            return None
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif ft != 0:
            return None
        rows += line
        prev = line
    return _png_to_rgb(rows, w, h, depth, color, plte, trns)


def _unpack_bits(rows: bytearray, w: int, h: int, depth: int) -> list[int]:
    """把 1/2/4 位深的行数据摊平成每像素一个值（每行按字节对齐）。"""
    per_row = (w * depth + 7) // 8
    out: list[int] = []
    mask = (1 << depth) - 1
    for y in range(h):
        base = y * per_row
        for x in range(w):
            bit = x * depth
            byte = rows[base + (bit >> 3)] if base + (bit >> 3) < len(rows) else 0
            shift = 8 - depth - (bit & 7)
            out.append((byte >> shift) & mask)
    return out


def _png_to_rgb(rows: bytearray, w: int, h: int, depth: int, color: int,
                plte: bytes, trns: bytes) -> dict | None:
    n = w * h
    rgb = bytearray(n * 3)
    cov = bytearray(b"\xff" * n)
    if color == 3:                                   # 调色板
        idxs = _unpack_bits(rows, w, h, depth)
        for i, ix in enumerate(idxs):
            o = ix * 3
            if o + 2 >= len(plte):
                return None
            rgb[i * 3:i * 3 + 3] = plte[o:o + 3]
            if ix < len(trns) and trns[ix] == 0:
                cov[i] = 0
    elif color in (0, 4):                            # 灰度（+alpha）
        if depth == 8:
            for i in range(n):
                g = rows[i * (2 if color == 4 else 1)]
                rgb[i * 3] = rgb[i * 3 + 1] = rgb[i * 3 + 2] = g
                if color == 4:
                    cov[i] = rows[i * 2 + 1]
        else:
            scale = 255 // ((1 << depth) - 1)
            vals = _unpack_bits(rows, w, h, depth)
            for i, v in enumerate(vals):
                g = v * scale
                rgb[i * 3] = rgb[i * 3 + 1] = rgb[i * 3 + 2] = g
    elif color == 2:                                 # RGB
        rgb[:] = rows[:n * 3]
    elif color == 6:                                 # RGBA
        for i in range(n):
            rgb[i * 3:i * 3 + 3] = rows[i * 4:i * 4 + 3]
            cov[i] = rows[i * 4 + 3]
    else:
        return None
    return {"w": w, "h": h, "rgb": bytes(rgb), "cover": bytes(cov) if color in (4, 6, 3) else None}


# --------------------------------------------------------------------------- GIF
def _gif_lzw(data: bytes, min_code: int, expected: int) -> bytes:
    clear = 1 << min_code
    end = clear + 1
    table: list[bytes] = [bytes([i]) for i in range(clear)] + [b"", b""]
    code_size = min_code + 1
    next_code = end + 1
    out = bytearray()
    total_bits = len(data) * 8
    bit = 0
    prev: bytes | None = None
    while bit + code_size <= total_bits and len(out) < expected:
        byte_i = bit >> 3
        chunk = int.from_bytes(data[byte_i:byte_i + 3].ljust(3, b"\x00"), "little")
        code = (chunk >> (bit & 7)) & ((1 << code_size) - 1)
        bit += code_size
        if code == clear:
            table = [bytes([i]) for i in range(clear)] + [b"", b""]
            code_size = min_code + 1
            next_code = end + 1
            prev = None
            continue
        if code == end:
            break
        if prev is None:
            if code >= len(table):
                break
            entry = table[code]
        elif code < len(table) and table[code]:
            entry = table[code]
        elif code == next_code:
            entry = prev + prev[:1]
        else:
            break
        out += entry
        if prev is not None and next_code < 4096:
            new = prev + entry[:1]
            if len(table) <= next_code:
                table.append(new)
            else:
                table[next_code] = new
            next_code += 1
            if next_code == (1 << code_size) and code_size < 12:
                code_size += 1
        prev = entry
    return bytes(out[:expected])


def _gif_deinterlace(idx: bytes, w: int, h: int) -> bytes:
    out = bytearray(len(idx))
    rows = [0] * h
    i = 0
    for start, step in ((0, 8), (4, 8), (2, 4), (1, 2)):
        for y in range(start, h, step):
            if i >= h:
                break
            rows[i] = y
            i += 1
    for src, dst in enumerate(rows):
        out[dst * w:(dst + 1) * w] = idx[src * w:(src + 1) * w]
    return bytes(out)


def _gif_frames(data: bytes, max_frames: int) -> list[dict]:
    if len(data) < 13:
        return []
    w, h, flags, bg, _ar = struct.unpack("<HHBBB", data[6:13])
    if not w or not h:
        return []
    pos = 13
    gct = b""
    if flags & 0x80:
        n = 3 * (1 << ((flags & 7) + 1))
        gct = data[pos:pos + n]
        pos += n
    bg_rgb = gct[3 * bg:3 * bg + 3] if len(gct) >= 3 * bg + 3 else b"\x00\x00\x00"
    canvas = bytearray(bytes(bg_rgb) * (w * h))
    frames: list[dict] = []
    trans: int | None = None
    disposal = 0
    saved: bytes | None = None
    saved_rect: tuple[int, int, int, int] | None = None
    pending_disposal = 0
    pending_rect: tuple[int, int, int, int] | None = None
    while pos < len(data) and len(frames) < max_frames:
        blk = data[pos]
        pos += 1
        if blk == 0x21:                                    # 扩展块
            label = data[pos] if pos < len(data) else 0
            pos += 1
            if label == 0xF9 and pos < len(data):
                ln = data[pos]
                pos += 1
                gce = data[pos:pos + ln]
                pos += ln
                if pos < len(data) and data[pos] == 0:
                    pos += 1
                if len(gce) >= 4:
                    f2 = gce[0]
                    disposal = (f2 >> 2) & 7
                    trans = gce[3] if f2 & 1 else None
                continue
            while pos < len(data):                          # 跳过子块
                ln = data[pos]
                pos += 1
                if ln == 0:
                    break
                pos += ln
            continue
        if blk != 0x2C or pos + 9 > len(data):
            break
        x, y, fw, fh, lflags = struct.unpack("<HHHHB", data[pos:pos + 9])
        pos += 9
        lct = b""
        if lflags & 0x80:
            n = 3 * (1 << ((lflags & 7) + 1))
            lct = data[pos:pos + n]
            pos += n
        if pos >= len(data):
            break
        mcs = data[pos]
        pos += 1
        blob = bytearray()
        while pos < len(data):
            ln = data[pos]
            pos += 1
            if ln == 0:
                break
            blob += data[pos:pos + ln]
            pos += ln
        # 上一帧的 disposal 生效
        if pending_disposal == 2 and pending_rect:
            px0, py0, pw, ph = pending_rect
            for yy in range(py0, min(py0 + ph, h)):
                s = (yy * w + px0) * 3
                canvas[s:s + pw * 3] = bytes(bg_rgb) * min(pw, w - px0)
        elif pending_disposal == 3 and saved is not None and saved_rect:
            px0, py0, pw, ph = saved_rect
            for yy in range(py0, min(py0 + ph, h)):
                s = (yy * w + px0) * 3
                canvas[s:s + pw * 3] = saved[s:s + pw * 3]
        saved = bytes(canvas) if disposal == 3 else None
        saved_rect = (x, y, fw, fh) if disposal == 3 else None
        pending_disposal, pending_rect = disposal, (x, y, fw, fh)
        table = lct or gct
        idx = _gif_lzw(bytes(blob), mcs, fw * fh)
        if lflags & 0x40:
            idx = _gif_deinterlace(idx, fw, fh)
        if len(table) >= 3:
            for row in range(fh):
                yy = y + row
                if yy >= h:
                    break
                for col in range(fw):
                    xx = x + col
                    if xx >= w or row * fw + col >= len(idx):
                        continue
                    ix = idx[row * fw + col]
                    if trans is not None and ix == trans:
                        continue
                    o = ix * 3
                    if o + 2 >= len(table):
                        continue
                    s = (yy * w + xx) * 3
                    canvas[s:s + 3] = table[o:o + 3]
        frames.append({"w": w, "h": h, "rgb": bytes(canvas), "cover": None})
    return frames


# --------------------------------------------------------------------------- JPEG
class _Bits:
    """JPEG 熵编码段的比特读取（含 0xFF00 填充字节与重启标记处理）。"""

    def __init__(self, data: bytes, pos: int):
        self.data = data
        self.pos = pos
        self.buf = 0
        self.n = 0
        self.eof = False

    def _fill(self) -> None:
        if self.eof:
            return
        if self.pos >= len(self.data):
            self.eof = True
            return
        b = self.data[self.pos]
        self.pos += 1
        if b == 0xFF:
            nxt = self.data[self.pos] if self.pos < len(self.data) else 0
            if nxt == 0x00:
                self.pos += 1
            elif 0xD0 <= nxt <= 0xD7:
                self.pos += 1
                return                      # 重启标记不进比特流
            else:
                self.eof = True
                return
        self.buf = b
        self.n = 8

    def bit(self) -> int:
        if self.n == 0:
            self._fill()
            if self.n == 0:
                self.eof = True
                return 0
        self.n -= 1
        return (self.buf >> self.n) & 1

    def bits(self, k: int) -> int:
        v = 0
        for _ in range(k):
            v = (v << 1) | self.bit()
        return v


def _huff_table(bits: list[int], vals: bytes) -> dict:
    table: dict[tuple[int, int], int] = {}
    code = 0
    k = 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            if k < len(vals):
                table[(length, code)] = vals[k]
            k += 1
            code += 1
        code <<= 1
    return table


def _huff_decode(br: _Bits, table: dict) -> int:
    code = 0
    for length in range(1, 17):
        code = (code << 1) | br.bit()
        v = table.get((length, code))
        if v is not None:
            return v
    return 0


def _extend(v: int, t: int) -> int:
    return v - (1 << t) + 1 if t and v < (1 << (t - 1)) else v


def _jpeg_thumb(data: bytes) -> dict | None:
    """只解基线 JPEG 的 DC 系数，得到 (w/8)x(h/8) 的缩略图。"""
    pos = 2
    qt: dict[int, list[int]] = {}
    huff_dc: dict[int, dict] = {}
    huff_ac: dict[int, dict] = {}
    comps: list[list[int]] = []
    w = h = 0
    restart = 0
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9:
            break
        if pos + 2 > len(data):
            break
        seglen = int.from_bytes(data[pos:pos + 2], "big")
        seg = data[pos + 2:pos + seglen]
        nxt = pos + seglen
        if marker in (0xC0, 0xC1):
            if len(seg) < 6:
                return None
            h = int.from_bytes(seg[1:3], "big")
            w = int.from_bytes(seg[3:5], "big")
            n = seg[5]
            if len(seg) < 6 + 3 * n:
                return None
            comps = [[seg[6 + 3 * i], seg[7 + 3 * i] >> 4, seg[7 + 3 * i] & 15, seg[8 + 3 * i]]
                     for i in range(n)]
        elif marker in (0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return None                                     # 渐进式/算术编码：不支持
        elif marker == 0xC4:
            p = 0
            while p + 17 <= len(seg):
                tc_th = seg[p]
                counts = list(seg[p + 1:p + 17])
                total = sum(counts)
                vals = seg[p + 17:p + 17 + total]
                p += 17 + total
                t = _huff_table(counts, vals)
                if tc_th >> 4 == 0:
                    huff_dc[tc_th & 15] = t
                else:
                    huff_ac[tc_th & 15] = t
        elif marker == 0xDB:
            p = 0
            while p + 1 < len(seg):
                pq_tq = seg[p]
                p += 1
                n = 64 * (2 if pq_tq >> 4 else 1)
                raw = seg[p:p + n]
                p += n
                if pq_tq >> 4:
                    table = [int.from_bytes(raw[i * 2:i * 2 + 2], "big") for i in range(64)]
                else:
                    table = list(raw)
                qt[pq_tq & 15] = table
        elif marker == 0xDD and len(seg) >= 2:
            restart = int.from_bytes(seg[:2], "big")
        elif marker == 0xDA:
            if not comps or not w or not h or len(seg) < 4:
                return None
            ns = seg[0]
            if ns != len(comps):
                return None                                     # 单分量扫描：不支持
            order = []
            for i in range(ns):
                cid = seg[1 + 2 * i]
                tsel = seg[2 + 2 * i]
                comp = next((c for c in comps if c[0] == cid), None)
                if comp is None:
                    return None
                order.append((comp, tsel >> 4, tsel & 15))
            return _jpeg_scan(data, nxt, w, h, order, qt, huff_dc, huff_ac, restart)
        pos = nxt
    return None


def _jpeg_scan(data: bytes, pos: int, w: int, h: int, order: list, qt: dict,
               huff_dc: dict, huff_ac: dict, restart: int) -> dict | None:
    hmax = max(c[1] for c, _d, _a in order)
    vmax = max(c[2] for c, _d, _a in order)
    if not hmax or not vmax:
        return None
    mw = (w + 8 * hmax - 1) // (8 * hmax)                   # MCU 列数
    mh = (h + 8 * vmax - 1) // (8 * vmax)
    bw, bh = mw * hmax, mh * vmax                           # 以"亮度 8x8 块"为单位的输出尺寸
    br = _Bits(data, pos)
    pred = {c[0]: 0 for c, _d, _a in order}
    plane: dict[int, list[float]] = {c[0]: [0.0] * (bw * bh) for c, _d, _a in order}
    counter = 0
    for my in range(mh):
        for mx in range(mw):
            if restart and counter == restart:
                counter = 0
                for k in pred:
                    pred[k] = 0
                br.n = 0                                  # 对齐到标记
            counter += 1
            for comp, td, ta in order:
                cid, ch, cv, tq = comp
                dt = huff_dc.get(td)
                at = huff_ac.get(ta)
                q = qt.get(tq)
                if dt is None or at is None or not q:
                    return None
                for by in range(cv):
                    for bx in range(ch):
                        t = _huff_decode(br, dt)
                        diff = _extend(br.bits(t), t) if t else 0
                        pred[cid] += diff
                        val = pred[cid] * q[0] / 8.0 + 128.0
                        # 跳过 AC 系数（必须走完，才能对齐比特流）
                        k = 1
                        while k < 64:
                            rs = _huff_decode(br, at)
                            r, s = rs >> 4, rs & 15
                            if s == 0:
                                if r == 15:
                                    k += 16
                                    continue
                                break
                            k += r + 1
                            if s:
                                br.bits(s)
                        # 把这一块的值写进"亮度块网格"：色度块覆盖的每个亮度块都填同一个值
                        if cid == order[0][0][0]:                   # 亮度：自己一块
                            bxi, byi = mx * hmax + bx, my * vmax + by
                            if bxi < bw and byi < bh:
                                plane[cid][byi * bw + bxi] = val
                        else:                                       # 色度：按采样比例铺开
                            rw = max(1, hmax // ch)
                            rh = max(1, vmax // cv)
                            for dy in range(rh):
                                for dx in range(rw):
                                    bxi = (mx * ch + bx) * rw + dx
                                    byi = (my * cv + by) * rh + dy
                                    if bxi < bw and byi < bh:
                                        plane[cid][byi * bw + bxi] = val
                        if br.eof:
                            break
                    if br.eof:
                        break
                if br.eof:
                    break
            if br.eof:
                break
    rgb = bytearray(bw * bh * 3)
    ymap = plane[order[0][0][0]]
    cbmap = plane[order[1][0][0]] if len(order) >= 3 else None
    crmap = plane[order[2][0][0]] if len(order) >= 3 else None
    for i in range(bw * bh):
        yv = ymap[i]
        if cbmap is not None and crmap is not None:
            cb = cbmap[i] - 128.0
            cr = crmap[i] - 128.0
        else:
            cb = cr = 0.0
        o = i * 3
        rgb[o] = max(0, min(255, int(yv + 1.402 * cr)))
        rgb[o + 1] = max(0, min(255, int(yv - 0.344136 * cb - 0.714136 * cr)))
        rgb[o + 2] = max(0, min(255, int(yv + 1.772 * cb)))
    return {"w": bw, "h": bh, "rgb": bytes(rgb), "cover": None}
