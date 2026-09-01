# -*- coding: utf-8 -*-
"""反向回调服务（纯标准库 http.server，无需 Flask）：
接收 AstrBot 插件转发的 QQ 消息（或 NapCat OneBot v11 上报），
触发 bridge.forward_to_xiaotiancai() 把消息发到小天才。

关键点：HTTP 立即返回（插件侧超时 5s），ADB 操作在后台线程执行，
避免 ConnectionAbortedError。

NapCat 直报配置（可选，与插件转发二选一）：
  NapCat 网络设置 → 新建「HTTP 服务器（事件上报）」→
  上报地址 http://127.0.0.1:5000/qq_callback，方法 POST，
  access_token 填 config.yaml → webhook.token。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def create_webhook_server(bridge, host: str = "127.0.0.1", port: int = 5000,
                          path: str = "/qq_callback", token: str = "",
                          logger=None) -> ThreadingHTTPServer:
    route = path.rstrip("/") or "/"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 静默访问日志
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
                self._send(404, b"not found")
                return
            got = (self.headers.get("X-Bridge-Token", "")
                   or parse_qs(parsed.query).get("access_token", [""])[0])
            if token and got != token:
                self._send(401, b"unauthorized")
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                self._send(400, b"bad json")
                return

            message = extract_message(data, bridge, logger)
            if message:
                # 立即返回，ADB 发送放后台线程（ADB 操作可能耗时数秒）
                user = str(data.get("user_id") or "")
                group = str(data.get("group_id") or "")
                request_id = str(data.get("request_id") or "")
                threading.Thread(
                    target=bridge.forward_to_xiaotiancai,
                    args=(message, user, group, request_id),
                    daemon=True, name="qq-to-xtc",
                ).start()
                self._send(200, b"OK")
            elif data.get("action") == "login":
                # /小天才登录 命令：执行账密登录（后台线程），结果回传插件引用+@ 回复
                user = str(data.get("user_id") or "")
                group = str(data.get("group_id") or "")
                if bridge.qq_sender_allowed(user, group):
                    request_id = str(data.get("request_id") or "")
                    threading.Thread(
                        target=bridge.login_xiaotiancai, args=(request_id,),
                        daemon=True, name="xtc-login",
                    ).start()
                    self._send(200, b"OK")
                else:
                    self._send(200, b"IGNORED")
            else:
                self._send(200, b"IGNORED")

    return ThreadingHTTPServer((host, port), Handler)


def extract_message(data: dict, bridge, logger=None):
    """解析 OneBot v11 事件或插件转发 JSON，做白名单校验；
    返回要发到小天才的文本，不通过返回 None。"""
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
