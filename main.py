# -*- coding: utf-8 -*-
# nuitka-project: --windows-console-mode=force
"""程序入口（源码运行 / Nuitka 编译后的单文件可执行程序 都走这里）。

用法：
  xtc-qq-bridge --check                # 环境自检后退出
  xtc-qq-bridge --debug dump-ui        # 打印当前界面控件（等价 tools/dump_ui.py）
  xtc-qq-bridge --debug adb-info       # 打印 adb/输入法/剪贴板诊断
  xtc-qq-bridge --once                 # 轮询一轮后退出（测试读取链路）
  xtc-qq-bridge --paths                # 打印运行路径（排障：配置在哪、日志在哪）
  xtc-qq-bridge --install-plugin       # 把捆绑的 AstrBot 插件装到 ~/.astrbot/...
  xtc-qq-bridge                        # 正常启动桥接

源码运行把 `xtc-qq-bridge` 换成 `python main.py` 即可。
"""
from __future__ import annotations

import argparse
import json
import locale
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runtime_paths                      # noqa: E402
from version import __version__           # noqa: E402

try:                                      # 非 UTF-8 控制台（Windows cp1252）下 print 中文不崩
    from utils.logger import make_console_tolerant
    make_console_tolerant()
except Exception:  # noqa: BLE001 兜底失败不影响主流程
    pass


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


def install_plugin(dest: str = "") -> int:
    """把打包进来的 AstrBot 插件复制到 AstrBot 插件目录（编译版无需 Python/install 脚本）。

    返回 0 成功、1 失败。目标目录默认 ~/.astrbot/data/plugins/xtc_qq_bridge。
    """
    src = runtime_paths.resource_dir(runtime_paths.PLUGIN_SRC_DIR)
    if src is None:
        print("[错误] 找不到捆绑的插件源码目录 astrbot_plugin_xtc_bridge/"
              "（编译时需带 --include-data-dir=astrbot_plugin_xtc_bridge=astrbot_plugin_xtc_bridge）")
        return 1
    target = Path(dest).expanduser() if dest else runtime_paths.plugin_dir()
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for f in sorted(src.iterdir()):
        if f.is_file() and f.suffix.lower() in (".py", ".yaml", ".json", ".md"):
            shutil.copy2(f, target / f.name)
            copied += 1
    print(f"[OK] 已复制 {copied} 个插件文件到: {target}")
    print("     下一步：AstrBot WebUI -> 插件管理 -> 重载/启用 xtc_qq_bridge")
    # 顺带把初始插件配置放到 AstrBot 的配置目录（已存在则不覆盖）
    cfg_dst = Path.home() / ".astrbot" / "data" / "config" / "xtc_qq_bridge_config.json"
    example = src / "plugin_config.example.json"
    if example.is_file() and not cfg_dst.exists():
        try:
            cfg_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(example, cfg_dst)
            print(f"[OK] 插件初始配置: {cfg_dst}")
        except OSError as e:
            print(f"[warn] 插件初始配置写入失败（可在 WebUI 里配置）: {e}")
    else:
        print("[OK] 插件配置已存在，保持不变")
    return 0


def verify_bundle() -> int:
    """校验"这个可执行文件本身是否完整"（不连设备）：依赖、捆绑资源、可写目录、配置解析。

    编译产物分发给别人后，用 `xtc-qq-bridge --verify` 就能确认资源没丢、YAML 能读。
    """
    ok = True
    print(f"版本: {__version__}")
    print(runtime_paths.describe())

    print("---- 依赖 ----")
    try:
        import yaml
        print(f"  pyyaml      : OK ({yaml.__version__})")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  pyyaml      : FAIL {e}（配置是 YAML 时必须；编译时需 --include-package=yaml）")
    for mod in ("adb_controller", "bridge", "xiaotiancai", "plugin_client", "qq_webhook",
                "msg_log", "tools.dump_ui"):
        try:
            __import__(mod)
            print(f"  {mod:<12}: OK")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  {mod:<12}: FAIL {e}")

    print("---- 捆绑资源 ----")
    checks = [
        (runtime_paths.CONFIG_EXAMPLE_NAME, True),
        (runtime_paths.PLUGIN_SRC_DIR + "/main.py", True),
        ("keyboardservice-debug.apk", False),
        ("README.md", False),
    ]
    for name, required in checks:
        p = runtime_paths.resource(*name.split("/"))
        if p.exists():
            print(f"  {name:<32}: OK  {p}")
        elif required:
            ok = False
            print(f"  {name:<32}: FAIL 缺失（编译时需 --include-data-*）")
        else:
            print(f"  {name:<32}: (缺，可选)")

    print("---- 可写目录 ----")
    runtime_paths.ensure_dirs()
    try:
        probe = runtime_paths.DATA_DIR / ".verify_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"  数据目录可写 : OK  {runtime_paths.DATA_DIR}")
    except OSError as e:
        ok = False
        print(f"  数据目录可写 : FAIL {e}")

    print("---- 配置 ----")
    cfg_path = runtime_paths.resolve_config()
    if cfg_path.exists():
        try:
            cfg = load_config(str(cfg_path))
            keys = ", ".join(sorted(cfg.keys())) or "(空)"
            print(f"  {cfg_path}")
            print(f"  解析         : OK（顶层键: {keys}）")
        except SystemExit as e:
            ok = False
            print(f"  解析         : FAIL {e}")
    else:
        print(f"  {cfg_path} 不存在 —— 首次运行会自动从模板生成，编辑后再启动")

    print("\n结果: " + ("全部通过" if ok else "存在问题（见上面的 FAIL）"))
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=f"小天才 <-> QQ 桥接（AstrBot 插件版）v{__version__}")
    ap.add_argument("--config", default="", help="配置文件路径（默认：exe 旁边的 config.yaml）")
    ap.add_argument("--check", action="store_true", help="环境自检后退出")
    ap.add_argument("--debug", choices=["dump-ui", "adb-info"], help="调试命令")
    ap.add_argument("--once", action="store_true", help="轮询一轮后退出")
    ap.add_argument("--no-adbkeyboard", action="store_true", help="跳过 ADBKeyBoard 自动安装")
    ap.add_argument("--paths", action="store_true", help="打印运行路径（配置/日志/数据目录）后退出")
    ap.add_argument("--verify", action="store_true",
                    help="校验本可执行文件是否完整（依赖/捆绑资源/配置解析，不连设备）")
    ap.add_argument("--install-plugin", nargs="?", const="", default=None, metavar="DEST",
                    help="把捆绑的 AstrBot 插件复制到 ~/.astrbot/data/plugins/xtc_qq_bridge"
                         "（可指定目标目录）")
    ap.add_argument("--no-banner", action="store_true", help="不打印启动横幅")
    ap.add_argument("--version", action="version", version=f"xtc-qq-bridge {__version__}")
    args = ap.parse_args()

    from utils.logger import setup_logger

    runtime_paths.ensure_dirs()

    if args.paths:
        print(runtime_paths.describe())
        print(f"版本          : {__version__}")
        sys.exit(0)

    if args.verify:
        sys.exit(verify_bundle())

    if args.install_plugin is not None:
        sys.exit(install_plugin(args.install_plugin))

    if args.check:
        print(f"版本: {__version__}")
        print(runtime_paths.describe())
        sys.exit(run_check())

    # 配置文件：命令行 > exe 旁边 > 当前目录 > 打包资源目录；缺失时从模板生成一份
    cfg_path = runtime_paths.resolve_config(args.config)
    if runtime_paths.ensure_config(cfg_path):
        setup_logger(level="INFO", file="logs/bridge.log")
        print(f"[初始化] 已从模板生成配置文件: {cfg_path}")
        print("         请先编辑它（QQ 号、联系人、账密、token），然后重新运行。")
        sys.exit(2)
    if not cfg_path.exists():
        print(f"[错误] 找不到配置文件: {cfg_path}")
        sys.exit(2)

    cfg = load_config(str(cfg_path))
    log = setup_logger(level=(cfg.get("logging") or {}).get("level", "INFO"),
                       file=(cfg.get("logging") or {}).get("file"))
    if not args.no_banner:
        log.info(f"小天才 <-> QQ 桥接 v{__version__} | 配置: {cfg_path}")
        log.info(f"数据目录: {runtime_paths.APP_DIR}（{'冻结' if runtime_paths.IS_FROZEN else '源码'}运行）"
                 f"{'' if runtime_paths.IS_FROZEN else ''}")

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

    # 保持子系统屏幕常亮：WSA 的虚拟屏闲置后会 Asleep，之后 uiautomator 一直报
    # "null root node"，每次读界面都要先唤醒（实机单次 dump 从 1s 变 20~45s）。
    if adb_cfg.get("keep_awake", True):
        if adb.keep_awake():
            log.info("已请求子系统保持常亮（避免息屏后 uiautomator 读不到界面）")
        else:
            log.warning("保持常亮设置未生效（镜像可能不允许写设置）；息屏时桥接会自动唤醒屏幕")

    from xiaotiancai import Xiaotiancai
    xtc = Xiaotiancai(adb, cfg.get("xiaotiancai") or {}, logger=log)
    xtc.launch()

    xc_cfg = cfg.get("xiaotiancai") or {}
    auto_login = bool(xc_cfg.get("auto_login", True))
    acc = xc_cfg.get("login") or {}
    has_cred = bool(str(acc.get("phone", "")).strip() and str(acc.get("password", "")).strip())
    state = xtc.login_state(force=True)
    if state == xtc.NOT_LOGGED_IN:
        if auto_login and has_cred:
            log.warning("小天才 App 未登录——约 5 秒后自动账密登录"
                        "（若失败会按 login_retry_interval 自动重试，可在 QQ 发 /小天才 自动登录 关闭）")
        elif auto_login and not has_cred:
            log.warning("小天才 App 未登录，但未配置 xiaotiancai.login.phone/password"
                        " -> 自动登录不会生效，请手动登录或补齐配置")
        else:
            log.warning("小天才 App 未登录（自动登录已关闭）——请手动登录")
    elif state == xtc.LOGIN_UNKNOWN:
        # 读不到界面（息屏/锁屏/App 不在前台）时不再谎报"未登录"
        log.warning("暂时无法确认小天才登录态（界面读不到或 App 不在前台）；"
                    "桥接会继续运行并按需自动恢复，不视为未登录")

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
    apk = runtime_paths.bundled_apk()
    print(f"捆绑 APK: {apk or '(未找到：设备上没装 ADBKeyBoard 时无法输入中文)'}")
    plug = runtime_paths.resource_dir(runtime_paths.PLUGIN_SRC_DIR)
    print(f"捆绑插件: {plug or '(未找到：编译时需 --include-data-dir)'}"
          f"{'' if plug else f'（可用 --install-plugin 安装到 {runtime_paths.plugin_dir()}）'}")
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
