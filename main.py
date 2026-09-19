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
import locale
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def read_text_auto(path: str) -> str:
    """读配置文本：优先 UTF-8（含 BOM），失败再按系统 ANSI（中文 Windows = GBK）读。

    中文 Windows 上 Python 的默认文件编码是 cp936，而配置模板/大多数编辑器
    保存的是 UTF-8；不显式处理会在读取时报 UnicodeDecodeError 或读成乱码。
    """
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    for enc in ("utf-8", locale.getpreferredencoding(False), "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def load_config(path: str) -> dict:
    text = read_text_auto(path)
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
    ap = argparse.ArgumentParser(description="小天才 <-> QQ 桥接（AstrBot 插件版）")
    ap.add_argument("--config", default="config.yaml", help="配置文件路径")
    ap.add_argument("--check", action="store_true", help="环境自检后退出")
    ap.add_argument("--debug", choices=["dump-ui", "adb-info"], help="调试命令")
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
                        serial=adb_cfg.get("serial", ""), logger=log,
                        extra_ports=adb_cfg.get("extra_ports") or [],
                        wsa_port=int(adb_cfg.get("wsa_port", 0) or 0),
                        input_retries=int(adb_cfg.get("input_retries", 2) or 2),
                        dump_retries=int(adb_cfg.get("dump_retries", 2) or 2),
                        dump_delay=float(adb_cfg.get("dump_delay", 0.8) or 0.8),
                        focus_ttl=float(adb_cfg.get("focus_cache_ttl", 1.5) or 0.0),
                        dump_timeout=float(adb_cfg.get("dump_timeout", 60) or 60))

    if args.debug == "dump-ui":
        from tools import dump_ui
        sys.exit(dump_ui.run(adb))

    if args.debug == "adb-info":
        print(adb.dump_diagnostics())
        sys.exit(0)

    adb.ensure_connected(auto_launch_wsa=bool(adb_cfg.get("auto_launch_wsa", False)),
                         auto_launch_emulator=bool(adb_cfg.get("auto_launch_emulator", False)))
    log.info(f"ADB 就绪: {adb.device_summary()} | 屏幕 {adb.get_screen_size()}")
    if adb.is_wsa() or adb.runtime_tag() in ("Waydroid", "Genymotion"):
        log.info(f"检测到 {adb.runtime_tag()} 环境（输入法={adb.current_ime() or '未知'}，"
                 f"软键盘显示={adb.ime_shown()}）")

    # 关闭窗口/转场/属性动画：持续动画会让 uiautomator dump 一直 "could not get
    # idle state"（静默失败并读到旧文件），关闭后 dump 才能稳定工作。
    if (adb_cfg.get("disable_animations", True)):
        for key in ("window_animation_scale", "transition_animation_scale",
                    "animator_duration_scale"):
            try:
                adb.shell(f"settings put global {key} 0")
            except Exception:  # noqa: BLE001 个别镜像不允许写设置，忽略
                pass
        log.info("已关闭系统动画（uiautomator dump 稳定性）")

    from xiaotiancai import Xiaotiancai
    xtc = Xiaotiancai(adb, cfg.get("xiaotiancai") or {}, logger=log)
    xtc.launch()

    xc_cfg = cfg.get("xiaotiancai") or {}
    auto_login = bool(xc_cfg.get("auto_login", True))
    acc = xc_cfg.get("login") or {}
    has_cred = bool(str(acc.get("phone", "")).strip() and str(acc.get("password", "")).strip())
    if not xtc.is_logged_in(force=True):
        if auto_login and has_cred:
            log.warning("小天才 App 未登录——约 5 秒后自动账密登录"
                        "（若失败会按 login_retry_interval 自动重试，可在 QQ 发 /小天才 自动登录 关闭）")
        elif auto_login and not has_cred:
            log.warning("小天才 App 未登录，但未配置 xiaotiancai.login.phone/password"
                        " -> 自动登录不会生效，请手动登录或补齐配置")
        else:
            log.warning("小天才 App 未登录（自动登录已关闭）——请手动登录")

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
            log.warning("webhook 白名单为空：QQ->小天才 将拒绝所有消息，"
                        "请在 config.yaml -> webhook.allow_from（私聊）/ allow_groups（群聊）配置")

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
    from adb_controller import (ADBController, AdbError, IS_WINDOWS, available_launchers,
                                platform_tag, waydroid_present, wsa_adb_port, wsa_installed)
    try:
        adb = ADBController()
    except AdbError as e:
        print(f"FAIL 查找 adb: {e}")
        return 1
    print(f"平台: {platform_tag()}")
    print(f"adb: {adb.adb_path}")
    if IS_WINDOWS:
        print(f"内置探测：WSA 已安装={wsa_installed()} "
              f"注册表端口={wsa_adb_port()} 候选端口={adb._port_candidates()}")
    else:
        print(f"内置探测：Waydroid={waydroid_present()} "
              f"可用启动器={available_launchers() or '(无)'} 候选端口={adb._port_candidates()}")
    if not adb.is_connected():
        try:
            adb.connect()
        except Exception as e:  # noqa: BLE001
            print(f"FAIL 连接: {e}")
            return 1
    if not adb.is_connected():
        print("FAIL 连接：没有在线设备")
        print(adb._connect_hint())
        return 1
    print(adb.dump_diagnostics())
    try:
        root = adb.dump_ui()
        print(f"UI dump: OK（{len(list(root.iter('node')))} 节点）")
    except Exception as e:  # noqa: BLE001
        print(f"UI dump: FAIL {e}")
    if not adb.adbkeyboard_ready():
        print("提示: 未检测到 ADBKeyBoard（main.py 启动时会自动安装本地 APK）")
    elif adb.current_ime() != "com.android.adbkeyboard/.AdbIME":
        print(f"提示: 当前输入法为 {adb.current_ime() or '(未知)'}，发送前会自动切到 ADBKeyBoard")
    print("自检完成。")
    return 0


if __name__ == "__main__":
    main()
