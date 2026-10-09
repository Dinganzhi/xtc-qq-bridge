# -*- coding: utf-8 -*-
"""消息桥接调度层：轮询小天才新消息 -> 转发（当前支持 log 打印 /
AstrBot 插件端点两种模式），并负责去重、回声过滤与 ADB 断线重连。

反向（QQ->小天才）由 qq_webhook.py 调用 bridge.forward_to_xiaotiancai()，
webhook.enabled=true 且 NapCat/插件回调就绪后启用。
"""
from __future__ import annotations

import base64
import collections
import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from utils.deduplicate import Deduplicator, EchoFilter, HistoryFilter
from utils import imgtool
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
        # 表情包 / 照片（**仅 小天才 -> QQ 单向**）：优先读 App 数据目录/图片缓存里的
        # **原文件**（动图 GIF 能保住动画、照片能拿回原图），拿不到再按气泡截图，
        # 最后退回发"表情X"/"图片"文字。
        _emoji = cfg.get("emoji") or {}
        self._emoji_image = bool(_emoji.get("forward_image", True))
        self._emoji_caption = bool(_emoji.get("caption", True))
        self._emoji_from_data = bool(_emoji.get("from_app_data", True))
        # 照片（图片消息）单独一个开关：原图动辄 1~2MB，想省流量/流量贵的可以关掉，
        # 关掉后仍会发**气泡截图**（内容对，只是清晰度低）
        self._emoji_photo = bool(_emoji.get("forward_photo", True))
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
        # 断连重连的退避状态：设备真的连不上（如 WSA transport 卡成 offline）时，
        # 不要每 10 秒就"重连一次"刷屏 + 白跑 12 个候选端口；失败后指数退避到最多 60 秒。
        self._reconnect_fails = 0
        self._next_reconnect_ts = float("-inf")
        # 取图时等发送线程让出界面锁的最长时间（秒）：超过就先按旧坐标试，
        # 宁可少一张图，也不要让补发流程卡在发送线程手里。
        self._media_lock_timeout = float((cfg.get("emoji") or {}).get("lock_timeout", 8) or 8)
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
        self._wsa_reconnect_hinted = False  # WSA 断网提示只打一次
        # FIFO 任务队列：QQ->小天才 发送 / 登录 由单工作线程串行执行，
        # 保证多消息到达时按顺序处理，避免并发抢锁导致前后关系紊乱
        self._job_queue: queue.Queue = queue.Queue()
        # "发送专用"高优先级队列：QQ->小天才 是**用户正等着看**的交互（他在 QQ 里发完
        # 就盯着手表），绝不能排在小天才->QQ 的转发、历史查询、聊天内"发送成功"回执后面。
        # 实测（09-29 日志）：一条 QQ 消息在队列里等了 15~47 秒才轮到，就是被这些任务占着。
        self._fast_queue: queue.Queue = queue.Queue()
        # 为了给发送让路而"先取出来、稍后再做"的普通任务（保持原有先后顺序）
        self._held_jobs: collections.deque = collections.deque()
        self._job_thread: threading.Thread | None = None
        # ---- 转发"补投计划"：只补没发出去的目标，绝不因为"有个群失败"就把私聊/别的群再发一遍 ----
        # 为什么要独立计划 + 独立线程（用户实测过的坑）：
        #   * 旧实现靠"轮询重新读屏"来重试，于是**退避节奏完全失控**（每 5 秒重发一次）；
        #   * 重试时它会把**所有目标**再发一遍（私聊被刷屏几十分钟）；
        #   * 消息身份在"标签读到了/没读到"之间会跳变，靠身份去重也挡不住这种重发。
        # 现在：第一次尝试就把"这条已经处理过"记进历史，失败的目标写进补投计划，
        # 由专门的线程按退避节奏**只补那些目标**；到达上限后放弃并明确报错。
        self._fwd_plan: dict[tuple, dict] = {}
        self._fwd_plan_lock = threading.Lock()
        self._fwd_retry_thread: threading.Thread | None = None
        # 内容级"刚转发过"护栏：同一联系人 + 同一文本，N 秒内不再重复转发
        # （App 的时间标签会时有时无，光靠"文本+标签"当身份挡不住重复；用户报告的就是这个）
        self._recent_fwd: dict[tuple, float] = {}
        _fwd = cfg.get("forward") or {}
        try:
            # 0 = 失败就放弃（不补投）；默认 3 次
            self._forward_max_tries = max(0, int(_fwd.get("retry_max_tries", 3) or 0))
        except (TypeError, ValueError):
            self._forward_max_tries = 3
        try:
            self._forward_retry_backoff = max(5.0, float(_fwd.get("retry_backoff", 60) or 60))
        except (TypeError, ValueError):
            self._forward_retry_backoff = 60.0
        try:
            self._forward_content_guard = max(
                0.0, float(_fwd.get("content_dedup_secs", 300) or 0))
        except (TypeError, ValueError):
            self._forward_content_guard = 300.0
        # 媒体（图片/表情）**还没下载完**时的等待重试：App 先画出占位图，原图可能
        # 几秒~几分钟后才落盘（实测 22:12 检测到消息、22:19 原图才出现）。
        # 这期间先不转发，否则 QQ 只会收到"图片"两个字。
        self._pending_media: dict[tuple, dict] = {}
        # 同一 App 时间标签"第一次被用掉"的时刻：同组后续消息其实更晚（用户实测 15:46 被写成
        # 15:41），超过 label_stale_secs 就用当前时间显示（身份仍用 App 标签）
        self._label_used: dict[tuple, float] = {}
        try:
            self._label_stale_secs = max(10.0, float(_xc.get("label_stale_secs", 60) or 60))
        except (TypeError, ValueError):
            self._label_stale_secs = 60.0
        _media = cfg.get("emoji") or {}
        try:
            self._media_retry_max = max(1, int(_media.get("wait_retries", 8) or 8))
        except (TypeError, ValueError):
            self._media_retry_max = 8
        try:
            self._media_retry_base = max(1.0, float(_media.get("wait_base", 5) or 5))
        except (TypeError, ValueError):
            self._media_retry_base = 5.0
        try:
            self._media_retry_max_wait = max(
                30.0, float(_media.get("wait_max_secs", 1800) or 1800))
        except (TypeError, ValueError):
            self._media_retry_max_wait = 1800.0
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
        # 补投线程：只补"没发出去的目标"（不碰界面，所以不会和发送抢界面锁）
        self._fwd_retry_thread = threading.Thread(target=self._forward_retry_loop,
                                                  name="xtc-fwd-retry", daemon=True)
        self._fwd_retry_thread.start()
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
        if self._fwd_retry_thread:
            self._fwd_retry_thread.join(timeout=5)

    # ------------------------------------------------------------------ 任务队列（FIFO，保证顺序）
    def _next_job(self):
        """取下一个任务：**发送优先**（返回 (来源队列, 任务)）。

        规则：
          1) 发送队列里有活 -> 先做发送（用户正等着）；
          2) 之前为让路而暂存的任务 -> 按原顺序继续（不会再被插队）；
          3) 否则从普通队列取；取到后发现发送在排队 -> 把普通任务暂存，先做发送。
        这样既不饿死普通任务，也不会让"最新的一条 QQ 消息"排在一堆转发后面。
        """
        try:
            return self._fast_queue, self._fast_queue.get_nowait()
        except queue.Empty:
            pass
        if self._held_jobs:
            return self._held_jobs.popleft()
        try:
            job = self._job_queue.get(timeout=0.2)
        except queue.Empty:
            return None
        if not self._fast_queue.empty():
            self._held_jobs.appendleft((self._job_queue, job))
            return self._fast_queue, self._fast_queue.get_nowait()
        return self._job_queue, job

    def _job_worker(self) -> None:
        while self.running:
            picked = self._next_job()
            if picked is None:
                continue
            src, job = picked
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
                    f_disp = job[5] if len(job) > 5 else ""
                    self._do_forward_job(f_contact, f_text, f_label, sticker=f_sticker,
                                         display_label=f_disp)
                elif kind == "confirm":
                    # 小天才聊天里的"发送成功：…"回执：低优先级（见 _queue_confirm_xtc）
                    self._confirm_xtc_delivery(job[1])
                elif kind == "login":
                    _, request_id = job
                    self._do_login_job(request_id)
                elif kind == "init":
                    _, request_id = job
                    self._do_init_job(request_id)
                elif kind == "history":
                    # 小天才历史消息：count + 回传方式（request_id 或写入小天才聊天）+ 来源过滤
                    _, count, request_id, into_chat = job[:4]
                    src_name = job[4] if len(job) > 4 else ""
                    self._do_history_job(count, request_id, into_chat, src_name)
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
                try:
                    src.task_done()
                except ValueError:  # noqa: BLE001 理论上不会发生，别让工作线程挂掉
                    pass

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
                        if not self._heartbeat_reconnect(now):
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
                           or self.echo.is_echo(text)
                           or self._forward_suppressed(contact, text, label))
                    if not dup and not label:
                        # 读不到时间标签时只能按文本保守判定：宁可不重发，也不要反复刷同一条
                        dup = self.msgs.seen(text, "xtc")
                    # 图片/表情：媒体（原图/贴纸）可能**还没下载完**（App 先画出占位图，
                    # 原图要过几秒~几分钟才落盘）。这时不能马上按"文字"转发 ——
                    # 否则 QQ 只会收到"图片"两个字（用户实测：22:12 检测到、22:19 原图才出现）。
                    # 已经在等待重试队列里的：这一轮什么都不做（重试节奏由 _retry_pending_media
                    # 统一掌握，避免同一条消息一轮里被抠图两三次）。
                    is_media = (text == getattr(self.xtc, "IMAGE_TEXT", "图片")
                                or text.startswith("表情"))
                    mkey = self._fwd_key(contact, text, label)
                    if not dup and is_media and mkey in self._pending_media:
                        dup = True          # 本轮静默跳过：在等媒体/重试中，不转发也不刷日志
                    if not dup:
                        self._log("info", f"[收到小天才消息] 来源={self._xtc_source(contact)} "
                                          f"时间={time_label or '(无)'} 内容={text!r}")
                        # 表情包/图片：**必须在轮询线程里取**（此刻快照/气泡位置才准、
                        # 缓存里刚写进来的原文件也还在），而且要**挡住发送线程**——
                        # 发送会点输入框/打字/写"发送成功"提示，界面一动坐标就废了。
                        # 传消息自己的时间：检测可能滞后几分钟（刚重启/刚唤醒时），
                        # 缓存文件是"消息显示时"写进去的，只有按消息时间才找得回原图。
                        if is_media:
                            got_lock = self._op_lock.acquire(timeout=self._media_lock_timeout)
                            try:
                                sticker = self._capture_sticker(
                                    root, text, near_epoch=self._label_epoch(time_label or ""),
                                    match_label=key[3] or raw_label)
                            finally:
                                if got_lock:
                                    self._op_lock.release()
                            if sticker is None:
                                # 拿不到媒体：登记"稍后重试"，**这一轮先不发**
                                # （不发的话 QQ 只会收到"图片"两个字）
                                self._media_retry_failed(mkey, contact, text, label, time_label,
                                                         "界面上还是占位图、缓存里也还没有原图")
                                skip_media = True
                            else:
                                self._media_retry_clear(mkey)
                                skip_media = False
                        else:
                            sticker = None
                            skip_media = False
                        if not skip_media:
                            # 异步转发。**身份标签用归一化后的绝对时间**（与补发路径完全一致）：
                            # 以前这里传的是原始标签（如 "21:55"），而补发用的是绝对标签
                            # （"09-29 21:55"）—— 两条路径算出的去重键不同，于是同一条消息
                            # 会被"补发 + 实时"各转发一次（用户报的"2 转发了两遍"就是这么来的）。
                            self._queue_forward(contact, text, label, sticker=sticker,
                                                display_label=self._display_time_label(contact,
                                                                                       label))
                # 媒体（图片/表情）当时没就绪的那些：到点后重试取图并补发（不依赖"还是不是最新"）
                if self._pending_media:
                    try:
                        self._retry_pending_media(root)
                    except Exception as e:  # noqa: BLE001 重试异常不影响轮询
                        self._log("warning", f"[媒体] 重试流程异常: {e}")
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
        # 有表情/图片要做补发时：**先在界面锁内重新读一次界面**，整轮补发都用这一份新鲜快照。
        # 为什么：补发是连着发的，每转发成功一条就会往聊天里写一条"发送成功"提示把列表往上顶；
        # 用轮询那份（可能已经过去几秒）的坐标去抠图，抠到的是被顶走后的空白区。
        # 一次 dump 服务整轮（而不是每条媒体各 dump 一次）—— 后者在积压多条图片时白花好几秒。
        if any(it.get("sticker") or it.get("image") for it in bubbles):
            fresh = self._fresh_chat_root()
            if fresh is not None:
                try:
                    items = self.xtc._chat_bubbles(fresh, include_own=False)
                    if items:
                        bubbles, root = items, fresh
                except Exception as e:  # noqa: BLE001 解析不出来就沿用旧的
                    self._log("debug", f"[补发] 重读后的界面解析失败: {e}")
            else:
                self._log("debug", "[补发] 没能重新读界面，按现有快照取图")

        pending: list[tuple[str, str, dict | None, str]] = []
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
            # 身份用**这条消息自己的时间**（`own_label`，与实时路径的 `_label_for_bubble` 同源），
            # 显示用"它上方最近的时间标签"（`time_label`，App 的分组标签）。
            # 两条路径的身份必须**同源同格式**，否则同一条消息会被"补发 + 实时"各发一次。
            own_lbl = (it.get("own_label") or "").strip()
            disp_lbl = (it.get("time_label") or "").strip()
            ident = self._abs_time_label(own_lbl) or own_lbl
            if not own_lbl and not disp_lbl:
                # 界面上没有时间标签（App 只在一组消息的第一条上方画一个，滚出屏幕就没了）：
                # 退回用"上一条已入库消息的时间"，而不是"当前时间" —— 否则补发旧消息会写成
                # 转发时刻（用户实测：22:04 发的"噢"在 22:20 被补发，显示成了 22:20）。
                disp_lbl = self._estimate_time_label()
            else:
                # 显示时间：同组标签"已经用过且隔了 60 秒以上"就用当前时间（用户实测：
                # 15:46 发的被写成 15:41）。身份 ident 不变，只影响显示。
                disp_lbl = self._display_time_label(
                    contact, ident or self._abs_time_label(disp_lbl) or disp_lbl)
            if self._in_store(contact, text, ident):
                break                               # 撞库 -> 停，不再往上翻
            if self.dedup.seen(("xtc", contact or "", text, ident)):
                # 刚试过（120 秒内：正在发的、或上次转发失败的）：本轮先跳过它，
                # 但不当作边界，继续往上找更老的那几条
                self._log("debug", f"[补发] 这条最近试过，先跳过: {text[:24]!r}")
                continue
            if self._forward_suppressed(contact, text, ident):
                # 正在补投 / 刚转发过同一内容（标签跳变也算同一条）：**不当作边界**（库里
                # 仍没有它），但这一轮不重发。旧实现没有这层，于是一个群失败会让同一条消息
                # 每 5 秒被重转一次，还连已经成功的私聊一起重发（用户实测的刷屏）。
                self._log("debug", f"[补发] 这条在补投/内容护栏内，先跳过: {text[:24]!r}")
                continue
            # 补发的表情/图片也取原图：用上面那份**新鲜快照**（整轮共用，坐标不会过期）
            sticker = None
            if it.get("sticker") or it.get("image"):
                mkey = self._fwd_key(contact, text, ident)
                if mkey in self._pending_media:
                    # 已经在"等媒体"队列里：交给 `_retry_pending_media`（那里掌握退避节奏，
                    # 而且不依赖它是不是最新一条）。这里**直接停**，不往上翻（否则顺序会反）。
                    self._log("debug", f"[补发] 这条的媒体在等待重试，先跳过: {text[:24]!r}")
                    break
                sticker = self._capture_sticker(
                    root, text, near_epoch=self._label_epoch(disp_lbl or own_lbl),
                    match_label=ident or disp_lbl)
                if sticker is None:
                    # 媒体还没就绪：登记等待重试，这一条先不放行（不发"图片"两个字）
                    self._media_retry_failed(mkey, contact, text, ident, disp_lbl,
                                             "界面上还是占位图、缓存里也还没有原图")
                    break
            pending.append((text, ident, sticker, disp_lbl))

        if not pending:
            return 0
        pending.reverse()                           # 变成 旧 -> 新 的顺序转发
        if self._catchup_max and len(pending) > self._catchup_max:
            self._log("warning", f"[补发] 待补 {len(pending)} 条，超过上限 {self._catchup_max}，"
                                 f"先补最早的 {self._catchup_max} 条（下一轮继续）")
            pending = pending[:self._catchup_max]
        self._log("info", f"[补发] 有 {len(pending)} 条消息库里没有，按时间顺序补发")

        sent = 0
        for text, label, sticker, disp in pending:
            self._log("info", f"[收到小天才消息] 来源={self._xtc_source(contact)} "
                              f"时间={disp or label or '(无)'} 内容={text!r}（补发）")
            # 异步转发：不阻塞读屏（见 _queue_forward）
            self._queue_forward(contact, text, label, sticker=sticker, display_label=disp)
            sent += 1
        return sent

    def _estimate_time_label(self, max_gap: float = 1800.0) -> str:
        """消息没有时间标签时的兜底：用**上一条已入库消息的时间**（`MM-DD HH:MM`）。

        为什么需要：App 只在"一组消息的第一条"上方画时间标签，那一行滚出屏幕后，组内其余
        消息就没有任何标签了 —— 旧实现直接退化成"当前时间"，补发旧消息时就会写成**转发
        时刻**（实测：22:04 发的"噢"在 22:20 被补发，显示成了 22:20）。
        消息是严格按时间顺序处理的，所以"上一条已入库消息的时间"是个很接近的下界；
        只有当它离现在足够近（默认 30 分钟内）才敢用，桥接停了很久时宁可退回"当前时间"。
        """
        try:
            recent = self.msgs.recent(1)
        except Exception:  # noqa: BLE001 取不到就用旧行为
            return ""
        if not recent:
            return ""
        try:
            t = float(recent[-1].get("t") or 0)
        except (TypeError, ValueError):
            return ""
        if t <= 0 or abs(time.time() - t) > max_gap:
            return ""
        return datetime.fromtimestamp(t).strftime("%m-%d %H:%M")

    def _fresh_chat_root(self):
        """在界面锁内重新读一次界面（拿不到锁就等一小会儿，超时返回 None）。

        补发取图用它：发送线程（点输入框/打字/点发送/写"发送成功"提示）拿的是同一把锁，
        所以"读界面 -> 抠图"这一小段界面不会被人动。
        """
        got_lock = self._op_lock.acquire(timeout=self._media_lock_timeout)
        try:
            try:
                return self.xtc.adb.dump_ui(retries=2, delay=0.3)
            except Exception as e:  # noqa: BLE001 读不到就返回 None，调用方沿用旧快照
                self._log("debug", f"[补发] 重新读界面失败: {e}")
                return None
        finally:
            if got_lock:
                self._op_lock.release()

    def _queue_forward(self, contact: str, text: str, label: str,
                       sticker: dict | None = None, display_label: str = "") -> None:
        """把"小天才 -> QQ"的转发丢给工作线程，立刻返回。

        为什么异步：插件要等 QQ 侧真实发送结果才回包（最长 30 秒），同步做的话
        轮询线程会被卡住，读屏/检测跟着变慢（转发越快，漏消息窗口也越小）。
        这里立刻 short-term 去重，避免下一轮把同一条再入队。

        label：**身份标签**（归一化的绝对时间，实时与补发两条路径必须同源同格式）。
        display_label：显示用的标签（App 的分组标签，显示更准）；留空则用 label。
        sticker：表情图（{data, kind, animated, source}）。**必须在轮询线程里取**——
        那一刻界面快照/气泡位置才准、缓存里刚写进来的文件也还在；没有它就按文字发。
        """
        self.dedup.mark(("xtc", contact or "", text, label or ""))
        # 注意：内容级护栏（_recent_fwd_seen）**不在这里**打点 —— 打点必须发生在
        # "真的要发出去"那一刻（见 _do_forward_job），否则刚入队的这条会被自己挡掉。
        self._job_queue.put(("forward", contact, text, label or "", sticker,
                             display_label or label or ""))

    # ---- 媒体（图片/表情）还没下载完时的"等一会儿再取" ----
    def _media_retry_decision(self, key: tuple) -> str:
        """返回 'now'（可以试取）/ 'wait'（还没到点）/ 'give_up'（等太久了，按文字发）。"""
        st = self._pending_media.get(key)
        if st is None:
            return "now"
        now = time.monotonic()
        if (now - float(st.get("first_ts") or now) > self._media_retry_max_wait
                or int(st.get("tries") or 0) >= self._media_retry_max):
            return "give_up"
        if now < float(st.get("next_ts") or 0.0):
            return "wait"
        return "now"

    def _media_retry_failed(self, key: tuple, contact, text: str, label: str,
                            disp: str, reason: str = "") -> None:
        """媒体取不到：登记"稍后重试"，这一轮**先不转发**（免得 QQ 只收到"图片"两个字）。"""
        now = time.monotonic()
        st = self._pending_media.get(key)
        if st is None:
            while len(self._pending_media) >= 20:      # 只留最近若干条
                self._pending_media.pop(next(iter(self._pending_media)), None)
            st = {"tries": 0, "first_ts": now, "next_ts": now}
            self._pending_media[key] = st
        st["tries"] = int(st.get("tries") or 0) + 1
        gap = min(300.0, self._media_retry_base * (2 ** (st["tries"] - 1)))
        st["next_ts"] = now + gap
        st.update({"contact": contact, "text": text, "label": label, "disp": disp})
        self._log("info", f"[媒体] {text[:24]!r} 的原图还没就绪（{reason or '缓存/界面里都还没有'}）；"
                          f"{int(gap)} 秒后重试（第 {st['tries']}/{self._media_retry_max} 次），"
                          "先不发文字，免得只发出去「图片」两个字")

    def _media_retry_clear(self, key: tuple) -> None:
        self._pending_media.pop(key, None)

    def _retry_pending_media(self, root) -> None:
        """到点后重试"媒体还没就绪"的消息：拿到图就补发，等太久就退回发文字。

        为什么放在轮询里、而且**不依赖"它还是不是最新一条"**：App 先画出"图片"占位气泡、
        原图可能几分钟后才落盘（实测 22:12 检测到、22:19:44 原图文件才出现）。这期间要是
        用户又发了别的消息，光标/补发都轮不到它了 —— 这里按**消息自己的时间标签**去找那条
        气泡，找到就取图补发（顺序上它比后来的消息早，回 QQ 时会带自己的时间抬头）。
        """
        if not self._pending_media:
            return
        for key, st in list(self._pending_media.items()):
            contact = st.get("contact")
            text = st.get("text") or ""
            label = st.get("label") or ""
            disp = st.get("disp") or label
            decision = self._media_retry_decision(key)
            if decision == "wait":
                continue
            sticker = None
            if decision == "now":          # give_up 时不再取图，直接按文字发
                got_lock = self._op_lock.acquire(timeout=self._media_lock_timeout)
                try:
                    sticker = self._capture_sticker(
                        root, text, near_epoch=self._label_epoch(disp or label),
                        match_label=label or disp)
                except Exception as e:  # noqa: BLE001 取图失败继续等下一轮
                    self._log("debug", f"[媒体] 重试取图失败: {e}")
                finally:
                    if got_lock:
                        self._op_lock.release()
            if sticker is None and decision != "give_up":
                self._media_retry_failed(key, contact, text, label, disp,
                                         "重试时还是没拿到原图/截图")
                continue
            self._media_retry_clear(key)
            self._log("info", f"[媒体] {'拿到图，补发' if sticker else '等太久了，按文字转发'}: "
                              f"{text[:24]!r}")
            self._queue_forward(contact, text, label, sticker=sticker, display_label=disp)

    def _display_time_label(self, contact, label: str) -> str:
        """挑一个**显示**用的时间：App 的分组标签只画在一组消息的第一条上方。

        用户实测：15:41 那条之后同一组里的消息（其实 15:46 才发）也被写成 15:41。
        规则：同一个 App 标签我们已经用它转发过、且距那时已经超过 `label_stale_secs`
        （默认 60 秒）-> 说明这条是**同组里更晚**的消息，改用当前时间。
        **身份（去重/历史）仍然用 App 标签**，只有显示时间变 —— 否则身份会跟着时间漂移，
        又会出现"同一条被反复转发"的老问题。
        """
        if not label:
            return self._estimate_time_label() or datetime.now().strftime("%m-%d %H:%M")
        now = time.time()
        key = (contact or "", label)
        first = self._label_used.get(key)
        if first is None:
            self._label_used[key] = now
            return label
        if now - first > self._label_stale_secs:
            return datetime.fromtimestamp(now).strftime("%m-%d %H:%M")
        return label

    def _fwd_key(self, contact, text: str, label: str) -> tuple:
        """转发身份键（与实时/补发两条路径共用，必须同源）。"""
        return ("xtc", contact or "", text, label or "")

    # ---- 内容级护栏 + 补投计划（"只有一个群失败"时绝不重发私聊/别的群） ----
    def _recent_fwd_seen(self, contact, text: str) -> bool:
        """同一联系人 + 同一文本，最近是否已经转发过（默认 300 秒内不再重复转发）。

        为什么不能只靠"文本 + 时间标签"当身份：App 的分组时间标签**时有时无**
        （同一屏里同一条消息一会儿读得到 "14:49"、一会儿读不到），标签一变身份就变，
        于是一条失败消息会被当成"新消息"反复转发 —— 用户报的"只有一个群没发出去，
        却连着别的群和私信一起一直转发"就是这个。
        """
        if self._forward_content_guard <= 0:
            return False
        key = ("xtc", contact or "", text or "")
        now = time.monotonic()
        ts = self._recent_fwd.get(key)
        if ts is None:
            return False
        if now - ts > self._forward_content_guard:
            self._recent_fwd.pop(key, None)
            return False
        return True

    def _mark_recent_fwd(self, contact, text: str) -> None:
        if self._forward_content_guard <= 0:
            return
        now = time.monotonic()
        # 顺手清掉过期项，避免字典无限增长
        for k, ts in list(self._recent_fwd.items()):
            if now - ts > self._forward_content_guard:
                self._recent_fwd.pop(k, None)
        self._recent_fwd[("xtc", contact or "", text or "")] = now

    def _has_active_plan(self, contact, text: str) -> bool:
        """是否已有"同一联系人 + 同一文本"的补投计划在跑（有就别再发一遍整条）。"""
        with self._fwd_plan_lock:
            for key, plan in self._fwd_plan.items():
                if key[1] == (contact or "") and key[2] == (text or ""):
                    return True
        return False

    def _forward_suppressed(self, contact, text: str, label: str) -> bool:
        """这条消息现在要不要跳过：正在补投 / 刚转发过同一内容。"""
        if self._has_active_plan(contact, text):
            return True
        return self._recent_fwd_seen(contact, text)

    def _plan_key(self, contact, text: str, label: str) -> tuple:
        """补投计划的身份（**不含标签**）：标签会跳变，补投只认"谁 + 什么内容"。

        计划里单独记着这条消息的显示标签，所以补投出去的时间抬头依然是对的。
        """
        return ("xtc", contact or "", text or "")

    def _mark_forwarded(self, contact, text: str, label: str, disp: str) -> None:
        """记入长期历史 + 本地消息库（此后轮询不会再把它当成"库里没有"的消息）。"""
        self.history.mark("xtc", contact or "", text, label or "")
        if not self.msgs.seen(text, "xtc"):
            self.msgs.append("xtc", contact or "", text, t=self._label_epoch(disp),
                             source=self._xtc_source(contact), source_id=contact or "")

    def _schedule_fwd_plan(self, contact, text: str, label: str, disp: str,
                           sticker: dict | None, failed: list) -> None:
        """登记/更新补投计划：**只记没发出去的目标**。"""
        if not failed:
            return
        if self._forward_max_tries <= 0:
            self._log("error", f"[转发] 有 {len(failed)} 个目标没发出去"
                               f"（{'、'.join(f'{t}:{i}' for t, i in failed)}）：{text[:24]!r}；"
                               "forward.retry_max_tries=0 -> 不补投（只报这一次）")
            return
        key = self._plan_key(contact, text, label)
        gap = self._forward_retry_backoff
        with self._fwd_plan_lock:
            plan = self._fwd_plan.get(key)
            if plan is None:
                plan = {"contact": contact, "text": text, "label": label, "disp": disp,
                        "sticker": sticker, "failed": list(failed), "tries": 0,
                        "next_ts": time.monotonic() + gap}
                self._fwd_plan[key] = plan
            else:
                plan["failed"] = list(failed)
                plan["sticker"] = sticker or plan.get("sticker")
                plan["tries"] = 0
                plan["next_ts"] = time.monotonic() + gap
        self._log("warning", f"[转发] 有 {len(failed)} 个目标没发出去"
                             f"（{'、'.join(f'{t}:{i}' for t, i in failed)}）：{text[:24]!r}；"
                             f"{int(gap)} 秒后**只重试这些目标**（最多 {self._forward_max_tries} 次，"
                             "已经成功的私聊/群不会重发）")

    def _forward_retry_loop(self) -> None:
        """补投线程：按退避节奏只重试失败的目标，到上限就放弃并明确报错。

        为什么单独一个线程：补投是一次 HTTP（QQ 侧超时可能 30 秒），放在工作线程里会把
        用户新发的 QQ 消息一起堵住；补投**不碰界面**，所以它不需要界面锁。
        """
        while self.running:
            time.sleep(0.5)
            now = time.monotonic()
            with self._fwd_plan_lock:
                due = [k for k, p in self._fwd_plan.items()
                       if now >= float(p.get("next_ts") or 0.0)]
            for key in due:
                try:
                    self._retry_failed_targets(key)
                except Exception as e:  # noqa: BLE001 补投异常不能让线程退出
                    self._log("warning", f"[转发] 补投异常: {e}")

    def _retry_failed_targets(self, key: tuple) -> None:
        """补投一次：**只发上次失败的目标**；到上限就放弃（记历史，不再重试）。"""
        with self._fwd_plan_lock:
            plan = self._fwd_plan.get(key)
            if plan is None:
                return
            plan["tries"] = int(plan.get("tries") or 0) + 1
            # 先占位（成功会删掉、失败会重排），避免同一轮被取两次
            plan["next_ts"] = time.monotonic() + self._forward_retry_backoff * 4
            snapshot = dict(plan)
        text = snapshot.get("text") or ""
        failed = list(snapshot.get("failed") or [])
        tries = int(snapshot.get("tries") or 0)
        if tries > self._forward_max_tries:
            with self._fwd_plan_lock:
                self._fwd_plan.pop(key, None)
            # 放弃：记入历史，轮询不会再拿它重转（失败的目标不再尝试）
            self._mark_forwarded(snapshot.get("contact", ""), text, snapshot.get("label", ""),
                                 snapshot.get("disp", ""))
            self._log("error", f"[转发] 放弃补投（已试 {tries - 1} 次）: {text[:24]!r}；"
                               f"失败目标 {'、'.join(f'{t}:{i}' for t, i in failed)} 仍未发出，"
                               "已停止重试（不会再刷屏）")
            return
        ok, _sent, still_failed = self._forward(
            snapshot.get("contact", ""), text,
            snapshot.get("disp", "") or snapshot.get("label", ""),
            sticker=snapshot.get("sticker"), only=failed)
        if ok:
            with self._fwd_plan_lock:
                self._fwd_plan.pop(key, None)
            self._mark_forwarded(snapshot.get("contact", ""), text, snapshot.get("label", ""),
                                 snapshot.get("disp", ""))
            self._log("info", f"[转发] 补投成功（第 {tries} 次）: {text[:24]!r}")
            return
        with self._fwd_plan_lock:
            cur = self._fwd_plan.get(key)
            if cur is not None and still_failed:
                cur["failed"] = list(still_failed)
        self._log("warning", f"[转发] 补投仍失败（第 {tries}/{self._forward_max_tries} 次）: "
                             f"{text[:24]!r}；"
                             + (f"失败目标 {'、'.join(f'{t}:{i}' for t, i in still_failed)}"
                                if still_failed else "没有可重试的目标"))

    def _queue_confirm_xtc(self, message: str) -> None:
        """小天才聊天里的"发送成功：…"回执 -> 入**普通队列**（低优先级）。

        为什么不再同步做：它要在聊天页里点输入框/打字/点发送（占界面锁好几秒），
        同步做就会把"用户刚在 QQ 发的那条消息"一起堵在后面（实测堵过 20 秒）。
        """
        self._job_queue.put(("confirm", message))

    def _do_forward_job(self, contact: str, text: str, label: str,
                        sticker: dict | None = None, display_label: str = "") -> None:
        """小天才 -> QQ 的**第一次**尝试（之后由补投线程只补失败的目标）。

        label 是身份标签（去重/历史都用它），display_label 只用于给 QQ 那条消息的抬头时间。
        三条纪律（用户实测过的坑）：
          * 已有补投计划 / 刚转发过同一内容 -> 直接跳过（标签跳变也不能骗过它）；
          * 有目标成功就立刻记历史：轮询不会再拿这条消息重转（私聊不会被刷屏）；
          * 失败的目标进补投计划：只补它们，退避 + 有上限，不再"每 5 秒一轮"。
        """
        disp = display_label or label
        if self._forward_suppressed(contact, text, label):
            self._log("debug", f"[转发] 这条正在补投或刚转发过，跳过: {text[:24]!r}")
            return
        # 打点必须在这里（"真的要发一次"那一刻）：轮询之后每 2~4 秒就会重新读到这条消息，
        # 靠它挡住"标签跳变 -> 被当成新消息 -> 整条重发"。
        self._mark_recent_fwd(contact, text)
        try:
            ok, sent, failed = self._forward(contact, text, disp, sticker=sticker)
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"转发异常: {e}")
            ok, sent, failed = False, [], []
        if ok:
            self._mark_forwarded(contact, text, label, disp)
            return
        if sent:
            # 部分成功：**立刻**记历史（否则标签一变就会被当成新消息，重发一遍私聊）
            self._mark_forwarded(contact, text, label, disp)
        self._schedule_fwd_plan(contact, text, label, disp, sticker, failed)

    def _warm_emoji_store(self) -> None:
        """后台预热表情包名字索引（失败无所谓，收到表情时会按需再建）。"""
        try:
            n = self._emoji_store.warm()
            self._log("debug", f"表情包索引预热完成：{n} 个名字")
        except Exception as e:  # noqa: BLE001
            self._log("debug", f"表情包索引预热失败: {e}")

    def _capture_sticker(self, root, text: str,
                         near_epoch: float | None = None,
                         match_label: str = "") -> dict | None:
        """取"要发到 QQ 的那张图"（表情包 **和** 照片都走这里）。

        表情包：
          1) **App 数据目录/图片缓存里的原文件**（`emoji_store`）：保真，动图保住动画，
             也不依赖气泡在屏幕上；挑哪张**由像素比对决定** —— 先抠下界面上这张气泡的
             截图当基准，再逐个候选比相似度，够像才发（根治"小猫流汗发成乌龟"）；
          2) 没有可信原图时，**就发这张气泡截图**（静止一帧，但一定是对的）；
          3) 连气泡都定位不到 -> 返回 None，调用方照样发"表情X"文字。

        照片（`text` == `图片`）：
          1) 缓存里的**原图**（大图；形状与气泡一致、写入时间就在消息时间附近才敢用）；
          2) 拿不到就发气泡截图（低清但一定对）—— 照片动辄 2000x3000，纯 Python 解不动
             （实测只解 DC 就要 37 秒），所以这条路不比像素，只认唯一候选。

        near_epoch：补发老消息时传"这条消息自己的时间"，好让缓存查找按消息时间去匹配
        （缓存文件是消息显示时写进来的），否则补发的贴图会退化成静态截图。
        """
        if not self._emoji_image:
            return None
        is_image = (text or "").strip() == getattr(self.xtc, "IMAGE_TEXT", "图片")
        is_sticker = (text or "").strip().startswith("表情")
        if not (is_image or is_sticker):
            # 防呆：文字消息**绝不能**取图。以前实时路径对每条消息都调这里，而下面
            # `sticker_of_latest(root, "")` 会退化成"屏幕上最新那张贴纸"—— 于是发一条
            # "2" 时把上一条消息的表情包一起发了出去（用户报的"附带前面一条消息的表情包"）。
            self._log("debug", f"这条不是表情/图片消息（{text[:24]!r}），不取图")
            return None
        name = (text or "").strip()
        if name.startswith("表情"):
            name = name[len("表情"):].strip()
        # 有的贴纸名字自带扩展名（实机：'表情弹吉他.png'）——查表情包索引前先去掉
        name = re.sub(r"\.(png|gif|webp|jpe?g|apng)$", "", name, flags=re.I).strip()
        # 先在快照里定位气泡：① 拿它的形状去缓存里挑原图 ② 抠它的截图当核对基准/兜底
        def _locate(r):
            try:
                if is_image:
                    return self.xtc.image_of_latest(r, text, match_label=match_label)
                return (self.xtc.sticker_of_latest(r, text, match_label=match_label)
                        or self.xtc.sticker_of_latest(r, ""))
            except Exception as e:  # noqa: BLE001 定位失败不影响取原图
                self._log("debug", f"定位图片/表情气泡失败: {e}")
                return None

        item = _locate(root)
        # **气泡被消息列表裁掉时先滚进可视区**：实测贴边那条抠下来只有半张（原图比对
        # 0.78 不过 -> 发出去的是半张 png），往下滚一屏后同一张能到 0.99。
        # 抓完图按原路滚回去（列表停在中间会让轮询读不到新消息）。
        steps: list[int] = []
        vb = None
        if item is not None:
            try:
                vb = self.xtc.chat_view_bounds(root)
            except Exception as e:  # noqa: BLE001 拿不到可视区就按"没被裁"处理
                self._log("debug", f"读消息列表可视区失败（按未裁处理）: {e}")
                vb = None
        for _ in range(2):
            if not item or not vb or not self.xtc.bubble_clipped(root, item["bounds"]):
                break
            dy = int((vb[3] - vb[1]) * 0.3) or 40
            # 贴着上边 = 上面被裁 -> 内容往下拖；贴着下边 = 下面被裁 -> 内容往上拖
            step = dy if item["bounds"][1] <= vb[1] + 2 else -dy
            if not self.xtc.scroll_chat_by(vb, step):
                break
            steps.append(step)
            fresh = self.xtc._dump_with_retry(1)
            if fresh is None:
                break
            root = fresh
            item = _locate(root)
        if steps:
            self._log("info", f"[媒体] 这条气泡被消息列表裁到了，已滚动 {len(steps)} 次取完整图"
                              "（抓完会滚回底部）")
        try:
            return self._capture_media_now(root, text, name, is_image, item,
                                           near_epoch=near_epoch)
        finally:
            # 滚回原位：逆向、同样次数（列表停在中间会让轮询读不到新消息）
            for step in reversed(steps):
                self.xtc.scroll_chat_by(vb, -step)

    def _capture_media_now(self, root, text: str, name: str, is_image: bool, item,
                           near_epoch: float | None = None) -> dict | None:
        """真正取图（气泡已确保完整可见）。拆出来是为了让"滚动 -> 取图 -> 滚回"成对出现。"""
        aspect = None
        if item and item.get("bounds"):
            x1, y1, x2, y2 = item["bounds"]
            if y2 > y1:
                aspect = (x2 - x1) / (y2 - y1)
        # **先把界面上的气泡抠下来**：聊天会自动滚动（新消息/送达确认都会把列表顶上去），
        # 任何慢操作（建表情名索引 3~4 秒、扫缓存）之后再抠，坐标就对不上了 —— 实测
        # 晚几秒抠到的是别处的**空白占位图**，转发出去就是一张灰底问号（用户会以为发错了）。
        ref = None
        if item and item.get("bounds"):
            need_ref = True
            if self._emoji_store is not None and name and not is_image:
                try:
                    # 索引已就绪才能"秒判"名字是否唯一；没就绪就直接截图（别为了省一次
                    # 截图去等 4 秒建索引，那样反而抠错位置）
                    if self._emoji_store.index_ready():
                        need_ref = not self._emoji_store.name_is_unique(name)
                except Exception as e:  # noqa: BLE001 判不出来就老老实实截图
                    self._log("debug", f"判断表情名是否唯一失败: {e}")
            if need_ref:
                try:
                    ref = self.xtc.capture_sticker(item.get("bounds"))
                except Exception as e:  # noqa: BLE001 抠图失败就走老路
                    self._log("debug", f"抠气泡截图失败: {e}")
                    ref = None
                if ref and imgtool.looks_blank(ref):
                    # 抠到空白/占位图：先重试一次（界面可能正在重绘）
                    retry = None
                    try:
                        retry = self.xtc.capture_sticker(item.get("bounds"))
                    except Exception as e:  # noqa: BLE001
                        self._log("debug", f"重抠气泡截图失败: {e}")
                    if retry and not imgtool.looks_blank(retry):
                        ref = retry
                    else:
                        dom, std = imgtool.content_stats(ref)
                        self._log("info", f"[表情包] 抠到的气泡像是空白/占位图（主色占比 {dom:.2f}、"
                                          f"亮度标准差 {std:.0f}）")
                        ref = None
        # ---------------- 照片（图片消息） ----------------
        if is_image:
            if self._emoji_photo and self._emoji_store is not None:
                try:
                    bw = item["bounds"][2] - item["bounds"][0] if item else 0
                    bh = item["bounds"][3] - item["bounds"][1] if item else 0
                    # allow_now=True（补发也一样）：App 显示图片时会把原图重新写进缓存，
                    # 所以"正在看的那张"必定刚写过；补发往往只是轮询晚了几秒而已。
                    # 真老的照片没有新鲜文件，自然落到"发气泡截图"兜底。
                    got = self._emoji_store.find_photo(near_epoch=near_epoch, aspect=aspect,
                                                       min_px=max(bw, bh))
                except Exception as e:  # noqa: BLE001 取不到就退回截图
                    self._log("debug", f"找照片原图失败（改用截图）: {e}")
                    got = None
                if got and got.get("data"):
                    self._log("info", f"[图片] 取自图片缓存原图：{got.get('kind', '?')} "
                                      f"{len(got['data'])} 字节（{got.get('w')}x{got.get('h')}）")
                    got["label"] = "图片"
                    return got
            if not item:
                self._log("info", f"[图片] 界面上没找到这条图片消息的气泡（{text!r}），按文字转发")
                return None
            if ref:
                self._log("info", f"[图片] 按气泡截图 {len(ref)} 字节（{item.get('bounds')}），"
                                  "随转发发给 QQ（缓存里没找到原图）")
                return {"data": ref, "kind": "png", "animated": False,
                        "source": "screenshot", "label": "图片"}
            self._log("info", f"[图片] 没能拿到这张照片（气泡 {item.get('bounds')}），按文字转发")
            return None
        # ---------------- 表情包 ----------------
        if self._emoji_from_data and self._emoji_store is not None:
            try:
                got = self._emoji_store.find(name, near_epoch=near_epoch,
                                             aspect=aspect, reference=ref,
                                             # 看得到气泡却没抠到可用截图 -> 只认能被证明的候选
                                             verified_only=bool(item) and ref is None)
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
            self._log("info", f"[表情包] 没有可信原图（{name!r}），发界面气泡截图 "
                              f"{len(ref)} 字节（静止一帧）")
            return {"data": ref, "kind": "png", "animated": False, "source": "screenshot"}
        if not item:
            self._log("info", f"[表情包] 界面上没找到这条表情的气泡（{text!r}），按文字转发")
            return None
        try:
            png = self.xtc.capture_sticker(item.get("bounds"))
            if png and not imgtool.looks_blank(png):
                self._log("info", f"[表情包] 按气泡截图 {len(png)} 字节（静态一帧）"
                                  f"（{item.get('bounds')}），随转发发给 QQ")
                return {"data": png, "kind": "png", "animated": False, "source": "screenshot"}
            self._log("info", f"[表情包] 抠到的气泡是空白/占位图（或截图失败），"
                              f"按文字转发（气泡 {item.get('bounds')}）")
            return None
        except Exception as e:  # noqa: BLE001 截图失败不影响文字转发
            self._log("warning", f"[表情包] 取图异常（按文字转发）: {e}")
            return None

    def _heartbeat_reconnect(self, now: float) -> bool:
        """心跳发现 ADB 断连时的重连 + 退避。返回 True=本轮继续轮询，False=跳过本轮。

        退避的意义（实机踩过）：WSA 的 transport 卡成 offline 时，`adb connect` 只会回
        "already connected to ..."，重连其实**救不回来**；旧实现每 10 秒就重跑一遍
        "12 个候选端口 × 2 个 host"的连接尝试，日志刷屏且白等。现在失败一次就退避
        （10/20/30…最多 60 秒），并且只在第一次与每次失败时各喊一声。
        """
        if now < self._next_reconnect_ts:
            self._log("debug", "ADB 仍未恢复，等退避时间到再试")
            return False
        if self._reconnect_fails == 0:
            self._log("warning", "ADB 断连，尝试重连...")
        try:
            self.adb.ensure_connected()
        except Exception as e:  # noqa: BLE001
            self._reconnect_fails += 1
            backoff = min(60.0, 10.0 * self._reconnect_fails)
            self._next_reconnect_ts = now + backoff
            self._log("error", f"重连失败（连续 {self._reconnect_fails} 次）: "
                               f"{str(e).splitlines()[0][:160]}；{backoff:.0f} 秒后再试")
            self._hint_wsa_reconnect()
            return False
        self._log("info", "ADB 已重连" if self._reconnect_fails == 0
                          else f"ADB 已重连（第 {self._reconnect_fails + 1} 次尝试）")
        self._reconnect_fails = 0
        self._next_reconnect_ts = float("-inf")
        return True

    def _hint_wsa_reconnect(self) -> None:
        """WSA/WSABuilds 反复断网时的提示（只提示一次）。"""
        if self._wsa_reconnect_hinted:
            return
        self._wsa_reconnect_hinted = True
        if os.name != "nt":
            return
        self._log("warning",
                  "若使用 WSA / WSABuilds 且经常断网，可先在 WSA 设置里重启子系统"
                  "（或执行 wsa:// 设置里的 Repair），桥接会自动重连 ADB 继续工作")

    def _typed_body(self, text: str) -> str:
        """给转发内容加上**类型标记**，让表情和照片一眼分得开（用户要求）。

        * 表情（贴纸）-> `[表情] 开心`（名字读不到时就是 `[表情]`）
        * 照片（图片消息）-> `[图片]`
        * 文字消息原样不动

        为什么要标：贴纸拿不到原图时会退化成**气泡截图**（一张 png），看起来和照片没区别，
        用户分不清哪条是表情、哪条是照片（实测 15:24 那条）。标记只加在**发出去的文字**上，
        去重/历史的身份仍然是原始 `text`，不会影响判重。
        """
        t = (text or "").strip()
        if not t:
            return t
        if t == getattr(self.xtc, "IMAGE_TEXT", "图片"):
            return "[图片]"
        if t.startswith("表情"):
            name = t[len("表情"):].strip()
            # 有的贴纸名字自带扩展名（实机：'表情弹吉他.png'）—— 显示时去掉
            name = re.sub(r"\.(png|gif|webp|jpe?g|apng)$", "", name, flags=re.I).strip()
            return f"[表情] {name}" if name else "[表情]"
        return t

    def _forward(self, contact, text: str, time_label: str = "",
                 sticker: dict | None = None,
                 only: list | None = None) -> tuple[bool, list, list]:
        """转发到 QQ 目标。返回 (是否全部成功, 成功的目标, 失败的目标)。

        sticker：表情图（{data, kind, animated}）。给了就发"图片（+说明文字）"，
        失败自动退回纯文字。**单向**：只有 小天才 -> QQ 走图片，QQ -> 小天才 依旧是文字。
        only：只发这些目标（补投计划用它**只重试失败的目标**，绝不碰已经成功的那几个）。
        """
        targets = self._qq_targets()
        if not targets:
            self._log("info", f"[占位] 收到小天才消息（未配置 QQ 目标，仅打印）: {text}")
            return True, [], []
        if only is not None:
            wanted = {(t, i) for t, i in only}
            targets = [t for t in targets if t in wanted]
            if not targets:
                return True, [], []
        # 转发格式：[日期时间] [本地配置昵称] 消息内容。
        # 时间优先取小天才 App 内该消息的日期标签（如 "昨天 23:42"、"8月30日"）；
        # 只有时分（当天消息）时补当天日期；无标签时用当前时间。
        time_str = self._format_xtc_time(time_label)
        nickname = self._display_name(contact)
        message = f"[{time_str}] [{nickname}] {self._typed_body(text)}"
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
        sent: list = []
        failed: list = []
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
                sent.append((target_type, target_id))   # 已交给插件，重发会变成两条
                self._log("warning", f"[转发未确认] {target_type}:{target_id} "
                                     "插件刚启动，消息只是排队（未确认已发出）")
                continue
            shown = message + (f"（+{sticker.get('label') or '表情图'} {image_size} 字节）"
                               if image_b64 else "")
            if ok:
                sent.append((target_type, target_id))
                self._log("info", f"[转发成功] {target_type}:{target_id} <- {shown}")
            else:
                failed.append((target_type, target_id))
                self._log("error", f"[转发失败] {target_type}:{target_id} <- {shown}"
                                   + (f"  原因: {why}" if why else ""))
                ok_all = False
        if ok_all and not queued:
            # 标记原文 + 格式化消息：多实例/重启后也不会再转发同一条
            self.echo.mark(text)
            self.echo.mark(message)
            # 小天才侧送达确认（发送成功：<内容>）走**低优先级任务**，不堵住后续发送
            self._queue_confirm_xtc(message)
        elif ok_all and queued:
            # 已交出去但没确认：同样记历史避免重复，但不发"发送成功"（不撒谎）
            self.echo.mark(text)
            self.echo.mark(message)
            self._log("info", "小天才侧送达确认已跳过：本次转发未确认（插件排队中）")
        return ok_all, sent, failed

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
                          + f"（队列中 {self._job_queue.qsize() + self._fast_queue.qsize()}"
                            " 条待处理）")
        # 让轮询先停一轮 dump，把 adb/操作锁让给发送（见 _poll_loop 开头的让路逻辑）
        if not self._send_pending:
            self._send_pending_ts = time.monotonic()
        self._send_pending = True
        # 走**发送专用队列**：工作线程优先处理它，不排在转发/历史/回执后面（见 _next_job）
        self._fast_queue.put(("send", text, user_id, group_id, request_id))
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

    def _push_text_to_xtc(self, text: str, tag: str = "QQ->小天才") -> bool:
        """把一段文字送进小天才聊天窗口（能用"先手打字"就用，失败退回稳妥流程）。

        tag 只用于日志前缀（QQ->小天才 / 送达确认），两条路径共用同一套发送逻辑，
        所以"回执"也能享受先手打字的 ~1 秒速度，而不是每次都走 2~3 次 dump 的稳妥流程。
        """
        contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
        if not contact:
            self._log("error", "反向转发需要 config.yaml -> target.xtc_contact")
            return False
        ok = False
        skip_safe = False
        # ① "先手打字"：直接按缓存坐标点输入框 + 广播注入 + 点发送，
        #    **不等轮询那次 dump、也不抢操作锁** —— 文字 ~1 秒内就出现在输入框里。
        #    复核放在后面（要 dump），失败再退回稳妥流程。
        if self._blind_send_allowed(contact):
            t0 = time.monotonic()
            staged, why = self.xtc.begin_blind_send(text)
            if staged:
                self._log("info", f"[{tag}] 已先手输入（{time.monotonic() - t0:.1f}s），"
                                  "正在复核…")
                with self._op_lock:
                    ok, why, retryable = self.xtc.end_blind_send(text)
                if ok:
                    self._log("info", f"[{tag}] 先手发送已复核通过"
                                      f"（总 {time.monotonic() - t0:.1f}s）")
                else:
                    skip_safe = not retryable
                    self._log("warning" if skip_safe else "info",
                              f"[{tag}] 先手发送未确认（{why}）"
                              + ("，且不宜重发，按失败上报" if skip_safe else "，改用稳妥流程"))
            else:
                self._log("debug", f"[{tag}] 先手输入未启用（{why}），走稳妥流程")
        # ② 稳妥流程（未走先手 / 先手没发出去且可安全重发时）
        if not ok and not skip_safe:
            with self._op_lock:
                in_chat = self.xtc.open_chat(contact)
                ok = in_chat and self.xtc.send_message(text)
            if not in_chat:
                self._log("error", f"[{tag}] 未能进入小天才聊天窗口，未发送")
        return ok

    def _do_send_job(self, text: str, user_id: str, group_id: str, request_id: str) -> None:
        """实际执行 QQ->小天才 发送 + 送达确认（工作线程内，按入队顺序）。"""
        self.echo.mark(text)
        contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
        if not contact:
            self._log("error", "反向转发需要 config.yaml -> target.xtc_contact")
            return
        self._log("info", f"[QQ->小天才] 开始发送: {text[:80]!r}")
        t_all = time.monotonic()
        ok = False
        try:
            ok = self._push_text_to_xtc(text, tag="QQ->小天才")
        except Exception as e:  # noqa: BLE001 单条发送异常不能让工作线程退出
            self._log("warning", f"[QQ->小天才] 发送异常: {e}")
            ok = False
        finally:
            # 队列里还有待发的就继续让路（_send_pending_ts 不刷新，最长 30 秒兜底）
            self._send_pending = not (self._job_queue.empty() and self._fast_queue.empty())
        # 带上总耗时：以后"到底慢在哪一步"看这一行就够了（输入阶段/复核阶段也各有日志）
        self._log("info" if ok else "error",
                  f"[QQ->小天才] {'发送成功' if ok else '发送失败'}"
                  f"（总 {time.monotonic() - t_all:.1f}s）: {text[:80]!r}")
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
        确认消息以"发送成功"开头且为家长侧消息（右侧气泡），读取路径按前缀过滤，不会循环转发。

        这段是**低优先级任务**（见 `_queue_confirm_xtc`）：要往聊天页打字，占界面锁好几秒，
        所以排在用户新发的 QQ 消息后面做；能用先手打字就用（~1 秒）。"""
        if not self._confirm_delivery():
            return
        try:
            contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
            # 能用先手打字时不必先 dump 一次查"在不在聊天页"（门闩已经保证刚确认过聊天页）
            if not self._blind_send_allowed(contact) and not self.xtc.is_in_chat():
                return  # 不在聊天页就不打扰
            confirm_text = "发送成功：" + message
            self.echo.mark(confirm_text)
            if self._push_text_to_xtc(confirm_text, tag="送达确认"):
                self._log("info", f"[送达确认] 已在小天才聊天回复 {confirm_text}")
            else:
                self._log("warning", f"[送达确认] 未能在小天才聊天回复 {confirm_text}")
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
