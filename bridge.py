# -*- coding: utf-8 -*-
"""消息桥接调度层：轮询小天才新消息 → 转发（当前支持 log 打印 /
AstrBot 插件端点两种模式），并负责去重、回声过滤与 ADB 断线重连。

反向（QQ→小天才）由 qq_webhook.py 调用 bridge.forward_to_xiaotiancai()，
webhook.enabled=true 且 NapCat/插件回调就绪后启用。
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

from utils.deduplicate import Deduplicator, EchoFilter


def make_forwarder(cfg: dict, logger=None):
    """按 config.forward.mode 构造转发器。"""
    fwd = (cfg.get("forward") or {})
    mode = fwd.get("mode", "log")
    if mode == "plugin":
        pc = fwd.get("plugin", {})
        from plugin_client import PluginClient
        client = PluginClient(base_url=pc.get("base_url", "http://127.0.0.1:11452"),
                              token=pc.get("token", ""))
        return PluginForwarder(client, logger)
    return LogForwarder(logger)


class LogForwarder:
    """调试占位：只打印，不真正发送。"""

    def __init__(self, logger=None):
        self.logger = logger

    def send(self, target_type, target_id, message: str) -> bool:
        if self.logger:
            self.logger.info(f"[转发-占位] {target_type}:{target_id} ← {message}")
        else:
            print(f"[转发-占位] {target_type}:{target_id} ← {message}")
        return True


class PluginForwarder:
    def __init__(self, client, logger=None):
        self.client = client
        self.logger = logger
        self._last_err_log = 0.0
        self._err_log_interval = 60.0  # 同一故障最多 60s 报一次，避免刷屏

    def send(self, target_type, target_id, message: str) -> bool:
        ok = self.client.send(target_type, target_id, message)
        if not ok and self.logger:
            now = time.monotonic()
            if now - self._last_err_log >= self._err_log_interval:
                self.logger.error(
                    "转发到 AstrBot 插件失败（插件未启动？检查 forward.plugin 配置与插件 http_port/token）"
                )
                self._last_err_log = now
        return ok


class MessageBridge:
    def __init__(self, cfg: dict, adb, xtc, forwarder, logger=None):
        self.cfg = cfg
        self.adb = adb
        self.xtc = xtc
        self.forwarder = forwarder
        self.logger = logger
        self.running = False
        self._thread: threading.Thread | None = None
        self.dedup = Deduplicator()
        # 回声状态写入文件：多实例/重启后共享，防止重复转发
        store_path = str(Path(__file__).resolve().parent / "data" / "echo_cache.json")
        self.echo = EchoFilter(store_path=store_path)
        self._poll_interval = float((cfg.get("xiaotiancai") or {}).get("check_interval", 2))
        self._heartbeat_interval = float((cfg.get("adb") or {}).get("heartbeat_interval", 10))
        # 操作锁：发送/导航期间暂停轮询，避免两个线程同时 uiautomator dump 冲突
        self._op_lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self.running = True
        self._thread = threading.Thread(target=self._poll_loop, name="xtc-poll", daemon=True)
        self._thread.start()
        self._log("info", f"小天才消息轮询已启动（间隔 {self._poll_interval}s）")

    def stop(self) -> None:
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------ 轮询
    def _poll_loop(self) -> None:
        last_heartbeat = 0.0
        while self.running:
            try:
                now = time.monotonic()
                if now - last_heartbeat >= self._heartbeat_interval:
                    last_heartbeat = now
                    if not self.adb.is_connected():
                        self._log("warning", "ADB 断连，尝试重连...")
                        try:
                            self.adb.ensure_connected()
                        except Exception as e:  # noqa: BLE001
                            self._log("error", f"重连失败: {e}")
                            time.sleep(2)
                            continue

                if not self._op_lock.acquire(blocking=False):
                    continue  # 正在发送/导航，跳过本轮，避免 dump 冲突
                try:
                    contact, text = self.xtc.get_latest_message()
                finally:
                    self._op_lock.release()
                if text:
                    key = ("xtc", contact or "", text)
                    if not self.dedup.seen(key) and not self.echo.is_echo(text):
                        self._forward(contact, text)
                        self.dedup.mark(key)
            except Exception as e:  # noqa: BLE001 单轮异常不致命
                self._log("warning", f"轮询异常: {e}")
            time.sleep(self._poll_interval)

    def _forward(self, contact, text: str) -> None:
        targets = self._qq_targets()
        if not targets:
            self._log("info", f"[占位] 收到小天才消息（未配置 QQ 目标，仅打印）: {text}")
            return
        # 转发格式：[时间] [本地配置昵称] 消息内容（昵称不用 App 里的原始姓名）
        now = datetime.now().strftime("%H:%M")
        nickname = self._display_name(contact)
        message = f"[{now}] [{nickname}] {text}"
        ok_all = True
        for target_type, target_id in targets:
            try:
                ok = self.forwarder.send(target_type, target_id, message)
            except Exception as e:  # noqa: BLE001
                self._log("error", f"转发异常({target_type}:{target_id}): {e}")
                ok = False
            if ok:
                self._log("info", f"[转发成功] {target_type}:{target_id} ← {message}")
            else:
                self._log("error", f"[转发失败] {target_type}:{target_id} ← {message}")
                ok_all = False
        if ok_all:
            # 标记原文 + 格式化消息：多实例/重启后也不会再转发同一条
            self.echo.mark(text)
            self.echo.mark(message)

    def _qq_targets(self) -> list[tuple[str, str]]:
        """转发目标列表 [(type, id)]；qq_private/qq_group 支持单个字符串或列表。"""
        t = self.cfg.get("target") or {}
        targets: list[tuple[str, str]] = []
        for key, mtype in (("qq_private", "private"), ("qq_group", "group")):
            v = t.get(key)
            if not v:
                continue
            items = v if isinstance(v, (list, tuple)) else [v]
            for it in items:
                s = str(it).strip()
                if s:
                    targets.append((mtype, s))
        return targets

    def _display_name(self, contact) -> str:
        """小天才联系人 → 本地配置的显示昵称。映射优先级：
        target.nicknames[联系人] → target.default_nickname → App 原始名。
        聊天窗口模式 contact 可能为 None，此时按 target.xtc_contact 查映射。"""
        t = self.cfg.get("target") or {}
        nicknames = t.get("nicknames") or {}
        name = contact or t.get("xtc_contact", "") or ""
        if name and name in nicknames:
            return str(nicknames[name])
        default = t.get("default_nickname")
        if default:
            return str(default)
        return name

    # ------------------------------------------------------------------ 反向
    def qq_sender_allowed(self, qq: str, group: str = "") -> bool:
        """QQ→小天才 接收白名单（config.yaml → webhook）：
        - 私聊消息：QQ 号必须在 webhook.allow_from 列表里；
        - 群聊消息：群号必须在 webhook.allow_groups 列表里；
        - 对应列表为空 = 该类消息全部拒绝（严格白名单）。
        """
        wh = self.cfg.get("webhook") or {}
        if group:
            allow = {str(g) for g in (wh.get("allow_groups") or [])}
            ok = str(group) in allow
            if not ok:
                self._log("info", f"群聊 {group} 不在白名单（webhook.allow_groups），已忽略")
            return ok
        allow = {str(u) for u in (wh.get("allow_from") or [])}
        ok = str(qq) in allow
        if not ok:
            self._log("info", f"私聊 {qq} 不在白名单（webhook.allow_from），已忽略")
        return ok

    def forward_to_xiaotiancai(self, text: str) -> bool:
        """QQ → 小天才：由 webhook 回调触发。发送前先标记回声。
        全程持有操作锁，轮询线程在此期间暂停（避免 uiautomator dump 冲突）。"""
        if not text:
            return False
        self.echo.mark(text)
        contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
        if not contact:
            self._log("error", "反向转发需要 config.yaml → target.xtc_contact")
            return False
        with self._op_lock:
            if not self.xtc.open_chat(contact):
                return False
            return self.xtc.send_message(text)

    def _log(self, level: str, msg: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(msg)
