# -*- coding: utf-8 -*-
"""反向回调服务（纯标准库 http.server，无需 Flask）：
接收 AstrBot 插件转发的 QQ 消息（或 NapCat OneBot v11 上报），
触发 bridge.forward_to_xiaotiancai() 把消息发到小天才。

关键点：
- HTTP 立即返回（插件侧超时 5s），ADB 操作在后台线程执行，
  避免 ConnectionAbortedError；
- **每一次回调都在控制台留痕**（收到 / 放行 / 拒绝 / 动作），
  解决"收到 QQ 命令但控制台什么都看不到"。

NapCat 直报配置（可选，与插件转发二选一）：
  NapCat 网络设置 -> 新建「HTTP 服务器（事件上报）」->
  上报地址 http://127.0.0.1:5000/qq_callback，方法 POST，
  access_token 填 config.yaml -> webhook.token。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 每个动作的中文名（日志用）
_ACTION_NAMES = {
    "login": "登录",
    "auto_login": "自动登录开关",
    "init": "初始化",
    "history": "历史消息",
}


def _log(logger, level: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    getattr(logger, level, logger.info)(msg)


def _where(user: str, group: str) -> str:
    if group:
        return f"群 {group}"
    if user:
        return f"私聊 {user}"
    return "未知会话"


def create_webhook_server(bridge, host: str = "127.0.0.1", port: int = 5000,
                          path: str = "/qq_callback", token: str = "",
                          logger=None) -> ThreadingHTTPServer:
    route = path.rstrip("/") or "/"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 静默访问日志（我们有自己的业务日志）
            pass

        def _send(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass  # 客户端提前断开不影响处理结果

        def do_GET(self):
            if urlparse(self.path).path.rstrip("/") == "/health":
                self._send(200, b"OK")
            else:
                self._send(404, b"not found")

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path.rstrip("/") != route:
                _log(logger, "warning", f"[QQ回调] 收到未知路径的请求: {self.path}（已忽略）")
                self._send(404, b"not found")
                return
            got = (self.headers.get("X-Bridge-Token", "")
                   or parse_qs(parsed.query).get("access_token", [""])[0])
            if token and got != token:
                _log(logger, "warning", "[QQ回调] token 不匹配，已拒绝（检查 "
                                       "webhook.token 与插件 python_callback_token 是否一致）")
                self._send(401, b"unauthorized")
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                _log(logger, "warning", "[QQ回调] 请求体不是合法 JSON，已忽略")
                self._send(400, b"bad json")
                return

            user = str(data.get("user_id") or "")
            group = str(data.get("group_id") or "")
            action = str(data.get("action") or "")
            request_id = str(data.get("request_id") or "")
            kind = (f"动作={_ACTION_NAMES.get(action, action)}" if action
                    else ("OneBot上报" if data.get("post_type") == "message" else "消息"))
            preview = str(data.get("message") or data.get("raw_message") or "")[:120]
            _log(logger, "info",
                 f"[QQ回调] 收到 {kind} 来源={_where(user, group)}"
                 + (f" 内容={preview!r}" if preview else "")
                 + (f" request_id={request_id}" if request_id else ""))

            message = extract_message(data, bridge, logger)
            if message:
                # 白名单已在 extract_message 内校验（拒绝时会写日志）
                _log(logger, "info", f"[QQ回调] 放行 -> 转发到小天才（来源={_where(user, group)}）")
                threading.Thread(
                    target=bridge.forward_to_xiaotiancai,
                    args=(message, user, group, request_id),
                    daemon=True, name="qq-to-xtc",
                ).start()
                self._send(200, b"OK")
                return
            if data.get("post_type") == "message" or data.get("source") == "astrbot":
                _log(logger, "info",
                     f"[QQ回调] 未转发（空消息或来源不在白名单）：来源={_where(user, group)}")
                self._send(200, b"IGNORED")
                return

            if action:
                if not bridge.qq_sender_allowed(user, group):
                    _log(logger, "warning",
                         f"[QQ回调] 已拒绝：来源={_where(user, group)} 不在白名单"
                         "（webhook.allow_from / allow_groups）")
                    self._send(200, b"IGNORED")
                    return
                if action == "login":
                    threading.Thread(
                        target=bridge.login_xiaotiancai, args=(request_id,),
                        daemon=True, name="xtc-login",
                    ).start()
                    self._send(200, b"OK")
                elif action == "auto_login":
                    bridge.toggle_auto_login(request_id)
                    self._send(200, b"OK")
                elif action == "init":
                    threading.Thread(
                        target=bridge.init_xiaotiancai, args=(request_id,),
                        daemon=True, name="xtc-init",
                    ).start()
                    self._send(200, b"OK")
                elif action == "history":
                    try:
                        count = int(data.get("history_count") or 20)
                    except (TypeError, ValueError):
                        count = 20
                    src = str(data.get("history_source") or "").strip()
                    threading.Thread(
                        target=bridge.fetch_xtc_history,
                        args=(count, request_id, False, src),
                        daemon=True, name="xtc-history",
                    ).start()
                    self._send(200, b"OK")
                else:
                    _log(logger, "warning", f"[QQ回调] 未知动作: {action}（已忽略）")
                    self._send(200, b"IGNORED")
                return

            _log(logger, "info", "[QQ回调] 事件不含要转发的消息（已忽略）")
            self._send(200, b"IGNORED")

    return ThreadingHTTPServer((host, port), Handler)


def extract_message(data: dict, bridge, logger=None):
    """解析 OneBot v11 事件或插件转发 JSON，做白名单校验；
    返回要发到小天才的文本，不通过返回 None。

    注意：动作类请求（login/init/...）返回 None，由调用方按 action 分派。
    """
    if data.get("post_type") == "message":
        # OneBot v11 HTTP 上报
        msg = data.get("raw_message") or ""
        user = str(data.get("user_id") or "")
        group = str(data.get("group_id") or "")
        if not bridge.qq_sender_allowed(user, group):
            return None
        return msg or None
    if data.get("source") == "astrbot":
        # AstrBot 插件转发格式
        msg = data.get("message") or ""
        user = str(data.get("user_id") or "")
        group = str(data.get("group_id") or "")
        if not bridge.qq_sender_allowed(user, group):
            return None
        return msg or None
    return None


if __name__ == "__main__":
    # 独立调试：python qq_webhook.py
    import sys

    class _Fake:
        cfg = {"target": {"qq_private": "123"}, "webhook": {}}

        def qq_sender_allowed(self, qq, group):
            return True

        def forward_to_xiaotiancai(self, text):
            print(f"[假转发] {text}")
            return True

    srv = create_webhook_server(_Fake(), token="")
    print("测试服务运行中: http://127.0.0.1:5000/qq_callback （Ctrl+C 退出）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
