# -*- coding: utf-8 -*-
"""小天才 ↔ QQ 桥接插件（AstrBot v4.x，已对照 4.27.4 桌面版源码核对 API）

职责：
1. Python → QQ：暴露本地 HTTP 端点（默认 http://127.0.0.1:11452/api/forward），
   接收 Python 桥脚本（main.py）的转发请求，经 context.send_message() 发送 QQ 消息。
2. QQ → 小天才：只响应命令 `/小天才 <文本>`，把文本按
   `[时间] [QQ发送人昵称] 文本` 格式转发到 Python 桥脚本的 webhook
   （默认 http://127.0.0.1:5000/qq_callback，即 qq_webhook.py）。

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

    # ------------------------------------------------------------------ QQ → 小天才（命令）
    @filter.command("小天才")
    async def on_xtc_command(self, event: AstrMessageEvent, text: GreedyStr) -> None:
        """用法：/小天才 <文本> —— 把文本发到小天才手表。"""
        try:
            self._platform_ids.add(event.session.platform_id)
            if not self._is_sender_allowed(event):
                return
            text = (str(text) if text else "").strip()
            if not text:
                event.set_result(event.plain_result("用法：/小天才 <文本>（把消息发到小天才手表）"))
                return
            sender_name = event.get_sender_name() or str(event.get_sender_id())
            now = datetime.now().strftime("%m-%d %H:%M")
            message = f"[{now}] [{sender_name}] {text}"
            await self._forward_to_python(self._base_payload(event, message=message))
        except Exception as e:  # noqa: BLE001
            self._lg().exception(f"[xtc_qq_bridge] 命令处理异常: {e}")

    @filter.command("小天才登录")
    async def on_xtc_login(self, event: AstrMessageEvent) -> None:
        """用法：/小天才登录 —— 用配置的手机号+密码登录小天才。"""
        try:
            self._platform_ids.add(event.session.platform_id)
            if not self._is_sender_allowed(event):
                return
            await self._forward_to_python(self._base_payload(event, action="login"))
            event.set_result(event.plain_result("正在登录小天才（手机号+密码）..."))
        except Exception as e:  # noqa: BLE001
            self._lg().exception(f"[xtc_qq_bridge] 登录命令处理异常: {e}")

    def _base_payload(self, event: AstrMessageEvent, message: str = "", action: str = "") -> dict:
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
        allow_users = self.config.get("allow_senders") or []
        if allow_users:
            if str(event.get_sender_id()) not in {str(u) for u in allow_users}:
                return False
        allow_groups = self.config.get("allow_groups") or []
        if allow_groups:
            gid = str(event.get_group_id() or "")
            if gid and gid not in {str(g) for g in allow_groups}:
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

    def _on_forward_request(self, target_type: str, target_id: str, text: str) -> None:
        """HTTP 线程调用：尽量切到主事件循环执行发送。"""
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._do_send(target_type, target_id, text), self._loop
            )
        else:
            self._pending.append((target_type, target_id, text))

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
    POST /api/forward             → 转发消息 {target_type, target_id, message}
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

    def do_POST(self) -> None:
        if urllib.parse.urlparse(self.path).path.rstrip("/") != "/api/forward":
            self._json({"ok": False, "error": "not found"}, 404)
            return
        expected = self.plugin.config.get("token", "") if self.plugin else ""
        if expected and self.headers.get("X-Bridge-Token", "") != expected:
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            self._json({"ok": False, "error": "bad json"}, 400)
            return
        target_type = body.get("target_type", "private")
        target_id = str(body.get("target_id", ""))
        text = body.get("message", "")
        if not target_id or not text:
            self._json({"ok": False, "error": "missing target_id/message"}, 400)
            return
        if self.plugin is not None:
            self.plugin._on_forward_request(target_type, target_id, text)
        self._json({"ok": True, "accepted": True})
