# -*- coding: utf-8 -*-
"""核对：不同性能档位下，Xiaotiancai / ADB 实际拿到什么值（不需要设备）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import speed_profile as sp  # noqa: E402
from main import load_config  # noqa: E402
from xiaotiancai import Xiaotiancai  # noqa: E402


class FakeAdb:
    def get_screen_size(self):
        return (1366, 768)


rows = []
for prof in sp.PROFILE_NAMES:
    cfg = load_config("config.yaml")
    cfg["performance"] = {"profile": prof, "force": True}
    name, filled, pinned = sp.apply_profile(cfg)
    xtc = Xiaotiancai(FakeAdb(), cfg.get("xiaotiancai") or {}, logger=None)
    rows.append(
        f"{prof:9} confirm_delay={xtc._confirm_delay} blind={xtc._blind_enabled} "
        f"fast_send={xtc._fast_send} snapshot_reuse={xtc._snapshot_reuse} "
        f"send_retries={xtc._send_retries} delay={xtc._delay} "
        f"dump_retries={cfg['adb']['dump_retries']} dump_delay={cfg['adb']['dump_delay']} "
        f"check_interval={cfg['xiaotiancai']['check_interval']} "
        f"keep_awake={cfg['adb']['keep_awake_interval']} "
        f"wait_retries={cfg['emoji']['wait_retries']} fwd_backoff={cfg['forward']['retry_backoff']}")

Path("data").mkdir(exist_ok=True)
Path("data/_profiles.txt").write_text("\n".join(rows), encoding="utf-8")
print("\n".join(rows))
