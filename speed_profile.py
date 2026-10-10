# -*- coding: utf-8 -*-
"""速度 / 稳定 档位（`performance.profile`）：一次把"稳一点"或"快一点"的默认值铺开。

为什么要这个：发送快慢和"稳不稳"是**一组互相牵制**的参数（要不要先手盲打、等多久再 dump、
dump 重试几次、轮询间隔多少、图没下载完等多久…）。让用户逐个去调这些键不现实，
所以给几档：**stable / balanced / fast / turbo**。

规则（很重要）：档位只**填空**——用户已经在 config.yaml 里显式写过的键一律不动。
所以"用 fast 档，但我想把 confirm_delay 单独调大"这种也能写：
`performance.profile: fast` + `xiaotiancai.ui.confirm_delay: 1.2`。
"""
from __future__ import annotations

from typing import Any

# 每一档要铺的默认值：路径 -> 值（路径都是 config.yaml 里的真实层级）
# 想加一档/改一档，只动这里即可（README / config.example.yaml 里也有一份说明）
PROFILES: dict[str, dict[str, Any]] = {
    # ---------------- 稳定优先：不用盲打、等更久、重试更宽容 ----------------
    "stable": {
        "xiaotiancai.check_interval": 2.0,
        "xiaotiancai.label_stale_secs": 60,
        "xiaotiancai.catchup_scan": 30,
        "xiaotiancai.ui.blind_send": False,      # 不盲打：每条都先读界面再注入
        "xiaotiancai.ui.fast_send": False,       # 不点缓存坐标
        "xiaotiancai.ui.interaction_delay": 0.8,
        "xiaotiancai.ui.confirm_delay": 1.0,     # 点完发送多等一会儿再 dump（少一次白 dump）
        "xiaotiancai.ui.send_retries": 3,
        "xiaotiancai.ui.snapshot_reuse": 1.0,    # 快照很快过期 -> 每次重新读，信息最新
        "adb.dump_retries": 3,
        "adb.dump_delay": 1.0,
        "adb.keep_awake_interval": 5,            # 更勤地叫醒虚拟屏（息屏是最常见的故障源）
        "adb.input_retries": 3,
        "emoji.wait_base": 5,
        "emoji.wait_retries": 12,
        "emoji.wait_max_secs": 3600,
        "forward.retry_backoff": 30,
        "forward.retry_max_tries": 5,
    },
    # ---------------- 均衡（默认）：= 一直以来的默认行为 ----------------
    "balanced": {
        "xiaotiancai.check_interval": 2.0,
        "xiaotiancai.label_stale_secs": 60,
        "xiaotiancai.catchup_scan": 30,
        "xiaotiancai.ui.blind_send": True,
        "xiaotiancai.ui.fast_send": True,
        "xiaotiancai.ui.interaction_delay": 0.6,
        "xiaotiancai.ui.confirm_delay": 0.6,
        "xiaotiancai.ui.send_retries": 2,
        "xiaotiancai.ui.snapshot_reuse": 3.0,
        "adb.dump_retries": 2,
        "adb.dump_delay": 0.8,
        "adb.keep_awake_interval": 10,
        "adb.input_retries": 2,
        "emoji.wait_base": 5,
        "emoji.wait_retries": 8,
        "emoji.wait_max_secs": 1800,
        "forward.retry_backoff": 60,
        "forward.retry_max_tries": 3,
    },
    # ---------------- 速度优先：先手打字 + 更短等待 + 少一轮 dump 重试 ----------------
    "fast": {
        "xiaotiancai.check_interval": 3.0,       # 轮询留出空隙 -> 发送更容易立刻拿到界面锁
        "xiaotiancai.label_stale_secs": 60,
        "xiaotiancai.catchup_scan": 20,
        "xiaotiancai.ui.blind_send": True,
        "xiaotiancai.ui.fast_send": True,
        "xiaotiancai.ui.interaction_delay": 0.45,
        "xiaotiancai.ui.confirm_delay": 0.8,     # 实测：等太短第一帧没画好，白 dump 一次约 3.5 秒
        "xiaotiancai.ui.send_retries": 2,
        "xiaotiancai.ui.snapshot_reuse": 6.0,
        "adb.dump_retries": 2,
        "adb.dump_delay": 0.5,
        "adb.keep_awake_interval": 8,
        "adb.input_retries": 2,
        "emoji.wait_base": 4,
        "emoji.wait_retries": 6,
        "emoji.wait_max_secs": 1800,
        "forward.retry_backoff": 45,
        "forward.retry_max_tries": 3,
    },
    # ---------------- 极速：能省的都省（代价：失败会更多地如实上报） ----------------
    "turbo": {
        "xiaotiancai.check_interval": 4.0,
        "xiaotiancai.label_stale_secs": 60,
        "xiaotiancai.catchup_scan": 12,
        "xiaotiancai.ui.blind_send": True,
        "xiaotiancai.ui.fast_send": True,
        "xiaotiancai.ui.interaction_delay": 0.3,
        "xiaotiancai.ui.confirm_delay": 1.0,     # 只 dump 一次就得准，所以等得比 fast 还久一点
        "xiaotiancai.ui.send_retries": 1,
        "xiaotiancai.ui.snapshot_reuse": 8.0,
        "adb.dump_retries": 1,
        "adb.dump_delay": 0.4,
        "adb.keep_awake_interval": 6,
        "adb.input_retries": 1,
        "emoji.wait_base": 3,
        "emoji.wait_retries": 4,
        "emoji.wait_max_secs": 900,
        "forward.retry_backoff": 30,
        "forward.retry_max_tries": 2,
    },
}

DEFAULT_PROFILE = "balanced"
PROFILE_NAMES = tuple(PROFILES)


def raw_profile(cfg: dict) -> str:
    """用户写的原始档位名（用来提示"这个名字不认识"）。"""
    perf = cfg.get("performance") or {}
    return str(perf.get("profile") or cfg.get("speed_profile") or "").strip()


def get_profile(cfg: dict) -> str:
    """读 `performance.profile`（也兼容顶层 `speed_profile`），非法值回落到 balanced。"""
    name = raw_profile(cfg).lower()
    return name if name in PROFILES else DEFAULT_PROFILE


def apply_profile(cfg: dict) -> tuple[str, list[str], list[str]]:
    """把档位默认值**填进 cfg**。返回 (档位名, 实际填入的键, 被用户写死而跳过的键)。

    默认只填空（用户显式写过的键优先）。想"档位说了算"（连写死的键也覆盖）：
    `performance.force: true`。
    """
    name = get_profile(cfg)
    force = bool((cfg.get("performance") or {}).get("force"))
    bundle = PROFILES[name]
    filled: list[str] = []
    pinned: list[str] = []
    for path, value in bundle.items():
        node: Any = cfg
        parts = path.split(".")
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        leaf = parts[-1]
        if leaf in node and node[leaf] is not None and not force:
            pinned.append(f"{path}={node[leaf]}")
            continue
        if leaf in node and node[leaf] == value:
            continue                       # 值本来就一样，不用记
        node[leaf] = value
        filled.append(f"{path}={value}")
    return name, filled, pinned


def describe() -> str:
    """给 --check / 帮助文本用的一句话说明。"""
    return ("速度档位 performance.profile：" +
            "；".join(f"{k}={_short(v)}" for k, v in PROFILES.items()))


def _short(bundle: dict) -> str:
    return (f"先手打字={'开' if bundle['xiaotiancai.ui.blind_send'] else '关'}、"
            f"确认等待={bundle['xiaotiancai.ui.confirm_delay']}s、"
            f"轮询={bundle['xiaotiancai.check_interval']}s")
