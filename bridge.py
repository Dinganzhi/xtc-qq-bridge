# -*- coding: utf-8 -*-
"""消息桥接调度层：轮询小天才新消息 → 转发（当前支持 log 打印 /
AstrBot 插件端点两种模式），并负责去重、回声过滤与 ADB 断线重连。

反向（QQ→小天才）由 qq_webhook.py 调用 bridge.forward_to_xiaotiancai()，
webhook.enabled=true 且 NapCat/插件回调就绪后启用。
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from utils.deduplicate import Deduplicator, EchoFilter, HistoryFilter


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

    def reply_result(self, request_id: str, message: str) -> bool:
        if self.logger:
            self.logger.info(f"[占位-回传] {request_id}: {message}")
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

    def reply_result(self, request_id: str, message: str) -> bool:
        return self.client.reply_result(request_id, message)


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
        # 长期已处理消息表（7 天持久化）：跨重启/多实例去重，杜绝死循环重复转发
        self.history = HistoryFilter(
            store_path=str(Path(__file__).resolve().parent / "data" / "history_cache.json"))
        self._poll_interval = float((cfg.get("xiaotiancai") or {}).get("check_interval", 2))
        self._heartbeat_interval = float((cfg.get("adb") or {}).get("heartbeat_interval", 10))
        self._login_check_interval = float(
            (cfg.get("xiaotiancai") or {}).get("login_check_interval", 600))
        # 操作锁：发送/导航期间暂停轮询，避免两个线程同时 uiautomator dump 冲突
        self._op_lock = threading.Lock()
        self._login_thread: threading.Thread | None = None
        # 登录待恢复标记：触发安全验证/登录失败后置位；
        # 轮询检测到重新登录时自动确认并 QQ 通知（无需重启）
        self._pending_login_notify = False
        # FIFO 任务队列：QQ→小天才 发送 / 登录 由单工作线程串行执行，
        # 保证多消息到达时按顺序处理，避免并发抢锁导致前后关系紊乱
        self._job_queue: queue.Queue = queue.Queue()
        self._job_thread: threading.Thread | None = None
        self._last_chat_open = 0.0  # 聊天窗口重开冷却（避免频繁打扰用户导航）
        # 自动登录检测开关（/小天才 自动登录 可切换；默认开启）
        self._auto_login_enabled = bool(
            (cfg.get("xiaotiancai") or {}).get("auto_login", True))

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self.running = True
        self._thread = threading.Thread(target=self._poll_loop, name="xtc-poll", daemon=True)
        self._thread.start()
        self._login_thread = threading.Thread(target=self._login_check_loop, name="xtc-login-check", daemon=True)
        self._login_thread.start()
        self._job_thread = threading.Thread(target=self._job_worker, name="xtc-jobs", daemon=True)
        self._job_thread.start()
        self._log("info", f"小天才消息轮询已启动（间隔 {self._poll_interval}s）")

    def stop(self) -> None:
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self._login_thread:
            self._login_thread.join(timeout=5)
        if self._job_thread:
            self._job_thread.join(timeout=5)

    # ------------------------------------------------------------------ 任务队列（FIFO，保证顺序）
    def _job_worker(self) -> None:
        while self.running:
            try:
                job = self._job_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                kind = job[0]
                if kind == "send":
                    _, text, user_id, group_id, request_id = job
                    self._do_send_job(text, user_id, group_id, request_id)
                elif kind == "login":
                    _, request_id = job
                    self._do_login_job(request_id)
                elif kind == "init":
                    _, request_id = job
                    self._do_init_job(request_id)
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"任务处理异常: {e}")
            finally:
                self._job_queue.task_done()

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
                    # 恢复检测：安全验证完成后自动确认并通知（无需重启）
                    if self._pending_login_notify:
                        if self.xtc.is_logged_in():
                            self._pending_login_notify = False
                            self._notify("✅ 小天才已重新登录（安全验证完成），桥接继续运行")
                    # 确保在聊天页读取（列表预览无法判断发送方，会把家长侧消息误当对方消息）；
                    # 离开聊天页后最多每 30s 重开一次，避免频繁打断用户
                    if not self.xtc.is_in_chat():
                        xtc_contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
                        if xtc_contact and time.monotonic() - self._last_chat_open >= 30:
                            self._last_chat_open = time.monotonic()
                            self.xtc.open_chat(xtc_contact)
                    contact, text, time_label = self.xtc.get_latest_message()
                finally:
                    self._op_lock.release()
                if text:
                    key = ("xtc", contact or "", text)
                    if not self.history.seen("xtc", contact or "", text) \
                            and not self.dedup.seen(key) and not self.echo.is_echo(text):
                        try:
                            ok = self._forward(contact, text, time_label)
                        except Exception as e:  # noqa: BLE001
                            self._log("warning", f"转发异常: {e}")
                            ok = False
                        if ok:
                            # 只有转发成功才记入长期历史（7 天），避免失败后永不重试
                            self.history.mark("xtc", contact or "", text)
                        self.dedup.mark(key)  # 无论成败都短期去重（120s），失败会自动重试
            except Exception as e:  # noqa: BLE001 单轮异常不致命
                self._log("warning", f"轮询异常: {e}")
            time.sleep(self._poll_interval)

    def _forward(self, contact, text: str, time_label: str = "") -> bool:
        """转发到所有 QQ 目标。返回是否全部成功（供轮询决定是否记入长期历史）。"""
        targets = self._qq_targets()
        if not targets:
            self._log("info", f"[占位] 收到小天才消息（未配置 QQ 目标，仅打印）: {text}")
            return True
        # 转发格式：[日期时间] [本地配置昵称] 消息内容。
        # 时间优先取小天才 App 内该消息的日期标签（如 "昨天 23:42"、"8月30日"）；
        # 只有时分（当天消息）时补当天日期；无标签时用当前时间。
        time_str = self._format_xtc_time(time_label)
        nickname = self._display_name(contact)
        message = f"[{time_str}] [{nickname}] {text}"
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
            self._confirm_xtc_delivery(message)  # 小天才侧送达确认（发送成功：<内容>）
        return ok_all

    def _format_xtc_time(self, time_label: str) -> str:
        """把 App 内的时间标签转成 [日期时间] 格式：
        - '06:56'（当天）→ '08-31 06:56'（补当天日期）
        - '昨天 23:42' / '8月30日 23:42' → 原样
        - 空 → 当前时间 'MM-DD HH:MM'"""
        label = (time_label or "").strip()
        if not label:
            return datetime.now().strftime("%m-%d %H:%M")
        if re.fullmatch(r"\d{1,2}:\d{2}", label):
            return datetime.now().strftime("%m-%d") + " " + label
        return label

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

    def forward_to_xiaotiancai(self, text: str, user_id: str = "",
                               group_id: str = "", request_id: str = "") -> bool:
        """QQ → 小天才：入队（FIFO 保证多消息按顺序处理），由单工作线程串行执行。"""
        if not text:
            return False
        self._job_queue.put(("send", text, user_id, group_id, request_id))
        return True

    def _do_send_job(self, text: str, user_id: str, group_id: str, request_id: str) -> None:
        """实际执行 QQ→小天才 发送 + 送达确认（工作线程内，按入队顺序）。"""
        self.echo.mark(text)
        contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
        if not contact:
            self._log("error", "反向转发需要 config.yaml → target.xtc_contact")
            return
        with self._op_lock:
            ok = self.xtc.open_chat(contact) and self.xtc.send_message(text)
        if ok:
            # 记录到长期历史：即使重启，这条消息也不会被当作"新消息"转发回 QQ
            self.history.mark("qq2xtc", text)
        if self._confirm_delivery() and (user_id or group_id):
            result_msg = ("发送成功：" if ok else "发送失败：") + text
            if request_id:
                try:
                    ok_r = self.forwarder.reply_result(request_id, result_msg)
                    self._log("info" if ok_r else "error",
                              f"[送达确认] {result_msg}（引用+@ 回传{'成功' if ok_r else '失败'}）")
                except Exception as e:  # noqa: BLE001
                    self._log("warning", f"送达确认回传异常: {e}")
            else:
                self._send_confirm(user_id, group_id, result_msg)

    # ------------------------------------------------------------------ 送达确认
    def _confirm_delivery(self) -> bool:
        return bool((self.cfg.get("target") or {}).get("confirm_delivery", True))

    def _send_confirm(self, user_id: str, group_id: str, message: str) -> None:
        """向 QQ 发送方发送送达确认（无 request_id 时的降级路径，走插件 /api/forward）。"""
        try:
            if group_id:
                ok = self.forwarder.send("group", str(group_id), message)
            elif user_id:
                ok = self.forwarder.send("private", str(user_id), message)
            else:
                return
            self._log("info" if ok else "error",
                      f"[送达确认] {message} → {group_id or user_id}（{'成功' if ok else '失败'}）")
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"送达确认发送异常: {e}")

    def _confirm_xtc_delivery(self, message: str) -> None:
        """小天才消息转发到 QQ 成功后，在小天才聊天内回复「发送成功：<转发内容>」。
        确认消息以"发送成功"开头且为家长侧消息（右侧气泡），读取路径按前缀过滤，不会循环转发。"""
        if not self._confirm_delivery():
            return
        try:
            if not self.xtc.is_in_chat():
                return  # 不在聊天页就不打扰
            confirm_text = "发送成功：" + message
            self.echo.mark(confirm_text)
            with self._op_lock:
                self.xtc.send_message(confirm_text)
            self._log("info", f"[送达确认] 已在小天才聊天回复 {confirm_text}")
        except Exception as e:  # noqa: BLE001
            self._log("debug", f"小天才送达确认跳过: {e}")

    # ------------------------------------------------------------------ 登录
    def login_xiaotiancai(self, request_id: str = "") -> str:
        """执行账密登录（入队，由工作线程串行执行，保证与其他发送任务的顺序）。
        有 request_id 时把结果回传给插件（原会话引用+@ 回复发送人）；否则走 _notify。"""
        self._job_queue.put(("login", request_id))
        return "queued"

    def _do_login_job(self, request_id: str) -> None:
        acc = (self.cfg.get("xiaotiancai") or {}).get("login") or {}
        phone = str(acc.get("phone", "")).strip()
        password = str(acc.get("password", "")).strip()
        if not phone or not password:
            msg = "未配置手机号/密码（config.yaml → xiaotiancai.login.phone/password）"
            self._login_reply(request_id, "小天才登录：" + msg)
            return
        try:
            with self._op_lock:
                status = self.xtc.login(phone, password)
        except Exception as e:  # noqa: BLE001
            self._log("error", f"自动登录异常: {e}")
            self._login_reply(request_id, "小天才自动登录出错，请手动检查模拟器")
            return
        if status == "risk":
            self._pending_login_notify = True  # 等待用户手动完成安全验证
            self._login_reply(request_id, "需要安全验证：请手动打开模拟器完成验证")
            self._notify("⚠️ 小天才登录触发安全验证，请手动打开模拟器完成验证")
        elif status == "fail":
            self._pending_login_notify = True  # 避免每 10 分钟反复重试刷屏
            self._login_reply(request_id, "登录失败（账号或密码错误等），请检查配置或手动登录")
            self._notify("❌ 小天才自动登录失败（账号或密码错误等）")
        elif status == "error":
            self._login_reply(request_id, "登录出错（控件未找到），请运行 tools/dump_ui.py 查看登录页")
        elif status == "ok":
            self._login_reply(request_id, "小天才登录成功")
        elif status == "already":
            self._login_reply(request_id, "小天才已登录，无需重复登录")

    def _login_reply(self, request_id: str, message: str) -> None:
        """登录结果回复：有 request_id 回传插件（引用+@）；否则 _notify 兜底。"""
        if request_id:
            try:
                ok = self.forwarder.reply_result(request_id, message)
                self._log("info" if ok else "error",
                          f"[登录结果] {message}（回传{'成功' if ok else '失败'}）")
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"登录结果回传异常: {e}")
        else:
            self._notify("小天才登录：" + message)

    # ------------------------------------------------------------------ 自动登录开关 / 初始化
    def toggle_auto_login(self, request_id: str = "") -> str:
        """/小天才 自动登录：切换自动登录检测开关，结果回传插件。"""
        self._auto_login_enabled = not self._auto_login_enabled
        state = "已开启（每 10 分钟检测，未登录自动登录）" if self._auto_login_enabled else "已关闭"
        msg = f"自动登录检测{state}"
        if request_id:
            try:
                self.forwarder.reply_result(request_id, msg)
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"自动登录开关回传异常: {e}")
        else:
            self._notify("小天才" + msg)
        self._log("info", f"[自动登录] {msg}")
        return msg

    def init_xiaotiancai(self, request_id: str = "") -> str:
        """/小天才 初始化：入队执行界面状态检测与恢复。"""
        self._job_queue.put(("init", request_id))
        return "queued"

    def _do_init_job(self, request_id: str) -> None:
        """检测并恢复界面状态：启动 → 弹窗 → 登录态 → 聊天页 → 文字模式 → 清空输入框。"""
        msgs: list[str] = []
        try:
            with self._op_lock:
                launched = self.xtc.launch()
                msgs.append("启动" + ("OK" if launched else "失败"))
                self.xtc.dismiss_blockers()
                if self.xtc.is_logged_in():
                    msgs.append("已登录")
                else:
                    acc = (self.cfg.get("xiaotiancai") or {}).get("login") or {}
                    phone = str(acc.get("phone", "")).strip()
                    password = str(acc.get("password", "")).strip()
                    if phone and password:
                        status = self.xtc.login(phone, password)
                        msgs.append("登录：" + self._login_status_text(status))
                    else:
                        msgs.append("未登录（未配置账密，请手动登录）")
                contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
                if contact and self.xtc.open_chat(contact):
                    msgs.append("已进入聊天")
                msgs.append(self.xtc.ensure_input_clean())
            reply = "初始化完成：" + "，".join(msgs)
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"初始化异常: {e}")
            reply = "初始化失败：" + str(e)
        self._log("info", f"[初始化] {reply}")
        if request_id:
            try:
                self.forwarder.reply_result(request_id, reply)
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"初始化回传异常: {e}")
        else:
            self._notify(reply)

    @staticmethod
    def _login_status_text(status: str) -> str:
        return {
            "already": "已登录",
            "ok": "成功",
            "risk": "需安全验证（请手动完成）",
            "fail": "失败（账号或密码错误等）",
            "error": "出错（控件未找到）",
        }.get(status, status)

    def _login_check_loop(self) -> None:
        """检测登录态，未登录则自动登录（可被 /小天才 自动登录 关闭）。
        首次启动：等 App 稳定后（约 5s）立即检测一次，之后每 login_check_interval 秒一次。"""
        interval = self._login_check_interval
        time.sleep(5)  # 等 App 启动稳定，避免误判未登录
        last = float("-inf")  # 首次循环立即触发检测
        while self.running:
            try:
                now = time.monotonic()
                if now - last >= interval:
                    last = now
                    if not self._auto_login_enabled:
                        self._log("debug", "自动登录检测已关闭，跳过")
                        time.sleep(10)
                        continue
                    if not self._op_lock.acquire(blocking=False):
                        continue
                    try:
                        logged = self.xtc.is_logged_in()
                    finally:
                        self._op_lock.release()
                    if not logged:
                        if self._pending_login_notify:
                            # 安全验证/登录失败待处理：等用户手动完成，不反复自动重试
                            self._log("info", "小天才登录待处理（安全验证/失败），等待用户手动操作，暂不重试")
                        else:
                            self._log("info", "检测到小天才未登录，尝试自动登录...")
                            self.login_xiaotiancai()
                    else:
                        if self._pending_login_notify:
                            self._pending_login_notify = False
                            self._notify("✅ 小天才已重新登录（安全验证完成），桥接继续运行")
                        else:
                            self._log("debug", "小天才登录态正常")
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"自动登录检测异常: {e}")
            time.sleep(10)

    def _notify(self, text: str) -> None:
        """发送登录相关通知到 QQ（target.notify_qq，缺省用 qq_private 第一个）。"""
        target = self._notify_target()
        if not target:
            self._log("info", f"[通知占位] {text}")
            return
        try:
            ok = self.forwarder.send("private", target, text)
            if ok:
                self._log("info", f"[通知] private:{target} ← {text}")
            else:
                self._log("error", f"[通知失败] private:{target} ← {text}")
        except Exception as e:  # noqa: BLE001
            self._log("error", f"通知发送异常: {e}")

    def _notify_target(self) -> str:
        t = self.cfg.get("target") or {}
        v = t.get("notify_qq")
        if v:
            return str(v[0]) if isinstance(v, (list, tuple)) else str(v)
        for mtype, tid in self._qq_targets():
            if mtype == "private":
                return tid
        return ""

    def _log(self, level: str, msg: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(msg)
