# -*- coding: utf-8 -*-
"""WSA / WSABuilds 网络守护（**独立工具，与主程序无关**，放在 tools/ 下单独运行）

为什么需要它
------------
WSA（含 WSABuilds / MagiskOnWSA）跑久了会"隔三差五断网"：宿主侧 adb 还在，
但 Android 子系统里的网络栈已经不通（或 adb 端口掉了）。表现是桥接程序里
`uiautomator` 读不到界面、消息发不出去、`adb devices` 里设备时有时无。
这不是桥接程序的 bug，靠桥接自己重连也救不回来，所以单独做一个守护进程：
它只做"看门 + 分级修复"，不碰桥接逻辑，可以单独开一个终端常驻。

检测 + 分级修复（由轻到重，逐级升级，成功后退回）
-------------------------------------------------
1. adb 掉线         -> 重连（读注册表拿新端口 / 常见端口轮询）
                    -> 仍不行：`adb kill-server` + `start-server` 再连
2. 子系统网络不通    -> 关飞行模式 + `svc wifi/data enable`
（adb 通、ping 不通）-> 关再开 wifi（airplane 切换）复位网络栈
3. 依旧不通          -> `adb reboot` 重启 Android 子系统（冷却期内只做轻量修复）
4. adb 完全连不上    -> （需显式开启 --restart-wsa）重启宿主 WSA 客户端并等新端口

用法
----
  python tools/wsa_net_guard.py                 # 常驻守护（默认 30s 检测一次）
  python tools/wsa_net_guard.py --status        # 只打印一次状态（退出码 0=正常）
  python tools/wsa_net_guard.py --once          # 检测一次，必要时修复
  python tools/wsa_net_guard.py --dry-run       # 只诊断，不执行任何修复动作
  python tools/wsa_net_guard.py --no-reboot     # 禁止重启 Android 子系统
  python tools/wsa_net_guard.py --restart-wsa   # 允许重启宿主 WSA 客户端（更重）
  python tools/wsa_net_guard.py --interval 20 --serial 127.0.0.1:58526
  python tools/wsa_net_guard.py --test          # 跑内置离线自测（不需要设备）

配置（可选，写在 config.yaml 里；命令行参数优先）：
  wsa_guard:
    interval: 30                 # 检测间隔（秒）
    ping_hosts: ["223.5.5.5", "8.8.8.8"]
    recover_wait: 8              # 每次修复后等待（秒）再复查
    allow_reboot: true           # 允许 adb reboot 重启子系统（第 3 级）
    reboot_cooldown: 600         # 两次重启的最小间隔（秒）
    allow_restart_wsa: false     # 允许重启宿主 WSA 客户端（第 4 级，最重）
    light_recover: true          # 允许飞行模式/wifi 开关等轻量修复
    log_file: "logs/wsa_guard.log"

状态文件：data/wsa_guard_state.json（最近一次检测结果，便于其它脚本读取）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # 中文 Windows 控制台兜底（日志里可能有中文）
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass

from adb_controller import (IS_WINDOWS, AdbError, _WSA_COMMON_PORTS,  # noqa: E402
                            _is_wsa_serial, find_adb, launch_wsa, platform_tag,
                            wsa_adb_port, wsa_connection_info, wsa_installed)
from utils.logger import setup_logger  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "wsa_guard_state.json"
DEFAULT_HOSTS = ("223.5.5.5", "8.8.8.8")
TASK_NAME = "XTC-WSA-NetGuard"


# ---------------------------------------------------------------- 纯逻辑（可离线测试）
def parse_ping(out: str):
    """从 `ping` 输出判断连通性。

    返回 True（通）/ False（不通）/ None（无法判断，例如镜像里没有 ping 命令）。
    不把"命令不存在/权限不足"当成断网，避免误触发重启。
    """
    if not out:
        return None
    low = out.lower()
    for bad in ("not found", "inaccessible", "permission denied", "unknown option",
                "no such file", "bad address"):
        if bad in low:
            return None
    if "100% packet loss" in low or "100% loss" in low:
        return False
    if " 0% packet loss" in low and ("1 received" in low or "1 packets received" in low
                                    or "received" in low):
        return True
    if "1 packets transmitted, 1 received" in low or "1 received" in low:
        return True
    if "destination host unreachable" in low or "network is unreachable" in low:
        return False
    if "unknown host" in low:
        return False
    return None


def classify(shell_ok: bool, ping: bool | None) -> str:
    """把一轮检测结果归类：'ok' / 'no_adb' / 'shell_dead' / 'net_down' / 'unknown'。"""
    if not shell_ok:
        return "no_adb"
    if ping is True:
        return "ok"
    if ping is False:
        return "net_down"
    return "unknown"


def plan_action(problem: str, streak: int, level: int, *, allow_reboot: bool,
                allow_restart_wsa: bool, light_recover: bool,
                reboot_allowed_now: bool) -> str:
    """按问题类型 + 连续失败次数决定这一步做什么（纯函数，便于测试）。

    problem: classify() 的返回值
    streak:  连续异常的轮数（1 起）
    level:   已经升级到的级别（0 起），用于"重启后仍未恢复 -> 再升级"的判定
    返回动作名：none / reconnect / kill_server / restart_wsa / net_reset /
                net_cycle / guest_reboot
    """
    if problem in ("ok", "unknown"):
        return "none"
    if problem == "no_adb":
        if allow_restart_wsa and (level >= 3 or streak >= 4):
            return "restart_wsa"
        if streak >= 3:
            return "kill_server"
        return "reconnect"
    if problem == "shell_dead":
        return "kill_server" if streak >= 2 else "reconnect"
    # net_down：adb/shell 是通的，只是子系统网络不通
    if not light_recover:
        return "none"
    if streak <= 1:
        return "net_reset"
    if streak == 2:
        return "net_cycle"
    if allow_reboot and reboot_allowed_now:
        return "guest_reboot"
    return "net_cycle"      # 冷却期内只做轻量修复，不重启


def host_port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """宿主侧 TCP 探测 adb 端口是否在监听（WSA 关掉时端口不会开）。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- ADB 小封装
class Adb:
    """只用到 adb 的几个命令，独立于主程序的 ADBController（本工具要保持"分开"）。"""

    def __init__(self, adb_path: str, serial: str = "", logger=None):
        self.adb_path = adb_path
        self.serial = serial
        self.logger = logger

    def run(self, args: list, timeout: float = 25.0, check: bool = False):
        cmd = [self.adb_path] + (["-s", self.serial] if self.serial else []) + list(args)
        kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "timeout": timeout}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            p = subprocess.run(cmd, **kwargs)
        except subprocess.TimeoutExpired:
            if check:
                raise AdbError(f"adb 超时: {' '.join(args)}")
            return "", "timeout", 1
        out = p.stdout.decode("utf-8", errors="replace")
        err = p.stderr.decode("utf-8", errors="replace")
        if check and p.returncode != 0:
            raise AdbError(f"adb 失败({p.returncode}): {' '.join(args)} {err.strip()[:200]}")
        return out, err, p.returncode

    def shell(self, cmd: str, timeout: float = 25.0) -> str:
        out, _err, rc = self.run(["shell", cmd], timeout=timeout)
        if rc != 0:
            raise AdbError(f"shell 失败: {cmd}")
        return out

    def try_shell(self, cmd: str, timeout: float = 25.0) -> str:
        try:
            return self.shell(cmd, timeout=timeout)
        except AdbError:
            return ""

    def devices(self) -> dict:
        out, _err, _rc = self.run(["devices"], timeout=15)
        states: dict = {}
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                states[parts[0]] = parts[1]
        return states

    def online_serials(self) -> list:
        return [s for s, st in self.devices().items() if st == "device"]

    def connect(self, target: str, timeout: float = 8.0) -> bool:
        out, _err, _rc = self.run(["connect", target], timeout=timeout)
        return "connected" in out.lower()

    def adopt(self, prefer: str = "") -> str:
        """选一个在线设备作为 serial：优先指定值 -> WSA 形态 -> 第一个在线设备。"""
        serials = self.online_serials()
        if prefer and prefer in serials:
            self.serial = prefer
            return prefer
        for s in serials:
            if _is_wsa_serial(s):
                self.serial = s
                return s
        if serials:
            self.serial = serials[0]
            return self.serial
        return ""


# ---------------------------------------------------------------- 守护主体
class WsaNetGuard:
    def __init__(self, cfg: dict | None = None, logger=None, dry_run: bool = False):
        c = dict(cfg or {})
        self.cfg = c
        self.dry_run = bool(dry_run)
        self.logger = logger
        self.adb_path = find_adb(str(c.get("adb_path", "") or ""))
        self.serial = str(c.get("serial", "") or "").strip()
        self.host = str(c.get("host", "") or "").strip() or wsa_connection_info()["ip"]
        self.port = int(c.get("port", 0) or 0)
        self.ping_hosts = list(c.get("ping_hosts") or DEFAULT_HOSTS)
        self.interval = float(c.get("interval", 30) or 30)
        self.recover_wait = float(c.get("recover_wait", 8) or 8)
        self.allow_reboot = bool(c.get("allow_reboot", True))
        self.reboot_cooldown = float(c.get("reboot_cooldown", 600) or 600)
        self.allow_restart_wsa = bool(c.get("allow_restart_wsa", False))
        self.light_recover = bool(c.get("light_recover", True))
        self.adb = Adb(self.adb_path, self.serial, logger)
        self.streak = 0
        self.level = 0
        self.last_reboot = 0.0
        self.recoveries = 0
        self.fixes: list = []

    # -------------------------------------------------- 日志
    def log(self, level: str, msg: str) -> None:
        if self.logger is None:
            print(msg)
            return
        getattr(self.logger, level, self.logger.info)(msg)

    # -------------------------------------------------- 单轮检测
    def check(self) -> dict:
        """返回 {'problem', 'shell_ok', 'ping', 'serial', 'device_state'}。"""
        result = {"problem": "no_adb", "shell_ok": False, "ping": None,
                  "serial": self.serial, "device_state": ""}
        if not self.serial:
            self.serial = self.adb.adopt("")
            result["serial"] = self.serial
        states = {}
        try:
            states = self.adb.devices()
        except AdbError:
            pass
        result["device_state"] = states.get(self.serial, "") if self.serial else ""
        if self.serial and states.get(self.serial) != "device":
            # 配置的 serial 不在线（WSA 重启后端口会变）：先尝试重连一次再判定
            self._reconnect_once()
            try:
                states = self.adb.devices()
            except AdbError:
                states = {}
            result["serial"] = self.serial
            result["device_state"] = states.get(self.serial, "") if self.serial else ""
        if not self.serial or states.get(self.serial) != "device":
            return result
        # shell 是否可用
        try:
            echo = self.adb.shell("echo xtc-guard-ok", timeout=15)
        except AdbError:
            result["problem"] = "shell_dead"
            return result
        result["shell_ok"] = "xtc-guard-ok" in echo
        if not result["shell_ok"]:
            result["problem"] = "shell_dead"
            return result
        ping = None
        for host in self.ping_hosts:
            out = self.adb.try_shell(f"ping -c 1 -W 2 {host}", timeout=15)
            r = parse_ping(out)
            if r is True:
                ping = True
                break
            if r is False:
                ping = False
            elif ping is None:
                ping = None
        result["ping"] = ping
        result["problem"] = classify(True, ping)
        return result

    # -------------------------------------------------- 修复动作
    def _targets(self) -> list:
        """候选连接目标：配置的 serial -> 注册表端口 -> 常见端口（去重）。"""
        targets: list = []
        if self.serial and ":" in self.serial:
            targets.append(self.serial)
        port = self.port or wsa_adb_port()
        for p in [port, wsa_adb_port(), *_WSA_COMMON_PORTS]:
            t = f"{self.host}:{int(p)}"
            if t not in targets:
                targets.append(t)
        return targets

    def _reconnect_once(self) -> bool:
        """尝试 adb connect（不动 adb server）。"""
        for t in self._targets():
            try:
                if self.adb.connect(t):
                    self.adb.serial = t
                    self.serial = t
                    self.log("info", f"已连接到 {t}")
                    return True
            except AdbError:
                continue
        return False

    def _do(self, name: str, fn) -> bool:
        if self.dry_run:
            self.log("info", f"[dry-run] 跳过动作: {name}")
            return False
        self.log("warning", f"执行修复动作: {name}")
        try:
            ok = bool(fn())
        except Exception as e:  # noqa: BLE001
            self.log("error", f"修复动作 {name} 异常: {e}")
            ok = False
        self.fixes.append(name)
        self.recoveries += 1
        self.log("info" if ok else "warning", f"修复动作 {name} {'完成' if ok else '未成功'}")
        if self.recover_wait > 0:
            time.sleep(self.recover_wait)
        return ok

    def action_reconnect(self) -> bool:
        return self._do("reconnect", self._reconnect_once)

    def action_kill_server(self) -> bool:
        def _fn() -> bool:
            self.adb.run(["kill-server"], timeout=20)
            time.sleep(2)
            self.adb.run(["start-server"], timeout=40)
            time.sleep(2)
            self.serial = ""
            self.adb.serial = ""
            self.adb.adopt("")
            return self._reconnect_once()
        return self._do("kill_server", _fn)

    def action_net_reset(self) -> bool:
        """轻量网络复位：关飞行模式 + 打开 wifi/数据（WSA 的"网络"就是这个）。"""
        def _fn() -> bool:
            self.adb.try_shell("settings put global airplane_mode_on 0")
            self.adb.try_shell("am broadcast -a android.intent.action.AIRPLANE_MODE "
                               "--ez state false")
            self.adb.try_shell("svc wifi enable")
            self.adb.try_shell("svc data enable")
            return True
        return self._do("net_reset", _fn)

    def action_net_cycle(self) -> bool:
        """网络栈复位：飞行模式开->关（比单纯 svc 更彻底，仍不重启子系统）。"""
        def _fn() -> bool:
            self.adb.try_shell("settings put global airplane_mode_on 1")
            self.adb.try_shell("am broadcast -a android.intent.action.AIRPLANE_MODE "
                               "--ez state true")
            time.sleep(2)
            self.adb.try_shell("svc wifi disable")
            time.sleep(2)
            self.adb.try_shell("settings put global airplane_mode_on 0")
            self.adb.try_shell("am broadcast -a android.intent.action.AIRPLANE_MODE "
                               "--ez state false")
            self.adb.try_shell("svc wifi enable")
            self.adb.try_shell("svc data enable")
            return True
        return self._do("net_cycle", _fn)

    def action_guest_reboot(self) -> bool:
        """重启 Android 子系统（adb reboot）。端口可能变化，重启后重新读注册表连接。"""
        def _fn() -> bool:
            self.last_reboot = time.monotonic()
            self.adb.try_shell("reboot", timeout=15)
            self.log("warning", "已发出 reboot（Android 子系统重启中，约 30~90 秒后恢复）")
            time.sleep(10)
            self.adb.run(["kill-server"], timeout=20)
            self.adb.run(["start-server"], timeout=40)
            self.serial = ""
            self.adb.serial = ""
            deadline = time.time() + 180
            while time.time() < deadline:
                if self._reconnect_once():
                    return True
                time.sleep(5)
            return False
        return self._do("guest_reboot", _fn)

    def action_restart_wsa(self) -> bool:
        """重启宿主 WSA 客户端（WSABuilds 也适用）：杀掉 wsaclient 再拉起。"""
        def _fn() -> bool:
            if not wsa_installed():
                self.log("warning", "本机没检测到 WSA，跳过 restart_wsa")
                return False
            try:
                subprocess.run(["taskkill", "/IM", "WsaClient.exe", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=20,
                               **({"creationflags": subprocess.CREATE_NO_WINDOW}
                                  if IS_WINDOWS else {}))
            except Exception as e:  # noqa: BLE001
                self.log("debug", f"taskkill WsaClient 失败（可能本来就没运行）: {e}")
            time.sleep(3)
            launch_wsa()
            self.adb.run(["kill-server"], timeout=20)
            self.adb.run(["start-server"], timeout=40)
            self.serial = ""
            self.adb.serial = ""
            deadline = time.time() + 180
            while time.time() < deadline:
                if self._reconnect_once():
                    return True
                time.sleep(5)
            return False
        return self._do("restart_wsa", _fn)

    # -------------------------------------------------- 一轮完整流程
    def step(self) -> dict:
        res = self.check()
        problem = res["problem"]
        if problem == "ok":
            if self.streak or self.level:
                self.log("info", f"网络已恢复正常（连续异常 {self.streak} 轮后自愈）")
            self.streak = 0
            self.level = 0
            return {**res, "action": "none", "action_ok": True}
        if problem == "unknown":
            self.log("warning", "无法判断子系统网络（镜像里可能没有 ping 命令）；"
                                "只做 adb 保活，不执行网络修复")
            self.streak = 0
            return {**res, "action": "none", "action_ok": True}
        self.streak += 1
        now = time.monotonic()
        reboot_allowed_now = (now - self.last_reboot) >= self.reboot_cooldown
        action = plan_action(problem, self.streak, self.level,
                             allow_reboot=self.allow_reboot,
                             allow_restart_wsa=self.allow_restart_wsa,
                             light_recover=self.light_recover,
                             reboot_allowed_now=reboot_allowed_now)
        self.log("warning", f"检测到异常：{problem}（连续 {self.streak} 轮，级别 {self.level}）"
                            f" -> 计划动作: {action}")
        handler = {
            "reconnect": self.action_reconnect,
            "kill_server": self.action_kill_server,
            "net_reset": self.action_net_reset,
            "net_cycle": self.action_net_cycle,
            "guest_reboot": self.action_guest_reboot,
            "restart_wsa": self.action_restart_wsa,
        }.get(action)
        ok = True
        if handler is not None:
            ok = handler()
            if action in ("guest_reboot", "restart_wsa"):
                self.level = max(self.level, 3)
        return {**res, "action": action, "action_ok": ok}

    # -------------------------------------------------- 状态文件
    def write_state(self, res: dict) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "ts": time.time(),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "platform": platform_tag(),
                "adb": self.adb_path,
                "serial": res.get("serial") or self.serial,
                "problem": res.get("problem"),
                "ping": res.get("ping"),
                "action": res.get("action"),
                "action_ok": res.get("action_ok"),
                "streak": self.streak,
                "recoveries": self.recoveries,
                "dry_run": self.dry_run,
            }
            tmp = str(STATE_FILE) + ".tmp"
            Path(tmp).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            Path(tmp).replace(STATE_FILE)
        except Exception as e:  # noqa: BLE001 写状态失败不影响守护
            self.log("debug", f"状态文件写入失败: {e}")

    # -------------------------------------------------- 主循环
    def run_forever(self) -> int:
        self.log("info", f"WSA 网络守护启动：平台={platform_tag()} adb={self.adb_path} "
                         f"serial={self.serial or '(自动)'} 间隔={self.interval:g}s "
                         f"允许重启子系统={self.allow_reboot} 允许重启WSA={self.allow_restart_wsa}"
                         + ("（dry-run 只诊断）" if self.dry_run else ""))
        try:
            while True:
                res = self.step()
                self.write_state(res)
                if res["problem"] == "ok":
                    self.log("debug", f"状态正常（serial={res.get('serial')}）")
                time.sleep(max(5.0, self.interval))
        except KeyboardInterrupt:
            self.log("info", "收到 Ctrl+C，守护退出")
            return 0

    def status_text(self, res: dict | None = None) -> str:
        res = res if res is not None else self.check()
        self.write_state(res)
        problem = res["problem"]
        zh = {"ok": "正常", "no_adb": "ADB 掉线（设备不在线）",
              "shell_dead": "设备在线但 shell 无响应",
              "net_down": "ADB 正常但子系统网络不通",
              "unknown": "无法判断（无 ping 命令?）"}.get(problem, problem)
        lines = [f"WSA 网络守护状态：{zh}",
                 f"  adb      : {self.adb_path}",
                 f"  serial   : {res.get('serial') or '(未选定)'}",
                 f"  device   : {res.get('device_state') or '(不在 adb devices 里)'}",
                 f"  ping     : {res.get('ping')}",
                 f"  候选目标 : {' '.join(self._targets()[:4])}",
                 f"  宿主端口 : {self.host}:{self.port or wsa_adb_port()} "
                 f"{'监听中' if host_port_open(self.host, self.port or wsa_adb_port()) else '未监听'}"]
        return "\n".join(lines)


# ---------------------------------------------------------------- 配置 / 命令行
def _mini_yaml_scalar(raw: str):
    """把 YAML 标量/内联列表转成 Python 值（只覆盖本工具用到的几种写法）。"""
    s = raw.strip()
    if not s or s in ("null", "~", "None"):
        return None
    if s.lower() in ("true", "yes", "on"):
        return True
    if s.lower() in ("false", "no", "off"):
        return False
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_mini_yaml_scalar(x) for x in inner.split(",")] if inner else []
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s.split("#", 1)[0].strip() if "#" in s else s


def load_guard_config(config_path: str, section: str = "wsa_guard") -> dict:
    """从 config.yaml 读 wsa_guard 段。

    优先 pyyaml；没装 pyyaml 时退化为"只解析这一段"的极简解析器（本工具不依赖
    主程序的配置加载，也不因为缺 pyyaml 就跑不起来）。
    """
    p = Path(config_path)
    if not p.exists():
        return {}
    try:
        raw = p.read_bytes()
        text = raw.decode("utf-8-sig") if raw.startswith(b"\xef\xbb\xbf") else raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"[warn] 读取配置失败（改用命令行参数）: {e}")
        return {}
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
        sec = data.get(section) if isinstance(data, dict) else None
        return dict(sec) if isinstance(sec, dict) else {}
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 yaml 解析失败也别崩
        print(f"[warn] YAML 解析失败（改用极简解析/命令行参数）: {e}")
    # 极简 fallback：只取 section 下的 key: value（够本工具用）
    out: dict = {}
    in_section = False
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_section = line.split(":", 1)[0].strip() == section
            continue
        if not in_section or ":" not in line:
            continue
        key, _, val = line.strip().partition(":")
        key = key.strip()
        if key:
            out[key] = _mini_yaml_scalar(val)
    return out


def install_task(python_exe: str, script: str, minutes: int, task_name: str) -> int:
    """Windows：注册计划任务，每 N 分钟跑一次 `--once`（不想常驻时的替代方案）。"""
    if not IS_WINDOWS:
        print("--install-task 只支持 Windows（其它平台可用 systemd timer / cron 调用 --once）")
        return 2
    cmd = (f'"{python_exe}" "{script}" --once --quiet')
    args = ["schtasks", "/Create", "/TN", task_name, "/TR", cmd,
            "/SC", "MINUTE", "/MO", str(int(minutes)), "/RL", "LIMITED", "/F"]
    print("执行:", " ".join(args))
    rc = subprocess.run(args).returncode
    print("已注册计划任务" if rc == 0 else f"注册失败（退出码 {rc}，可尝试以管理员身份运行）")
    return rc


def uninstall_task(task_name: str) -> int:
    if not IS_WINDOWS:
        print("--uninstall-task 只支持 Windows")
        return 2
    rc = subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"]).returncode
    print("已删除计划任务" if rc == 0 else f"删除失败（退出码 {rc}）")
    return rc


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="WSA / WSABuilds 网络守护（独立工具）：检测断网并按级别修复",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml", help="配置文件路径（读取 wsa_guard 段）")
    ap.add_argument("--serial", default="", help="设备序列号（如 127.0.0.1:58526）")
    ap.add_argument("--host", default="", help="WSA 宿主地址（默认取注册表/127.0.0.1）")
    ap.add_argument("--port", type=int, default=0, help="WSA adb 端口（0=自动读注册表）")
    ap.add_argument("--interval", type=float, default=0, help="检测间隔秒数（默认 30）")
    ap.add_argument("--ping-host", action="append", default=[],
                    help="用于判断子系统网络的目标（可重复指定）")
    ap.add_argument("--status", action="store_true", help="打印一次状态后退出")
    ap.add_argument("--once", action="store_true", help="检测一次，必要时修复，然后退出")
    ap.add_argument("--dry-run", action="store_true", help="只诊断，不执行修复动作")
    ap.add_argument("--no-reboot", action="store_true", help="禁止重启 Android 子系统")
    ap.add_argument("--restart-wsa", action="store_true", help="允许重启宿主 WSA 客户端（最重）")
    ap.add_argument("--no-light-recover", action="store_true", help="禁用网络轻量修复")
    ap.add_argument("--log-file", default="", help="日志文件（默认 logs/wsa_guard.log）")
    ap.add_argument("--quiet", action="store_true", help="不输出到控制台（只写日志文件）")
    ap.add_argument("--install-task", action="store_true",
                    help="Windows：注册每 N 分钟运行 --once 的计划任务")
    ap.add_argument("--uninstall-task", action="store_true", help="Windows：删除该计划任务")
    ap.add_argument("--task-minutes", type=int, default=5, help="计划任务间隔分钟（默认 5）")
    ap.add_argument("--task-name", default=TASK_NAME, help=f"计划任务名（默认 {TASK_NAME}）")
    ap.add_argument("--test", action="store_true", help="跑内置离线自测后退出")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.test:
        return run_selftest()

    if args.install_task:
        return install_task(sys.executable or "python",
                            str(Path(__file__).resolve()), args.task_minutes, args.task_name)
    if args.uninstall_task:
        return uninstall_task(args.task_name)

    cfg = load_guard_config(args.config)
    if args.serial:
        cfg["serial"] = args.serial
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port
    if args.interval:
        cfg["interval"] = args.interval
    if args.ping_host:
        cfg["ping_hosts"] = args.ping_host
    if args.no_reboot:
        cfg["allow_reboot"] = False
    if args.restart_wsa:
        cfg["allow_restart_wsa"] = True
    if args.no_light_recover:
        cfg["light_recover"] = False

    log_file = args.log_file or str(cfg.get("log_file") or (ROOT / "logs" / "wsa_guard.log"))
    logger = setup_logger(level=str(cfg.get("log_level", "INFO")),
                          file=log_file, name="wsa-guard", console=not args.quiet)
    try:
        guard = WsaNetGuard(cfg, logger=logger, dry_run=args.dry_run)
    except AdbError as e:
        logger.error(f"初始化失败: {e}")
        return 2

    if args.status:
        res = guard.check()
        print(guard.status_text(res))
        return 0 if res["problem"] == "ok" else 1
    if args.once:
        res = guard.step()
        guard.write_state(res)
        ok = res["problem"] == "ok" or res.get("action_ok")
        logger.info(f"--once 完成：problem={res['problem']} action={res.get('action')} "
                    f"（{'正常/已恢复' if ok else '仍未恢复'}）")
        return 0 if ok else 1
    return guard.run_forever()


# ---------------------------------------------------------------- 内置离线自测
def run_selftest() -> int:
    cases: list = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        cases.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")

    print("--- parse_ping ---")
    check("正常 ping 判定为通",
          parse_ping("PING 223.5.5.5 (223.5.5.5) 56(84) bytes of data.\n"
                     "64 bytes from 223.5.5.5: icmp_seq=1 ttl=117 time=8.1 ms\n"
                     "--- 223.5.5.5 ping statistics ---\n"
                     "1 packets transmitted, 1 received, 0% packet loss") is True)
    check("100% 丢包判定为不通",
          parse_ping("1 packets transmitted, 0 received, 100% packet loss") is False)
    check("没有 ping 命令 -> 无法判断（None，不误报断网）",
          parse_ping("/system/bin/sh: ping: not found") is None)
    check("Network is unreachable -> 不通",
          parse_ping("connect: Network is unreachable") is False)

    print("--- classify ---")
    check("shell 挂 -> no_adb", classify(False, None) == "no_adb")
    check("adb 通 + 网络不通 -> net_down", classify(True, False) == "net_down")
    check("adb 通 + ping 通 -> ok", classify(True, True) == "ok")
    check("adb 通 + 无法判断 -> unknown", classify(True, None) == "unknown")

    print("--- plan_action（分级升级） ---")
    kw = dict(allow_reboot=True, allow_restart_wsa=False, light_recover=True,
              reboot_allowed_now=True)
    check("第 1 轮 -> 重连", plan_action("no_adb", 1, 0, **kw) == "reconnect")
    check("第 3 轮 -> 重启 adb server", plan_action("no_adb", 3, 0, **kw) == "kill_server")
    check("第 1 轮网络不通 -> 轻量复位", plan_action("net_down", 1, 0, **kw) == "net_reset")
    check("第 2 轮网络不通 -> 网络栈复位", plan_action("net_down", 2, 0, **kw) == "net_cycle")
    check("第 3 轮网络不通 -> 重启子系统", plan_action("net_down", 3, 0, **kw) == "guest_reboot")
    check("冷却期内不重启",
          plan_action("net_down", 5, 3, **{**kw, "reboot_allowed_now": False}) == "net_cycle")
    check("禁止重启时只做轻量修复",
          plan_action("net_down", 5, 3, **{**kw, "allow_reboot": False}) == "net_cycle")
    check("允许重启 WSA 且多轮失败 -> restart_wsa",
          plan_action("no_adb", 5, 3, **{**kw, "allow_restart_wsa": True}) == "restart_wsa")
    check("健康时不动作", plan_action("ok", 0, 0, **kw) == "none")
    check("关闭轻量修复时网络问题不动作",
          plan_action("net_down", 3, 0, **{**kw, "light_recover": False}) == "none")

    print("--- 配置解析 ---")
    check("load_guard_config 缺文件返回空 dict",
          load_guard_config("不存在的配置文件.yaml") == {})
    check("YAML 标量解析（无 pyyaml 时也能读配置）",
          _mini_yaml_scalar("30") == 30 and _mini_yaml_scalar("true") is True
          and _mini_yaml_scalar('["1.1.1.1", "8.8.8.8"]') == ["1.1.1.1", "8.8.8.8"])

    failed = cases.count(False)
    print(f"\n===== wsa_net_guard 自测：{cases.count(True)} 通过 / {failed} 失败 =====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
