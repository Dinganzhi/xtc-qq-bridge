# -*- coding: utf-8 -*-
"""程序入口。

用法：
  python main.py --check               # 环境自检后退出
  python main.py --debug dump-ui       # 打印当前界面控件（等价 tools/dump_ui.py）
  python main.py --once                # 轮询一轮后退出（测试读取链路）
  python main.py                       # 正常启动桥接
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_config(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        try:  # JSON 是 YAML 子集，无 pyyaml 时可用 JSON 格式配置
            return json.loads(text)
        except Exception:
            raise SystemExit("无法解析配置：请先 pip install pyyaml（或将 config.yaml 写成 JSON 格式）")
    except Exception as e:
        raise SystemExit(f"配置文件解析失败: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="小天才 ↔ QQ 桥接（AstrBot 插件版）")
    ap.add_argument("--config", default="config.yaml", help="配置文件路径")
    ap.add_argument("--check", action="store_true", help="环境自检后退出")
    ap.add_argument("--debug", choices=["dump-ui"], help="调试命令")
    ap.add_argument("--once", action="store_true", help="轮询一轮后退出")
    ap.add_argument("--no-adbkeyboard", action="store_true", help="跳过 ADBKeyBoard 自动安装")
    args = ap.parse_args()

    from utils.logger import setup_logger

    if args.check:
        sys.exit(run_check())

    cfg = load_config(args.config)
    log = setup_logger(level=(cfg.get("logging") or {}).get("level", "INFO"),
                       file=(cfg.get("logging") or {}).get("file"))

    from adb_controller import ADBController
    adb_cfg = cfg.get("adb") or {}
    adb = ADBController(adb_path=adb_cfg.get("path", ""),
                        host=adb_cfg.get("host", "127.0.0.1"),
                        port=int(adb_cfg.get("port", 5555)),
                        serial=adb_cfg.get("serial", ""), logger=log)

    if args.debug == "dump-ui":
        from tools import dump_ui
        sys.exit(dump_ui.run(adb))

    adb.ensure_connected()
    log.info(f"ADB 就绪: {adb.serial} | Android {adb.android_version()} "
             f"| 屏幕 {adb.get_screen_size()}")

    from xiaotiancai import Xiaotiancai
    xtc = Xiaotiancai(adb, cfg.get("xiaotiancai") or {}, logger=log)
    xtc.launch()

    if not xtc.is_logged_in():
        log.warning("小天才 App 未登录——将自动尝试账密登录"
                    "（已配置 xiaotiancai.login 时；否则请手动登录）")

    xc_cfg = cfg.get("xiaotiancai") or {}
    if xc_cfg.get("auto_install_adbkeyboard", True) and not args.no_adbkeyboard:
        adb.install_adbkeyboard()

    from bridge import MessageBridge, make_forwarder
    bridge = MessageBridge(cfg, adb, xtc, make_forwarder(cfg, log), logger=log)

    # 反向回调（NapCat / AstrBot 插件就绪后再启用 webhook.enabled）
    webhook_server = None
    wh_cfg = cfg.get("webhook") or {}
    if wh_cfg.get("enabled"):
        from qq_webhook import create_webhook_server
        webhook_server = create_webhook_server(
            bridge, host=wh_cfg.get("host", "127.0.0.1"),
            port=int(wh_cfg.get("port", 5000)),
            path=wh_cfg.get("path", "/qq_callback"),
            token=wh_cfg.get("token", ""), logger=log)
        threading.Thread(target=webhook_server.serve_forever, daemon=True).start()
        log.info(f"反向回调已启动: http://{wh_cfg.get('host', '127.0.0.1')}:"
                 f"{wh_cfg.get('port', 5000)}{wh_cfg.get('path', '/qq_callback')}")
        if not (wh_cfg.get("allow_from") or wh_cfg.get("allow_groups")):
            log.warning("webhook 白名单为空：QQ→小天才 将拒绝所有消息，"
                        "请在 config.yaml → webhook.allow_from（私聊）/ allow_groups（群聊）配置")

    bridge.start()
    if args.once:
        time.sleep(3)  # 至少跑一轮
        bridge.stop()
        log.info("--once 测试完成")
        return

    log.info("桥接运行中，Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
        if webhook_server:
            webhook_server.shutdown()
        log.info("已停止")


def run_check() -> int:
    from adb_controller import ADBController
    adb = ADBController()
    print(f"adb: {adb.adb_path}")
    if not adb.is_connected():
        try:
            adb.connect()
        except Exception as e:  # noqa: BLE001
            print(f"FAIL 连接: {e}")
            return 1
    print(f"设备: {adb.serial}")
    print(f"Android: {adb.android_version()} (SDK {adb.android_sdk()})")
    print(f"屏幕: {adb.get_screen_size()}")
    print(f"当前前台: {adb.get_current_focus() or '(未知)'}")
    try:
        root = adb.dump_ui()
        print(f"UI dump: OK（{len(list(root.iter('node')))} 节点）")
    except Exception as e:  # noqa: BLE001
        print(f"UI dump: FAIL {e}")
    print("自检完成。")
    return 0


if __name__ == "__main__":
    main()
