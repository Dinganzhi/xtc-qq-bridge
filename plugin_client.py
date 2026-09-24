# -*- coding: utf-8 -*-
"""AstrBot 插件客户端（替代 HTTP API）：把消息 POST 到 AstrBot 插件在本机暴露的
端点 /api/forward。纯标准库（urllib），无需 requests。

注意：AstrBot 插件尚未就绪时，config.yaml 的 forward.mode 保持 "log"；
插件就绪后改为 "plugin" 并填写 base_url / token。

**超时说明**：插件要等 QQ 侧真实发送结果才回包（避免"假成功"），最长 30 秒；
所以这里的超时必须比它长——早先默认 10 秒，会把"发得慢但成功"误判成失败
（桥接还会因此重试，造成重复消息）。
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request


class PluginClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11452", token: str = "",
                 timeout: float = 35.0):
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
        return self.send_detail(target_type, target_id, message)[0]

    def send_detail(self, target_type: str, target_id, message: str) -> tuple:
        """发送并返回 (是否成功, 失败原因)。

        为什么要返回原因：早先失败只报"插件未启动？检查 http_port/token"，
        但插件明明是好的、真正的原因是 QQ 侧（NapCat/QQNT）发不出去——
        错误信息把人带偏了。这里把 HTTP 状态码、插件返回的 error、以及
        连接/超时的真实异常都带出来。
        """
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
                body = r.read().decode("utf-8", errors="replace")
                if r.status != 200:
                    return False, f"插件返回 HTTP {r.status}: {body[:200]}"
                try:
                    obj = json.loads(body) if body.strip() else {}
                except ValueError:
                    return False, f"插件返回的不是 JSON: {body[:200]}"
                if obj.get("ok") is False or obj.get("accepted") is False:
                    return False, f"QQ 侧发送失败: {obj.get('error') or obj or body[:200]}"
                if obj.get("queued"):
                    # 插件刚起来、事件循环还没跑：消息只是排队，**没有真的发出去**
                    return True, "queued"
                return True, ""
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            if e.code == 401:
                return False, ("插件拒绝：token 不匹配（检查 config.yaml 的 "
                               "forward.plugin.token 与插件配置里的 token）")
            return False, f"插件返回 HTTP {e.code}: {detail}"
        except (socket.timeout, TimeoutError):
            return False, (f"等插件回包超时（{self.timeout:.0f}s）：插件要等 QQ 侧真实发送结果，"
                           "说明 QQ/NapCat 那边卡住了（重启 NapCat/QQ 通常即可）")
        except urllib.error.URLError as e:
            return False, (f"连不上插件 {self.base_url}（{e.reason}）：确认 AstrBot 已启动且"
                           "插件已加载（http_port/token 见插件配置）")
        except Exception as e:  # noqa: BLE001
            return False, f"转发异常: {type(e).__name__}: {e}"

    def send_private(self, user_id, message: str) -> bool:
        return self.send("private", user_id, message)

    def send_group(self, group_id, message: str) -> bool:
        return self.send("group", group_id, message)

    def send_image(self, target_type: str, target_id, image_b64: str,
                   caption: str = "") -> tuple:
        """发一张图片（base64 PNG）给 QQ，可带一句说明文字。返回 (是否成功, 失败原因)。

        用途：小天才 -> QQ 的**表情包单向转发**（贴纸按气泡位置截图后发过去）。
        走同一个 /api/forward 端点，多带一个 image_b64 字段；失败时调用方会退回发文字。
        """
        payload = {
            "target_type": target_type,   # "private" | "group"
            "target_id": str(target_id),
            "message": caption or "",
            "image_b64": image_b64,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/api/forward", data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Bridge-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
                if r.status != 200:
                    return False, f"插件返回 HTTP {r.status}: {body[:200]}"
                try:
                    obj = json.loads(body) if body.strip() else {}
                except ValueError:
                    return False, f"插件返回的不是 JSON: {body[:200]}"
                if obj.get("ok") is False or obj.get("accepted") is False:
                    return False, f"QQ 侧发送失败: {obj.get('error') or obj or body[:200]}"
                if obj.get("queued"):
                    return True, "queued"
                return True, ""
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            if e.code == 401:
                return False, ("插件拒绝：token 不匹配（检查 config.yaml 的 "
                               "forward.plugin.token 与插件配置里的 token）")
            if e.code == 404:
                return False, "插件没有图片端点（AstrBot 插件版本过旧，请更新插件）"
            return False, f"插件返回 HTTP {e.code}: {detail}"
        except (socket.timeout, TimeoutError):
            return False, (f"等插件回包超时（{self.timeout:.0f}s）：QQ/NapCat 那边可能卡住了")
        except urllib.error.URLError as e:
            return False, (f"连不上插件 {self.base_url}（{e.reason}）：确认 AstrBot 已启动且"
                           "插件已加载（http_port/token 见插件配置）")
        except Exception as e:  # noqa: BLE001
            return False, f"转发异常: {type(e).__name__}: {e}"

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

    # ------------------------------------------------------------------ QQ 数据查询（xtc 侧命令用）
    def _post(self, path: str, payload: dict, timeout: float = 30.0) -> dict | None:
        """POST 到插件端点并解析 JSON；失败返回 None。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Bridge-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
                return json.loads(body) if body.strip() else None
        except Exception:  # noqa: BLE001 插件未启动/超时等
            return None

    def qq_search(self, keyword: str, allow_from: list, allow_groups: list,
                  limit: int = 30) -> dict | None:
        """在白名单 QQ 私聊/群聊里按昵称搜人。返回 {ok, people:[...]} 或 None。"""
        return self._post("/api/qq_search", {
            "keyword": keyword,
            "allow_from": [str(x) for x in (allow_from or [])],
            "allow_groups": [str(x) for x in (allow_groups or [])],
            "limit": int(limit),
        })

    def qq_online(self, minutes: int, allow_from: list, allow_groups: list) -> dict | None:
        """最近 N 分钟白名单会话发言的去重人数。返回 {ok, total, sessions:[...]} 或 None。"""
        return self._post("/api/qq_online", {
            "minutes": int(minutes),
            "allow_from": [str(x) for x in (allow_from or [])],
            "allow_groups": [str(x) for x in (allow_groups or [])],
        })

    def qq_remind(self, group_id, qq_id: str, text: str = "") -> dict | None:
        """在群内 @ 提醒某 QQ 用户。返回 {ok, error?} 或 None。"""
        return self._post("/api/qq_remind", {
            "group_id": str(group_id),
            "qq_id": str(qq_id),
            "text": text,
        })
