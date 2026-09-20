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
import re
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
import runtime_paths  # noqa: E402

# 状态/日志都写在"可写数据目录"：Nuitka onefile 下是 exe 旁边（而不是临时解包目录）
runtime_paths.ensure_dirs()
ROOT = runtime_paths.APP_DIR
STATE_FILE = runtime_paths.data_path("wsa_guard_state.json")
DEFAULT_HOSTS = ("223.5.5.5", "8.8.8.8")
# TCP 探测目标（host:port）：WSA 的 NAT 不转发 ICMP（ping 永远 100% 丢包），
# 只有 TCP 连接能真实反映"App 能不能联网"。默认用几个公共 DNS 的 53 端口。
DEFAULT_TCP_TARGETS = ("223.5.5.5:53", "8.8.8.8:53", "114.114.114.114:53")
TASK_NAME = "XTC-WSA-NetGuard"


# ---------------------------------------------------------------- 纯逻辑（可离线测试）
def shorten(text: str, limit: int = 160) -> str:
    """把命令输出压成一行短文本（写日志/状态文件用）。"""
    return " ".join(str(text or "").split())[:limit]


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


def ping_commands(host: str, custom: str = "") -> list:
    """探测连通性的 ping 命令候选（不同镜像的 ping 实现与选项不同，逐个试）。

    用户反馈过"镜像里没有 ping / 选项不被支持"导致守护只能干看着，所以这里：
    * 自定义命令（config: ping_command，支持 {host} 占位）优先；
    * 内置从 iputils 风格（-W）到 toybox/busybox 风格（-w）再到最保守写法。
    """
    if custom:
        return [custom.replace("{host}", host)]
    return [
        f"ping -c 1 -W 2 {host}",     # iputils（Android 常见）
        f"ping -c 1 -w 4 {host}",     # toybox/busybox：-w 是总超时
        f"ping -c 1 {host}",          # 最保守
        f"toybox ping -c 1 {host}",
        f"busybox ping -c 1 {host}",
    ]


def parse_ip_addr(out: str):
    """从 `ip -4 addr` / `ifconfig` 输出判断"有没有非回环 IPv4"。

    返回 True（有）/ False（没有）/ None（命令不可用/输出不认识）。
    """
    if not out:
        return None
    low = out.lower()
    for bad in ("not found", "inaccessible", "permission denied", "unknown option"):
        if bad in low:
            return None
    ips = re.findall(r"inet\s+(?:addr:)?\s*(\d+\.\d+\.\d+\.\d+)", low)
    if not ips:
        return None
    return any(not ip.startswith("127.") for ip in ips)


def parse_connectivity(out: str):
    """从 `dumpsys connectivity` 输出判断默认网络是否可用：True/False/None。"""
    if not out:
        return None
    low = out.lower()
    if "active default network: none" in low:
        return False
    if "state: connected/connected" in low:
        return True
    if re.search(r"active default network:\s*\d+", low):
        return False if "state: disconnected" in low else True
    return None


def classify_probe(ping, has_ip, default_net) -> str:
    """（保留）只用 ping/IP/默认网络三证据的旧判定，等价于 net_probe_verdict(ping_ok=ping,...)。"""
    return net_probe_verdict(None, ping, has_ip, default_net)


def net_probe_verdict(nc_ok, ping_ok, has_ip, default_net) -> str:
    """综合判定网络：'ok' / 'net_down' / 'unknown'（纯函数，便于测试）。

    **为什么不能只看 ping**：WSA 的 NAT 不转发 ICMP，`ping` 在 WSA 上**永远是 100% 丢包**
    （实测：镜像里 /system/bin/ping 存在、有 IP、默认网络正常，但 ping 公共地址必然失败）。
    所以判定顺序是：
      1) `nc` 的 TCP 连接成功 或 ping 成功 -> ok（最贴近 App 的真实连通性）
      2) 连非回环 IPv4 都没有 -> net_down
      3) `dumpsys connectivity` 明确说没有默认网络 -> net_down
      4) 系统说有默认网络（VALIDATED/CONNECTED）-> ok
      5) 其余 -> unknown：**不触发任何修复动作**（只保活 adb），避免在 WSA 上误判成断网而反复重启
    """
    if nc_ok is True or ping_ok is True:
        return "ok"
    if has_ip is False:
        return "net_down"
    if default_net is False:
        return "net_down"
    if default_net is True:
        return "ok"
    return "unknown"


def parse_tcp_target(target: str) -> tuple:
    """把 "host:port" 拆成 (host, port)；非法返回 ('', 0)。"""
    m = re.match(r"^\s*([0-9A-Za-z_.\-]+)\s*:\s*(\d{1,5})\s*$", target or "")
    if not m:
        return "", 0
    return m.group(1), int(m.group(2))


def nc_command(host: str, port: int, timeout: int = 3) -> str:
    """用 `nc` 做一次 TCP 连接测试（做完立即退出）。

    `< /dev/null` 很关键：否则 nc 会等 stdin，adb shell 下会一直挂着。
    WSA 上 ICMP 被 NAT 屏蔽，这条 TCP 探测才是"App 到底能不能联网"的真实信号。
    """
    return f"nc -w {int(timeout)} {host} {int(port)} < /dev/null"


def classify(shell_ok: bool, ping: bool | None) -> str:
    """把一轮检测结果归类：'ok' / 'no_adb' / 'shell_dead' / 'net_down' / 'unknown'。"""
    if not shell_ok:
        return "no_adb"
    if ping is True:
        return "ok"
    if ping is False:
        return "net_down"
    return "unknown"


# ---------------------------------------------------------------- 联网验证（captive portal）
# 为什么需要这一层：WSA 里 TCP/DNS 可能都是通的，但 Android 判定"能不能上网"靠的是
# 自己的验证探针。默认探针地址 connectivitycheck.gstatic.com 在国内**连不上**，
# 于是系统把网络标成 PARTIAL_CONNECTIVITY（能力里带 INTERNET 但没有 VALIDATED），
# 各种 App 就会当作"没有网络"（小天才发消息报网络异常就是这么来的）。
# 这几个设置写在 /data 里，重启子系统后依然有效，重复执行也是幂等的。
VALIDATION_SETTINGS = (
    ("captive_portal_mode", "0"),
    ("captive_portal_http_url", "http://connectivitycheck.platform.hicloud.com/generate_204"),
    ("captive_portal_https_url", "https://connectivitycheck.platform.hicloud.com/generate_204"),
    ("captive_portal_fallback_url", "http://connect.rom.miui.com/generate_204"),
    ("captive_portal_other_fallback_urls", "http://wifi.vivo.com.cn/generate_204"),
    ("captive_portal_use_https", "0"),
    ("private_dns_mode", "off"),
)

# 实测可达的 generate_204 端点（用 nc 从 WSA 里逐个验过）；gstatic 在国内不通
VALIDATION_PROBE_HOSTS = ("connectivitycheck.platform.hicloud.com",
                          "connect.rom.miui.com", "wifi.vivo.com.cn")


def validation_settings_commands() -> list:
    """修联网验证需要执行的 adb shell 命令（幂等）。"""
    return [f"settings put global {k} {v}" for k, v in VALIDATION_SETTINGS]


def parse_validation_state(out: str) -> str:
    """从 `dumpsys connectivity` 的 NetworkAgentInfo 行判断验证状态。

    返回 'validated' / 'partial' / 'unknown'。
    注意只看 NetworkAgentInfo 那一行：请求段里也会出现 "VALIDATED" 字样，
    用它判断会把"没验证通过"误判成通过。
    """
    low = (out or "").lower()
    if "networkagentinfo" not in low:
        return "unknown"
    if "partial_connectivity" in low:
        return "partial"
    if "validated" in low:
        return "validated"
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

    def shell_rc(self, cmd: str, timeout: float = 25.0) -> tuple:
        """执行 shell 并返回 (输出, 退出码)，**不因非零退出码丢输出**。

        ping/nc 这类探测命令"失败"时退出码本来就是 1，用 try_shell 会把
        "100% packet loss" 这种关键证据直接吞掉（历史上就因此一直报"无法判断"）。
        """
        out, err, rc = self.run(["shell", cmd], timeout=timeout)
        text = out or ""
        if err and err.strip():
            text = f"{text}\n{err}".strip()
        return text, rc

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
        # 自定义探测命令（支持 {host} 占位）；留空用内置的多种 ping 写法
        self.ping_command = str(c.get("ping_command", "") or "").strip()
        # TCP 探测目标（host:port）：WSA 上 ICMP 被屏蔽，这条才是"能不能联网"的真实信号
        self.tcp_targets = list(c.get("tcp_targets") or DEFAULT_TCP_TARGETS)
        self.tcp_timeout = int(c.get("tcp_timeout", 3) or 3)
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
        # 网络探测状态：ping/nc 是否可用（None=还没试过）、最后一次 ping 输出（诊断用）
        self._ping_usable: bool | None = None
        self._nc_usable: bool | None = None
        self._ping_evidence = ""
        self._last_unknown_log = 0.0
        # 联网验证（PARTIAL_CONNECTIVITY）修复：默认开启，10 分钟最多修一次
        self.fix_validation_enabled = bool(c.get("fix_validation", True))
        self.validation_fix_cooldown = float(c.get("validation_fix_cooldown", 600) or 600)
        self._last_validation_fix = 0.0

    # -------------------------------------------------- 日志
    def log(self, level: str, msg: str) -> None:
        if self.logger is None:
            print(msg)
            return
        getattr(self.logger, level, self.logger.info)(msg)

    # -------------------------------------------------- 单轮检测
    def _probe_network(self) -> tuple:
        """判断子系统网络是否通，返回 (True/False/None, 证据说明)。

        顺序（每一步都把"为什么这么判"写进证据，日志与状态文件里都能看到）：
          1) `nc` TCP 连接 —— **WSA 上唯一可靠的联网信号**：WSA 的 NAT 不转发 ICMP，
             镜像里 ping 明明存在、IP 与默认网络都正常，ping 公共地址却永远 100% 丢包；
          2) `ping` —— 模拟器 / Waydroid / 真机上 ICMP 正常，ping 通即判定正常；
          3) 有没有非回环 IPv4（没有 = 明确断网）；
          4) `dumpsys connectivity` 的默认网络状态（系统自己的结论）。
        """
        evidence: list = []
        nc_ok = None
        ping_ok = None

        # --- 1) TCP 探测（nc）---
        if self._nc_usable is not False:
            for target in self.tcp_targets:
                host, port = parse_tcp_target(target)
                if not host:
                    continue
                out, rc = self.adb.shell_rc(nc_command(host, port, self.tcp_timeout), timeout=15)
                if rc == 0:
                    self._nc_usable = True
                    return True, f"TCP 连接 {target} 成功（nc）"
                low = (out or "").lower()
                if "not found" in low or "inaccessible" in low or "unknown option" in low:
                    self._nc_usable = False
                    evidence.append(f"nc 不可用（{shorten(out)}）")
                    break
                evidence.append(f"TCP {target} 失败(rc={rc})")
            if self._nc_usable is None:
                self._nc_usable = True        # 命令可用，只是这次没连上

        # --- 2) ping 探测（多种写法逐个试；只在 TCP 不确定时才有意义）---
        if self._ping_usable is not False:
            for host in self.ping_hosts:
                for cmd in ping_commands(host, self.ping_command):
                    out, _rc = self.adb.shell_rc(cmd, timeout=15)
                    r = parse_ping(out)
                    if r is True:
                        self._ping_usable = True
                        return True, f"ping 通（{cmd}）"
                    if r is False:
                        self._ping_usable = True
                        ping_ok = False
                        evidence.append(f"ping {host} 不通")
                        break
                    self._ping_evidence = f"{cmd} -> {shorten(out) or '(无输出)'}"
            if self._ping_usable is None:
                self._ping_usable = False
                evidence.append(f"ping 不可用（{self._ping_evidence}）")

        # --- 3) 有没有 IP ---
        ip_out = ""
        for cmd in ("ip -4 addr show", "ip addr", "ifconfig"):
            ip_out, _rc = self.adb.shell_rc(cmd, timeout=15)
            if parse_ip_addr(ip_out) is not None:
                break
        has_ip = parse_ip_addr(ip_out)

        # --- 4) 连通性服务的默认网络状态 ---
        conn_out, _rc = self.adb.shell_rc("dumpsys connectivity", timeout=25)
        default_net = parse_connectivity(conn_out)

        verdict = net_probe_verdict(nc_ok, ping_ok, has_ip, default_net)
        detail = (f"IP={'有' if has_ip else ('无' if has_ip is False else '未知')}"
                  f"，默认网络={'正常' if default_net else ('异常' if default_net is False else '未知')}"
                  f"，ping={'通' if ping_ok else '不通'}")
        if evidence:
            detail = f"{detail}；" + "；".join(evidence[:3])
        if verdict == "ok":
            return True, f"判定为通（{detail}）"
        if verdict == "net_down":
            return False, f"判定为断网（{detail}）"
        return None, (f"无法判断（{detail}）。注：WSA 的 NAT 不转发 ICMP，ping 不通不代表断网；"
                      "此时只保活 adb、不触发修复。可用 wsa_guard.tcp_targets 调整 TCP 探测目标")

    # -------------------------------------------------- 单轮检测
    def check(self) -> dict:
        """返回 {'problem', 'shell_ok', 'ping', 'net_probe', 'serial', 'device_state'}。"""
        result = {"problem": "no_adb", "shell_ok": False, "ping": None, "net_probe": "",
                  "serial": self.serial, "device_state": "",
                  "validation": "", "validation_evidence": ""}
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
        ping, evidence = self._probe_network()
        result["ping"] = ping
        result["net_probe"] = evidence
        result["problem"] = classify(True, ping)
        # 联网验证状态：TCP/DNS 通不代表 App 认为有网（见 VALIDATION_SETTINGS 注释）
        if self.fix_validation_enabled:
            state, verr = self.check_validation()
            result["validation"] = state
            result["validation_evidence"] = verr
        return result

    def check_validation(self) -> tuple:
        """读系统的联网验证状态，返回 (state, 证据)。

        'partial' = TCP/DNS 都通，但系统验证没通过（WSA 上默认探针被墙时就是这样）。
        """
        try:
            out = self.adb.shell("dumpsys connectivity | grep -e NetworkAgentInfo | head -3",
                                 timeout=25)
        except AdbError as e:  # noqa: BLE001
            return "unknown", f"读取失败: {e}"
        state = parse_validation_state(out)
        if state == "partial":
            return state, "网络被标记为 PARTIAL_CONNECTIVITY（验证探针不通，App 会以为没网）"
        if state == "validated":
            return state, "系统已确认该网络可上网（VALIDATED）"
        return state, shorten(" ".join((out or "").split())) or "无输出"

    def action_fix_validation(self) -> bool:
        """修"能上网但系统认为没网"：验证探针换成国内可达端点 + 关 DoT（幂等）。"""
        def _fn() -> bool:
            ok = True
            for cmd in validation_settings_commands():
                out, rc = self.adb.shell_rc(cmd, timeout=15)
                if rc != 0:
                    ok = False
                    self.log("warning", f"设置失败（rc={rc}）: {cmd} {shorten(out)}")
            return ok
        return self._do("fix_validation", _fn)

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
            # 能上网但系统没验证通过：修验证探针，而不是去重启子系统
            # （重启既治不了这个病，还会把正在运行的桥接/小天才一起打断）
            if self.fix_validation_enabled and res.get("validation") == "partial":
                now = time.monotonic()
                if now - self._last_validation_fix >= self.validation_fix_cooldown:
                    self._last_validation_fix = now
                    self.log("warning",
                             "子系统能上网（TCP/DNS 正常），但系统标记为 PARTIAL_CONNECTIVITY："
                             "默认验证探针在国内不通，App 会当作没网 -> 改验证探针")
                    ok = self.action_fix_validation()
                    self.log("info" if ok else "warning",
                             "联网验证设置已更新（写入 /data，重启子系统后依然有效；"
                             "系统下一轮验证通过后即变为 VALIDATED）" if ok else "联网验证设置失败")
                    return {**res, "action": "fix_validation", "action_ok": ok}
            if self.streak or self.level:
                self.log("info", f"网络已恢复正常（连续异常 {self.streak} 轮后自愈）")
            self.streak = 0
            self.level = 0
            return {**res, "action": "none", "action_ok": True}
        if problem == "unknown":
            # 探测手段不可用时不再每轮刷屏：10 分钟提示一次，并带上证据（ping 原始输出等），
            # 这样用户/维护者能看出到底是"没有 ping"还是"选项不支持"还是"输出不认识"。
            now = time.monotonic()
            if now - self._last_unknown_log >= 600:
                self._last_unknown_log = now
                self.log("warning",
                         f"无法判断子系统网络，暂不执行网络修复（只保活 adb）："
                         f"{res.get('net_probe') or self._ping_evidence or '无探测输出'}。"
                         "可设 wsa_guard.ping_command 自定义探测命令，或 wsa_guard.ping_hosts 换目标")
            else:
                self.log("debug", f"网络状态未知（已节流提示）: {res.get('net_probe')}")
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
                "net_probe": res.get("net_probe") or self._ping_evidence,
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
              "unknown": "无法判断（探测手段不可用）"}.get(problem, problem)
        lines = [f"WSA 网络守护状态：{zh}",
                 f"  adb      : {self.adb_path}",
                 f"  serial   : {res.get('serial') or '(未选定)'}",
                 f"  device   : {res.get('device_state') or '(不在 adb devices 里)'}",
                 f"  ping     : {res.get('ping')}（ping 可用={self._ping_usable}）",
                 f"  探测证据 : {res.get('net_probe') or self._ping_evidence or '(无)'}",
                 f"  联网验证 : {res.get('validation') or '(未检测)'}"
                 f"{'  ' + str(res.get('validation_evidence') or '') if res.get('validation') else ''}",
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
        out = dict(sec) if isinstance(sec, dict) else {}
        # 顺带带出顶层 adb.path：本工具必须和主程序用**同一个 adb**。
        # 两个不同版本的 adb.exe 会互相杀掉对方在 5037 上的 server，表现就是
        # 设备一会儿在线一会儿掉线。
        adb_sec = data.get("adb") if isinstance(data, dict) else None
        if isinstance(adb_sec, dict) and adb_sec.get("path") and not out.get("adb_path"):
            out["adb_path"] = adb_sec["path"]
        return out
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
    # 同样把顶层 adb.path 带出来（与主程序共用同一个 adb）
    in_adb = False
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_adb = line.split(":", 1)[0].strip() == "adb"
            continue
        if in_adb and ":" in line:
            key, _, val = line.strip().partition(":")
            if key.strip() == "path" and not out.get("adb_path"):
                out["adb_path"] = _mini_yaml_scalar(val)
    return out


def self_command() -> str:
    """本程序的可执行命令：冻结（Nuitka）时直接用 exe，源码运行时用 python + 脚本。

    计划任务必须指向**稳定路径**：onefile 下 `__file__` 是退出即删的临时目录，
    用它注册任务会在下次触发时报"文件不存在"。
    """
    if runtime_paths.IS_FROZEN:
        exe = runtime_paths._launcher_exe() or Path(sys.executable)
        return f'"{exe}"'
    return f'"{sys.executable or "python"}" "{Path(__file__).resolve()}"'


def install_task(minutes: int, task_name: str) -> int:
    """Windows：注册计划任务，每 N 分钟跑一次 `--once`（不想常驻时的替代方案）。"""
    if not IS_WINDOWS:
        print("--install-task 只支持 Windows（其它平台可用 systemd timer / cron 调用 --once）")
        return 2
    cmd = f"{self_command()} --once --quiet"
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
    ap.add_argument("--adb", default="", help="adb 可执行文件（默认用 config.yaml 里 adb.path，"
                                             "必须和主程序一致）")
    ap.add_argument("--fix-validation", action="store_true",
                    help="修「能上网但系统认为没网」：改写联网验证探针地址（幂等，立即生效）")
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
        return install_task(args.task_minutes, args.task_name)
    if args.uninstall_task:
        return uninstall_task(args.task_name)

    if not args.config or args.config == "config.yaml":
        args.config = str(runtime_paths.resolve_config())   # 冻结时找 exe 旁边的配置
    cfg = load_guard_config(args.config)
    if args.adb:
        cfg["adb_path"] = args.adb
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
    if args.fix_validation:
        state, ev = guard.check_validation()
        print(f"当前联网验证状态: {state}\n  证据: {ev}")
        ok = guard.action_fix_validation()
        state2, ev2 = guard.check_validation()
        print(f"修复动作: {'成功' if ok else '失败'}；重新读取: {state2}\n  证据: {ev2}")
        print("说明：设置写入 /data，重启子系统后依然有效。系统要等下一轮验证才会把状态"
              "变成 VALIDATED（TCP/DNS 通但状态暂时仍是 partial 属正常）。")
        return 0 if ok else 1
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

    print("--- ping 不可用时的回退探测 ---")
    check("ping 命令候选覆盖 iputils/toybox/busybox 风格",
          len(ping_commands("1.1.1.1")) >= 4
          and ping_commands("1.1.1.1")[0].startswith("ping -c 1 -W 2")
          and any("-w 4" in c for c in ping_commands("1.1.1.1")),
          str(ping_commands("1.1.1.1")[:2]))
    check("自定义 ping_command 生效（{host} 占位）",
          ping_commands("9.9.9.9", "my-ping {host} -n 1") == ["my-ping 9.9.9.9 -n 1"])
    check("TCP 探测目标解析",
          parse_tcp_target("223.5.5.5:53") == ("223.5.5.5", 53)
          and parse_tcp_target("bad") == ("", 0),
          str(parse_tcp_target("223.5.5.5:53")))
    check("nc 探测命令带 -w 且关闭 stdin（否则会挂住）",
          nc_command("8.8.8.8", 53, 3) == "nc -w 3 8.8.8.8 53 < /dev/null",
          nc_command("8.8.8.8", 53, 3))
    check("ip 输出里有非回环 IPv4 -> 有网",
          parse_ip_addr("2: eth0: <UP> inet 172.20.1.5/24 brd 172.20.1.255") is True)
    check("只有 127.0.0.1 -> 没网",
          parse_ip_addr("1: lo: inet 127.0.0.1/8 scope host lo") is False)
    check("ip 命令不存在 -> 无法判断", parse_ip_addr("ip: not found") is None)
    check("dumpsys connectivity：有默认网络且 CONNECTED -> 正常",
          parse_connectivity("Active default network: 100\n  state: CONNECTED/CONNECTED") is True)
    check("dumpsys connectivity：没有默认网络 -> 断网",
          parse_connectivity("Active default network: none") is False)
    check("dumpsys connectivity：输出不认识 -> 无法判断",
          parse_connectivity("some unrelated dump") is None)

    print("--- 综合判定（WSA 上 ICMP 被屏蔽，ping 不通 ≠ 断网）---")
    check("TCP 探测成功 -> ok", net_probe_verdict(True, None, True, None) == "ok")
    check("ping 通 -> ok", net_probe_verdict(None, True, True, None) == "ok")
    check("WSA 典型场景：ping 不通但 IP/默认网络正常 -> ok",
          net_probe_verdict(False, False, True, True) == "ok")
    check("连 IP 都没有 -> net_down", net_probe_verdict(False, False, False, None) == "net_down")
    check("系统说没有默认网络 -> net_down",
          net_probe_verdict(False, False, True, False) == "net_down")
    check("ping 不通 + 默认网络未知 -> unknown（不触发修复，避免误重启）",
          net_probe_verdict(False, False, True, None) == "unknown")
    check("什么都测不到 -> unknown", net_probe_verdict(None, None, None, None) == "unknown")
    check("shorten 把多行压成一行", "\n" not in shorten("a\nb\nc") and shorten("a\nb\nc") == "a b c")

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

    print("--- 联网验证（captive portal）---")
    check("PARTIAL_CONNECTIVITY -> partial",
          parse_validation_state(
              'NetworkAgentInfo{network{102} ni{Ethernet CONNECTED} '
              'nc{[ Capabilities: INTERNET&PARTIAL_CONNECTIVITY&NOT_VPN ]}}') == "partial")
    check("VALIDATED（无 partial）-> validated",
          parse_validation_state(
              'NetworkAgentInfo{network{102} nc{[ Capabilities: INTERNET&VALIDATED ]}}')
          == "validated")
    check("只看 NetworkAgentInfo：请求段里的 VALIDATED 不算通过",
          parse_validation_state(
              'callbackRequest [ NetworkRequest [ LISTEN [ Capabilities: INTERNET&VALIDATED ]]]')
          == "unknown")
    check("空输出 -> unknown", parse_validation_state("") == "unknown")
    cmds = validation_settings_commands()
    check("验证探针命令覆盖 http/https/fallback/DoT",
          any("captive_portal_http_url" in c and "hicloud" in c for c in cmds)
          and any("captive_portal_https_url" in c for c in cmds)
          and any("captive_portal_fallback_url" in c for c in cmds)
          and any("private_dns_mode off" in c for c in cmds), str(cmds[:2]))
    check("验证探针不再是国内不通的 gstatic",
          all("gstatic" not in c for c in cmds))

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
