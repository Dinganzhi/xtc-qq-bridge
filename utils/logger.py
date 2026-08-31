# -*- coding: utf-8 -*-
"""日志工具：控制台 + 滚动文件双输出。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


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
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    if file:
        try:
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
