# -*- coding: utf-8 -*-
"""纯标准库的 PNG 编码（把"小天才表情包"的截图区域抠成图片发到 QQ）。

为什么不引 Pillow：本项目要保持零额外依赖——单文件产物每多一个库都要跟着变大，
而这里只需要"原始 RGBA 像素 -> 裁一块 -> 编码成 PNG"这一件事，
`zlib` + `struct` 就够了（screencap 给的正是 RGBA_8888 原始像素）。
"""
from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_png_rgb(width: int, height: int, rows: list[bytes]) -> bytes:
    """把 RGB 行数据编码成 PNG（真彩色、无 alpha、不做隔行）。

    rows: 长度为 height 的列表，每行 `width * 3` 字节。
    丢掉 alpha 是故意的：截屏在个别镜像上 alpha 通道是 0，带 alpha 会得到一张全透明图。
    """
    if width <= 0 or height <= 0 or len(rows) != height:
        raise ValueError(f"PNG 尺寸不合法: {width}x{height} rows={len(rows)}")
    expected = width * 3
    for r in rows:
        if len(r) != expected:
            raise ValueError(f"行宽不匹配: {len(r)} != {expected}")
    raw = b"".join(b"\x00" + r for r in rows)          # 每行前缀 filter=0（None）
    return (PNG_SIGNATURE
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 6))
            + _chunk(b"IEND", b""))


def rgba_to_rgb_rows(rgba: bytes, width: int, height: int, box=None) -> list[bytes]:
    """从 RGBA_8888 像素里取出 box=(x1,y1,x2,y2) 区域，返回 RGB 行数据。

    坐标会被夹到画面内；越界就返回空列表（调用方当成"抠不出来"）。
    """
    if width <= 0 or height <= 0 or len(rgba) < width * height * 4:
        return []
    x1, y1, x2, y2 = (0, 0, width, height) if not box else tuple(int(v) for v in box)
    x1 = max(0, min(x1, width - 1))
    y1 = max(0, min(y1, height - 1))
    x2 = max(x1 + 1, min(x2, width))
    y2 = max(y1 + 1, min(y2, height))
    rows: list[bytes] = []
    cw = x2 - x1
    for y in range(y1, y2):
        start = (y * width + x1) * 4
        line = rgba[start:start + cw * 4]
        if len(line) < cw * 4:
            return []
        rgb = bytearray(cw * 3)
        rgb[0::3] = line[0::4]
        rgb[1::3] = line[1::4]
        rgb[2::3] = line[2::4]
        rows.append(bytes(rgb))
    return rows


def crop_png_from_rgba(rgba: bytes, width: int, height: int, box) -> bytes:
    """从整屏 RGBA 像素里抠出 box 区域并编码成 PNG（抠不出来抛 ValueError）。"""
    rows = rgba_to_rgb_rows(rgba, width, height, box)
    if not rows:
        raise ValueError("截图区域不合法或数据不完整")
    x1, y1, x2, y2 = (0, 0, width, height) if not box else tuple(int(v) for v in box)
    x1 = max(0, min(x1, width - 1))
    x2 = max(x1 + 1, min(x2, width))
    return encode_png_rgb(x2 - x1, len(rows), rows)
