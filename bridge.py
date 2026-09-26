# -*- coding: utf-8 -*-
"""消息桥接调度层：轮询小天才新消息 -> 转发（当前支持 log 打印 /
AstrBot 插件端点两种模式），并负责去重、回声过滤与 ADB 断线重连。

反向（QQ->小天才）由 qq_webhook.py 调用 bridge.forward_to_xiaotiancai()，
webhook.enabled=true 且 NapCat/插件回调就绪后启用。
"""
from __future__ import annotations

import base64
import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from utils.deduplicate import Deduplicator, EchoFilter, HistoryFilter
from msg_log import MessageLog
from xiaotiancai import LOGIN_LOGGED_IN, LOGIN_NOT_LOGGED_IN, LOGIN_UNKNOWN
import runtime_paths


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
            self.logger.info(f"[转发-占位] {target_type}:{target_id} <- {message}")
        else:
            print(f"[转发-占位] {target_type}:{target_id} <- {message}")
        return True

    def send_detail(self, target_type, target_id, message: str) -> tuple:
        return self.send(target_type, target_id, message), ""

    def send_image(self, target_type, target_id, image_b64, caption="") -> tuple:
        """log 模式：只打印不发送（表情包也一样，便于离线调试）。返回 (ok, why)。"""
        if self.logger:
            self.logger.info(f"[转发-占位] 图片 {target_type}:{target_id} "
                             f"{len(image_b64)} 字节 base64 <- {caption}")
        return True, ""

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
        self._last_err_log = float("-inf")   # 初值用 -inf：monotonic 零点任意，0.0 会误判成"刚报过"
        self._err_log_interval = 60.0  # 同一故障最多 60s 报一次，避免刷屏

    def send(self, target_type, target_id, message: str) -> bool:
        ok, detail = self.send_detail(target_type, target_id, message)
        if not ok and self.logger:
            now = time.monotonic()
            if now - self._last_err_log >= self._err_log_interval:
                self.logger.error(f"转发到 AstrBot 插件失败：{detail or '未知原因'}")
                self._last_err_log = now
        return ok

    def send_detail(self, target_type, target_id, message: str) -> tuple:
        """转发并带出真实失败原因（QQ 侧发不出去 / 超时 / 连不上插件 是三种完全不同的病）。"""
        fn = getattr(self.client, "send_detail", None)
        if fn is None:                       # 兼容旧的 client
            return self.client.send(target_type, target_id, message), ""
        return fn(target_type, target_id, message)

    def send_image(self, target_type, target_id, image_b64, caption="") -> tuple:
        """转发一张图片（表情包，base64）。返回 (ok, why)。

        注意：这个方法**必须**在包装层也暴露 —— 实机踩过：`PluginClient` 有了 send_image，
        但桥接用的是 `PluginForwarder` 包装类，包装层没有就 `getattr(..., 'send_image', None)`
        得到 None，于是每张表情都退化成文字（日志里那句"转发器不支持图片"）。
        """
        fn = getattr(self.client, "send_image", None)
        if fn is None:
            return False, "插件客户端不支持图片（plugin_client.py 过旧）"
        ok, why = fn(target_type, target_id, image_b64, caption)
        if not ok and self.logger:
            now = time.monotonic()
            if now - self._last_err_log >= self._err_log_interval:
                self.logger.error(f"表情图转发失败：{why or '未知原因'}")
                self._last_err_log = now
        return ok, why

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
        # 去重/回声/消息库的状态文件统一放"可写数据目录"（Nuitka onefile 下是 exe 旁边，
        # 放在 __file__ 旁边会写进退出即删的临时目录 -> 重启丢状态）
        runtime_paths.ensure_dirs()
        # 回声状态写入文件：多实例/重启后共享，防止重复转发
        store_path = str(runtime_paths.data_path("echo_cache.json"))
        self.echo = EchoFilter(store_path=store_path)
        # 长期已处理消息表（7 天持久化）：跨重启/多实例去重，杜绝死循环重复转发
        self.history = HistoryFilter(store_path=str(runtime_paths.data_path("history_cache.json")))
        # 本地消息库（/小天才 历史消息 数据源 + "库里有没有这条消息"的判定库）：
        # 条数上限 / 分库 / 只读最新几个分库，都可由 config.yaml -> msg_log 配置
        _ml = cfg.get("msg_log") or {}
        self.msgs = MessageLog(
            path=str(runtime_paths.data_path("msg_log.json")),
            cap=int(_ml.get("cap", 5000) or 0),                # 0 = 不限制
            shard_size=int(_ml.get("shard_size", 2000) or 0),  # 0 = 不分库（单文件）
            read_shards=int(_ml.get("read_shards", 1) or 1))   # 默认只加载最新分库
        self._poll_interval = float((cfg.get("xiaotiancai") or {}).get("check_interval", 2))
        self._heartbeat_interval = float((cfg.get("adb") or {}).get("heartbeat_interval", 10))
        self._login_check_interval = float(
            (cfg.get("xiaotiancai") or {}).get("login_check_interval", 600))
        # 自动登录重试节奏：超时/临时问题快速重试，明确的账号密码错误与安全验证则拉长间隔，
        # 避免"一次失败就永久不再尝试"（用户报告的"自动登录不生效"）。
        self._login_retry_interval = float(
            (cfg.get("xiaotiancai") or {}).get("login_retry_interval", 120))
        self._login_retry_after_fail = float(
            (cfg.get("xiaotiancai") or {}).get("login_retry_after_fail", 1800))
        self._login_retry_after_risk = float(
            (cfg.get("xiaotiancai") or {}).get("login_retry_after_risk", 900))
        # 操作锁：发送/导航期间暂停轮询，避免两个线程同时 uiautomator dump 冲突
        self._op_lock = threading.Lock()
        # 表情包（**仅 小天才 -> QQ 单向**）：优先读 App 数据目录/图片缓存里的**原文件**
        # （动图 GIF 能保住动画），拿不到再按气泡截图，最后退回发"表情X"文字。
        _emoji = cfg.get("emoji") or {}
        self._emoji_image = bool(_emoji.get("forward_image", True))
        self._emoji_caption = bool(_emoji.get("caption", True))
        self._emoji_from_data = bool(_emoji.get("from_app_data", True))
        self._emoji_store = None
        if self._emoji_image and self._emoji_from_data and adb is not None:
            try:
                from emoji_store import EmojiStore
                self._emoji_store = EmojiStore(
                    adb, package=(cfg.get("xiaotiancai") or {}).get("package", "com.xtc.watch"),
                    logger=logger, recent_secs=float(_emoji.get("cache_recent_secs", 45) or 45))
            except Exception as e:  # noqa: BLE001 取原文件的能力不可用就只用截图
                self._log("debug", f"表情原文件读取不可用（改为截图）: {e}")
        # 有发送任务在排队/执行时置位：轮询据此让路（见 _poll_loop），别和发送抢 dump
        self._send_pending = False
        self._send_pending_ts = float("-inf")
        # 最近一次"轮询确认过就在聊天页"的时刻与标题（"先手打字"快发的放行条件）
        self._chat_ok_ts = float("-inf")
        self._chat_ok_title = ""
        # "别睡"心跳：每 keep_awake_interval 秒发一次 WAKEUP（0 = 关闭）。
        # 实测：WSA 大约每 50 秒把虚拟屏睡一次（stayOn/screen_off_timeout 都挡不住），
        # 而每 10 秒发一次 WAKEUP 能让它连续 2 分钟保持 Awake、App 一直留在前台
        # （单次开销仅 0.08 秒）。这是"先手打字"能稳定命中、发送不再莫名 10~30 秒的关键。
        try:
            self._keep_awake_interval = float((cfg.get("adb") or {}).get("keep_awake_interval", 10) or 0)
        except (TypeError, ValueError):
            self._keep_awake_interval = 10.0
        self._last_awake_poke = float("-inf")
        self._login_thread: threading.Thread | None = None
        # 登录待恢复标记：触发安全验证/登录失败后置位；
        # 轮询检测到重新登录时自动确认并 QQ 通知（无需重启）
        self._pending_login_notify = False
        # 自动登录节流状态
        self._login_not_before = 0.0      # 早于该时刻不再尝试自动登录
        self._login_inflight = False      # 已有一次登录任务在队列/执行中
        self._warned_no_cred = False      # 未配置账密的提示只打一次
        self._notify_seen: dict[str, float] = {}   # 通知去重：文本 -> 上次发送时刻
        self._log_seen: dict[str, float] = {}      # 日志去重：key -> 上次打印时刻
        self._poll_fail_streak = 0        # 连续"读不到消息"的轮数（触发界面自愈）
        self._last_state = ""             # 上一次判定的 App 状态（用于"状态变化才记日志"）
        # 漏消息补发（从最新往回走、撞库即停）：弹窗挡住/界面读不到期间到达的、
        # 以及启动前积压在聊天里的消息，都会按时间顺序补齐
        _xc = cfg.get("xiaotiancai") or {}
        self._catchup_enabled = bool(_xc.get("catchup_missed", True))
        self._catchup_max = int(_xc.get("catchup_max", 0) or 0)   # 0 = 不限制（默认）
        self._wsa_guard_hinted = False    # WSA 断网提示只打一次
        # FIFO 任务队列：QQ->小天才 发送 / 登录 由单工作线程串行执行，
        # 保证多消息到达时按顺序处理，避免并发抢锁导致前后关系紊乱
        self._job_queue: queue.Queue = queue.Queue()
        self._job_thread: threading.Thread | None = None
        self._last_chat_open = float("-inf")  # 聊天窗口重开冷却（初值 -inf：见上）
        # 自动登录检测开关（/小天才 自动登录 可切换；默认开启）
        self._auto_login_enabled = bool(
            (cfg.get("xiaotiancai") or {}).get("auto_login", True))
        # xtc 侧命令（在小天才聊天里直接输入，由本桥程序解析执行，与 QQ 命令隔离）：
        # 命令前缀。去重按"身份 (侧, 文本, 时间标签)"：已执行过的命令（文件持久化）
        # 不再重复执行——即使消息仍是"最新一条"或桥接重启，也不会反复发帮助；
        # 用户再次输入相同命令（新消息/新时间标签）仍可执行。
        self._xtc_cmd_prefix = str(
            (cfg.get("xiaotiancai") or {}).get("cmd_prefix", "/小天才")).strip()
        self._cmd_pending: dict[str, tuple[str, str]] = {}  # text -> (side, label)
        self._cmd_done: set[tuple[str, str, str]] = set()
        # 会话级"这条命令已经处理过"记忆：(side, text) -> 已处理时的 App 时间标签。
        # 解决"同一条 /小天才 反复触发"：命令消息会一直挂在"最新一条"上，若 App 的
        # 时间标签也一直不变（同一分钟/解析不出时间），仅靠 (side, text, label) 判重
        # 不足；这里标签一变（说明是用户新输入的一条）才允许再次执行。
        self._cmd_seen_text: dict[tuple[str, str], str] = {}
        self._cmd_done_file = str(runtime_paths.data_path("xtc_cmd_done.json"))
        self._cmd_lock = threading.Lock()
        self._load_cmd_done()

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self.running = True
        self._thread = threading.Thread(target=self._poll_loop, name="xtc-poll", daemon=True)
        self._thread.start()
        # 表情包名字索引后台预热：第一次收到表情时才建要 ~4 秒（会卡一下读屏），
        # 提前在后台建好，索引本身缓存 10 分钟。
        if self._emoji_store is not None:
            threading.Thread(target=self._warm_emoji_store, name="xtc-emoji-warm",
                             daemon=True).start()
        self._login_thread = threading.Thread(target=self._login_check_loop, name="xtc-login-check", daemon=True)
        self._login_thread.start()
        self._job_thread = threading.Thread(target=self._job_worker, name="xtc-jobs", daemon=True)
        self._job_thread.start()
        self._log("info", f"小天才消息轮询已启动（间隔 {self._poll_interval}s）")
        # 启动即自动初始化（等价于 QQ 命令 /小天才 初始化，以前只有手动发命令才会做）：
        # 清弹窗 -> 确认前台 -> 按需登录 -> 进入聊天页 -> 清空输入框残留。
        # 不做这一步时，启动后的第一轮轮询可能还在列表页/残留状态上，
        # 读到的第一条"新消息"其实是启动前的旧消息。
        try:
            self._job_queue.put(("init", ""))
            self._log("info", "已排队启动自动初始化（清弹窗 / 进聊天页 / 清输入框残留）")
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"启动自动初始化入队失败: {e}")

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
                elif kind == "forward":
                    # 小天才 -> QQ 的转发放在工作线程做：插件要等 QQ 侧真实结果才回包
                    # （最长 30 秒），放在轮询线程里会把"读屏"一起拖住。
                    _, f_contact, f_text, f_label = job[:4]
                    f_sticker = job[4] if len(job) > 4 else None
                    self._do_forward_job(f_contact, f_text, f_label, sticker=f_sticker)
                elif kind == "login":
                    _, request_id = job
                    self._do_login_job(request_id)
                elif kind == "init":
                    _, request_id = job
                    self._do_init_job(request_id)
                elif kind == "history":
                    # 小天才历史消息：count + 回传方式（request_id 或写入小天才聊天）+ 来源过滤
                    _, count, request_id, into_chat = job[:4]
                    src = job[4] if len(job) > 4 else ""
                    self._do_history_job(count, request_id, into_chat, src)
                elif kind == "cmd":
                    # xtc 侧命令（在小天才聊天里输入的 /小天才 xxx，手表侧或家长侧均可）
                    _, text = job
                    ident = self._cmd_pending.pop(text, None)
                    ok = self._do_cmd_job(text)
                    if ok and ident is not None:
                        # 执行成功 -> 标记为已完成（持久化），同一条消息不再重复执行
                        self._cmd_done_add(ident[0], text, ident[1])
                    # 回复失败：不标记，下一轮轮询自动重试
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"任务处理异常: {e}")
            finally:
                self._job_queue.task_done()

    # ------------------------------------------------------------------ 轮询
    def _poll_loop(self) -> None:
        last_heartbeat = float("-inf")   # 首轮就做一次 ADB 心跳检查（monotonic 零点任意）
        while self.running:
            loop_started = time.monotonic()
            # 有消息要发时先让路：发送要拿操作锁，轮询若正好开始一轮 dump（3~4 秒），
            # 发送就得等它做完 —— 用户看到的"点了发送半天才出现文字"有一部分就是这个。
            # 最多让路 30 秒（长队列不会把读消息饿死；漏掉的消息由撞库补发兜底）。
            if self._send_pending and (loop_started - self._send_pending_ts) < 30:
                time.sleep(0.05)
                continue
            # 轻量"别睡"心跳：WSA 会随宿主窗口状态息屏（screen_off_timeout 拉满也挡不住），
            # 隔一会儿发一次 WAKEUP，把屏叫醒并重置用户活动计时 —— 屏不睡，App 就一直在前台，
            # "App 不在前台 -> 重新拉起"（实测每次 ~20 秒）和"先手打字"被门闩挡住都会少很多。
            if self._keep_awake_interval > 0 and \
                    loop_started - self._last_awake_poke >= self._keep_awake_interval:
                self._last_awake_poke = loop_started
                try:
                    self.adb.poke_awake()
                except Exception as e:  # noqa: BLE001 心跳失败不影响轮询
                    self._log("debug", f"保活心跳失败: {e}")
            try:
                now = time.monotonic()
                if now - last_heartbeat >= self._heartbeat_interval:
                    last_heartbeat = now
                    if not self.adb.is_connected():
                        self._log("warning", "ADB 断连，尝试重连...")
                        try:
                            self.adb.ensure_connected()
                            self._log("info", "ADB 已重连")
                        except Exception as e:  # noqa: BLE001
                            self._log("error", f"重连失败: {e}")
                            self._hint_wsa_guard()
                            time.sleep(2)
                            continue

                if not self._op_lock.acquire(blocking=False):
                    continue  # 正在发送/导航，跳过本轮，避免 dump 冲突
                try:
                    # 恢复检测：安全验证完成后自动确认并通知（无需重启）
                    if self._pending_login_notify:
                        if self.xtc.login_state() == LOGIN_LOGGED_IN:
                            self._pending_login_notify = False
                            self._notify("小天才已重新登录（安全验证完成），桥接继续运行")
                    # 先判状态再决定做什么（以前只问"在不在聊天页"，于是在登录页/
                    # 别的页面上反复找联系人，刷一堆"找不到联系人"且不对症）。
                    state, root = self.xtc.app_state_with_root(2)
                    self._log_state(state)
                    xtc_contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
                    if state == self.xtc.STATE_CHAT:
                        # 记下"最近一次确认过就在聊天页"（含标题），"先手打字"快发据此放行
                        self._chat_ok_ts = time.monotonic()
                        self._chat_ok_title = self.xtc.chat_title(root)
                        contact, text, time_label, own_text, own_recent = \
                            self.xtc.get_latest_message(root)
                    else:
                        contact, text, time_label, own_text, own_recent = (None, None, "", "", [])
                        if state == self.xtc.STATE_LOGIN:
                            # 登录/安全验证页：等登录线程处理，绝不在这里找联系人
                            self._log_once("state_login",
                                           "当前在小天才登录/安全验证页，等待登录完成"
                                           "（此时不会去找联系人）", interval=600)
                        elif state == self.xtc.STATE_BACKGROUND:
                            # WSA 窗口失去焦点/息屏时 App 会"看起来不在前台"，但窗口往往还在。
                            # 先花 ~0.3 秒问一次电源状态：睡着就叫醒 —— 唤醒后 App 通常立刻
                            # 回到前台（它本来就是 resumed 的 Activity），下一轮就能正常读消息，
                            # 省掉"重新拉起 App"那 20 秒。
                            if self.adb is not None and self.adb.wake_if_asleep():
                                self._log_once("wake_asleep",
                                               "子系统息屏导致 App 不在前台，已唤醒屏幕"
                                               "（唤醒后通常直接回到聊天页）", interval=120)
                        elif state == self.xtc.STATE_POPUP:
                            # 弹窗遮挡（如"升级提醒"）：弹窗不关就完全读不到消息，
                            # 所以这里**不设冷却**，每次轮询都尝试关掉；
                            # 关不掉时由 xiaotiancai 侧 10 分钟提醒一次，避免刷屏。
                            if self.xtc.settle():
                                self._log("info", "检测到弹窗遮挡界面，已自动关闭")
                            else:
                                self._log_once(
                                    "state_popup_stuck",
                                    "弹窗遮挡界面且无法自动关闭（可在 config.yaml -> "
                                    "xiaotiancai.ui.popup_skip_texts 里补上它的按钮文案）",
                                    interval=600)
                        elif state in (self.xtc.STATE_LIST, self.xtc.STATE_OTHER):
                            cooldown = 30.0 if self._poll_fail_streak < 5 else 5.0
                            if xtc_contact and time.monotonic() - self._last_chat_open >= cooldown:
                                self._last_chat_open = time.monotonic()
                                if not self.xtc.open_chat(xtc_contact):
                                    reason = self.xtc.last_open_reason or "未知原因"
                                    # 同一原因 5 分钟只报一次，避免轮询刷屏
                                    self._log_once(f"open_chat:{reason[:24]}",
                                                   f"进入聊天失败：{reason}", interval=300)
                finally:
                    self._op_lock.release()
                # 补发：从最新一条往回走、撞库即停（弹窗挡住期间漏掉的、启动前积压的都补齐）。
                # 放在命令流程之前：它会跳过命令文本与系统提示，只处理真实消息。
                # 注意：聊天窗口模式下 get_latest_message 的 contact 本来就是 None，
                # 所以这里**不能**用 `contact is not None` 当条件 —— 那样补发永远不会跑
                # （用户报的"漏掉的消息再也补不上"就是这么来的）。
                if state == self.xtc.STATE_CHAT:
                    try:
                        self._forward_backlog(root, contact or "")
                    except Exception as e:  # noqa: BLE001
                        self._log("warning", f"补发流程异常: {e}")
                # 连续读不到任何东西（且不在聊天页/被弹窗挡住）-> 自愈
                if not text and not own_recent:
                    self._poll_fail_streak += 1
                    if self._poll_fail_streak in (6, 20) or self._poll_fail_streak % 60 == 0:
                        with self._op_lock:
                            state = self.xtc.recover(
                                (self.cfg.get("target") or {}).get("xtc_contact", ""))
                        self._log("info", f"轮询连续 {self._poll_fail_streak} 轮没有读到消息，"
                                          f"界面自愈: {state}")
                else:
                    self._poll_fail_streak = 0
                is_cmd_text = bool(self._xtc_cmd_prefix and text
                                   and text.startswith(self._xtc_cmd_prefix))
                # xtc 侧命令（/小天才 …）：手表侧或家长侧输入均可。
                # 1) 手表侧（对方发来）的命令 -> 执行，不转发、不入消息库；
                #    已被处理过（同一侧+同一文本+同一时间标签）就不再进入执行流程，
                #    这样桥接自己发出去的"结果/帮助"被读回时也不会再被当成新命令。
                if is_cmd_text:
                    if not self._cmd_text_handled("watch", text, time_label):
                        self._log("info", f"[收到小天才命令] 来源=手表 内容={text!r} 时间标签={time_label or '(无)'}")
                        self._maybe_xtc_cmd("watch", text, time_label)
                    else:
                        self._log("debug", f"[收到小天才命令] 重复（已执行过）: {text!r}")
                # 2) 家长侧输入的命令：可能被送达确认等新消息盖过（不再是"最新一条"），
                #    扫最近若干条自己发的消息；跳过桥接自己转发过去的旧命令文本
                elif own_recent:
                    for t, lbl in own_recent:
                        if not (self._xtc_cmd_prefix and t.startswith(self._xtc_cmd_prefix)):
                            continue
                        if self.history.seen("qq2xtc", t):
                            continue  # 曾经由桥接转发进聊天的文本，不视为新输入的命令
                        if self._cmd_text_handled("own", t, lbl):
                            continue  # 已经执行过这条（含桥接自己发出去的回复）
                        self._log("info", f"[收到小天才命令] 来源=家长侧 内容={t!r} 时间标签={lbl or '(无)'}")
                        self._maybe_xtc_cmd("own", t, lbl)
                        break
                # 普通手表消息 -> 转发（命令已被上面拦截，绝不转发/入库）
                if text and not is_cmd_text:
                    # 事件身份 = 文本 + **绝对时间**（把 App 的"今天/昨天/星期X"标签统一成
                    # MM-DD HH:MM）。用 App 原始标签当身份会出事：同一条消息隔天标签从
                    # "16:18" 变成 "昨天 16:18"，于是一条旧消息会被当成新消息重复转发。
                    raw_label = (time_label or "").strip()
                    label = self._abs_time_label(raw_label) or raw_label
                    key = ("xtc", contact or "", text, label)
                    dup = (self.history.seen(*key) or self.dedup.seen(key)
                           or self.echo.is_echo(text))
                    if not dup and not label:
                        # 读不到时间标签时只能按文本保守判定：宁可不重发，也不要反复刷同一条
                        dup = self.msgs.seen(text, "xtc")
                    if not dup:
                        self._log("info", f"[收到小天才消息] 来源={self._xtc_source(contact)} "
                                          f"时间={time_label or '(无)'} 内容={text!r}")
                        # 表情包：**必须在轮询线程里取**（此刻快照/气泡位置才准、
                        # 缓存里刚写进来的原文件也还在）。
                        # 传消息自己的时间：检测可能滞后几分钟（刚重启/刚唤醒时），
                        # 缓存文件是"消息显示时"写进去的，只有按消息时间才找得回原图。
                        sticker = self._capture_sticker(
                            root, text, near_epoch=self._label_epoch(time_label or ""))
                        # 异步转发；成功后才写入长期历史与消息库（见 _do_forward_job）
                        self._queue_forward(contact, text, time_label, sticker=sticker)
            except Exception as e:  # noqa: BLE001 单轮异常不致命
                self._log("warning", f"轮询异常: {e}")
            # 只补"剩下的"时间：一轮里 dump+转发可能已花 3~4 秒，再无条件 sleep
            # 一个完整间隔就变成 6 秒一轮（检测延迟白白翻倍）。
            time.sleep(max(0.0, self._poll_interval - (time.monotonic() - loop_started)))

    def _in_store(self, contact: str, text: str, label: str = "") -> bool:
        """这条消息"库里有没有"（**补发**时的撞库判定，只认持久记录）。

        依次看：长期已处理表（同一文本 + 同一时间标签）-> 本地消息库（同文本）-> 回声过滤。

        补发这里刻意**保守**：同文本就算"有"（消息库/长期表都存了文本），
        宁可少补一条同名消息，也不要往 QQ 刷屏。
        注意这里**不看**短期去重表（120 秒那个）：它只是防重发的节流，
        转发失败的消息不该因为它而被当成"已处理"（否则永远不会重试）。
        """
        if self.history.seen("xtc", contact or "", text, label or ""):
            return True
        if self.msgs.seen(text, "xtc"):
            return True
        return bool(self.echo.is_echo(text))

    def _forward_backlog(self, root, contact: str) -> int:
        """从最新一条往回走，把库里没有的消息补齐；**撞到库里已有的就停**。

        用户要的逻辑：
          发完最新一条后看**上一条**：和库里一样 -> 停（不转发）；
          不一样 -> 转发，再往上看一条，直到撞上库里已有的一条为止。

        好处：弹窗挡住期间漏掉的、以及启动前积压在聊天里的消息，都会按时间顺序补齐；
        而已处理过的消息一旦撞上就停，不会无限往上翻（也不需要记"启动时刻"来卡范围）。

        返回补发条数。命令文本与系统提示只跳过、不作为停止边界。
        """
        if not self._catchup_enabled:
            return 0
        try:
            bubbles = self.xtc._chat_bubbles(root, include_own=False)
        except Exception as e:  # noqa: BLE001 补发失败不影响正常轮询
            self._log("debug", f"补发扫描失败: {e}")
            return 0

        pending: list[tuple[str, str, dict | None]] = []
        for it in reversed(bubbles):                # 从最新往回走
            text = (it.get("text") or "").strip()
            if not text:
                continue
            try:
                if self.xtc._is_system_msg(text):
                    continue                        # 送达确认这类系统提示：不转发也不当边界
            except Exception:  # noqa: BLE001
                pass
            if self._xtc_cmd_prefix and text.startswith(self._xtc_cmd_prefix):
                continue                            # 命令文本由命令流程处理
            # 身份与显示都用**绝对时间**（理由同 live 路径：App 的标签会随日期变化）
            raw_lbl = (it.get("time_label") or "").strip()
            lbl = self._abs_time_label(raw_lbl) or raw_lbl
            if self._in_store(contact, text, lbl):
                break                               # 撞库 -> 停，不再往上翻
            if self.dedup.seen(("xtc", contact or "", text, lbl)):
                # 刚试过（120 秒内，多为上次转发失败）：本轮先跳过它，
                # 但不当作边界，继续往上找更老的那几条
                self._log("debug", f"[补发] 这条最近试过，先跳过: {text[:24]!r}")
                continue
            # 补发的表情包也取原图（此刻 root 里就有它的气泡位置，晚了就滚走了）；
            # 传消息自己的时间，让缓存查找能找到"当时写进来的那张"（保住动图）
            sticker = (self._capture_sticker(root, text, near_epoch=self._label_epoch(raw_lbl))
                       if it.get("sticker") else None)
            pending.append((text, lbl, sticker))

        if not pending:
            return 0
        pending.reverse()                           # 变成 旧 -> 新 的顺序转发
        if self._catchup_max and len(pending) > self._catchup_max:
            self._log("warning", f"[补发] 待补 {len(pending)} 条，超过上限 {self._catchup_max}，"
                                 f"先补最早的 {self._catchup_max} 条（下一轮继续）")
            pending = pending[:self._catchup_max]
        self._log("info", f"[补发] 有 {len(pending)} 条消息库里没有，按时间顺序补发")

        sent = 0
        for text, label, sticker in pending:
            self._log("info", f"[收到小天才消息] 来源={self._xtc_source(contact)} "
                              f"时间={label or '(无)'} 内容={text!r}（补发）")
            # 异步转发：不阻塞读屏（见 _queue_forward）
            self._queue_forward(contact, text, label, sticker=sticker)
            sent += 1
        return sent

    def _queue_forward(self, contact: str, text: str, label: str,
                       sticker: dict | None = None) -> None:
        """把"小天才 -> QQ"的转发丢给工作线程，立刻返回。

        为什么异步：插件要等 QQ 侧真实发送结果才回包（最长 30 秒），同步做的话
        轮询线程会被卡住，读屏/检测跟着变慢（转发越快，漏消息窗口也越小）。
        这里立刻 short-term 去重，避免下一轮把同一条再入队。

        sticker：表情图（{data, kind, animated, source}）。**必须在轮询线程里取**——
        那一刻界面快照/气泡位置才准、缓存里刚写进来的文件也还在；没有它就按文字发。
        """
        self.dedup.mark(("xtc", contact or "", text, label or ""))
        self._job_queue.put(("forward", contact, text, label or "", sticker))

    def _do_forward_job(self, contact: str, text: str, label: str,
                        sticker: dict | None = None) -> None:
        """工作线程里真正执行转发；成功才写入长期历史与消息库（失败下轮会重试）。"""
        try:
            ok = self._forward(contact, text, label, sticker=sticker)
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"转发异常: {e}")
            ok = False
        if not ok:
            self._log("warning", f"[转发] 这条没发出去，稍后重试: {text[:24]!r}")
            return
        self.history.mark("xtc", contact or "", text, label or "")
        if not self.msgs.seen(text, "xtc"):
            self.msgs.append("xtc", contact or "", text, t=self._label_epoch(label),
                             source=self._xtc_source(contact), source_id=contact or "")

    def _warm_emoji_store(self) -> None:
        """后台预热表情包名字索引（失败无所谓，收到表情时会按需再建）。"""
        try:
            n = self._emoji_store.warm()
            self._log("debug", f"表情包索引预热完成：{n} 个名字")
        except Exception as e:  # noqa: BLE001
            self._log("debug", f"表情包索引预热失败: {e}")

    def _capture_sticker(self, root, text: str, near_epoch: float | None = None) -> dict | None:
        """表情包（仅小天才 -> QQ 单向）：拿到贴纸图片，返回 {data, kind, animated, source}。

        顺序：
          1) **App 数据目录/图片缓存里的原文件**（`emoji_store`）：保真，动图保住动画，
             也不依赖气泡在屏幕上；挑哪张**由像素比对决定** —— 先抠下界面上这张气泡的
             截图当基准，再逐个候选比相似度，够像才发（根治"小猫流汗发成乌龟"）；
          2) 没有可信原图时，**就发这张气泡截图**（静止一帧，但一定是对的）；
          3) 连气泡都定位不到 -> 返回 None，调用方照样发"表情X"文字。

        near_epoch：补发老消息时传"这条消息自己的时间"，好让缓存查找按消息时间去匹配
        （缓存文件是消息显示时写进来的），否则补发的贴纸会退化成静态截图。
        """
        if not self._emoji_image:
            return None
        name = (text or "").strip()
        if name.startswith("表情"):
            name = name[len("表情"):].strip()
        # 有的贴纸名字自带扩展名（实机：'表情弹吉他.png'）——查表情包索引前先去掉
        name = re.sub(r"\.(png|gif|webp|jpe?g|apng)$", "", name, flags=re.I).strip()
        # 先在快照里定位气泡：① 拿它的形状去缓存里挑原图 ② 抠它的截图当核对基准
        item = None
        try:
            item = self.xtc.sticker_of_latest(root, text) or self.xtc.sticker_of_latest(root, "")
        except Exception as e:  # noqa: BLE001 定位失败不影响取原图
            self._log("debug", f"定位表情气泡失败: {e}")
        aspect = None
        if item and item.get("bounds"):
            x1, y1, x2, y2 = item["bounds"]
            if y2 > y1:
                aspect = (x2 - x1) / (y2 - y1)
        # 名字在本地表情包里唯一时无需核对（省一次截图）；否则必须抠基准图来比像素
        need_ref = True
        if self._emoji_store is not None and name:
            try:
                need_ref = not self._emoji_store.name_is_unique(name)
            except Exception as e:  # noqa: BLE001 索引查不动就老老实实截图
                self._log("debug", f"判断表情名是否唯一失败: {e}")
        ref = None
        if item and item.get("bounds") and need_ref:
            try:
                ref = self.xtc.capture_sticker(item.get("bounds"))
            except Exception as e:  # noqa: BLE001 抠基准图失败就走老路
                self._log("debug", f"抠表情气泡截图失败: {e}")
                ref = None
        if self._emoji_from_data and self._emoji_store is not None:
            try:
                got = self._emoji_store.find(name, near_epoch=near_epoch,
                                             aspect=aspect, reference=ref)
            except Exception as e:  # noqa: BLE001 读原文件失败就走截图
                self._log("debug", f"读表情原文件失败（改用截图）: {e}")
                got = None
            if got and got.get("data"):
                anim = "动图" if got.get("animated") else "静态"
                where = "图片缓存" if got.get("source") == "cache" else "表情包文件"
                score = got.get("score")
                tag = f"，与界面比对 {score:.2f}" if isinstance(score, (int, float)) else ""
                self._log("info", f"[表情包] 取自{where}：{got.get('kind', '?')} {anim} "
                                  f"{len(got['data'])} 字节（{got.get('w')}x{got.get('h')}）{tag}")
                return got
        if ref:
            self._log("info", f"[表情包] 没有可信原图（{name!r}），发界面气泡截图"
                              f"{len(ref)} 字节（静止一帧）")
            return {"data": ref, "kind": "png", "animated": False, "source": "screenshot"}
        if not item:
            self._log("info", f"[表情包] 界面上没找到这条表情的气泡（{text!r}），按文字转发")
            return None
        try:
            png = self.xtc.capture_sticker(item.get("bounds"))
            if png:
                self._log("info", f"[表情包] 按气泡截图 {len(png)} 字节（静态一帧）"
                                  f"（{item.get('bounds')}），随转发发给 QQ")
                return {"data": png, "kind": "png", "animated": False, "source": "screenshot"}
            self._log("info", f"[表情包] 截图没成功（气泡 {item.get('bounds')}），按文字转发")
            return None
        except Exception as e:  # noqa: BLE001 截图失败不影响文字转发
            self._log("warning", f"[表情包] 取图异常（按文字转发）: {e}")
            return None

    def _hint_wsa_guard(self) -> None:
        """WSA/WSABuilds 反复断网时提示配套的独立守护工具（只提示一次）。

        守护只适用于 Windows：WSA 是 Windows 独有组件，别的平台上没有它可用。
        """
        if self._wsa_guard_hinted:
            return
        self._wsa_guard_hinted = True
        if os.name != "nt":
            return
        self._log("warning",
                  "若使用 WSA / WSABuilds 且经常断网，可另开一个终端运行独立守护工具："
                  "python tools/wsa_net_guard.py（自动重连/重置网络/必要时重启 WSA，"
                  "详见 README「WSA 网络守护」）")

    def _forward(self, contact, text: str, time_label: str = "",
                 sticker: dict | None = None) -> bool:
        """转发到所有 QQ 目标。返回是否全部成功（供轮询决定是否记入长期历史）。

        sticker：表情图（{data, kind, animated}）。给了就发"图片（+说明文字）"，
        失败自动退回纯文字。**单向**：只有 小天才 -> QQ 走图片，QQ -> 小天才 依旧是文字。
        """
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
        image_b64 = ""
        image_size = 0
        if sticker and sticker.get("data"):
            try:
                image_b64 = base64.b64encode(sticker["data"]).decode("ascii")
                image_size = len(sticker["data"])
            except Exception as e:  # noqa: BLE001 编码失败就退回文字
                self._log("warning", f"表情图编码失败（按文字转发）: {e}")
                image_b64 = ""
        ok_all = True
        queued = False
        for target_type, target_id in targets:
            why = ""
            try:
                if image_b64:
                    send_image = getattr(self.forwarder, "send_image", None)
                    if send_image is not None:
                        caption = message if self._emoji_caption else ""
                        ok, why = send_image(target_type, target_id, image_b64, caption)
                    else:
                        self._log("warning", "转发器不支持图片，改发文字"
                                             "（插件需一并更新）")
                        ok, why = self._send_text(target_type, target_id, message)
                else:
                    ok, why = self._send_text(target_type, target_id, message)
            except Exception as e:  # noqa: BLE001
                self._log("error", f"转发异常({target_type}:{target_id}): {e}")
                ok, why = False, f"{type(e).__name__}: {e}"
            if ok and why == "queued":
                # 插件只把消息排进队列（事件循环还没起来）：没真的发出去，
                # 不能报"转发成功"，更不能发"发送成功"的送达确认
                queued = True
                self._log("warning", f"[转发未确认] {target_type}:{target_id} "
                                     "插件刚启动，消息只是排队（未确认已发出）")
                continue
            shown = message + (f"（+表情图 {image_size} 字节）" if image_b64 else "")
            if ok:
                self._log("info", f"[转发成功] {target_type}:{target_id} <- {shown}")
            else:
                self._log("error", f"[转发失败] {target_type}:{target_id} <- {shown}"
                                   + (f"  原因: {why}" if why else ""))
                ok_all = False
        if ok_all and not queued:
            # 标记原文 + 格式化消息：多实例/重启后也不会再转发同一条
            self.echo.mark(text)
            self.echo.mark(message)
            self._confirm_xtc_delivery(message)  # 小天才侧送达确认（发送成功：<内容>）
        elif ok_all and queued:
            # 已交出去但没确认：同样记历史避免重复，但不发"发送成功"（不撒谎）
            self.echo.mark(text)
            self.echo.mark(message)
            self._log("info", "小天才侧送达确认已跳过：本次转发未确认（插件排队中）")
        return ok_all

    def _send_text(self, target_type, target_id, message: str) -> tuple:
        """发纯文字（兼容只有 send() 的旧转发器）。返回 (ok, why)。"""
        send_fn = getattr(self.forwarder, "send_detail", None)
        if send_fn is not None:
            return send_fn(target_type, target_id, message)
        return bool(self.forwarder.send(target_type, target_id, message)), ""

    def _format_xtc_time(self, time_label: str) -> str:
        """把 App 内的时间标签转成**绝对** `MM-DD HH:MM`。

        为什么必须转绝对：App 对**同一条消息**的标签会随日期变化 ——
        当天显示 `16:18`，第二天变成 `昨天 16:18`，再往后可能变成 `09-25 16:18`。
        原样输出就会出现"转发里写着昨天"这种相对时间；拿它当消息身份更会导致
        同一条消息隔天被当成新消息重复转发。
        """
        return self._abs_time_label(time_label) or datetime.now().strftime("%m-%d %H:%M")

    def _abs_time_label(self, time_label: str, now: datetime | None = None) -> str:
        """把任意 App 时间标签统一成绝对 `MM-DD HH:MM`；解析不出来返回 ""。

        支持：`16:18`（今天）/ `今天 16:18` / `昨天 16:18` / `前天 16:18` /
        `星期一 16:18` / `8月30日 16:18` / 已经是绝对的 `09-25 16:18`、`2026-09-25 16:18`。
        """
        epoch = self._label_epoch(time_label, now)
        if epoch is None:
            return ""
        try:
            return datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M")
        except (OverflowError, OSError, ValueError):  # noqa: BLE001 时间戳异常就当解析不出来
            return ""

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
        """小天才联系人 -> 本地配置的显示昵称。映射优先级：
        target.nicknames[联系人] -> target.default_nickname -> App 原始名。
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
        """QQ->小天才 接收白名单（config.yaml -> webhook）：
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
        """QQ -> 小天才：入队（FIFO 保证多消息按顺序处理），由单工作线程串行执行。"""
        if not text:
            return False
        where = f"群 {group_id}" if group_id else (f"私聊 {user_id}" if user_id else "未知会话")
        # 收到就打印：便于在控制台确认 QQ 命令/消息真的到达了桥接（用户报告"看不到"）
        self._log("info", f"[收到QQ命令] 来源={where} 内容={text!r}"
                          + (f" request_id={request_id}" if request_id else "")
                          + f"（队列中 {self._job_queue.qsize()} 条待处理）")
        # 让轮询先停一轮 dump，把 adb/操作锁让给发送（见 _poll_loop 开头的让路逻辑）
        if not self._send_pending:
            self._send_pending_ts = time.monotonic()
        self._send_pending = True
        self._job_queue.put(("send", text, user_id, group_id, request_id))
        return True

    def _blind_send_allowed(self, contact: str) -> bool:
        """能不能走"先手打字"：最近一次轮询确认过**就是目标聊天页**（含标题校验）。

        为什么要这个门闩：先手打字是按**缓存坐标**盲点输入框/发送按钮，
        只有"确实在目标聊天页"时才安全。条件：
          * 近 30 秒内轮询判过 STATE_CHAT；
          * 那次读到的聊天标题就是目标联系人（防止盲点把消息发到别的聊天里）；
          * 上次成功发送留下了坐标缓存（`blind_send_ready`）。
        """
        if time.monotonic() - self._chat_ok_ts > 30.0:
            return False
        title = (self._chat_ok_title or "").strip()
        want = (self._display_name(contact) or contact or "").strip()
        if not title or not want:
            return False
        if want != title and want not in title and title not in want:
            return False
        try:
            if not self.xtc.blind_send_ready():
                return False
        except Exception:  # noqa: BLE001 判断失败就走稳妥流程
            return False
        return True

    def _do_send_job(self, text: str, user_id: str, group_id: str, request_id: str) -> None:
        """实际执行 QQ->小天才 发送 + 送达确认（工作线程内，按入队顺序）。"""
        self.echo.mark(text)
        contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
        if not contact:
            self._log("error", "反向转发需要 config.yaml -> target.xtc_contact")
            return
        self._log("info", f"[QQ->小天才] 开始发送: {text[:80]!r}")
        ok = False
        skip_safe = False
        try:
            # ① "先手打字"：直接按缓存坐标点输入框 + 广播注入 + 点发送，
            #    **不等轮询那次 dump、也不抢操作锁** —— 文字 ~1 秒内就出现在输入框里。
            #    复核放在后面（要 dump），失败再退回稳妥流程。
            if self._blind_send_allowed(contact):
                t0 = time.monotonic()
                staged, why = self.xtc.begin_blind_send(text)
                if staged:
                    self._log("info", f"[QQ->小天才] 已先手输入（{time.monotonic() - t0:.1f}s），"
                                      "正在复核…")
                    with self._op_lock:
                        ok, why, retryable = self.xtc.end_blind_send(text)
                    if ok:
                        self._log("info", f"[QQ->小天才] 先手发送已复核通过"
                                          f"（总 {time.monotonic() - t0:.1f}s）")
                    else:
                        skip_safe = not retryable
                        self._log("warning" if skip_safe else "info",
                                  f"[QQ->小天才] 先手发送未确认（{why}）"
                                  + ("，且不宜重发，按失败上报" if skip_safe else "，改用稳妥流程"))
                else:
                    self._log("debug", f"[QQ->小天才] 先手输入未启用（{why}），走稳妥流程")
            # ② 稳妥流程（未走先手 / 先手没发出去且可安全重发时）
            if not ok and not skip_safe:
                with self._op_lock:
                    in_chat = self.xtc.open_chat(contact)
                    ok = in_chat and self.xtc.send_message(text)
                if not in_chat:
                    self._log("error", "[QQ->小天才] 未能进入小天才聊天窗口，未发送")
        except Exception as e:  # noqa: BLE001 单条发送异常不能让工作线程退出
            self._log("warning", f"[QQ->小天才] 发送异常: {e}")
            ok = False
        finally:
            # 队列里还有待发的就继续让路（_send_pending_ts 不刷新，最长 30 秒兜底）
            self._send_pending = not self._job_queue.empty()
        self._log("info" if ok else "error",
                  f"[QQ->小天才] {'发送成功' if ok else '发送失败'}: {text[:80]!r}")
        if ok:
            # 记录到长期历史：即使重启，这条消息也不会被当作"新消息"转发回 QQ
            self.history.mark("qq2xtc", text)
            self._archive_qq_send(text, user_id=user_id, group_id=group_id)
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
    # 历史消息来源标签：明确每条消息是"谁从哪儿发的"
    def _xtc_source(self, contact: str = "") -> str:
        """小天才（手表）侧来源：手表/家长侧在 App 内说的都归这里。

        注意：标签里不要出现 GBK 无法表示的符号（某些 emoji / 特殊符号）——中文 Windows 控制台
        会因此整条日志丢失（utils/logger.py 已做兜底，这里也不主动引入）。
        """
        who = self._display_name(contact) if contact else ""
        return "手表" + (f"-{who}" if who else "")

    @staticmethod
    def _qq_source(user_id: str = "", group_id: str = "") -> str:
        """QQ 侧来源：QQ群 <群号> / QQ私聊 <QQ号>。"""
        if group_id:
            return f"QQ群 {group_id}"
        if user_id:
            return f"QQ私聊 {user_id}"
        return "QQ"

    def _archive_qq_send(self, text: str, user_id: str = "", group_id: str = "") -> None:
        """QQ -> 小天才 发送成功后归档到本地消息库（带来源标签）。
        - 发送的整条消息是插件格式 `[MM-DD HH:MM] [QQ昵称] 内容` -> 拆出昵称与内容；
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
        self.msgs.append("qq", sender, content,
                         source=self._qq_source(user_id, group_id),
                         source_id=str(group_id or user_id or ""))

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
                      f"[送达确认] {message} -> {group_id or user_id}（{'成功' if ok else '失败'}）")
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
        if self._login_inflight:
            self._log("info", "[自动登录] 已有一次登录任务在执行/排队，跳过重复触发")
            if request_id:
                self._login_reply(request_id, "小天才登录已在进行中，请稍候")
            return "inflight"
        self._login_inflight = True
        self._job_queue.put(("login", request_id))
        return "queued"

    def _has_credentials(self) -> tuple[str, str]:
        acc = (self.cfg.get("xiaotiancai") or {}).get("login") or {}
        return str(acc.get("phone", "")).strip(), str(acc.get("password", "")).strip()

    def _do_login_job(self, request_id: str) -> None:
        phone, password = self._has_credentials()
        if not phone or not password:
            self._login_inflight = False
            msg = "未配置手机号/密码（config.yaml -> xiaotiancai.login.phone/password）"
            self._login_reply(request_id, "小天才登录：" + msg)
            return
        try:
            with self._op_lock:
                status = self.xtc.login(phone, password)
        except Exception as e:  # noqa: BLE001
            self._log("error", f"自动登录异常: {e}")
            self._login_reply(request_id, "小天才自动登录出错，请手动检查目标 Android 环境")
            self._login_not_before = time.monotonic() + self._login_retry_interval
            return
        finally:
            self._login_inflight = False
        now = time.monotonic()
        if status == "risk":
            self._pending_login_notify = True  # 等待用户手动完成安全验证
            self._login_not_before = now + self._login_retry_after_risk
            self._login_reply(request_id, "需要安全验证：请手动打开小天才 App 所在窗口完成验证")
            self._notify_once("risk", "小天才登录触发安全验证，请手动打开对应窗口完成验证")
        elif status == "fail":
            self._pending_login_notify = True
            self._login_not_before = now + self._login_retry_after_fail
            self._login_reply(request_id, "登录失败（账号或密码错误等），请检查配置或手动登录")
            self._notify_once("fail", "小天才自动登录失败（账号或密码错误等）")
        elif status == "timeout":
            # 超时/网络类临时问题：**不算失败**，按较短间隔自动重试
            self._login_not_before = now + self._login_retry_interval
            self._login_reply(request_id, "登录未在时限内完成（界面一直显示登录中或网络较慢），稍后自动重试")
            self._notify_once("timeout", "小天才自动登录暂未成功（登录中/网络较慢），稍后会自动重试")
        elif status == "error":
            self._login_not_before = now + self._login_retry_interval
            self._login_reply(request_id, "登录出错（控件未找到），请运行 tools/dump_ui.py 查看登录页")
        elif status == "ok":
            self._login_not_before = now + self._login_check_interval
            self._login_reply(request_id, "小天才登录成功")
        elif status == "already":
            self._login_not_before = now + self._login_check_interval
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
        """检测并恢复界面状态（**按需执行**，不做无意义的重启/点击）：
        清理弹窗 -> 只在前台不对时启动 -> 只在未登录时登录 -> 只在不在聊天页时进入
        -> 只在输入框有残留时清空。"""
        msgs: list[str] = []
        try:
            acc = (self.cfg.get("xiaotiancai") or {}).get("login") or {}
            phone = str(acc.get("phone", "")).strip()
            password = str(acc.get("password", "")).strip()
            contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
            with self._op_lock:
                # 1) 弹窗清理（一次多轮）
                if self.xtc.settle():
                    msgs.append("已清理弹窗")
                # 2) App 前台：已经在前台就完全不启动（避免"已经启动还反复启动"）
                if self.xtc.adb.is_in_foreground(self.xtc.package):
                    msgs.append("App 已在前台")
                else:
                    launched = False
                    for _ in range(2):
                        if self.xtc.launch():
                            launched = True
                            break
                        time.sleep(1.5)
                    msgs.append("启动" + ("OK" if launched else "失败"))
                self.xtc.settle()
                # 3) 登录态：明确已登录就跳过；无法判断时**不猜**，只报告
                state = self.xtc.login_state(force=True)
                if state == LOGIN_LOGGED_IN:
                    msgs.append("已登录")
                elif state == LOGIN_NOT_LOGGED_IN and phone and password:
                    status = self.xtc.login(phone, password)
                    msgs.append("登录：" + self._login_status_text(status))
                elif state == LOGIN_NOT_LOGGED_IN:
                    msgs.append("未登录（未配置账密，请手动登录）")
                else:
                    msgs.append("无法确认登录态（App 不在前台或界面读不到，未做登录动作）")
                # 4) 聊天页：已经在聊天页就不导航
                if self.xtc.is_in_chat():
                    msgs.append("已在聊天页")
                elif contact:
                    chat_ok = False
                    for _ in range(3):
                        if self.xtc.open_chat(contact):
                            chat_ok = True
                            break
                        self.xtc.settle()
                        time.sleep(1.0)
                    msgs.append("已进入聊天" if chat_ok else "未进入聊天")
                else:
                    msgs.append("未配置联系人")
                # 5) 文字模式/输入框：只有真的有残留才清空
                if self.xtc.is_in_chat():
                    msgs.append(self.xtc.ensure_input_clean())
                    # 6) 顺手学一次"发送按钮坐标"：先手打字要用它，学完**重启后的第一条**
                    #    QQ 消息也能 ~1 秒内把文字打进去（只注入一个探针字符随即清空，不发消息）
                    if self.xtc.learn_send_point():
                        msgs.append("已记录发送按钮坐标")
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
                          into_chat: bool = False, source: str = "") -> str:
        """读取小天才最近对话历史（入队，由工作线程串行执行，保证与发送顺序）。
        - QQ 侧：request_id 回传插件，插件在原会话引用+@ 回复；
        - xtc 侧：into_chat=True 时结果写进小天才聊天；
        - source：可选来源过滤（手表 / QQ私聊 <号> / QQ群 <群号>），留空为全部。
        返回 'queued'。count 自动夹到 1..100。"""
        try:
            count = max(1, min(int(count), 100))
        except (TypeError, ValueError):
            count = 20
        self._job_queue.put(("history", count, request_id or "", bool(into_chat),
                             str(source or "")))
        return "queued"

    def _match_source(self, entries: list[dict], source: str) -> list[dict]:
        """按来源过滤：支持「手表」「QQ私聊 <号>」「QQ群 <群号>」或裸的号
        （只写号码时，私聊/群聊都能命中）。"""
        want = (source or "").strip()
        if not want:
            return entries
        if want in ("手表", "xtc", "watch", "家长", "宝贝"):
            return [e for e in entries if e.get("kind") == "xtc"]
        if want in ("qq", "QQ", "私聊", "群聊"):
            if want in ("私聊",):
                return [e for e in entries if "[QQ私聊" in (e.get("source") or "")]
            if want in ("群聊",):
                return [e for e in entries if "[QQ群" in (e.get("source") or "")]
            return [e for e in entries if e.get("kind") == "qq"]
        # 具体来源：优先 source_id 精确匹配，其次标签包含
        hit = [e for e in entries if str(e.get("source_id") or "") == want]
        if hit:
            return hit
        return [e for e in entries if want in (e.get("source") or "")]

    def _do_history_job(self, count: int, request_id: str, into_chat: bool,
                        source: str = "") -> None:
        """工作线程内：读本地消息库 -> 格式化回传（不滚动界面，不依赖聊天页状态）。"""
        entries = self.msgs.recent(1000)          # 先取全部，再做来源过滤
        if source:
            entries = self._match_source(entries, source)
        entries = entries[-count:]
        if not entries:
            reply = (f"小天才历史消息（来源：{source}）：暂无本地消息记录"
                     if source else
                     "小天才历史消息：暂无本地消息记录"
                     "（消息库自桥接启用后自动积累真实对话）")
        else:
            reply = self._format_history_text(entries, count,
                                              max_chars=900 if into_chat else 3800)
        self._log("info", f"[历史消息] 来源={source or '全部'} {len(entries)} 条，回传方式: "
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
        """把本地消息库条目格式化为文本。行格式：
            [MM-DD HH:MM] [来源] 发送方: 内容
        来源明确写出「手表」「QQ私聊 <号>」「QQ群 <群号>」，末尾附来源统计。
        - 日期用明确数字（昨天/前天 等已由归档时间戳换算成具体日期，如 09-01）；
        - 跳过系统提示（发送成功/发送失败）与命令文本（防御性过滤）；
        - 超长从最旧截断。"""
        sys_prefixes = self._system_msg_prefixes()
        lines: list[str] = []
        tally: dict[str, int] = {}
        for e in entries:
            text = (e.get("text") or "").strip()
            if not text:
                continue
            if any(text.startswith(p) for p in sys_prefixes):
                continue
            if self._xtc_cmd_prefix and text.startswith(self._xtc_cmd_prefix):
                continue
            sender = (e.get("sender") or "").strip()
            source = (e.get("source") or "").strip()
            if not source:  # 兼容旧数据（没有 source 字段）
                if e.get("kind") == "xtc":
                    source = self._xtc_source(sender)
                    sender = self._display_name(sender)
                else:
                    source = "QQ"
            elif e.get("kind") == "xtc":
                sender = self._display_name(sender)
            try:
                dt = datetime.fromtimestamp(float(e.get("t") or 0))
            except (TypeError, ValueError, OSError):
                dt = datetime.now()
            short = self._short_source(source)
            tally[short] = tally.get(short, 0) + 1
            lines.append(f"[{dt:%m-%d} {dt:%H:%M}] [{short}] {sender}: {text}")
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
        summary = "来源统计：" + "、".join(
            f"{k} {v} 条" for k, v in sorted(tally.items(), key=lambda x: -x[1]))
        return header + "\n" + "\n".join(lines) + "\n" + summary

    @staticmethod
    def _short_source(source: str) -> str:
        """来源标签里的「手表-昵称」简化为「手表」，避免每行过长（手表上打字慢）。"""
        s = (source or "").strip()
        if s.startswith("手表"):
            return "手表"
        return s

    def _system_msg_prefixes(self) -> list:
        """桥接系统提示前缀（送达确认等）。

        必须过滤空串：空串会让 `str.startswith("")` 恒为真，导致**所有**消息都被当成
        系统提示而永不转发（历史版本曾配置里带 emoji 项，删掉后要防止出现空项）。
        """
        ui = ((self.cfg.get("xiaotiancai") or {}).get("ui") or {})
        prefixes = ui.get("system_msg_prefixes", ["发送成功", "发送失败"])
        return [str(p) for p in prefixes if str(p or "").strip()]

    def _label_epoch(self, time_label: str, now: datetime | None = None) -> float | None:
        """把 App 时间标签解析成时间戳（本地消息库归档 / 绝对化都用它）：
        'HH:MM'->今天；'今天'/'昨天'/'前天 HH:MM'->对应日期；'星期X HH:MM'->最近的那个星期X；
        'M月D日 HH:MM'、'09-25 16:18'、'2026-09-25 16:18'->该日期。
        解析失败返回 None（归档时用当前时间兜底）。"""
        label = (time_label or "").strip()
        if not label:
            return None
        now = now or datetime.now()
        # 已经是绝对时间：09-25 16:18 / 2026-09-25 16:18 / 2026/09/25 16:18
        m = re.search(r"(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})[\sT]+(\d{1,2}):(\d{2})", label)
        if m:
            try:
                dt = now.replace(year=int(m.group(1)) if m.group(1) else now.year,
                                 month=int(m.group(2)), day=int(m.group(3)),
                                 hour=int(m.group(4)), minute=int(m.group(5)),
                                 second=0, microsecond=0)
            except ValueError:
                return None
            if not m.group(1) and dt > now + timedelta(days=1):
                dt = dt.replace(year=dt.year - 1)   # 没写年份且落在未来 -> 多半是去年的
            return dt.timestamp()
        # "今天 16:18" / "今日 16:18"
        m = re.fullmatch(r"(?:今天|今日)\s*(\d{1,2}):(\d{2})", label)
        if m:
            return now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                               second=0, microsecond=0).timestamp()
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
        # "星期一 16:18" -> 最近一个已经过去的那个星期几
        m = re.search(r"星期([一二三四五六日天])", label)
        if m:
            tm = re.search(r"(\d{1,2}):(\d{2})", label)
            idx = "一二三四五六日天".index(m.group(1)) + 1        # 周一=1 ... 周日=7
            days_back = (now.isoweekday() - idx) % 7
            dt = (now - timedelta(days=days_back)).replace(
                hour=int(tm.group(1)) if tm else 0,
                minute=int(tm.group(2)) if tm else 0, second=0, microsecond=0)
            if dt > now:
                dt = dt - timedelta(days=7)
            return dt.timestamp()
        m = re.search(r"(\d{1,2})月(\d{1,2})日", label)
        if m:
            tm = re.search(r"(\d{1,2}):(\d{2})", label)
            try:
                dt = now.replace(month=int(m.group(1)), day=int(m.group(2)),
                                 hour=int(tm.group(1)) if tm else 0,
                                 minute=int(tm.group(2)) if tm else 0,
                                 second=0, microsecond=0)
            except ValueError:
                return None
            if dt > now + timedelta(days=1):
                dt = dt.replace(year=dt.year - 1)   # 只写月日且落在未来 -> 去年
            return dt.timestamp()
        return None

    # ------------------------------------------------------------------ xtc 侧命令（在小天才聊天输入，由本桥执行）
    # 排版模仿 QQ 帮助信息（用法：+ 逐行命令 + 对齐说明）；聊天输入支持换行，
    # 会按多行原样发送。
    _XTC_USAGE = ("用法（在小天才聊天里直接输入）：\n"
                  "/小天才 搜索 <昵称>          白名单QQ私聊/群聊中找人（附最后消息时间）\n"
                  "/小天才 在线人数 <分钟>      最近N分钟白名单QQ会话发言人数（1-60）\n"
                  "/小天才 提醒 <群号> <QQID> [内容]    在指定QQ群内@提醒该用户\n"
                  "/小天才 历史消息 [条数] [来源]   查看对话记录（默认20条，可只看 手表/QQ群/QQ私聊）")

    def _xtc_usage(self) -> str:
        return self._XTC_USAGE

    # ------------------------------------------------------------------ 命令去重（身份持久化）
    def _load_cmd_done(self) -> None:
        """读取已执行命令身份 (side, text, time_label)。"""
        try:
            data = json.loads(Path(self._cmd_done_file).read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._cmd_done = {tuple(x) for x in data if isinstance(x, list) and len(x) == 3}
        except Exception:  # noqa: BLE001 文件缺失/损坏不致命
            self._cmd_done = set()

    def _cmd_done_has(self, side: str, text: str, label: str) -> bool:
        with self._cmd_lock:
            return (side, text, label) in self._cmd_done

    def _cmd_done_add(self, side: str, text: str, label: str) -> None:
        with self._cmd_lock:
            self._cmd_done.add((side, text, label))
            if len(self._cmd_done) > 300:  # 修剪上限
                self._cmd_done = set(list(self._cmd_done)[-300:])
            try:
                Path(self._cmd_done_file).parent.mkdir(parents=True, exist_ok=True)
                tmp = self._cmd_done_file + ".tmp"
                Path(tmp).write_text(
                    json.dumps([list(x) for x in self._cmd_done], ensure_ascii=False),
                    encoding="utf-8")
                Path(tmp).replace(self._cmd_done_file)
            except Exception:  # noqa: BLE001 写盘失败不影响运行
                pass

    def _cmd_text_handled(self, side: str, text: str, time_label: str) -> bool:
        """这条命令（同侧同文本同标签）是否已经处理过——用于轮询时跳过重复触发。"""
        key = (side, (text or "").strip())
        label = (time_label or "").strip()
        with self._cmd_lock:
            if self._cmd_seen_text.get(key) == label:
                return True
            return (side, (text or "").strip(), label) in self._cmd_done

    def _maybe_xtc_cmd(self, side: str, text: str, time_label: str) -> None:
        """命令去重入队（手表侧/家长侧输入共用）。

        三层去重，彻底解决"同一条命令被反复执行/反复回复"：
        - pending：已入队未完成 -> 不再重复入队；
        - done（持久化）：身份相同（侧+文本+时间标签）-> 不再执行，重启也不重复；
        - **seen_text（会话级）**：同一侧的同一条命令文本，只要 App 时间标签没变，
          就不再处理（含"桥接自己发出去的回复/结果"被读回的情况）。
          标签变了说明是用户新输入的一条（哪怕文本一样），仍会执行。
        """
        label = (time_label or "").strip()
        key = (side, text)
        with self._cmd_lock:
            if text in self._cmd_pending:
                # 仍在队列里/正在执行：同一次输入不重复入队；
                # 若是用户新发的一条（标签变了），把标签更新为新值，
                # 这样执行成功后 done 记录的是最新身份，不会因旧标签被反复触发。
                if label and self._cmd_pending[text][1] != label:
                    self._cmd_pending[text] = (side, label)
                return
            if self._cmd_seen_text.get(key) == label:
                return
            if (side, text, label) in self._cmd_done:
                self._cmd_seen_text[key] = label
                return
            self._cmd_seen_text[key] = label
            if len(self._cmd_seen_text) > 200:
                recent = list(self._cmd_seen_text.items())[-200:]
                self._cmd_seen_text = dict(recent)
        self._cmd_pending[text] = (side, label)
        self._log("info", f"[xtc命令] 收到: {text}")
        self._job_queue.put(("cmd", text))

    def _do_cmd_job(self, text: str) -> bool:
        """执行 xtc 侧命令。返回是否成功回复（失败时调用方复位去重，允许下轮重试）。
        语法错误 / 无法识别的子命令 / 缺参数 -> 一律回复完整帮助列表。"""
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
                reply = self._xtc_usage()            # 无法识别 -> 帮助列表
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"xtc 命令执行异常: {e}")
            reply = self._xtc_usage()
        if reply is None:
            return True
        return self._reply_into_xtc(reply)

    def _cmd_history(self, args: str):
        """参数：[条数] [来源]。返回 None=已执行（写回聊天）；str=帮助列表（参数错误）。"""
        n = 20
        source = ""
        parts = (args or "").split()
        if parts:
            if parts[0].isdigit():
                n = int(parts[0])
                if not 1 <= n <= 100:
                    return self._xtc_usage()
                parts = parts[1:]
            else:
                n = 20
            if parts:
                source = " ".join(parts).strip()
        # 已处于工作线程：直接同步执行历史读取任务（结果写入小天才聊天）
        self._do_history_job(n, "", into_chat=True, source=source)
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
            return self._xtc_usage()  # 参数缺失/非数字 -> 帮助列表
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
            self._log("error", "回复小天才需要 config.yaml -> target.xtc_contact")
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
            "timeout": "超时/网络较慢（稍后自动重试）",
            "error": "出错（控件未找到）",
        }.get(status, status)

    def _login_check_loop(self) -> None:
        """检测登录态，未登录则自动账密登录（可被 /小天才 自动登录 关闭）。

        节奏（修复"自动登录不生效"）：
        - 启动后约 5s 立即检测一次；
        - 未登录且配置了账密 -> 立即触发登录，并按结果设置下次可尝试时间；
        - 登录成功 -> 按 login_check_interval（默认 600s）复查；
        - 登录超时/网络问题 -> login_retry_interval（默认 120s）后自动重试；
        - 明确的账号密码错误 -> login_retry_after_fail（默认 1800s）后重试；
        - 触发安全验证 -> login_retry_after_risk（默认 900s）后重试（等用户手动完成）。
        """
        time.sleep(5)  # 等 App 启动稳定，避免误判未登录
        while self.running:
            try:
                if not self._auto_login_enabled:
                    time.sleep(10)
                    continue
                now = time.monotonic()
                if self._login_inflight or now < self._login_not_before:
                    time.sleep(5)
                    continue
                if not self._op_lock.acquire(blocking=False):
                    time.sleep(5)
                    continue
                try:
                    state = self.xtc.login_state(force=True)
                finally:
                    self._op_lock.release()
                action = self._auto_login_decision(state)
                if action == "none":
                    if state == LOGIN_UNKNOWN:
                        # 无法判断（App 不在前台 / 息屏 / 界面读不到）：**不去登录**，
                        # 只稍后再看。旧实现把这种情况当成"未登录"，于是已登录也报未登录。
                        self._log_once("login_unknown",
                                       "无法确认小天才登录态（App 不在前台或界面暂时读不到），"
                                       "跳过本轮自动登录")
                        self._login_not_before = now + 60
                    else:
                        if self._pending_login_notify:
                            self._pending_login_notify = False
                            self._notify("小天才已重新登录，桥接继续运行")
                        self._login_not_before = now + self._login_check_interval
                        self._log("debug", "小天才登录态正常")
                    time.sleep(10)
                    continue
                if action == "no_cred":
                    if not self._warned_no_cred:
                        self._warned_no_cred = True
                        self._log("warning",
                                  "检测到小天才未登录，但没有配置账密"
                                  "（config.yaml -> xiaotiancai.login.phone / password）"
                                  "-> 自动登录不会生效，请手动登录或补齐配置")
                    self._login_not_before = now + self._login_check_interval
                    time.sleep(10)
                    continue
                self._log("info", "检测到小天才未登录，触发自动登录（手机号+密码）...")
                self.login_xiaotiancai()
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"自动登录检测异常: {e}")
            time.sleep(5)

    def _auto_login_decision(self, state: str) -> str:
        """根据登录态决定自动登录动作（纯函数，便于测试）：

        - 'logged_in'      -> none（什么都不做）
        - 'unknown'        -> none（**不能**当成未登录，否则会误触发登录流程）
        - 'not_logged_in'  -> login（已配置账密）/ no_cred（未配置账密，提示一次）
        """
        if state in (LOGIN_LOGGED_IN, LOGIN_UNKNOWN):
            return "none"
        phone, password = self._has_credentials()
        return "login" if (phone and password) else "no_cred"

    def _log_once(self, key: str, text: str, interval: float = 600.0) -> None:
        """同类日志按 key 节流（避免每 5 秒刷屏）。"""
        now = time.monotonic()
        if now - self._log_seen.get(key, float("-inf")) < interval:
            return
        self._log_seen[key] = now
        self._log("info", text)

    def _log_state(self, state: str) -> None:
        """状态变化时记一条 INFO；长时间停在非聊天状态则每 10 分钟提醒一次。

        目的：日志里能一眼看出"当时到底处于什么状态"，而不是满屏重复的失败告警
        （用户反馈"不能判断当前状态，还老是提示找不到联系人"）。
        """
        text = self.xtc.STATE_TEXT.get(state, state)
        if state != self._last_state:
            self._last_state = state
            self._log("info", f"小天才状态: {text}")
            # 从现在起算 10 分钟，避免"状态没变"时立刻又提醒一次
            self._log_seen[f"stuck:{state}"] = time.monotonic()
            return
        if state not in (self.xtc.STATE_CHAT,):
            self._log_once(f"stuck:{state}", f"小天才状态持续为「{text}」", interval=600.0)

    def _notify_once(self, key: str, text: str, interval: float = 600.0) -> None:
        """同一类通知在 interval 秒内只发一次（避免刷屏）。"""
        now = time.monotonic()
        if now - self._notify_seen.get(key, float("-inf")) < interval:
            self._log("debug", f"[通知去重] {text}")
            return
        self._notify_seen[key] = now
        self._notify(text)

    def _notify(self, text: str) -> None:
        """发送登录相关通知到 QQ（target.notify_qq，缺省用 qq_private 第一个）。"""
        target = self._notify_target()
        if not target:
            self._log("info", f"[通知占位] {text}")
            return
        try:
            ok = self.forwarder.send("private", target, text)
            if ok:
                self._log("info", f"[通知] private:{target} <- {text}")
            else:
                self._log("error", f"[通知失败] private:{target} <- {text}")
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
