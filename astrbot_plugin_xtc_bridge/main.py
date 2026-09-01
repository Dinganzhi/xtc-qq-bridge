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
import urllib.parse
import urllib.request
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
                event.set_result(event.plain_result("⛔ 无权限执行此命令"))
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
            if self.config.get("command_mode", True):
                return  # 命令模式开启：只处理命令
            self._platform_ids.add(event.session.platform_id)
            if not self._is_sender_allowed(event):
                return
            text = (event.message_str or "").strip()
            if not text or text.startswith("/"):
                return  # 跳过命令本身（命令走 on_xtc_command）
            sender_name = event.get_sender_name() or str(event.get_sender_id())
            now = datetime.now().strftime("%m-%d %H:%M")
            message = f"[{now}] [{sender_name}] {text}"
            await self._forward_to_python(self._base_payload(event, message=message))
        except Exception as e:  # noqa: BLE001
            self._lg().exception(f"[xtc_qq_bridge] 全量转发异常: {e}")

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
                             action: str = "", timeout: float = 90) -> str | None:
        """POST 到桥接并挂起等待 /api/result 回传（引用+@ 回复用）。"""
        req_id = self._new_request_id()
        payload = self._base_payload(event, message=message, action=action)
        payload["request_id"] = req_id
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


class _HttpHandler(BaseHTTPRequestHandler):
    """本地端点：
    GET  /api/ping                → 健康检查
    POST /api/forward             → 转发消息 {target_type, target_id, message}（等待实际结果）
    POST /api/result              → 桥接结果回传 {request_id, message}（引用+@ 回复发送人）
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
        if path != "/api/forward":
            self._json({"ok": False, "error": "not found"}, 404)
            return
        if not self._auth():
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        body = self._read_json()
        if body is None:
            self._json({"ok": False, "error": "bad json"}, 400)
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
