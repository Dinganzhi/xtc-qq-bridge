# -*- coding: utf-8 -*-
"""日志工具：控制台 + 滚动文件双输出。

控制台编码兜底（重要）：
中文 Windows 的控制台默认是 GBK（cp936），而日志文本里可能带有 GBK 无法表示的字符
（例如某些 emoji、日文标点、特殊符号）。此时 `StreamHandler` 会在
`stream.write()` 抛 `UnicodeEncodeError`，logging 会打印一句 "--- Logging error ---"
后**整条日志丢失**——表现就是"控制台看不到收到的命令/消息"。
这里统一把标准输出/错误设成 `errors="replace"`（保留原编码，只把无法表示的字符换成 ?），
日志再也不会因为个别字符而整条消失。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _tolerant_stream(stream):
    """让标准流在遇到无法编码的字符时用 ? 代替，而不是抛异常丢日志。"""
    if stream is None:
        return None
    try:
        stream.reconfigure(errors="replace")   # Python 3.7+：保留原编码，只改错误处理
        return stream
    except Exception:  # noqa: BLE001 老版本/被替换的流：退化为包装器
        pass

    class _Tolerant:
        def __init__(self, raw):
            self._raw = raw

        def write(self, data):
            try:
                return self._raw.write(data)
            except UnicodeEncodeError:
                enc = getattr(self._raw, "encoding", None) or "utf-8"
                return self._raw.write(data.encode(enc, "replace").decode(enc, "replace"))

        def flush(self):
            try:
                self._raw.flush()
            except Exception:  # noqa: BLE001
                pass

        def __getattr__(self, item):
            return getattr(self._raw, item)

    return _Tolerant(stream)


def setup_logger(level: str = "INFO", file: str | None = None,
                 name: str = "xtc-bridge", console: bool = True) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:  # 已初始化过
        return logger

    try:
        logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    except Exception:
        logger.setLevel(logging.INFO)

    fmt = logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    if console:
        ch = logging.StreamHandler(_tolerant_stream(sys.stdout))
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    if file:
        try:
            # 相对路径按"可写数据目录"解析：Nuitka onefile 下日志写在 exe 旁边，
            # 而不是退出即被删除的临时解包目录（见 runtime_paths.py）
            try:
                import runtime_paths
                file = str(runtime_paths.log_path(file))
            except Exception:  # noqa: BLE001 单独使用本模块时不强依赖
                pass
            from pathlib import Path
            Path(file).parent.mkdir(parents=True, exist_ok=True)  # 自动创建日志目录
            fh = RotatingFileHandler(file, maxBytes=5 * 1024 * 1024, backupCount=3,
                                     encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except Exception as e:  # 日志文件打不开不致命
            logger.warning(f"日志文件不可用({file}): {e}")

    return logger


def get_logger(name: str = "xtc-bridge") -> logging.Logger:
    return logging.getLogger(name)
