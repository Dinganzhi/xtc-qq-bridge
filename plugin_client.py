# -*- coding: utf-8 -*-
"""AstrBot 插件客户端（替代 HTTP API）：把消息 POST 到 AstrBot 插件在本机暴露的
端点 /api/forward。纯标准库（urllib），无需 requests。

注意：AstrBot 插件尚未就绪时，config.yaml 的 forward.mode 保持 "log"；
插件就绪后改为 "plugin" 并填写 base_url / token。
"""
from __future__ import annotations

import json
import urllib.request


class PluginClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11452", token: str = "",
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def ping(self) -> bool:
        try:
            req = urllib.request.Request(self.base_url + "/api/ping", method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
                return bool(data.get("ok"))
        except Exception:  # noqa: BLE001
            return False

    def send(self, target_type: str, target_id, message: str) -> bool:
        payload = {
            "target_type": target_type,   # "private" | "group"
            "target_id": str(target_id),
            "message": message,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/api/forward", data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Bridge-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def send_private(self, user_id, message: str) -> bool:
        return self.send("private", user_id, message)

    def send_group(self, group_id, message: str) -> bool:
        return self.send("group", group_id, message)

    def reply_result(self, request_id: str, message: str) -> bool:
        """把命令处理结果回传给插件（插件据此在原会话引用+@ 回复发送人）。"""
        payload = {"request_id": request_id, "message": message}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/api/result", data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Bridge-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False
