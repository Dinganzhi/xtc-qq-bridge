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
from datetime import datetime, timedelta
from pathlib import Path

from utils.deduplicate import Deduplicator, EchoFilter, HistoryFilter
from msg_log import MessageLog


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

    # xtc 侧命令需要的 QQ 数据查询：log 模式无 QQ 数据源，一律返回 None
    def qq_search(self, keyword, allow_from, allow_groups, limit=30):
        return None

    def qq_online(self, minutes, allow_from, allow_groups):
        return None

    def qq_remind(self, group_id, qq_id, text=""):
        return None


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

    def qq_search(self, keyword, allow_from, allow_groups, limit=30):
        return self.client.qq_search(keyword, allow_from, allow_groups, limit)

    def qq_online(self, minutes, allow_from, allow_groups):
        return self.client.qq_online(minutes, allow_from, allow_groups)

    def qq_remind(self, group_id, qq_id, text=""):
        return self.client.qq_remind(group_id, qq_id, text)


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
        # 本地消息库（/小天才 历史消息 数据源）：记录真实对话，文件持久化
        self.msgs = MessageLog(
            path=str(Path(__file__).resolve().parent / "data" / "msg_log.json"))
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
        # xtc 侧命令（在小天才聊天里直接输入，由本桥程序解析执行，与 QQ 命令隔离）：
        # 命令前缀。命令去重：_cmd_pending=已入队未完成（防重复入队），
        # _cmd_handled=执行成功的时间戳表（5 分钟内不重复执行，之后允许再次输入）。
        self._xtc_cmd_prefix = str(
            (cfg.get("xiaotiancai") or {}).get("cmd_prefix", "/小天才")).strip()
        self._cmd_pending: set[str] = set()
        self._cmd_handled: dict[str, float] = {}

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
                elif kind == "history":
                    # 小天才历史消息：count + 回传方式（request_id 或写入小天才聊天）
                    _, count, request_id, into_chat = job
                    self._do_history_job(count, request_id, into_chat)
                elif kind == "cmd":
                    # xtc 侧命令（在小天才聊天里输入的 /小天才 xxx，手表侧或家长侧均可）
                    _, text = job
                    ok = self._do_cmd_job(text)
                    self._cmd_pending.discard(text)
                    if ok:
                        self._cmd_handled[text] = time.monotonic()
                        if len(self._cmd_handled) > 100:  # 修剪上限
                            for k in sorted(self._cmd_handled,
                                            key=self._cmd_handled.get)[:50]:
                                del self._cmd_handled[k]
                    # 回复失败：不加 handled，下一轮轮询自动重试
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
                            self._notify("小天才已重新登录（安全验证完成），桥接继续运行")
                    # 确保在聊天页读取（列表预览无法判断发送方，会把家长侧消息误当对方消息）；
                    # 离开聊天页后最多每 30s 重开一次，避免频繁打断用户
                    if not self.xtc.is_in_chat():
                        xtc_contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
                        if xtc_contact and time.monotonic() - self._last_chat_open >= 30:
                            self._last_chat_open = time.monotonic()
                            self.xtc.open_chat(xtc_contact)
                    contact, text, time_label, own_text, own_recent = \
                        self.xtc.get_latest_message()
                finally:
                    self._op_lock.release()
                is_cmd_text = bool(self._xtc_cmd_prefix and text
                                   and text.startswith(self._xtc_cmd_prefix))
                # xtc 侧命令（/小天才 …）：手表侧或家长侧输入均可。
                # 1) 手表侧（对方发来）的命令 → 执行，不转发、不入消息库
                if is_cmd_text:
                    self._maybe_xtc_cmd(text)
                # 2) 家长侧输入的命令：可能被送达确认等新消息盖过（不再是"最新一条"），
                #    扫最近若干条自己发的消息
                elif own_recent:
                    for t in own_recent:
                        if self._xtc_cmd_prefix and t.startswith(self._xtc_cmd_prefix):
                            self._maybe_xtc_cmd(t)
                            break
                # 普通手表消息 → 转发（命令已被上面拦截，绝不转发/入库）
                if text and not is_cmd_text:
                    key = ("xtc", contact or "", text)
                    if not self.history.seen("xtc", contact or "", text) \
                            and not self.dedup.seen(key) and not self.echo.is_echo(text):
                        # 本地消息库归档（发送成功等系统提示已被读取层过滤，
                        # 时间优先取 App 时间标签解析出的真实时刻）
                        ts = self._label_epoch(time_label)
                        self.msgs.append("xtc", contact or "", text, t=ts)
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
            self._archive_qq_send(text)
        # 命令类文本（/小天才 …）的"结果"由命令任务自己写回小天才聊天，
        # 不再向 QQ 发"发送成功"确认，避免误导。
        is_cmd_text = bool(self._xtc_cmd_prefix
                           and text.startswith(self._xtc_cmd_prefix))
        if self._confirm_delivery() and (user_id or group_id) and not is_cmd_text:
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
    def _archive_qq_send(self, text: str) -> None:
        """QQ → 小天才 发送成功后归档到本地消息库。
        - 发送的整条消息是插件格式 `[MM-DD HH:MM] [QQ昵称] 内容` → 拆出昵称与内容；
        - 命令文本（/小天才 …）与系统提示不入库。"""
        m = re.match(r"^\[(\d{2}-\d{2} \d{2}:\d{2})\] \[(.+?)\] (.*)$", text)
        if m:
            sender, content = m.group(2), m.group(3)
        else:
            sender, content = "QQ", text
        content = (content or "").strip()
        if not content:
            return
        if any(content.startswith(p) for p in self._system_msg_prefixes()):
            return
        if self._xtc_cmd_prefix and content.startswith(self._xtc_cmd_prefix):
            return
        self.msgs.append("qq", sender, content)

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
            self._notify("小天才登录触发安全验证，请手动打开模拟器完成验证")
        elif status == "fail":
            self._pending_login_notify = True  # 避免每 10 分钟反复重试刷屏
            self._login_reply(request_id, "登录失败（账号或密码错误等），请检查配置或手动登录")
            self._notify("小天才自动登录失败（账号或密码错误等）")
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
        """检测并恢复界面状态：启动 → 清理弹窗 → 登录态 → 聊天页 → 文字模式 → 清空输入框。
        每步前都清理弹窗（settle），失败自动重试，弹窗不会中断整个初始化。"""
        msgs: list[str] = []
        try:
            with self._op_lock:
                # 1) 启动（弹窗清理 + 重试）
                launched = False
                for _ in range(2):
                    self.xtc.settle()
                    if self.xtc.launch():
                        launched = True
                        break
                    time.sleep(2.0)
                msgs.append("启动" + ("OK" if launched else "失败"))
                time.sleep(2.0)
                self.xtc.settle()
                # 2) 登录态
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
                # 3) 进入聊天页（带重试 + 弹窗清理）
                contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
                chat_ok = False
                if contact:
                    for _ in range(3):
                        if self.xtc.is_in_chat():
                            chat_ok = True
                            break
                        self.xtc.settle()
                        if self.xtc.open_chat(contact):
                            chat_ok = True
                            break
                        time.sleep(1.5)
                msgs.append("已进入聊天" if chat_ok else ("未进入聊天" if contact else "未配置联系人"))
                # 4) 文字模式 + 清空输入框（带重试）
                clean = ""
                for _ in range(3):
                    clean = self.xtc.ensure_input_clean()
                    if "已清空" in clean or "已就绪" in clean:
                        break
                    self.xtc.settle()
                    time.sleep(1.5)
                msgs.append(clean or "输入框处理失败")
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

    # ------------------------------------------------------------------ 小天才历史消息（QQ 与 xtc 侧共用）
    def fetch_xtc_history(self, count: int = 20, request_id: str = "",
                          into_chat: bool = False) -> str:
        """读取小天才最近对话历史（入队，由工作线程串行执行，保证与发送顺序）。
        - QQ 侧：request_id 回传插件，插件在原会话引用+@ 回复；
        - xtc 侧：into_chat=True 时结果写进小天才聊天。
        返回 'queued'。count 自动夹到 1..100。"""
        try:
            count = max(1, min(int(count), 100))
        except (TypeError, ValueError):
            count = 20
        self._job_queue.put(("history", count, request_id or "", bool(into_chat)))
        return "queued"

    def _do_history_job(self, count: int, request_id: str, into_chat: bool) -> None:
        """工作线程内：读本地消息库 → 格式化回传（不滚动界面，不依赖聊天页状态）。"""
        entries = self.msgs.recent(count)
        if not entries:
            reply = ("小天才历史消息：暂无本地消息记录"
                     "（消息库自桥接启用后自动积累真实对话）")
        else:
            reply = self._format_history_text(entries, count,
                                              max_chars=900 if into_chat else 3800)
        self._log("info", f"[历史消息] 本地消息库 {len(entries)} 条，回传方式: "
                          f"{'写入小天才聊天' if into_chat else 'QQ'}")
        if into_chat:
            self._reply_into_xtc(reply)
        elif request_id:
            try:
                ok = self.forwarder.reply_result(request_id, reply)
                self._log("info" if ok else "error",
                          f"[历史消息回传] {reply[:120]}...（{'成功' if ok else '失败'}）")
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"历史消息回传异常: {e}")
        else:
            self._notify("小天才历史消息：\n" + reply)

    def _format_history_text(self, entries: list[dict], count: int,
                             max_chars: int = 3800) -> str:
        """把本地消息库条目格式化为文本。行格式：[MM-DD HH:MM] 发送方: 内容。
        - 日期用明确数字（昨天/前天 等已由归档时间戳换算成具体日期，如 09-01）；
        - 跳过系统提示（发送成功/发送失败）与命令文本（防御性过滤）；
        - 超长从最旧截断。"""
        sys_prefixes = self._system_msg_prefixes()
        lines: list[str] = []
        for e in entries:
            text = (e.get("text") or "").strip()
            if not text:
                continue
            if any(text.startswith(p) for p in sys_prefixes):
                continue
            if self._xtc_cmd_prefix and text.startswith(self._xtc_cmd_prefix):
                continue
            sender = (e.get("sender") or "").strip()
            if e.get("kind") == "xtc":
                sender = self._display_name(sender)
            try:
                dt = datetime.fromtimestamp(float(e.get("t") or 0))
            except (TypeError, ValueError, OSError):
                dt = datetime.now()
            lines.append(f"[{dt:%m-%d} {dt:%H:%M}] {sender}: {text}")
        if not lines:
            return "小天才历史消息：暂无本地消息记录"
        dropped = 0
        while len(lines) > 1 and sum(len(l) for l in lines) > max_chars:
            lines.pop(0)
            dropped += 1
        header = f"小天才历史消息（最近 {len(lines)} 条"
        if dropped:
            header += f"，省略更早 {dropped} 条"
        header += "）："
        return header + "\n" + "\n".join(lines)

    def _system_msg_prefixes(self) -> list:
        ui = ((self.cfg.get("xiaotiancai") or {}).get("ui") or {})
        return ui.get("system_msg_prefixes", ["发送成功", "发送失败", "✅", "❌"])

    def _label_epoch(self, time_label: str, now: datetime | None = None) -> float | None:
        """把 App 时间标签解析成时间戳（本地消息库归档用）：
        'HH:MM'→今天；'昨天 HH:MM'/'前天 HH:MM'→对应日期；'M月D日 HH:MM'→该日期。
        解析失败返回 None（归档时用当前时间兜底）。显示层用时间戳输出明确日期，
        不再出现"昨天/前天"字样。"""
        label = (time_label or "").strip()
        if not label:
            return None
        now = now or datetime.now()
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", label)
        if m:
            return now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                               second=0, microsecond=0).timestamp()
        m = re.fullmatch(r"(昨天|前天)\s*(\d{1,2}):(\d{2})", label)
        if m:
            days_back = 1 if m.group(1) == "昨天" else 2
            return (now - timedelta(days=days_back)).replace(
                hour=int(m.group(2)), minute=int(m.group(3)),
                second=0, microsecond=0).timestamp()
        m = re.search(r"(\d{1,2})月(\d{1,2})日", label)
        if m:
            tm = re.search(r"(\d{1,2}):(\d{2})", label)
            try:
                return now.replace(month=int(m.group(1)), day=int(m.group(2)),
                                   hour=int(tm.group(1)) if tm else 0,
                                   minute=int(tm.group(2)) if tm else 0,
                                   second=0, microsecond=0).timestamp()
            except ValueError:
                return None
        return None

    # ------------------------------------------------------------------ xtc 侧命令（在小天才聊天输入，由本桥执行）
    # 排版模仿 QQ 帮助信息（用法：+ 逐行命令 + 对齐说明）；聊天输入支持换行，
    # 会按多行原样发送。
    _XTC_USAGE = ("用法（在小天才聊天里直接输入）：\n"
                  "/小天才 搜索 <昵称>          白名单QQ私聊/群聊中找人（附最后消息时间）\n"
                  "/小天才 在线人数 <分钟>      最近N分钟白名单QQ会话发言人数（1-60）\n"
                  "/小天才 提醒 <群号> <QQID> [内容]    在指定QQ群内@提醒该用户\n"
                  "/小天才 历史消息 <条数>      查看最近对话记录（1-100，默认20）")

    def _xtc_usage(self) -> str:
        return self._XTC_USAGE

    def _maybe_xtc_cmd(self, text: str) -> None:
        """命令去重入队（手表侧/家长侧输入共用）。
        - pending：已入队未完成 → 不再重复入队；
        - handled：执行成功 5 分钟内不重复执行（避免命令仍是最新消息时反复触发）；
          5 分钟后用户再次输入相同命令可重新执行。
        回复失败不入 handled，下一轮轮询自动重试。"""
        now = time.monotonic()
        if text in self._cmd_pending:
            return
        hts = self._cmd_handled.get(text, 0)
        if hts and now - hts < 300:
            return
        # 清理过期的 handled
        stale = [k for k, ts in self._cmd_handled.items() if now - ts >= 300]
        for k in stale:
            del self._cmd_handled[k]
        self._cmd_pending.add(text)
        self._log("info", f"[xtc命令] 收到: {text}")
        self._job_queue.put(("cmd", text))

    def _do_cmd_job(self, text: str) -> bool:
        """执行 xtc 侧命令。返回是否成功回复（失败时调用方复位去重，允许下轮重试）。
        语法错误 / 无法识别的子命令 / 缺参数 → 一律回复完整帮助列表。"""
        raw = (text or "").strip()
        if not self._xtc_cmd_prefix or not raw.startswith(self._xtc_cmd_prefix):
            return True  # 非命令消息不应到任务里（轮询已过滤）
        rest = raw[len(self._xtc_cmd_prefix):].strip()
        if not rest or rest in ("帮助", "help", "-h", "?"):
            return self._reply_into_xtc(self._xtc_usage())
        sub, _, args = rest.partition(" ")
        sub = sub.strip()
        args = args.strip()
        reply = None
        try:
            if sub in ("历史消息", "history"):
                reply = self._cmd_history(args)      # None = 已同步执行并写回聊天
            elif sub in ("搜索", "search"):
                reply = self._cmd_search(args)
            elif sub in ("在线人数", "online"):
                reply = self._cmd_online(args)
            elif sub in ("提醒", "remind"):
                reply = self._cmd_remind(args)
            else:
                reply = self._xtc_usage()            # 无法识别 → 帮助列表
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"xtc 命令执行异常: {e}")
            reply = self._xtc_usage()
        if reply is None:
            return True
        return self._reply_into_xtc(reply)

    def _cmd_history(self, args: str):
        """返回 None=已执行（写回聊天）；str=帮助列表（参数错误时）。"""
        if args:
            try:
                n = int(args)
            except ValueError:
                return self._xtc_usage()
            if not 1 <= n <= 100:
                return self._xtc_usage()
        else:
            n = 20
        # 已处于工作线程：直接同步执行历史读取任务（结果写入小天才聊天）
        self._do_history_job(n, "", into_chat=True)
        return None

    def _cmd_search(self, args: str) -> str:
        if not args:
            return self._xtc_usage()
        allow_from, allow_groups = self._qq_scope()
        try:
            resp = self.forwarder.qq_search(args, allow_from, allow_groups)
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"QQ 搜索调用异常: {e}")
            resp = None
        if not resp:
            return "QQ 搜索失败：AstrBot 插件未连接（请确认 AstrBot 与插件已运行）"
        if not resp.get("ok"):
            return f"QQ 搜索失败：{resp.get('error') or '未知错误'}"
        people = resp.get("people") or []
        if not people:
            return f"未找到昵称含「{args}」的人（白名单私聊/群聊）"
        now = time.time()
        lines = ["搜索结果："]
        for p in people:
            name = p.get("name") or p.get("qq") or "?"
            qq = p.get("qq") or ""
            if p.get("scope") == "group":
                head = f"群聊 {p.get('session_name') or p.get('session_id')}(群{p.get('session_id')}) {name}(QQ{qq})"
            else:
                head = f"私聊 {name}(QQ{qq})"
            ts = p.get("last_ts")
            if ts:
                try:
                    lines.append(f"{head}：上次发送 {self._fmt_dt(ts)}，距现在 {self._fmt_ago(ts, now)}")
                except Exception:  # noqa: BLE001
                    lines.append(f"{head}：上次发送时间未知")
            else:
                lines.append(f"{head}：未发送过任何消息")
        limit = int((resp.get("limit") or 0) or 0)
        if limit and len(people) >= limit:
            lines.append(f"（结果过多，仅显示前 {limit} 条）")
        return "\n".join(lines)

    def _cmd_online(self, args: str) -> str:
        try:
            minutes = int(args or "")
        except ValueError:
            return self._xtc_usage()
        if not 1 <= minutes <= 60:
            return self._xtc_usage()
        allow_from, allow_groups = self._qq_scope()
        try:
            resp = self.forwarder.qq_online(minutes, allow_from, allow_groups)
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"QQ 在线人数调用异常: {e}")
            resp = None
        if not resp:
            return "在线人数查询失败：AstrBot 插件未连接（请确认 AstrBot 与插件已运行）"
        if not resp.get("ok"):
            return f"在线人数查询失败：{resp.get('error') or '未知错误'}"
        total = int(resp.get("total") or 0)
        lines = [f"在线人数（最近 {minutes} 分钟，白名单QQ会话）：{total} 人"]
        for s in resp.get("sessions") or []:
            if s.get("kind") == "private":
                lines.append(f"私聊：{s.get('count')} 人")
            else:
                sid = s.get("session_id") or ""
                name = s.get("session_name") or sid
                lines.append(f"群聊 {name}({sid})：{s.get('count')} 人")
        return "\n".join(lines)

    def _cmd_remind(self, args: str) -> str:
        parts = args.split(None, 2) if args else []
        if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            return self._xtc_usage()  # 参数缺失/非数字 → 帮助列表
        group_id, qq_id = parts[0], parts[1]
        content = parts[2] if len(parts) > 2 else ""
        allow_from, allow_groups = self._qq_scope()
        if group_id not in allow_groups:
            return f"群 {group_id} 不在白名单（webhook.allow_groups），无法提醒"
        try:
            resp = self.forwarder.qq_remind(group_id, qq_id, content)
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"QQ 提醒调用异常: {e}")
            resp = None
        if not resp:
            return "提醒失败：AstrBot 插件未连接（请确认 AstrBot 与插件已运行）"
        if resp.get("ok"):
            return f"已提醒 QQ{qq_id}（群 {group_id}）" + (f"：{content}" if content else "")
        return f"提醒失败：{resp.get('error') or '未知错误'}"

    # ------------------------------------------------------------------ xtc 命令辅助
    def _reply_into_xtc(self, text: str) -> bool:
        """把命令结果写进小天才聊天（家长侧消息，转发路径按"自己发的"忽略，不会回传 QQ）。
        实测聊天输入支持换行：多行文本（帮助列表/历史消息等）按原样作为一条消息发送，
        与 QQ 帮助信息的排版一致；超长截断。"""
        msg = (text or "").strip()
        if not msg:
            return False
        if len(msg) > 900:
            msg = msg[:900] + "（过长截断）"
        contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
        if not contact:
            self._log("error", "回复小天才需要 config.yaml → target.xtc_contact")
            return False
        with self._op_lock:
            if not self.xtc.is_in_chat():
                self.xtc.dismiss_blockers()
                if not self.xtc.open_chat(contact):
                    self._log("error", "命令结果写入失败：无法进入小天才聊天窗口")
                    return False
            self.echo.mark(msg)  # 本桥发出的消息，防止（理论上）被读回
            try:
                ok = self.xtc.send_message(msg)
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"命令结果写入异常: {e}")
                ok = False
        if ok:
            self._log("info", f"[xtc命令回复] {msg[:150]!r}...")
        else:
            self._log("error", "[xtc命令回复] 发送失败（请检查小天才聊天窗口状态）")
        return ok

    def _qq_scope(self) -> tuple[list[str], list[str]]:
        """白名单范围：webhook.allow_from（私聊）+ allow_groups（群聊）。"""
        wh = self.cfg.get("webhook") or {}
        allow_from = [str(x) for x in (wh.get("allow_from") or [])]
        allow_groups = [str(x) for x in (wh.get("allow_groups") or [])]
        return allow_from, allow_groups

    @staticmethod
    def _fmt_dt(ts) -> str:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")

    @staticmethod
    def _fmt_ago(ts, now: float | None = None) -> str:
        now = time.time() if now is None else now
        sec = max(0, now - float(ts))
        if sec < 60:
            return "刚刚"
        m = int(sec // 60)
        if m < 60:
            return f"{m} 分钟"
        h = int(m // 60)
        if h < 24:
            return f"{h} 小时"
        return f"{int(h // 24)} 天"

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
                            self._notify("小天才已重新登录（安全验证完成），桥接继续运行")
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
