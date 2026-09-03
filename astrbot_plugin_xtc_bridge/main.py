# -*- coding: utf-8 -*-
"""小天才 ↔ QQ 桥接插件（AstrBot v4.x，已对照 4.27.4 桌面版源码核对 API）

职责：
1. Python → QQ：暴露本地 HTTP 端点（默认 http://127.0.0.1:11452/api/forward），
   接收 Python 桥脚本（main.py）的转发请求，经 context.send_message() 发送 QQ 消息。
   /api/forward 会等待实际发送结果再返回（避免"假成功"）。
2. QQ → 小天才：子命令 `/小天才 发送|登录|自动登录|初始化|命令模式`；
   「命令模式」关闭时，群/私聊所有新消息都会转发到小天才。

关键 API（v4）：
- Star 基类：继承 star.Star，__init__(context, config)，async initialize()/terminate()
- 命令过滤：@filter.command("小天才")，剩余文本用 GreedyStr 接收
- 主动发送：await self.context.send_message("平台ID:消息类型:会话ID", MessageChain)
  其中 消息类型 = FriendMessage（私聊）/ GroupMessage（群聊），平台 ID 可用 /sid 查看
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

# AstrBot < 4.27 的 Star.__init__ 不注入 self.logger，这里做兜底
_FALLBACK_LOGGER = logging.getLogger("xtc_qq_bridge")

_USAGE = ("用法：\n"
          "/小天才 发送 <文本>    把消息发到小天才手表\n"
          "/小天才 登录          手动登录小天才\n"
          "/小天才 自动登录      开启/关闭十分钟自动登录检测\n"
          "/小天才 初始化        检测并恢复界面状态（登录/聊天/输入框）\n"
          "/小天才 历史消息 <条数>  查看小天才最近对话记录（1-100，默认20）\n"
          "/小天才 命令模式      切换命令模式（开=仅命令转发；关=所有新消息都转发）")


class Main(star.Star):
    def __init__(self, context: star.Context, config=None) -> None:
        super().__init__(context)
        self.config = config or {}
        if not getattr(self, "logger", None):
            try:
                self.logger = _FALLBACK_LOGGER
            except AttributeError:  # 只读属性时保持原样
                pass
        self._httpd: ThreadingHTTPServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: list[tuple[str, str, str]] = []  # (target_type, target_id, text)
        self._platform_ids: set[str] = set()  # 最近收到过消息的平台 ID
        # 待回传结果：request_id -> Future；/api/result 到达后由原事件引用+@ 回复发送人
        self._pending_replies: dict[str, asyncio.Future] = {}
        # QQ 活动记录（在线人数/搜索 的"最后消息时间"数据源）：(ts, kind, session_id, user_id, name)
        # 任何 QQ 消息事件都会记录（含命令模式开启时），查询时按白名单范围过滤。
        self._activity: deque = deque(maxlen=2000)

    def _lg(self):
        """跨版本安全的日志器访问。"""
        return getattr(self, "logger", None) or _FALLBACK_LOGGER

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        """插件激活时：捕获主事件循环并启动本地 HTTP 端点。"""
        self._loop = asyncio.get_running_loop()
        self._start_http()
        self._flush_pending()
        self._lg().info(
            "[xtc_qq_bridge] 插件已启动。本地端点: http://%s:%s/api/forward",
            self.config.get("http_host", "127.0.0.1"),
            self.config.get("http_port", 11452),
        )

    async def terminate(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._lg().info("[xtc_qq_bridge] 插件已停止")

    # ------------------------------------------------------------------ 命令
    @filter.command("小天才")
    async def on_xtc_command(self, event: AstrMessageEvent, args: GreedyStr) -> None:
        """子命令：发送 / 登录 / 自动登录 / 初始化 / 命令模式。"""
        try:
            self._platform_ids.add(event.session.platform_id)
            if not self._is_sender_allowed(event):
                event.set_result(event.plain_result("无权限执行此命令（不在白名单）"))
                return
            raw = (str(args) if args else "").strip()
            if not raw:
                event.set_result(event.plain_result(_USAGE))
                return
            sub, _, rest = raw.partition(" ")
            rest = rest.strip()
            if sub in ("发送", "send"):
                if not rest:
                    event.set_result(event.plain_result("用法：/小天才 发送 <文本>"))
                    return
                await self._send_flow(event, rest)
            elif sub in ("登录", "login"):
                await self._login_flow(event)
            elif sub == "自动登录":
                await self._toggle_flow(event, "auto_login")
            elif sub == "初始化":
                await self._toggle_flow(event, "init")
            elif sub in ("历史消息", "history"):
                await self._history_flow(event, rest)
            elif sub == "命令模式":
                self._toggle_command_mode(event)
            else:
                event.set_result(event.plain_result(_USAGE))
        except Exception as e:  # noqa: BLE001
            self._lg().exception(f"[xtc_qq_bridge] 命令处理异常: {e}")

    @filter.command("小天才登录")
    async def on_xtc_login_alias(self, event: AstrMessageEvent) -> None:
        """旧命令别名：/小天才登录 = /小天才 登录"""
        await self.on_xtc_command(event, GreedyStr("登录"))

    # ------------------------------------------------------------------ 命令模式关闭时：所有消息转发
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent) -> None:
        try:
            self._platform_ids.add(event.session.platform_id)
            # 机器人自己发出去的消息（NapCat 可能回环）不处理：不转发、不记录活动
            if self._is_self_message(event):
                return
            # 无论命令模式开/关，都记录 QQ 活动（xtc 侧 搜索/在线人数 的数据源）
            self._record_activity(event)
            if self.config.get("command_mode", True):
                return  # 命令模式开启：只处理命令（活动记录不受影响）
            if not self._is_sender_allowed(event):
                return
            text = (event.message_str or "").strip()
            if not text or text.startswith("/"):
                return  # 跳过命令本身（命令走 on_xtc_command）
            if self._looks_like_cmd(text):
                return  # @机器人/无斜杠形式的命令文本（如"小天才 命令模式"）不转发
            sender_name = event.get_sender_name() or str(event.get_sender_id())
            now = datetime.now().strftime("%m-%d %H:%M")
            message = f"[{now}] [{sender_name}] {text}"
            await self._forward_to_python(self._base_payload(event, message=message))
        except Exception as e:  # noqa: BLE001
            self._lg().exception(f"[xtc_qq_bridge] 全量转发异常: {e}")

    def _is_self_message(self, event: AstrMessageEvent) -> bool:
        """消息是否机器人自己发出（部分平台会回环自己发过的消息）。"""
        try:
            self_id = event.get_self_id()
            return bool(self_id) and str(event.get_sender_id() or "") == str(self_id)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _looks_like_cmd(text: str) -> bool:
        """判断 QQ 文本是否形如对小天才机器人的命令调用（含去斜杠 / @提及 形式），
        用于防止命令文本在命令模式关闭时被当作普通消息转发。"""
        t = text.strip()
        while t.startswith("/"):
            t = t[1:].lstrip()
        if t.startswith("@") and " " in t:  # 去掉开头的 @机器人
            t = t.split(" ", 1)[1].strip()
        parts = t.split()
        if not parts or parts[0] != "小天才":
            return False
        subs = {"发送", "登录", "自动登录", "初始化", "命令模式", "历史消息",
                "搜索", "在线人数", "提醒", "帮助", "小天才登录", "help", "发送登录"}
        if len(parts) == 1:
            return True  # 单独"小天才"= 帮助
        if parts[1] in subs:
            return True
        return False

    def _record_activity(self, event: AstrMessageEvent) -> None:
        """记录一条 QQ 消息活动：(时间, 私聊/群聊, 会话ID, 用户ID, 昵称)。"""
        try:
            uid = str(event.get_sender_id() or "")
            if not uid:
                return
            gid = str(event.get_group_id() or "")
            kind = "group" if gid else "private"
            sid = gid if gid else uid
            name = (event.get_sender_name() or "").strip() or uid
            self._activity.append((time.time(), kind, sid, uid, name))
        except Exception:  # noqa: BLE001 记录失败不影响主流程
            pass

    # ------------------------------------------------------------------ 各子命令实现
    async def _send_flow(self, event: AstrMessageEvent, content: str) -> None:
        """/小天才 发送 <文本>：发送并等待桥接结果，引用+@ 回复。"""
        sender_name = event.get_sender_name() or str(event.get_sender_id())
        now = datetime.now().strftime("%m-%d %H:%M")
        message = f"[{now}] [{sender_name}] {content}"
        result = await self._post_and_wait(event, message=message, timeout=90)
        if result:
            event.set_result(event.plain_result(result))

    async def _login_flow(self, event: AstrMessageEvent) -> None:
        """/小天才 登录：先发"正在登录"，结果回来再引用+@ 回复。"""
        try:
            await event.send(MessageChain().message("正在登录小天才（手机号+密码）..."))
        except Exception:  # noqa: BLE001 部分平台不支持中途发送
            pass
        result = await self._post_and_wait(event, action="login", timeout=120)
        if result:
            event.set_result(event.plain_result(result))

    async def _toggle_flow(self, event: AstrMessageEvent, action: str) -> None:
        """/小天才 自动登录 / 初始化：POST 给桥接，等待结果回复。"""
        result = await self._post_and_wait(event, action=action, timeout=120)
        if result:
            event.set_result(event.plain_result(result))

    async def _history_flow(self, event: AstrMessageEvent, args: str) -> None:
        """/小天才 历史消息 <条数>：让桥接读取小天才最近对话并回传。

        短内容（≤1300 字符）用引用+@ 回复；超长内容直接以纯文本发送到原会话——
        AstrBot 会把超过 forward_threshold(默认1500) 的纯文本回复包成"合并转发
        Node"（uin=机器人自己），NapCat 会报错或把内容"发给机器人自己"。"""
        count = 20
        if args:
            try:
                count = int(args)
            except ValueError:
                event.set_result(event.plain_result("用法：/小天才 历史消息 <条数>（1-100，默认20）"))
                return
        if not 1 <= count <= 100:
            event.set_result(event.plain_result("条数需在 1-100 之间"))
            return
        result = await self._post_and_wait(event, action="history",
                                           extra={"history_count": count}, timeout=150)
        if not result:
            return
        if len(result) > 1300:
            await self._send_plain_to_session(event, result)
        else:
            event.set_result(event.plain_result(result))

    async def _send_plain_to_session(self, event: AstrMessageEvent, text: str) -> None:
        """直接向事件所在会话发送纯文本（绕过 result_decorate 的合并转发转换）。"""
        platform = event.session.platform_id
        if event.get_group_id():
            session = f"{platform}:GroupMessage:{event.get_group_id()}"
        else:
            session = f"{platform}:FriendMessage:{event.get_sender_id()}"
        try:
            ok = await self.context.send_message(session, MessageChain().message(text))
            if not ok:
                self._lg().error("[xtc_qq_bridge] 历史消息纯文本发送失败（会话/平台不可用）")
        except Exception as e:  # noqa: BLE001
            self._lg().exception(f"[xtc_qq_bridge] 历史消息纯文本发送异常: {e}")

    def _toggle_command_mode(self, event: AstrMessageEvent) -> None:
        """/小天才 命令模式：本地切换（写回插件配置）。"""
        new_val = not bool(self.config.get("command_mode", True))
        self.config["command_mode"] = new_val
        try:
            self.config.save_config()
        except Exception:  # noqa: BLE001 持久化失败不影响运行
            pass
        state = "开启（仅命令转发）" if new_val else "关闭（所有新消息都转发）"
        self._lg().info(f"[xtc_qq_bridge] 命令模式已切换为 {state}")
        event.set_result(event.plain_result(f"命令模式已{state}"))

    async def _post_and_wait(self, event: AstrMessageEvent, message: str = "",
                             action: str = "", timeout: float = 90,
                             extra: dict | None = None) -> str | None:
        """POST 到桥接并挂起等待 /api/result 回传（引用+@ 回复用）。
        extra: 额外字段（如 history_count）合并进 payload。"""
        req_id = self._new_request_id()
        payload = self._base_payload(event, message=message, action=action)
        payload["request_id"] = req_id
        if extra:
            payload.update(extra)
        await self._forward_to_python(payload)
        fut = asyncio.get_running_loop().create_future()
        self._pending_replies[req_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending_replies.pop(req_id, None)

    @staticmethod
    def _new_request_id() -> str:
        import uuid
        return uuid.uuid4().hex[:12]

    def _resolve_request(self, request_id: str, result: str) -> None:
        """HTTP 线程调用：把桥接结果交给挂起的命令处理器。"""
        if self._loop is not None and self._loop.is_running():
            fut = self._pending_replies.get(request_id)
            if fut is not None and not fut.done():
                self._loop.call_soon_threadsafe(fut.set_result, result)

    def _base_payload(self, event: AstrMessageEvent, message: str = "",
                      action: str = "") -> dict:
        payload = {
            "source": "astrbot",
            "user_id": str(event.get_sender_id()),
            "group_id": str(event.get_group_id() or ""),
        }
        if message:
            payload["message"] = message
        if action:
            payload["action"] = action
        return payload

    def _is_sender_allowed(self, event: AstrMessageEvent) -> bool:
        """群消息按群白名单（allow_groups）判断，私聊按用户白名单（allow_senders）判断；
        对应列表为空 = 不限制（最终闸门在桥侧 webhook 白名单）。"""
        gid = str(event.get_group_id() or "")
        uid = str(event.get_sender_id())
        if gid:
            allow_groups = self.config.get("allow_groups") or []
            if allow_groups and gid not in {str(g) for g in allow_groups}:
                return False
        else:
            allow_users = self.config.get("allow_senders") or []
            if allow_users and uid not in {str(u) for u in allow_users}:
                return False
        return True

    async def _forward_to_python(self, payload: dict) -> None:
        url = self.config.get(
            "python_callback_url", "http://127.0.0.1:5000/qq_callback"
        )
        token = self.config.get("python_callback_token", "")
        try:
            await asyncio.to_thread(self._http_post, url, payload, token)
        except Exception as e:  # noqa: BLE001
            self._lg().warning(f"[xtc_qq_bridge] 转发到 Python 桥失败: {e}")

    @staticmethod
    def _http_post(url: str, payload: dict, token: str) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Bridge-Token"] = token
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()

    # ------------------------------------------------------------------ Python → QQ
    def _start_http(self) -> None:
        host = self.config.get("http_host", "127.0.0.1")
        port = int(self.config.get("http_port", 11452))
        handler = type("XtcBridgeHandler", (_HttpHandler,), {"plugin": self})
        self._httpd = ThreadingHTTPServer((host, port), handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def _on_forward_request(self, target_type: str, target_id: str, text: str):
        """HTTP 线程调用：切到主事件循环发送，返回 Future 供等待实际结果。
        未初始化（无事件循环）时入队并返回 None。"""
        if self._loop is not None and self._loop.is_running():
            return asyncio.run_coroutine_threadsafe(
                self._do_send(target_type, target_id, text), self._loop
            )
        self._pending.append((target_type, target_id, text))
        return None

    def _flush_pending(self) -> None:
        if self._loop is None:
            return
        for item in self._pending:
            asyncio.run_coroutine_threadsafe(self._do_send(*item), self._loop)
        self._pending.clear()

    async def _do_send(self, target_type: str, target_id: str, text: str) -> bool:
        platform = self.config.get("platform_id") or ""
        if not platform and self._platform_ids:
            platform = next(iter(self._platform_ids))
        if not platform:
            self._lg().error(
                "[xtc_qq_bridge] 无法确定平台 ID：请先让 QQ 给机器人发一条消息，"
                "或在插件配置里填写 platform_id（可让机器人执行 /sid 查看）"
            )
            return False
        msg_type = "GroupMessage" if target_type == "group" else "FriendMessage"
        session = f"{platform}:{msg_type}:{target_id}"
        chain = MessageChain().message(text)
        try:
            ok = await self.context.send_message(session, chain)
            if not ok:
                self._lg().error(f"[xtc_qq_bridge] 发送失败：未找到平台 {platform}")
            return ok
        except Exception as e:  # noqa: BLE001
            self._lg().exception(f"[xtc_qq_bridge] 发送异常: {e}")
            return False

    # ------------------------------------------------------------------ QQ 数据服务（xtc 侧命令用）
    # 说明：通过 aiocqhttp 适配器实例的 .bot（CQHttp）调 OneBot v11 API。
    # 插件在事件里记录活动日志；桥接在小天才聊天里执行 搜索/在线人数/提醒 时
    # 通过本地 HTTP 端点访问这些数据。所有调用在主事件循环执行并限时。

    def _pick_platform_id(self) -> str:
        pid = (self.config.get("platform_id") or "").strip()
        if not pid and self._platform_ids:
            pid = next(iter(self._platform_ids))
        return pid

    def _get_bot(self):
        """取 aiocqhttp 适配器实例上的 CQHttp bot（None = 未就绪）。"""
        pid = self._pick_platform_id()
        if not pid:
            return None
        try:
            inst = self.context.get_platform_inst(pid)
        except Exception:  # noqa: BLE001
            return None
        if inst is None:
            return None
        return getattr(inst, "bot", None)

    def _run_qq_query(self, coro_factory, timeout: float = 30.0) -> dict:
        """HTTP 线程调用：把异步查询投递到主事件循环并等待结果。"""
        if self._loop is None or not self._loop.is_running():
            return {"ok": False, "error": "插件尚未初始化（AstrBot 事件循环未就绪）"}
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
        try:
            result = fut.result(timeout=timeout)
            return result if isinstance(result, dict) else {"ok": False, "error": "返回类型错误"}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "QQ 查询超时"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"QQ 查询异常: {e}"}

    def qq_search_request(self, keyword: str, allow_from: list, allow_groups: list,
                          limit: int) -> dict:
        return self._run_qq_query(
            lambda: self._qq_search_async(keyword, allow_from, allow_groups, limit))

    def qq_online_request(self, minutes: int, allow_from: list, allow_groups: list) -> dict:
        return self._run_qq_query(
            lambda: self._qq_online_async(minutes, allow_from, allow_groups))

    def qq_remind_request(self, group_id: str, qq_id: str, text: str) -> dict:
        return self._run_qq_query(
            lambda: self._qq_remind_async(group_id, qq_id, text), timeout=20.0)

    def _last_ts(self, kind: str, session_id: str, user_id: str) -> float | None:
        """活动日志里某用户在某会话的最后发言时间（秒）。"""
        for ts, k, sid, uid, _name in reversed(self._activity):
            if k == kind and sid == session_id and uid == user_id:
                return ts
        return None

    async def _qq_search_async(self, keyword: str, allow_from: list, allow_groups: list,
                               limit: int) -> dict:
        bot = self._get_bot()
        if bot is None:
            return {"ok": False, "error": "未获取到 QQ 平台（请先让 QQ 给机器人发一条消息）"}
        kw = (keyword or "").strip().lower()
        if not kw:
            return {"ok": False, "error": "缺少昵称关键词"}
        people: list[dict] = []

        async def call(action: str, **params) -> list | dict:
            try:
                return await asyncio.wait_for(
                    bot.call_action(action, **params), timeout=15)
            except Exception:  # noqa: BLE001
                return []

        # 私聊范围 = 白名单好友
        if allow_from:
            friends = await call("get_friend_list")
            for f in friends or []:
                uid = str((f or {}).get("user_id") or "")
                if not uid or uid not in set(allow_from):
                    continue
                name = ((f or {}).get("remark") or (f or {}).get("nickname") or "").strip()
                if not name or kw not in name.lower():
                    continue
                ts = self._last_ts("private", uid, uid)
                people.append({"scope": "private", "session_id": uid, "session_name": "",
                               "qq": uid, "name": name, "last_ts": ts})
                if len(people) >= limit:
                    break
        # 群聊范围 = 白名单群的成员
        for gid in allow_groups:
            if len(people) >= limit:
                break
            gname = ""
            info = await call("get_group_info", group_id=int(gid))
            if isinstance(info, dict):
                gname = (info.get("group_name") or "").strip()
            members = await call("get_group_member_list", group_id=int(gid))
            for m in members or []:
                if len(people) >= limit:
                    break
                uid = str((m or {}).get("user_id") or "")
                if not uid:
                    continue
                name = ((m or {}).get("card") or (m or {}).get("nickname") or "").strip()
                if not name or kw not in name.lower():
                    continue
                ts = self._last_ts("group", str(gid), uid)
                people.append({"scope": "group", "session_id": str(gid),
                               "session_name": gname, "qq": uid, "name": name, "last_ts": ts})
        return {"ok": True, "people": people, "limit": limit}

    async def _qq_online_async(self, minutes: int, allow_from: list,
                               allow_groups: list) -> dict:
        bot = self._get_bot()
        window = time.time() - max(1, min(int(minutes), 60)) * 60
        allow_from_set = set(allow_from)
        allow_groups_set = set(allow_groups)
        active_uids: set[str] = set()
        private_uids: set[str] = set()
        group_uids: dict[str, set[str]] = {g: set() for g in allow_groups_set}
        for ts, kind, sid, uid, _name in self._activity:
            if ts < window:
                continue
            if kind == "private":
                if uid in allow_from_set:
                    active_uids.add(uid)
                    private_uids.add(uid)
            elif kind == "group" and sid in allow_groups_set:
                active_uids.add(uid)
                group_uids.setdefault(sid, set()).add(uid)
        sessions: list[dict] = []
        if private_uids:
            sessions.append({"kind": "private", "session_id": "", "session_name": "",
                             "count": len(private_uids)})
        for gid in allow_groups:
            uids = group_uids.get(gid) or set()
            if not uids:
                continue
            gname = str(gid)
            if bot is not None:
                try:
                    info = await asyncio.wait_for(
                        bot.call_action("get_group_info", group_id=int(gid)), timeout=8)
                    if isinstance(info, dict) and info.get("group_name"):
                        gname = str(info["group_name"])
                except Exception:  # noqa: BLE001 拿不到群名就用群号
                    pass
            sessions.append({"kind": "group", "session_id": str(gid),
                             "session_name": gname, "count": len(uids)})
        return {"ok": True, "total": len(active_uids), "sessions": sessions}

    async def _qq_remind_async(self, group_id: str, qq_id: str, text: str) -> dict:
        """在群内 @ 提醒（不校验成员身份，交由 NapCat 返回错误）。"""
        if not (str(group_id).isdigit() and str(qq_id).isdigit()):
            return {"ok": False, "error": "群号与QQID必须为数字"}
        platform = self._pick_platform_id()
        if not platform:
            return {"ok": False, "error": "未获取到 QQ 平台（请先让 QQ 给机器人发一条消息）"}
        session = f"{platform}:GroupMessage:{group_id}"
        try:
            chain = MessageChain().at("", qq_id)
            if (text or "").strip():
                chain.message(text)
            ok = await self.context.send_message(session, chain)
            if ok:
                return {"ok": True}
            return {"ok": False, "error": "发送失败（群号错误或机器人不在该群？）"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"发送异常: {e}"}


class _HttpHandler(BaseHTTPRequestHandler):
    """本地端点：
    GET  /api/ping                → 健康检查
    POST /api/forward             → 转发消息 {target_type, target_id, message}（等待实际结果）
    POST /api/result              → 桥接结果回传 {request_id, message}（引用+@ 回复发送人）
    POST /api/qq_search           → xtc 侧「搜索」：白名单私聊/群聊按昵称搜人
    POST /api/qq_online           → xtc 侧「在线人数」：最近N分钟白名单会话发言人数
    POST /api/qq_remind           → xtc 侧「提醒」：在群内 @ 某 QQ 用户
    """

    plugin: Main | None = None

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _json(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if urllib.parse.urlparse(self.path).path.rstrip("/") == "/api/ping":
            self._json({"ok": True, "plugin": "xtc_qq_bridge"})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def _auth(self) -> bool:
        expected = self.plugin.config.get("token", "") if self.plugin else ""
        return not expected or self.headers.get("X-Bridge-Token", "") == expected

    def _read_json(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return None

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path == "/api/result":
            if not self._auth():
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            body = self._read_json()
            if body is None:
                self._json({"ok": False, "error": "bad json"}, 400)
                return
            request_id = body.get("request_id", "")
            message = body.get("message", "")
            if self.plugin is not None and request_id:
                self.plugin._resolve_request(str(request_id), str(message))
            self._json({"ok": True})
            return
        if path != "/api/forward" and path not in (
                "/api/qq_search", "/api/qq_online", "/api/qq_remind"):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        if not self._auth():
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        body = self._read_json()
        if body is None:
            self._json({"ok": False, "error": "bad json"}, 400)
            return
        if self.plugin is None:
            self._json({"ok": False, "error": "plugin not ready"}, 500)
            return
        if path == "/api/qq_search":
            result = self.plugin.qq_search_request(
                str(body.get("keyword", "")),
                body.get("allow_from") or [],
                body.get("allow_groups") or [],
                int(body.get("limit") or 30))
            self._json(result)
            return
        if path == "/api/qq_online":
            result = self.plugin.qq_online_request(
                int(body.get("minutes") or 10),
                body.get("allow_from") or [],
                body.get("allow_groups") or [])
            self._json(result)
            return
        if path == "/api/qq_remind":
            result = self.plugin.qq_remind_request(
                str(body.get("group_id", "")),
                str(body.get("qq_id", "")),
                str(body.get("text", "")))
            self._json(result)
            return
        target_type = body.get("target_type", "private")
        target_id = str(body.get("target_id", ""))
        text = body.get("message", "")
        if not target_id or not text:
            self._json({"ok": False, "error": "missing target_id/message"}, 400)
            return
        if self.plugin is not None:
            # 等待实际发送结果再返回，避免"假成功"（发送失败时桥侧会重试）
            future = self.plugin._on_forward_request(target_type, target_id, text)
            if future is not None:
                try:
                    ok = bool(future.result(timeout=30))
                except Exception:  # noqa: BLE001 超时/异常视为失败
                    ok = False
                self._json({"ok": ok, "accepted": ok})
                return
        self._json({"ok": True, "accepted": True})
