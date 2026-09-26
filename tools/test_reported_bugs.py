# -*- coding: utf-8 -*-
"""回归测试：针对用户报告的具体问题（不需要设备，全部离线）。

覆盖：
  1. 历史消息带来源（手表 / QQ私聊 / QQ群），并附来源统计
  2. 同一条 /小天才 命令不会被反复执行/反复回复
  3. 已登录界面不再被误判为"未登录"（泛化"登录"字样不再触发）
  4. 登录表单按行精确校验；密码框是掩码时按长度校验（不再重复输入密码）
  5. "登录中"不再被误判为登录失败；只有确证的账号/密码错误才算失败
  6. 发送结果如实上报：读不到界面/出现失败提示/输入框仍有内容 -> 失败（不再假成功）
  7. App 已在前台时不再重复启动；界面自愈按需执行
  8. 自动登录失败/超时会安排后续重试（不会"一次失败就永久不再尝试"）
  9. 收到的 QQ 命令/回调一定在控制台留痕；日志不会因为个别字符编码失败而整条丢失

用法：python tools/test_reported_bugs.py
"""
from __future__ import annotations

import base64
import json
import os
import re
import struct
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import bridge as bridge_mod  # noqa: E402
import qq_webhook  # noqa: E402
import adb_controller as ac  # noqa: E402
import utils.logger as logger_mod  # noqa: E402
from adb_controller import ADBController, AdbError  # noqa: E402
from msg_log import MessageLog  # noqa: E402
from xiaotiancai import Xiaotiancai  # noqa: E402

RC = {"pass": 0, "fail": 0}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")
    RC["pass" if ok else "fail"] += 1


_WORK = Path.cwd()          # 测试临时文件直接放工作目录（子目录在受限沙箱里可能不可写）
_SEQ = {"n": 0}


def tmp_root() -> Path:
    """返回工作目录，并为本次用例生成唯一前缀（避免用例间互相看到对方的文件）。"""
    _SEQ["n"] += 1
    prefix = f".bugtest{_SEQ['n']}_"
    for old in _WORK.glob(".bugtest*"):
        try:
            old.unlink()
        except OSError:
            pass
    return _WORK


def _paths(root: Path) -> dict:
    prefix = f".bugtest{_SEQ['n']}_"
    return {"msgs": root / f"{prefix}msg_log.json",
            "done": root / f"{prefix}cmd_done.json"}


def cleanup(root: Path) -> None:
    for p in root.glob(".bugtest*"):
        try:
            p.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ 假设备
def node_xml(nodes: str) -> str:
    return ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
            f"<hierarchy rotation=\"0\">{nodes}</hierarchy>")


def n(cls="android.widget.TextView", text="", rid="", password="", desc="",
      bounds="[0,0][100,100]", focusable="false") -> str:
    attrs = [f'class="{cls}"', f'text="{text}"', f'resource-id="{rid}"',
             f'content-desc="{desc}"', f'focusable="{focusable}"',
             f'bounds="{bounds}"']
    if password:                      # 空属性会被 ElementTree 丢弃，只写非空
        attrs.append(f'password="{password}"')
    return "<node " + " ".join(attrs) + " />"


class FakeAdb:
    """只需要 dump_ui / 前台判断 / 点击输入等的最小实现。"""

    def __init__(self, xml: str = "", focus: str = "com.xtc.watch/.MainActivity"):
        self.xml = xml
        self.focus = focus
        self.calls: list[str] = []
        self.can_launch = True

    def dump_ui(self, retries: int = 3, delay: float = 2.0):
        return ET.fromstring(self.xml or node_xml(""))

    def get_current_focus(self) -> str:
        return self.focus

    def get_current_activity(self, use_cache: bool = True) -> str:
        return self.focus

    def is_in_foreground(self, package: str, use_cache: bool = True) -> bool:
        return bool(self.focus) and self.focus.startswith(package)

    def get_screen_size(self) -> tuple:
        return (1080, 1920)

    def shell(self, cmd: str, timeout=None) -> str:
        self.calls.append(cmd)
        return ""

    def ime_shown(self) -> bool:
        return False

    def has_focus_window(self) -> bool:
        return bool(self.focus)

    def screen_on(self):
        return True

    def wake_if_asleep(self) -> bool:
        """假设备默认"屏幕亮着"（真实实现见 adb_controller.wake_if_asleep）。"""
        self.calls.append("wake_if_asleep")
        return False

    def poke_awake(self) -> bool:
        self.calls.append("poke_awake")
        return True

    def wake_up(self) -> bool:
        self.calls.append("wake_up")
        return True

    def invalidate_focus(self) -> None:
        pass

    def package_installed(self, package: str) -> bool:
        self.calls.append(f"pm list packages {package}")
        return True

    def launch_app(self, package: str, activity: str = "", **kw) -> str:
        self.calls.append(f"am start {package}")
        if not self.can_launch:
            return ""
        self.focus = f"{package}/.MainActivity"
        return ".MainActivity"

    def tap_element(self, node) -> None:
        self.calls.append(f"tap {node.get('bounds')}")

    def tap(self, x, y) -> None:
        self.calls.append(f"tap {x},{y}")

    def swipe(self, x1, y1, x2, y2, duration_ms=300) -> None:
        self.calls.append(f"swipe {x1},{y1}->{x2},{y2}")

    def keyevent(self, code: int) -> None:
        self.calls.append(f"keyevent {code}")

    def clear_text_field(self) -> None:
        self.calls.append("clear_text_field")

    def input_text(self, text, verify=None, retries=0, ensure_ime=True) -> bool:
        self.calls.append(f"input_text {text}")
        self.xml = self.xml.replace("%INPUT%", text)
        return True if verify is None else bool(verify())

    # 用真实实现做节点查找，保证假设备与真设备行为一致
    def find_elements(self, root=None, **kw):
        root = root if root is not None else self.dump_ui()
        return [nd for nd in root.iter("node") if ADBController._node_matches(nd, **kw)]

    def find_element(self, root=None, index: int = 0, **kw):
        els = self.find_elements(root=root, **kw)
        return els[index] if len(els) > index else None

    node_bounds = staticmethod(ADBController.node_bounds)
    node_center = staticmethod(ADBController.node_center)


class ChatAdb(FakeAdb):
    """聊天页假设备：可模拟"发送后输入框清空/保留""出现发送失败提示""读不到界面"。"""

    def __init__(self, input_text: str = "", tip: str = "",
                 tap_send_clears: bool = True, tap_send_fails: bool = False):
        super().__init__("", "com.xtc.watch/.ChatActivity")
        self.tap_send_clears = tap_send_clears
        self.tap_send_fails = tap_send_fails
        self.fail_dump = False
        self.input_bounds = "[40,1700][900,1800]"      # 可改：模拟窗口缩放/键盘弹出
        self.send_bounds = "[920,1700][1060,1800]"
        self.title_text = ""                           # 聊天页标题（"先手打字"的门闩要用）
        self.set_ui(input_text=input_text, tip=tip)

    def set_ui(self, input_text: str = "", tip: str = "", bubble: str = "",
               title: str | None = None) -> None:
        if title is not None:
            self.title_text = title
        nodes = ""
        if self.title_text:
            nodes += n(cls="android.widget.TextView", text=self.title_text,
                       desc=f"和{self.title_text}的聊天",
                       rid="com.xtc.watch:id/tv_titleBar_title", bounds="[983,67][1068,92]")
        nodes += n(cls="android.widget.EditText", text=input_text,
                   rid="com.xtc.watch:id/et_chat_text_content",
                   bounds=self.input_bounds, focusable="true")
        nodes += n(cls="android.widget.TextView", text="发送",
                   rid="com.xtc.watch:id/tv_send_view", bounds=self.send_bounds)
        if tip:
            nodes += n(cls="android.widget.TextView", text=tip,
                       rid="com.xtc.watch:id/tv_weichat_uninstall_hint",
                       bounds="[100,1500][900,1560]")
        if bubble:
            nodes += n(cls="android.widget.TextView", text=bubble,
                       rid="com.xtc.watch:id/chat_msg_item_content",
                       desc=f"你发的消息,{bubble}", bounds="[600,1200][1000,1280]")
        self.xml = node_xml(nodes)

    def dump_ui(self, retries: int = 3, delay: float = 2.0):
        if self.fail_dump:
            raise AdbError("模拟界面读取失败")
        return ET.fromstring(self.xml)

    def input_text(self, text, verify=None, retries=0, ensure_ime=True) -> bool:
        self.calls.append(f"input_text {text}")
        self.set_ui(input_text=text, tip=self._tip(), bubble=self._bubble())
        return True if verify is None else bool(verify())

    def tap_element(self, node) -> None:
        rid = node.get("resource-id", "")
        self.calls.append(f"tap {rid}")
        if not rid.endswith("tv_send_view"):
            return
        if self.tap_send_fails:
            self.set_ui(input_text="", tip="网络异常，发送失败")
        elif self.tap_send_clears:
            self.set_ui(input_text="", bubble="刚发出的一条")

    def _node_text(self, rid_tail: str) -> str:
        for nd in ET.fromstring(self.xml).iter("node"):
            if (nd.get("resource-id") or "").endswith(rid_tail):
                return nd.get("text", "")
        return ""

    def _tip(self) -> str:
        return self._node_text("tv_weichat_uninstall_hint")

    def _bubble(self) -> str:
        return self._node_text("chat_msg_item_content")


class FastChatAdb(ChatAdb):
    """带"dump 计数 + 纯注入"的聊天页假设备，用来验证发送快路径真的省下了 dump。"""

    def __init__(self, **kw):
        kw.setdefault("input_text", "")
        super().__init__(**kw)
        # 实机聊天页的组件名（包里带 chatlist，曾把 activity 快路径判否）
        self.focus = "com.xtc.watch/com.xtc.wechat.view.chatlist.ChatActivity"
        self.dumps = 0
        self.plain_injects = 0
        self.sends = 0
        self.blind_tap_sends = True     # False = 模拟"盲点没点中发送按钮"
        self.set_ui(input_text=self._input(), tip=self._tip(), bubble=self._bubble())

    def dump_ui(self, retries: int = 3, delay: float = 2.0):
        self.dumps += 1
        return super().dump_ui(retries, delay)

    def input_text_plain(self, text, ensure_ime=True) -> bool:
        self.plain_injects += 1
        self.calls.append(f"input_text_plain {text}")
        self.set_ui(input_text=text, tip=self._tip(), bubble=self._bubble())
        return True

    def input_text(self, text, verify=None, retries=0, ensure_ime=True) -> bool:
        self.calls.append(f"input_text {text}")
        self.set_ui(input_text=text, tip=self._tip(), bubble=self._bubble())
        return True if verify is None else bool(verify())

    def tap(self, x, y) -> None:
        self.calls.append(f"tap {x},{y}")
        if not self.blind_tap_sends:
            return                      # 盲点没点中：输入框仍留有内容
        self.sends += 1
        self._apply_send()

    def tap_element(self, node) -> None:
        rid = node.get("resource-id", "")
        self.calls.append(f"tap {rid}")
        if not rid.endswith("tv_send_view"):
            return
        self.sends += 1
        self._apply_send()

    def _apply_send(self) -> None:
        if self.tap_send_fails:
            self.set_ui(input_text="", tip="网络异常，发送失败")
        elif self.tap_send_clears:
            self.set_ui(input_text="", bubble="刚发出的一条")

    def _input(self) -> str:
        return self._node_text("et_chat_text_content")


def make_xtc(xml: str = "", focus: str = "com.xtc.watch/.MainActivity",
             ui_cfg: dict | None = None, adb: FakeAdb | None = None) -> tuple:
    adb = adb or FakeAdb(xml, focus)
    xtc = Xiaotiancai(adb, {"ui": ui_cfg or {}}, logger=None)
    return xtc, adb


# ------------------------------------------------------------------ 1. 来源
def make_bridge(root: Path):
    cfg = {"target": {"xtc_contact": "张三"}, "xiaotiancai": {}, "webhook": {}}
    br = bridge_mod.MessageBridge(cfg, adb=None, xtc=None, forwarder=None, logger=None)
    paths = _paths(root)
    br.msgs = MessageLog(path=str(paths["msgs"]))
    br._cmd_done_file = str(paths["done"])
    br._load_cmd_done()      # 从本次用例专属文件加载（同一 root 再建实例即模拟"重启"）
    return br


def test_history_source_tags() -> None:
    root = tmp_root()
    try:
        br = make_bridge(root)
        br._archive_qq_send("[09-01 10:00] [张三] 中午吃什么", user_id="10001")
        br._archive_qq_send("[09-01 10:01] [李四] 群里说", user_id="10002", group_id="999")
        br.msgs.append("xtc", "张三", "我吃了", source="手表-张三", source_id="张三")
        entries = br.msgs.recent(10)
        check("QQ私聊 来源标签", entries[0]["source"] == "QQ私聊 10001", entries[0]["source"])
        check("QQ群 来源标签", entries[1]["source"] == "QQ群 999", entries[1]["source"])
        check("手表 来源标签", entries[2]["source"].startswith("手表"), entries[2]["source"])
        check("source_id 可按来源过滤",
              entries[1]["source_id"] == "999" and entries[0]["source_id"] == "10001")

        text = br._format_history_text(entries, 10)
        check("历史里带来源标注",
              "[QQ私聊 10001]" in text and "[QQ群 999]" in text and "[手表]" in text,
              text.replace("\n", " | ")[:180])
        check("历史末尾有来源统计", "来源统计：" in text and "QQ群 999 1 条" in text,
              text.splitlines()[-1])

        # 来源过滤
        only_xtc = br._match_source(entries, "手表")
        check("按来源过滤=手表", len(only_xtc) == 1 and only_xtc[0]["kind"] == "xtc")
        only_group = br._match_source(entries, "999")
        check("按来源过滤=群号", len(only_group) == 1 and only_group[0]["source"] == "QQ群 999")
    finally:
        cleanup(root)


def test_history_source_from_plugin_payload() -> None:
    """QQ 侧 /小天才 历史消息 带来源参数（history_source）时应按来源过滤。"""
    root = tmp_root()
    try:
        br = make_bridge(root)
        br._archive_qq_send("[09-01 10:00] [张三] 私聊消息", user_id="10001")
        br._archive_qq_send("[09-01 10:01] [李四] 群消息", user_id="10002", group_id="999")
        sent: list[str] = []
        br._reply_into_xtc = lambda t: (sent.append(t), True)[1]  # type: ignore
        br._do_history_job(20, "", True, "999")
        check("历史按来源过滤后只剩群消息",
              bool(sent) and "群消息" in sent[-1] and "私聊消息" not in sent[-1],
              (sent[-1][:120] if sent else "(无输出)"))
    finally:
        cleanup(root)


# ------------------------------------------------------------------ 2. 命令重复
def test_command_not_repeated() -> None:
    root = tmp_root()
    try:
        br = make_bridge(root)
        cmd = "/小天才 历史消息 5"
        # 1) 同一分钟内轮询 5 次（时间标签相同）-> 只执行一次
        for _ in range(5):
            br._maybe_xtc_cmd("own", cmd, "09:41")
        check("同标签重复轮询只入队一次", br._job_queue.qsize() == 1,
              f"queue={br._job_queue.qsize()}")
        check("_cmd_text_handled 认已处理", br._cmd_text_handled("own", cmd, "09:41"))

        # 2) 仍在队列里时，同一条命令不重复入队（标签变化只更新身份）
        br._maybe_xtc_cmd("own", cmd, "09:42")
        check("队列中的命令不重复入队", br._job_queue.qsize() == 1,
              f"queue={br._job_queue.qsize()}")

        # 3) 执行完成：出队 + 记为 done（持久化）
        text = br._job_queue.get_nowait()[1]
        ident = br._cmd_pending.pop(text, None)          # ('own', '09:42')
        br._cmd_done_add(ident[0], text, ident[1])
        check("执行后记录到 done 集合", br._cmd_done_has("own", cmd, ident[1]),
              str(br._cmd_done))

        # 4) 命令仍是"最新一条"、标签不变 -> 再轮询多次也不再执行
        for _ in range(4):
            br._maybe_xtc_cmd("own", cmd, "09:42")
        check("执行完后同标签不再重复", br._job_queue.qsize() == 0,
              f"queue={br._job_queue.qsize()}")

        # 5) 用户重新输入同一条命令（新消息 -> 新时间标签）-> 允许再次执行
        br._maybe_xtc_cmd("own", cmd, "09:45")
        check("新时间标签允许再次执行", br._job_queue.qsize() == 1,
              f"queue={br._job_queue.qsize()}")

        # 6) 重启后（重新加载 done）也不会重复执行已完成的同一条：
        #    新实例里 seen_text 是空的，此时只能靠持久化的 done 判重
        br2 = make_bridge(root)
        check("重启后 seen 为空（只有 done 兜底）", not br2._cmd_seen_text)
        check("done 持久化，重启后仍判重", br2._cmd_text_handled("own", cmd, "09:42"),
              f"done={br2._cmd_done}")
    finally:
        cleanup(root)


# ------------------------------------------------------------------ 3. 登录判断
def test_login_detection() -> None:
    # 已登录：主页里有"登录"相关的普通文案（旧逻辑会误判未登录）
    home = node_xml(
        n(text="微聊") + n(text="我的") +
        n(cls="android.widget.Button", text="退出登录") +
        n(cls="android.widget.TextView", text="登录设备管理") +
        n(cls="android.widget.EditText", text="", rid="com.xtc.watch:id/et_chat_text_content"))
    xtc, _ = make_xtc(home)
    check("已登录主页不再误判未登录", xtc.is_logged_in() is True)

    # 账号密码登录页：有密码框
    login = node_xml(
        n(cls="android.widget.EditText", text="", bounds="[0,200][100,260]") +
        n(cls="android.widget.EditText", text="", password="true",
          desc="请输入密码", bounds="[0,300][100,360]") +
        n(text="登录") + n(text="账号密码登录"))
    xtc2, _ = make_xtc(login)
    check("账密登录页判为未登录", xtc2.is_logged_in() is False)

    # 短信登录页：只有验证码特征
    sms = node_xml(
        n(cls="android.widget.EditText", text="", bounds="[0,200][100,260]") +
        n(text="获取验证码") + n(text="短信验证码登录"))
    xtc3, _ = make_xtc(sms)
    check("短信登录页判为未登录", xtc3.is_logged_in() is False)

    # 欢迎页（Activity 名含 welcome）
    xtc4, _ = make_xtc(node_xml(n(text="注册/登录")), focus="com.xtc.watch/.WelcomeActivity")
    check("欢迎页判为未登录", xtc4.is_logged_in() is False)

    # 不在前台
    xtc5, _ = make_xtc(node_xml(n(text="微聊")), focus="com.android.launcher/.Launcher")
    check("App 不在前台判为未登录", xtc5.is_logged_in() is False)


# ------------------------------------------------------------------ 4. 表单输入
def test_password_field_masked() -> None:
    pwd_root = ET.fromstring(node_xml(
        n(cls="android.widget.EditText", text="", password="true", desc="请输入密码")))
    pwd_node = next(iter(pwd_root.iter("node")))
    xtc, _ = make_xtc()
    check("识别密码框", xtc._is_masked_field(pwd_node) is True,
          f"password={pwd_node.get('password')!r} desc={pwd_node.get('content-desc')!r}")
    check("掩码文本识别", xtc._is_mask_text("••••••") and not xtc._is_mask_text("123456"))

    # 掩码字段：dump 出来是圆点、长度与明文一致 -> 判定成功（不再重复输入）
    masked_xml = node_xml(
        n(cls="android.widget.EditText", text="13800000000", bounds="[0,200][100,260]") +
        n(cls="android.widget.EditText", text="••••••", password="true",
          bounds="[0,300][100,360]"))
    xtc2, adb2 = make_xtc(masked_xml)
    edits = [nd for nd in adb2.dump_ui().iter("node")]
    ok, rows = xtc2.fill_login_form(edits, ["13800000000", "secret"])
    check("掩码密码框按长度校验通过", ok is True, f"rows={rows}")
    check("没有重复输入密码",
          sum(1 for c in adb2.calls if c.startswith("input_text")) == 2,
          str([c for c in adb2.calls if c.startswith("input_text")]))

    # 明文页面：文本与目标不一致 -> 报失败（而不是无限重输）
    wrong_xml = node_xml(
        n(cls="android.widget.EditText", text="13800000000") +
        n(cls="android.widget.EditText", text="别的密码", password="true"))
    xtc3, adb3 = make_xtc(wrong_xml)
    edits3 = [nd for nd in adb3.dump_ui().iter("node")]
    ok3, rows3 = xtc3.fill_login_form(edits3, ["13800000000", "secret"])
    check("内容不符时报失败", ok3 is False, f"rows={rows3}")
    check("重试次数有限（<=2 次/字段）",
          sum(1 for c in adb3.calls if c.startswith("input_text")) <= 4,
          str([c for c in adb3.calls if c.startswith("input_text")]))


# ------------------------------------------------------------------ 5. 登录中不判失败
def test_login_progress_not_failure() -> None:
    """登录中/网络临时问题不能被判成"密码错误"（用户报告的"登录中却提示登录失败"）。"""
    xtc, _ = make_xtc()

    progress = ET.fromstring(node_xml(
        n(text="登录中，请稍候...") + n(text="登录失败") + n(text="网络异常，请重试")))
    check("识别登录进度文案", bool(xtc._detect_login_progress(progress)),
          xtc._detect_login_progress(progress))
    check("登录中不判失败（即使同屏有'失败'字样）",
          xtc._detect_login_error(progress) == "", repr(xtc._detect_login_error(progress)))

    network = ET.fromstring(node_xml(n(text="网络连接失败，请重试")))
    check("网络类提示不判失败（算超时/稍后重试）",
          xtc._detect_login_error(network) == "", repr(xtc._detect_login_error(network)))
    check("网络类提示能被识别为临时问题",
          bool(xtc._detect_soft_error(network)), xtc._detect_soft_error(network))

    wrong_pwd = ET.fromstring(node_xml(n(text="账号或密码错误")))
    check("明确的密码错误仍判失败",
          xtc._detect_login_error(wrong_pwd) != "", repr(xtc._detect_login_error(wrong_pwd)))

    toast = ET.fromstring(node_xml(n(text="登录失败，请重新输入")))
    check("短提示里的'登录失败'算失败",
          xtc._detect_login_error(toast) != "", repr(xtc._detect_login_error(toast)))

    ok_page = ET.fromstring(node_xml(n(text="微聊") + n(text="我的")))
    check("正常界面不会被误判为登录失败", xtc._detect_login_error(ok_page) == "")

    # 等待结果：界面一直显示"登录中"-> 最终返回 timeout（稍后重试），而不是 fail
    xtc2, adb2 = make_xtc(node_xml(n(text="登录中，请稍候...")),
                          focus="com.xtc.watch/.LoginActivity",
                          ui_cfg={"login_timeout": 0.2})
    status = xtc2._await_login_result()
    check("一直登录中 -> timeout（不是 fail）", status == "timeout", status)


# ------------------------------------------------------------------ 6. 发送结果如实上报
def test_send_result_is_honest() -> None:
    """发送确认：只有真正确认发出才返回 True（不再"发失败也报成功"）。"""
    text = "晚上回家吃饭"

    # (a) 发送后输入框仍留着内容 -> 失败
    adb = ChatAdb(tap_send_clears=False)
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1, "send_retries": 1}})
    ok = xtc.send_message(text)
    check("输入框仍有内容 -> 报发送失败", ok is False)

    # (b) 发送后出现"发送失败"提示 -> 失败
    adb2 = ChatAdb(tap_send_fails=True)
    xtc2 = Xiaotiancai(adb2, {"ui": {"interaction_delay": 0.1, "send_retries": 1}})
    check("出现发送失败提示 -> 报发送失败", xtc2.send_message(text) is False)

    # (c) 旧的失败提示条（发送前就有）不能被误当成这次失败
    adb3 = ChatAdb(input_text="", tip="网络异常，发送失败")
    xtc3 = Xiaotiancai(adb3, {"ui": {"interaction_delay": 0.1, "send_retries": 1}})
    check("旧失败提示不误报（发送成功）", xtc3.send_message(text) is True)

    # (d) 正常发送（输入框清空 + 新气泡）-> 成功
    adb4 = ChatAdb()
    xtc4 = Xiaotiancai(adb4, {"ui": {"interaction_delay": 0.1, "send_retries": 1}})
    check("正常发送 -> 成功", xtc4.send_message(text) is True)

    # (e) 读不到界面 -> 不谎报成功
    adb5 = ChatAdb()
    adb5.fail_dump = True
    xtc5 = Xiaotiancai(adb5, {"ui": {"interaction_delay": 0.1, "send_retries": 1}})
    check("界面读不到 -> 报发送失败（不谎报）", xtc5.send_message(text) is False)


def test_send_speed_fast_path() -> None:
    """用户报告：发一条要十几秒。根因是发送链路里 **dump 次数太多**（WSA 上一次约 3 秒）。

    旧链路每发一条要 dump 5 次左右：聊天页 Activity 名被判否（包名 chatlist 含 list）
    → `is_in_chat()` 先 dump 一次；发送前再 dump 一次；注入后 dump 一次做校验；
    找发送按钮（这次已经复用）；点完再 dump 1~2 次确认。
    现在：Activity 名按类名判断（0 次）+ 复用轮询刚 dump 的快照（0 次）+
    布局没变时直接广播注入并点缓存的发送按钮坐标（0 次）+ 确认 1 次 = **1 次**。
    """
    text = "晚上回家吃饭"

    # ① Activity 名按**类名**判断：实机包名 chatlist 里的 "list" 不能再把聊天页判否
    adb = FakeAdb(chat_page_xml(),
                  "com.xtc.watch/com.xtc.wechat.view.chatlist.ChatActivity")
    xtc = Xiaotiancai(adb, {"ui": {}}, logger=None)
    check("包名含 chatlist 的 ChatActivity 算聊天页", xtc._activity_is_chat(adb.focus) is True)
    check("is_in_chat 走 Activity 快路径（不用 dump）",
          xtc.is_in_chat() is True and not adb.calls, str(adb.calls))
    xtc2 = Xiaotiancai(FakeAdb("", "com.xtc.watch/com.xtc.wechat.view.chatlist.ChatListActivity"),
                       {"ui": {}}, logger=None)
    check("真正的消息列表 Activity（类名含 List）仍排除",
          xtc2._activity_is_chat("com.xtc.watch/com.xtc.wechat.view.chatlist.ChatListActivity")
          is False)

    # ② 首次发送（还没有按钮坐标）走稳妥流程，之后记住坐标
    adb2 = FastChatAdb()
    xtc3 = Xiaotiancai(adb2, {"ui": {"interaction_delay": 0.05, "send_retries": 1}},
                       logger=None)
    check("首次发送成功", xtc3.send_message(text) is True)
    first_dumps = adb2.dumps
    check("首次发送记住发送按钮坐标", xtc3._send_cache is not None, str(xtc3._send_cache))

    # ③ 第二次发送：复用快照 + 快路径 -> 只要 1 次 dump
    adb2.dumps = 0
    adb2.plain_injects = 0
    adb2.sends = 0
    ok = xtc3.send_message("第二条消息")
    check("第二次发送成功", ok is True)
    check("第二次发送只 dump 1 次（旧实现 4~5 次）", adb2.dumps == 1, f"dumps={adb2.dumps}")
    check("快路径用纯广播注入（不做注入校验）", adb2.plain_injects == 1,
          f"plain_injects={adb2.plain_injects}")
    check("确实点到了发送按钮（不是盲点空转）", adb2.sends == 1, f"sends={adb2.sends}")
    check("首次发送的 dump 次数也没有变多", first_dumps <= 3, f"first_dumps={first_dumps}")

    # ④ 布局变了（WSA 窗口缩放/键盘顶起输入框）-> 不许按旧坐标盲点，退回稳妥流程。
    #    轮询会持续 dump，所以布局变化会体现在下一份快照里；这里手动模拟那次 dump。
    adb3 = FastChatAdb()
    xtc4 = Xiaotiancai(adb3, {"ui": {"interaction_delay": 0.05, "send_retries": 1}},
                       logger=None)
    xtc4.send_message(text)
    adb3.input_bounds = "[40,1500][900,1600]"     # 输入框整体上移 200px
    adb3.send_bounds = "[920,1500][1060,1600]"
    adb3.set_ui(input_text="", tip="", bubble="")
    xtc4._dump_fast()                             # 轮询读到新布局
    adb3.dumps = 0
    adb3.plain_injects = 0
    ok4 = xtc4.send_message("第三条消息")
    check("布局变了仍能发出", ok4 is True)
    check("布局变了不按旧坐标盲点（走带校验的稳妥流程）", adb3.plain_injects == 0,
          f"plain_injects={adb3.plain_injects}")
    check("布局变了会重新 dump 找按钮", adb3.dumps >= 2, f"dumps={adb3.dumps}")

    # ⑤ 快路径点偏了（输入框仍留有内容）-> 退回稳妥流程重发，且**只发一条**
    adb5 = FastChatAdb()
    xtc5 = Xiaotiancai(adb5, {"ui": {"interaction_delay": 0.05, "send_retries": 1}},
                       logger=None)
    xtc5.send_message(text)                        # 先缓存坐标
    adb5.blind_tap_sends = False                   # 之后盲点不再生效
    adb5.sends = 0
    ok5 = xtc5.send_message("第四条消息")
    check("快路径点偏后仍能发出", ok5 is True)
    check("只发出一条（没有重复发送）", adb5.sends == 1, f"sends={adb5.sends}")


def test_recent_snapshot_window() -> None:
    """最近快照复用：有效期内可用、过期/关闭就返回 None（调用方自己 dump）。"""
    adb = FakeAdb(chat_page_xml(), "com.xtc.watch/.ChatActivity")
    xtc = Xiaotiancai(adb, {"ui": {"snapshot_reuse": 3.0}}, logger=None)
    check("还没读过界面 -> 没有快照", xtc.recent_snapshot() is None)
    xtc._dump_fast()
    check("刚 dump 完可以复用", xtc.recent_snapshot() is not None)
    xtc._snapshot_ts -= 5.0
    check("超过 3 秒不再复用", xtc.recent_snapshot() is None)
    xtc2 = Xiaotiancai(adb, {"ui": {"snapshot_reuse": 0}}, logger=None)
    xtc2._dump_fast()
    check("snapshot_reuse=0 表示不复用（改动可关）", xtc2.recent_snapshot() is None)


def test_plain_injection_skips_dump() -> None:
    """纯注入（input_text_plain）只发广播、不 dump：省下的就是发送链路里的 3 秒。"""
    from adb_controller import ADBController as _C
    ctl = _C(adb_path="adb")
    cmds: list = []
    dumps = {"n": 0}
    ctl._adbkeyboard_ready = lambda: True
    ctl._adbkeyboard_active = lambda: True
    ctl.shell = lambda cmd, timeout=None: (cmds.append(cmd), "")[1]
    ctl.dump_ui = lambda *a, **k: dumps.__setitem__("n", dumps["n"] + 1)

    check("纯注入返回成功", ctl.input_text_plain("你好") is True)
    check("发的是 ADBKeyBoard 明文广播",
          any("ADB_INPUT_TEXT" in c for c in cmds), str(cmds))
    check("纯注入不做界面校验（0 次 dump）", dumps["n"] == 0, str(dumps))

    ctl._adbkeyboard_b64_ok = True      # 上次明文广播被吞过 -> 记住走 base64
    cmds.clear()
    ctl.input_text_plain("你好")
    check("记住 base64 通道后改走 B64",
          any("ADB_INPUT_B64" in c for c in cmds), str(cmds))

    ctl._adbkeyboard_ready = lambda: False
    check("输入法没就绪时返回 False（调用方会退回带校验的流程）",
          ctl.input_text_plain("你好") is False)


def test_ime_check_is_cached() -> None:
    """注入每条消息都要问一次"ADBKeyBoard 是不是当前输入法"（一次 adb shell 0.18s）。

    实测：这条链路上真正的 adb 开销只有 ~0.4s（settings 查询 0.18 + 广播 0.23），
    所以能省就省；IME 不会自己变，缓存 60 秒，切输入法/注入失败时作废。
    """
    from adb_controller import ADBController as _C
    ctl = _C(adb_path="adb")
    cmds: list = []
    ctl.shell = lambda cmd, timeout=None: (cmds.append(cmd), "com.android.adbkeyboard/.AdbIME")[1]
    check("第一次查询命中", ctl._adbkeyboard_active() is True)
    n1 = len(cmds)
    check("第二次走缓存（不再起 adb 进程）",
          ctl._adbkeyboard_active() is True and len(cmds) == n1, str(cmds))
    ctl.invalidate_ime_state()
    ctl._adbkeyboard_active()
    check("失效后重新查询", len(cmds) == n1 + 1, str(cmds))
    # 换了输入法 -> 返回 False（调用方会去切回 ADBKeyBoard）
    ctl.shell = lambda cmd, timeout=None: "com.android.inputmethod.pinyin/.InputService"
    ctl.invalidate_ime_state()
    check("当前不是 ADBKeyBoard 时返回 False", ctl._adbkeyboard_active() is False)


def test_launch_app_skips_hard_failures_fast() -> None:
    """启动策略"硬失败"（not exported / SecurityException）要立刻换下一套。

    实机 WSA 实测：`am start -a MAIN/LAUNCHER` 根本起不来（12 秒都到不了前台），
    而旧实现给每个策略 25 秒前台等待 —— 撞上它就白等 25 秒，这正是
    "发一条要十几秒"里的一段。现在单策略最多等 6 秒，且命令自己报错就立刻换。
    """
    from adb_controller import ADBController as _C
    ctl = _C(adb_path="adb")
    ctl.package_installed = lambda pkg: True
    ctl.is_in_foreground = lambda pkg, use_cache=True: False
    ctl.resolve_launcher_activity = lambda pkg: ".MainActivity"

    tried: list = []

    def shell(cmd, timeout=None):
        tried.append(cmd.split()[0] + " " + (cmd.split()[1] if len(cmd.split()) > 1 else ""))
        if cmd.startswith("am start -n"):
            # 模拟"这个 activity 不导出"的硬失败
            return ("Starting: Intent { cmp=com.xtc.watch/.MainActivity }\n"
                    "java.lang.SecurityException: Permission Denial: starting Intent "
                    "{ ... } not exported from uid 10087")
        return "Events injected: 1"

    ctl.shell = shell
    waits: list = []
    ctl.wait_for_activity = lambda pkg, timeout=20.0, interval=1.0: (
        waits.append(timeout), True)[1]

    got = ctl.launch_app("com.xtc.watch", ".MainActivity", attempts=1)
    check("换到能成功的策略并返回", bool(got), got)
    check("硬失败策略没有白等前台确认（只等了一次，且 ≤6 秒）",
          len(waits) == 1 and waits[0] <= 6.0, str(waits))
    check("确实试过 monkey", any("monkey" in c for c in tried), str(tried))


def test_poll_loop_yields_to_pending_send() -> None:
    """有发送在排队时轮询先让路：否则发送要等轮询那一次 3~4 秒的 dump 做完才开始输入。"""
    import threading

    class PollAdb(FakeAdb):
        def is_connected(self) -> bool:
            return True

        def ensure_connected(self) -> bool:
            return True

    calls = {"n": 0}

    class CountingXtc(Xiaotiancai):
        def app_state_with_root(self, attempts: int = 2):
            calls["n"] += 1
            return (self.STATE_CHAT, ET.fromstring(chat_page_xml()))

    def make_bridge() -> tuple:
        br = bridge_mod.MessageBridge({"target": {}, "xiaotiancai": {}, "webhook": {}},
                                      adb=PollAdb(), xtc=CountingXtc(PollAdb(), {"ui": {}},
                                                                     logger=None),
                                      forwarder=None, logger=Recorder())
        br.msgs = MessageLog(path=str(_paths(root)["msgs"]))
        br._cmd_done_file = str(_paths(root)["done"])
        br._poll_interval = 0.3
        return br

    def run(br, seconds: float) -> None:
        br.running = True
        t = threading.Thread(target=br._poll_loop, daemon=True)
        t.start()
        time.sleep(seconds)
        br.running = False
        t.join(3)

    root = tmp_root()
    try:
        br = make_bridge()
        br._send_pending = True
        br._send_pending_ts = time.monotonic()
        run(br, 0.5)
        check("发送排队期间轮询不抢 dump（让路）", calls["n"] == 0, str(calls))

        br._send_pending = False
        run(br, 0.5)
        check("发送结束后轮询恢复读消息", calls["n"] >= 1, str(calls))

        # 让路有上限：长队列（或发送线程卡住）不能把读消息饿死
        calls["n"] = 0
        br._send_pending = True
        br._send_pending_ts = time.monotonic() - 31
        run(br, 0.5)
        check("让路最多 30 秒（超时后轮询照常跑）", calls["n"] >= 1, str(calls))
    finally:
        cleanup(root)


def test_png_encoder_and_crop() -> None:
    """表情包转发用的纯标准库 PNG 编码 + 抠图（不引 Pillow）。

    校验方式：自己把 PNG 解回来（解析 chunk + zlib 解压 + 反 filter=0），
    比只看"字节非空"可靠得多。
    """
    from utils import pngtool

    def decode_png(png: bytes) -> tuple:
        check("PNG 签名正确", png.startswith(pngtool.PNG_SIGNATURE))
        pos, chunks, idat = 8, {}, b""
        while pos < len(png):
            length = int.from_bytes(png[pos:pos + 4], "big")
            tag = png[pos + 4:pos + 8]
            data = png[pos + 8:pos + 8 + length]
            crc = int.from_bytes(png[pos + 8 + length:pos + 12 + length], "big")
            check(f"chunk {tag.decode()} CRC 正确",
                  crc == (zlib.crc32(tag + data) & 0xFFFFFFFF))
            chunks[tag] = data
            if tag == b"IDAT":
                idat += data
            pos += 12 + length
        w, h, depth, ctype = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
        return w, h, depth, ctype, zlib.decompress(idat)

    # ① 2x2 纯色图：编码 -> 解码应完全一致
    row = bytes([10, 20, 30, 40, 50, 60])
    png = pngtool.encode_png_rgb(2, 2, [row, row])
    w, h, depth, ctype, raw = decode_png(png)
    check("IHDR 尺寸/位深/颜色类型正确", (w, h, depth, ctype) == (2, 2, 8, 2), f"{w}x{h} {ctype}")
    check("像素数据往返一致", raw == b"\x00" + row + b"\x00" + row, repr(raw))

    # ② 从"整屏 RGBA"里抠一块：坐标/颜色要对得上
    W, H = 4, 3
    rgba = bytearray()
    for y in range(H):
        for x in range(W):
            rgba += bytes([x * 10, y * 10, 200, 255])          # R=10x, G=10y, B=200
    out = pngtool.crop_png_from_rgba(bytes(rgba), W, H, (1, 1, 3, 3))
    w2, h2, _d, _c, raw2 = decode_png(out)
    check("抠图尺寸正确", (w2, h2) == (2, 2), f"{w2}x{h2}")
    rows = [raw2[i * (w2 * 3 + 1) + 1: i * (w2 * 3 + 1) + 1 + w2 * 3] for i in range(h2)]
    check("抠图像素来自指定区域（且丢掉了 alpha）",
          rows == [bytes([10, 10, 200, 20, 10, 200]), bytes([10, 20, 200, 20, 20, 200])],
          str(rows))

    # ③ 越界坐标会被夹回画面内（不能抛、也不能越界读）
    out3 = pngtool.crop_png_from_rgba(bytes(rgba), W, H, (-5, -5, 99, 99))
    w3, h3, _d, _c, _r = decode_png(out3)
    check("越界坐标夹到屏幕内", (w3, h3) == (W, H), f"{w3}x{h3}")
    check("数据不完整时返回空（调用方按失败处理）",
          pngtool.rgba_to_rgb_rows(b"\x00" * 10, 4, 3, (0, 0, 2, 2)) == [])


def test_screencap_header_parsing() -> None:
    """screencap 原始像素的头部解析：12 字节（Android 9+）与 16 字节（多 colorspace）都要认。"""
    from adb_controller import ADBController as _C
    ctl = _C(adb_path="adb")
    w, h = 3, 2
    pixels = bytes(range(1, w * h * 4 + 1))

    ctl._run = lambda *a, **k: (struct.pack("<III", w, h, 1) + pixels, "")
    got, gw, gh = ctl.screencap_rgba()
    check("12 字节头解析正确", (gw, gh) == (w, h) and got == pixels, f"{gw}x{gh} {len(got)}")

    ctl._run = lambda *a, **k: (struct.pack("<IIII", w, h, 1, 0) + pixels, "")
    got2, gw2, gh2 = ctl.screencap_rgba()
    check("16 字节头解析正确", (gw2, gh2) == (w, h) and got2 == pixels, f"{gw2}x{gh2} {len(got2)}")

    ctl._run = lambda *a, **k: (struct.pack("<III", 9, 9, 1) + b"\x00" * 16, "")
    try:
        ctl.screencap_rgba()
        check("数据不完整要报错", False, "没有抛异常")
    except Exception as e:  # noqa: BLE001
        check("数据不完整要报错", "不完整" in str(e), str(e)[:60])


def test_sticker_detection_and_capture() -> None:
    """表情/贴纸识别 + 按气泡截图（单向：小天才 -> QQ）。"""
    sticker_xml = node_xml(
        n(cls="android.widget.TextView", text="回家中", desc="屑猹不喝茶发的消息,回家中",
          rid="com.xtc.watch:id/chat_msg_item_content", bounds="[1070,586][1310,658]") +
        n(cls="android.widget.ImageView", text="", desc="屑猹不喝茶发的消息,表情啊啊啊",
          rid="com.xtc.watch:id/chat_msg_item_content", bounds="[948,267][1068,387]") +
        n(cls="android.widget.TextView", text="表情包发我", desc="屑猹不喝茶发的消息,表情包发我",
          rid="com.xtc.watch:id/chat_msg_item_content", bounds="[845,100][965,160]"))
    xtc, adb = make_xtc(sticker_xml, focus="com.xtc.watch/.ChatActivity", ui_cfg={})
    items = xtc._chat_bubbles(ET.fromstring(sticker_xml))
    stickers = [it["text"] for it in items if it.get("sticker")]
    check("只把 ImageView 的「表情X」判成表情",
          stickers == ["表情啊啊啊"], str([(it["text"], it.get("sticker")) for it in items]))
    check("文字消息「表情包发我」不算表情（不能靠前缀猜）",
          all(it.get("sticker") is False for it in items if it["text"] == "表情包发我"),
          str(items))

    hit = xtc.sticker_of_latest(ET.fromstring(sticker_xml), "表情啊啊啊")
    check("能取到最新表情气泡的位置",
          bool(hit) and hit["bounds"] == (948, 267, 1068, 387), str(hit))
    check("文本不匹配时不会拿错气泡",
          xtc.sticker_of_latest(ET.fromstring(sticker_xml), "表情没有的") is None)

    # 截图：ADB 侧用假的原始像素，验证"按气泡区域抠出来"
    captured = {}

    def fake_crop(box):
        captured["box"] = box
        return b"\x89PNG" + b"x" * 400

    adb.screencap_crop_png = fake_crop
    png = xtc.capture_sticker((948, 267, 1068, 387))
    check("按气泡区域截图", captured.get("box") == (948, 267, 1068, 387), str(captured))
    check("返回 PNG 字节", bool(png) and png.startswith(b"\x89PNG"), str(png)[:20])
    # 截图失败 -> None（调用方退回文字），不能抛
    adb.screencap_crop_png = lambda box: (_ for _ in ()).throw(RuntimeError("截图炸了"))
    check("截图异常返回 None（不抛）", xtc.capture_sticker((948, 267, 1068, 387)) is None)
    check("气泡太小/不在屏内都不截",
          xtc.capture_sticker((0, 0, 5, 5)) is None and xtc.capture_sticker((9999, 9999, 10050, 10050)) is None)


def test_emoji_store_reads_original_file() -> None:
    """表情**原文件**读取：优先缓存里刚写进来的那张（动图保动画），其次表情包目录按名字匹配。"""
    from emoji_store import EmojiStore

    def gif(w=90, h=90, frames=11, loop=True, pad=b"") -> bytes:
        head = b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 10
        if loop:
            head += b"NETSCAPE2.0"
        return head + pad + b"\x21\xf9\x04" * frames + b";"

    def png(w=120, h=120) -> bytes:
        return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
                + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00")

    # ① 只读文件头判断格式/尺寸/动图
    check("GIF 识别为动图",
          EmojiStore.sniff(gif()) == {"kind": "gif", "w": 90, "h": 90, "animated": True},
          str(EmojiStore.sniff(gif())))
    check("单帧 GIF 不算动图",
          EmojiStore.sniff(gif(frames=1, loop=False, pad=b"\x00" * 3000))["animated"] is False,
          str(EmojiStore.sniff(gif(frames=1, loop=False, pad=b"\x00" * 3000))))
    check("PNG 识别（静态）", EmojiStore.sniff(png())["kind"] == "png"
          and EmojiStore.sniff(png())["w"] == 120)
    apng = png() + b"acTL" + b"\x00" * 32
    check("APNG 识别为动图", EmojiStore.sniff(apng)["animated"] is True)
    check("认不出的头返回空", EmojiStore.sniff(b"hello world") == {})

    class FakeAdb:
        """假设备：给几条 shell/read_file 的固定回答。"""

        def __init__(self, files: dict, listing: str = "", now: int = 1000):
            self.files = files
            self.listing = listing
            self.now = now
            self.read_paths: list = []

        def shell(self, cmd: str, timeout=None) -> str:
            if cmd.startswith("date +%s;"):
                return f"{self.now}\n{self.listing}"
            if "-name desc.json" in cmd:
                return "\n".join(p for p in self.files if p.endswith("desc.json"))
            return ""

        def read_file(self, path: str, timeout=None) -> bytes:
            self.read_paths.append(path)
            return self.files.get(path, b"")

    root = "/sdcard/Android/data/com.xtc.watch"
    cache_gif = f"{root}/cache/big_image/com.xtc.watch/v1/99/abc.cnt"
    pack_png = f"{root}/files/xtcdata/telwatch/weichat/emoji/newEmoji/138/1/tiancaituQ/big/tiancaituQ_003"
    desc = f"{root}/files/xtcdata/telwatch/weichat/emoji/newEmoji/138/1/tiancaituQ/desc.json"
    index = json.dumps({"count": 1, "emojis": [{"code": "tiancaituQ_003", "desc": "爱你"}]},
                       ensure_ascii=False).encode("utf-16")

    # ② 缓存里有"刚写进来"的动图 -> 名字查不到时用它（保住动画）
    adb = FakeAdb({cache_gif: gif(), desc: index},
                  listing=f"995 28583 {cache_gif}\n960 9999 {root}/cache/old.jpg")
    store = EmojiStore(adb, package="com.xtc.watch", recent_secs=45)
    got = store.find("啊啊啊")
    check("名字查不到时取缓存里最近的动图",
          bool(got) and got["source"] == "cache" and got["animated"] is True,
          str({k: v for k, v in (got or {}).items() if k != "data"}))
    check("时间窗外的老文件不会被当成本次表情",
          "old.jpg" not in (got or {}).get("path", ""), str((got or {}).get("path")))

    # ②b 名字能命中时**优先**用表情包文件（确定性），不冒险用缓存里"最近的那张"
    adb_b = FakeAdb({cache_gif: gif(), pack_png: png(), desc: index},
                    listing=f"995 28583 {cache_gif}")
    got_b = EmojiStore(adb_b, package="com.xtc.watch", recent_secs=45).find("爱你")
    check("名字命中时优先用表情包原文件（避免把同时收到的照片当表情）",
          bool(got_b) and got_b["source"] == "pack" and got_b["path"] == pack_png,
          str({k: v for k, v in (got_b or {}).items() if k != "data"}))
    check("warm() 能预热索引", EmojiStore(adb_b, package="com.xtc.watch").warm() >= 1)

    # ③ 缓存里没有 -> 用表情包目录按名字精确匹配（desc.json 是 UTF-16）
    adb2 = FakeAdb({pack_png: png(), desc: index}, listing="")
    store2 = EmojiStore(adb2, package="com.xtc.watch")
    got2 = store2.find("爱你")
    check("按名字从表情包目录取原图",
          bool(got2) and got2["source"] == "pack" and got2["path"] == pack_png,
          str({k: v for k, v in (got2 or {}).items() if k != "data"}))
    check("名字对不上就不乱取", store2.find("不存在的表情") is None)
    check("索引只建一次（第二次不再读 desc.json）",
          store2._pack_index() is store2._pack_index())

    # ④ 缓存里那张是"大照片"时不当表情（避免把聊天里的图当贴纸发）
    big_jpeg = f"{root}/cache/big_image/com.xtc.watch/v1/11/photo.cnt"
    jpeg = (b"\xff\xd8\xff\xe0" + b"\x00" * 4
            + b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 2000, 3000, 3))
    adb3 = FakeAdb({big_jpeg: jpeg}, listing=f"999 2117629 {big_jpeg}")
    check("过大的图不当表情", EmojiStore(adb3, package="com.xtc.watch").find("") is None)


def test_sticker_forward_one_way() -> None:
    """表情包只走 小天才 -> QQ；原文件优先、截图兜底、失败退文字；QQ -> 小天才 一律文字。"""
    root = tmp_root()

    class _Fwd:
        def __init__(self):
            self.texts: list = []
            self.images: list = []

        def send_detail(self, t, i, m):
            self.texts.append((t, i, m))
            return True, ""

        def send_image(self, t, i, image_b64, caption=""):
            self.images.append((t, i, image_b64, caption))
            return True, ""

    sticker_xml = node_xml(
        n(cls="android.widget.ImageView", text="", desc="屑猹不喝茶发的消息,表情啊啊啊",
          rid="com.xtc.watch:id/chat_msg_item_content", bounds="[948,267][1068,387]"))
    cfg = {"target": {"xtc_contact": "屑猹不喝茶", "qq_private": "2218631043"},
           "xiaotiancai": {"ui": {}}, "webhook": {}, "emoji": {"forward_image": True}}
    try:
        # ① 有原文件 -> 发原图（动图保住动画），截图通道根本不用
        fwd = _Fwd()
        br = bridge_mod.MessageBridge(cfg, adb=None, xtc=None, forwarder=fwd, logger=None)
        br.msgs = MessageLog(path=str(_paths(root)["msgs"]))
        br._cmd_done_file = str(_paths(root)["done"])
        xtc, adb = make_xtc(sticker_xml, focus="com.xtc.watch/.ChatActivity", ui_cfg={})
        br.xtc = xtc
        shot = {"n": 0}
        adb.screencap_crop_png = lambda box: (shot.__setitem__("n", shot["n"] + 1),
                                              b"\x89PNG" + b"y" * 400)[1]

        class _Store:
            def find(self, name, near_epoch=None, aspect=None):
                return {"data": b"GIF89a" + b"z" * 500, "kind": "gif", "w": 90, "h": 90,
                        "animated": True, "path": "/x/y.cnt", "source": "cache"}

        br._emoji_store = _Store()
        br._emoji_from_data = True
        got = br._capture_sticker(ET.fromstring(sticker_xml), "表情啊啊啊")
        check("优先取 App 里的原文件（动图）",
              bool(got) and got["source"] == "cache" and got["animated"] is True, str(got))
        check("拿到原文件就不截图了", shot["n"] == 0, f"screencap={shot['n']}")
        br._forward("屑猹不喝茶", "表情啊啊啊", "13:03", sticker=got)
        check("表情走图片通道", len(fwd.images) == 1 and not fwd.texts,
              f"images={len(fwd.images)} texts={fwd.texts}")
        t, i, b64, caption = fwd.images[0]
        check("图片内容是 base64 GIF",
              t == "private" and i == "2218631043"
              and base64.b64decode(b64).startswith(b"GIF89a"), f"{t}:{i} {b64[:12]}")
        check("说明文字带来源与时间（可关）",
              "屑猹不喝茶" in caption and "13:03" in caption, caption)

        # ② 原文件取不到 -> 退回截图（静态一帧）
        br._emoji_store = type("S", (), {"find": lambda self, name, near_epoch=None,
                                         aspect=None: None})()
        got2 = br._capture_sticker(ET.fromstring(sticker_xml), "表情啊啊啊")
        check("原文件取不到时退回截图",
              bool(got2) and got2["source"] == "screenshot" and shot["n"] == 1, str(got2))

        # ③ 全都没有 -> 退回发文字（不丢消息）
        fwd2 = _Fwd()
        br.forwarder = fwd2
        br._forward("屑猹不喝茶", "表情啊啊啊", "13:03", sticker=None)
        check("没有表情图时走文字（老行为）",
              len(fwd2.texts) == 1 and not fwd2.images, f"{fwd2.texts} {fwd2.images}")

        # ④ 关掉表情图开关 -> 只发文字
        fwd3 = _Fwd()
        cfg3 = dict(cfg)
        cfg3["emoji"] = {"forward_image": False}
        br3 = bridge_mod.MessageBridge(cfg3, adb=None, xtc=None, forwarder=fwd3, logger=None)
        br3.msgs = MessageLog(path=str(_paths(root)["msgs"]))
        br3._cmd_done_file = str(_paths(root)["done"])
        br3.xtc = xtc
        check("emoji.forward_image=false 时不取表情",
              br3._capture_sticker(ET.fromstring(sticker_xml), "表情啊啊啊") is None)
        br3._forward("屑猹不喝茶", "表情啊啊啊", "13:03",
                     sticker=br3._capture_sticker(ET.fromstring(sticker_xml), "表情啊啊啊"))
        check("关掉后只发文字", len(fwd3.texts) == 1 and not fwd3.images, str(fwd3.images))

        # ⑤ 单向：QQ -> 小天才 的发送路径里没有任何图片逻辑
        fwd4 = _Fwd()
        xtc4 = Xiaotiancai(FakeAdb(chat_page_xml(), "com.xtc.watch/.ChatActivity"),
                           {"ui": {}}, logger=None)
        sent = {"n": 0}

        def fake_send(text):
            sent["n"] += 1
            return True

        xtc4.send_message = fake_send
        br4 = bridge_mod.MessageBridge(cfg, adb=None, xtc=xtc4, forwarder=fwd4, logger=None)
        br4.msgs = MessageLog(path=str(_paths(root)["msgs"]))
        br4._cmd_done_file = str(_paths(root)["done"])
        br4._do_send_job("普通文字", "2218631043", "", "")
        check("QQ->小天才 只用文字（图片是单向的）",
              sent["n"] == 1 and not fwd4.images and not fwd4.texts,
              f"sent={sent} images={fwd4.images}")
    finally:
        cleanup(root)


def test_blind_send_fast_typing() -> None:
    """用户报告："从 QQ 发消息到文字出现在输入框要 10 秒"。

    慢在**打字之前的等待**：稳妥流程要先等轮询那次 ~3.8 秒的 dump（操作锁）、再自己读一次
    界面（~3.5 秒）才注入。现在加了"先手打字"：按**上次成功发送留下的坐标**直接点输入框、
    广播注入、点发送，不占锁不读界面 —— 文字 ~1 秒内出现，然后才用一次 dump 复核。
    门闩（防止盲点发错聊天）：近 15 秒内轮询确认过在聊天页 + 标题就是目标联系人 + 有坐标缓存。
    """
    chat_xml = node_xml(
        n(cls="android.widget.TextView", text="屑猹不喝茶", desc="和屑猹不喝茶的聊天",
          rid="com.xtc.watch:id/tv_titleBar_title", bounds="[983,67][1068,92]") +
        n(cls="android.widget.EditText", text="", rid="com.xtc.watch:id/et_chat_text_content",
          bounds="[833,678][1179,721]") +
        n(cls="android.widget.TextView", text="发送", rid="com.xtc.watch:id/tv_send_view",
          bounds="[1179,677][1221,721]"))

    # ① 聊天页标题读取（用于"确实在目标聊天"的门闩）
    adb = FastChatAdb()
    adb.set_ui(title="屑猹不喝茶", input_text="")
    xtc = Xiaotiancai(adb, {"ui": {}}, logger=None)
    root_t = ET.fromstring(adb.xml)
    check("能读到聊天页标题", xtc.chat_title(root_t) == "屑猹不喝茶", xtc.chat_title(root_t))

    # ② 没有坐标缓存 -> 不许先手（走稳妥流程）
    check("没有坐标缓存时不先手", xtc.blind_send_ready() is False)
    staged, why = xtc.begin_blind_send("你好")
    check("没缓存时 begin_blind_send 明确拒绝", staged is False and "缓存" in why, why)

    # ③ 有缓存（模拟上一次发送留下的）-> 先手：点输入框 -> 广播 -> 点发送，顺序要对
    adb.calls.clear()
    xtc._send_cache = {"input": (833, 678, 1179, 721), "point": (1200, 699),
                       "input_point": (1006, 699)}
    xy: dict = {"taps": []}

    def tap_xy(x, y):
        xy["taps"].append((x, y))
        adb.calls.append(f"tap {x},{y}")

    adb.tap = tap_xy
    adb.input_text_plain = lambda text, ensure_ime=True: (
        adb.calls.append(f"input_text_plain {text}"), True)[1]
    xtc._snapshot = ET.fromstring(adb.xml)           # 轮询刚 dump 的快照（只为拿基线）
    xtc._snapshot_ts = time.monotonic()
    check("有缓存+快照时可以先手", xtc.blind_send_ready() is True)
    staged2, why2 = xtc.begin_blind_send("你好")
    check("先手输入成功", staged2 is True, why2)
    check("打字前先确认屏幕是亮的（息屏时点下去等于打在空壳上）",
          "wake_if_asleep" in adb.calls, str(adb.calls))
    check("先点了输入框、再注入、再点发送",
          xy.get("taps") == [(1006, 699), (1200, 699)], str(xy.get("taps")))
    check("注入走的是不做校验的纯广播",
          any(c.startswith("input_text_plain 你好") for c in adb.calls), str(adb.calls))

    # ④ 复核：输入框已清空 + 无新失败提示 -> 确认成功
    adb.set_ui(input_text="", bubble="刚发出的一条")
    ok, why3, retryable = xtc.end_blind_send("你好")
    check("复核通过", ok is True, why3)
    check("成功时不需要重发", retryable is False)

    # ⑤ 复核发现"输入框仍留有内容"（点偏了/没发出去）-> 允许安全重发 + 清掉缓存坐标
    xtc._send_cache = {"input": (833, 678, 1179, 721), "point": (1200, 699),
                       "input_point": (1006, 699)}
    xtc.begin_blind_send("你好")
    adb.set_ui(input_text="你好")                     # 文字还在输入框里
    ok2, why4, retryable2 = xtc.end_blind_send("你好")
    check("没发出去时如实报未确认", ok2 is False and "输入框仍留有内容" in why4, why4)
    check("这种情况允许安全重发", retryable2 is True)
    check("重发前清掉缓存坐标（改用稳妥流程）", xtc._send_cache is None)

    # ⑥ 桥接门闩：太久没确认过聊天页 / 标题不对 -> 不先手
    br = bridge_mod.MessageBridge(
        {"target": {"xtc_contact": "屑猹不喝茶"}, "xiaotiancai": {"ui": {}}, "webhook": {}},
        adb=None, xtc=xtc, forwarder=None, logger=None)
    br.msgs = MessageLog(path=str(_paths(tmp_root())["msgs"]))
    br._cmd_done_file = str(_paths(tmp_root())["done"])
    xtc._send_cache = {"input": (833, 678, 1179, 721), "point": (1200, 699),
                       "input_point": (1006, 699)}
    check("没有最近确认过聊天页时不先手", br._blind_send_allowed("屑猹不喝茶") is False)
    br._chat_ok_ts = time.monotonic()
    br._chat_ok_title = "别的联系人"
    check("标题不是目标联系人时不先手（防发错聊天）",
          br._blind_send_allowed("屑猹不喝茶") is False)
    br._chat_ok_title = "屑猹不喝茶"
    check("确认过目标聊天页且标题一致 -> 允许先手",
          br._blind_send_allowed("屑猹不喝茶") is True)
    br._chat_ok_ts = time.monotonic() - 60
    check("超过 15 秒没再确认过就不先手", br._blind_send_allowed("屑猹不喝茶") is False)


def test_wake_before_relaunch() -> None:
    """用户报"隔三差五就息屏/被判不在前台，其实我什么都没动"。

    WSA 的虚拟屏会随**宿主窗口状态**睡过去（`screen_off_timeout` 拉到极大值、
    `svc power stayon true` 都挡不住 —— 实测设置都在、屏照样睡）。屏一睡：
      * App 看起来"不在前台"（前台变成 com.microsoft.windows.homeapp/PlaceholderActivity）；
      * uiautomator 报 null root node。
    旧逻辑遇到"不在前台"就直接**重新拉起 App**（实测 ~20 秒），而其实窗口还在、
    Activity 还是 resumed —— 叫醒屏幕就回来了。这条用例盯住这个行为。
    """
    # ① 息屏导致"不在前台" -> 先唤醒，不重启 App
    #  （息屏时 dump 出来的是 WSA 主屏的空壳，不是聊天页——所以要按"睡着就换 XML"来模拟）
    adb = FakeAdb("",
                  focus="com.microsoft.windows.homeapp/com.microsoft.windows.placeholder.PlaceholderActivity")
    adb.asleep = True
    adb.screen_on = lambda: not adb.asleep

    def dump_ui(retries=3, delay=2.0):
        return ET.fromstring(node_xml("") if adb.asleep else chat_page_xml())

    adb.dump_ui = dump_ui

    def wake_if_asleep():
        if not adb.asleep:
            return False
        adb.asleep = False
        adb.focus = "com.xtc.watch/com.xtc.wechat.view.chatlist.ChatActivity"
        adb.calls.append("wake_if_asleep")
        return True

    adb.wake_if_asleep = wake_if_asleep
    xtc = Xiaotiancai(adb, {"ui": {}}, logger=None)
    ok = xtc.open_chat("屑猹不喝茶")
    check("息屏时先唤醒就能用（返回成功）", ok is True)
    check("确实走了「息屏先唤醒」这条路", "wake_if_asleep" in adb.calls, str(adb.calls))
    check("没有白白重新拉起 App", not any("am start" in c or "monkey" in c for c in adb.calls),
          str(adb.calls))

    # ② 屏幕亮着、App 是真的不在前台（例如 WSA 停在主屏）-> 才启动 App
    adb2 = FakeAdb(node_xml(n(cls="android.widget.TextView", text="桌面")),
                   focus="com.android.launcher/.Launcher")
    xtc2 = Xiaotiancai(adb2, {"ui": {}}, logger=None)
    xtc2.open_chat("屑猹不喝茶")
    check("真的不在前台才启动 App",
          any("am start" in c or "monkey" in c for c in adb2.calls), str(adb2.calls))
    check("没息屏时不乱唤醒", "wake_up" not in adb2.calls, str(adb2.calls))

    # ③ wake_if_asleep 的语义：睡着才唤醒，并记住"这块屏会睡"（之后 dump 前主动先唤醒）
    from adb_controller import ADBController as _C
    ctl = _C(adb_path="adb")
    ctl.screen_on = lambda: False
    ctl.wake_up = lambda: (setattr(ctl, "_woke", getattr(ctl, "_woke", 0) + 1), True)[1]
    check("睡着时会唤醒", ctl.wake_if_asleep() is True and getattr(ctl, "_woke", 0) == 1)
    check("唤醒后把这块屏标记为可疑（dump 前会先确认）", ctl._screen_suspect is True)
    ctl.screen_on = lambda: True
    check("亮着时不重复唤醒",
          ctl.wake_if_asleep() is False and getattr(ctl, "_woke", 0) == 1)


def test_keep_awake_heartbeat() -> None:
    """轮询里的"别睡"心跳：隔一会儿发一次 WAKEUP，屏不睡 -> App 一直在前台。"""
    import threading

    class PokeAdb(FakeAdb):
        def __init__(self):
            super().__init__("", "com.xtc.watch/.ChatActivity")
            self.pokes = 0

        def is_connected(self) -> bool:
            return True

        def ensure_connected(self) -> bool:
            return True

        def poke_awake(self) -> bool:
            self.pokes += 1
            return True

    calls = {"n": 0}

    class CountingXtc(Xiaotiancai):
        def app_state_with_root(self, attempts: int = 2):
            calls["n"] += 1
            return (self.STATE_CHAT, ET.fromstring(chat_page_xml()))

    root = tmp_root()
    try:
        adb = PokeAdb()
        br = bridge_mod.MessageBridge({"target": {}, "xiaotiancai": {}, "webhook": {}},
                                      adb=adb, xtc=CountingXtc(adb, {"ui": {}}, logger=None),
                                      forwarder=None, logger=Recorder())
        br.msgs = MessageLog(path=str(_paths(root)["msgs"]))
        br._cmd_done_file = str(_paths(root)["done"])
        br._poll_interval = 0.2
        br._keep_awake_interval = 0.25       # 压短以便测试
        br._last_awake_poke = float("-inf")
        br.running = True
        t = threading.Thread(target=br._poll_loop, daemon=True)
        t.start()
        time.sleep(0.9)
        br.running = False
        t.join(3)
        check("轮询会周期性发保活心跳", adb.pokes >= 2, f"pokes={adb.pokes}")
    finally:
        cleanup(root)


class _FwdHist:
    """最简转发器：send/send_detail/send_image 都算成功，并记录发出去的文本。"""

    def __init__(self, ok: bool = True):
        self.sent: list = []
        self.ok = ok

    def send(self, t, i, m):
        self.sent.append(m)
        return self.ok

    def send_detail(self, t, i, m):
        self.sent.append(m)
        return self.ok, ""

    def send_image(self, t, i, b64, caption=""):
        self.sent.append(caption)
        return self.ok, ""


def test_repeated_message_is_forwarded_again() -> None:
    """用户报"未能成功读取消息"的**真凶**：去重只看文本（长期表 TTL 7 天）。

    实测：09-23 转发过的贴纸"表情流汗"，09-25 又发了一次 —— 文本一样，
    `history.seen("xtc", contact, "表情流汗")` 一直为真 -> 这条消息被**静默丢掉**，
    日志里连"收到小天才消息"都没有（用户看到的就是"消息读不出来"）。
    聊天里能出现同一张贴纸/同一句短文本（"好""1""嗯"）无数次，所以身份必须是
    **文本 + 这条消息自己的时间标签**。
    """
    root = tmp_root()
    try:
        fwd = _FwdHist()
        cfg = {"target": {"xtc_contact": "屑猹不喝茶", "qq_private": "2218631043"},
               "xiaotiancai": {"ui": {}}, "webhook": {}}
        br = bridge_mod.MessageBridge(cfg, adb=None, xtc=None, forwarder=fwd, logger=None)
        # 用本用例专属的状态文件：否则会读写真实 data/ 下的历史/回声缓存（用例互相污染）
        prefix = f".bugtest{_SEQ['n']}_"
        br.msgs = MessageLog(path=str(root / f"{prefix}msg_log.json"))
        br.history = bridge_mod.HistoryFilter(store_path=str(root / f"{prefix}history.json"))
        br.echo = bridge_mod.EchoFilter(store_path=str(root / f"{prefix}echo.json"))
        br._cmd_done_file = str(root / f"{prefix}cmd_done.json")

        # 09-23 转发过一次"表情流汗"
        br.history.mark("xtc", "屑猹不喝茶", "表情流汗", "20:58")
        br.msgs.append("xtc", "屑猹不喝茶", "表情流汗", source="手表-屑猹不喝茶")
        check("同一文本 + 同一时间标签 -> 判为已处理",
              br.history.seen("xtc", "屑猹不喝茶", "表情流汗", "20:58") is True)
        check("同一文本 + **不同**时间标签 -> 不算已处理（隔天再发能转发）",
              br.history.seen("xtc", "屑猹不喝茶", "表情流汗", "14:39") is False)

        # 用真实的长文本路径记录一次，确认写入的 key 也带标签
        br._do_forward_job("屑猹不喝茶", "表情流汗", "14:39", sticker=None)
        check("转发成功后按（文本+标签）登记",
              br.history.seen("xtc", "屑猹不喝茶", "表情流汗", "14:39") is True
              and bool(fwd.sent), str(fwd.sent))
        br._queue_forward("屑猹不喝茶", "表情流汗", "15:10", sticker=None)
        check("入队时按（文本+标签）短期去重（同一件事不会每轮重复入队）",
              br.dedup.seen(("xtc", "屑猹不喝茶", "表情流汗", "15:10")) is True
              and br.dedup.seen(("xtc", "屑猹不喝茶", "表情流汗", "15:11")) is False)
    finally:
        cleanup(root)


def test_backlog_runs_without_contact() -> None:
    """补发在**正常轮询**里必须真的被调用：聊天窗口模式下 contact 恒为 None。

    `get_latest_message` 在聊天窗口模式返回的 contact 就是 None，而轮询里写的条件是
    `if state == CHAT and contact is not None:` —— 于是补发**永远不会执行**
    （用户要的"从最新往回走、撞库即停"补发形同虚设）。
    """
    import threading

    class PollAdb(FakeAdb):
        def __init__(self, xml):
            super().__init__(xml, "com.xtc.watch/.ChatActivity")

        def is_connected(self) -> bool:
            return True

        def ensure_connected(self) -> bool:
            return True

        def wake_if_asleep(self) -> bool:
            return False

    root = tmp_root()
    try:
        chat = node_xml(
            n(cls="android.widget.TextView", text="屑猹不喝茶", desc="和屑猹不喝茶的聊天",
              rid="com.xtc.watch:id/tv_titleBar_title", bounds="[983,67][1068,92]") +
            n(cls="android.widget.TextView", text="漏掉的一条",
              desc="屑猹不喝茶发的消息,漏掉的一条",
              rid="com.xtc.watch:id/chat_msg_item_content", bounds="[845,200][965,280]") +
            n(cls="android.widget.EditText", text="", rid="com.xtc.watch:id/et_chat_text_content",
              bounds="[833,678][1179,721]") +
            n(cls="android.widget.TextView", text="发送", rid="com.xtc.watch:id/tv_send_view",
              bounds="[1179,677][1221,721]"))
        adb = PollAdb(chat)
        xtc = Xiaotiancai(adb, {"ui": {}}, logger=None)
        fwd = _FwdHist()
        br = bridge_mod.MessageBridge({"target": {"xtc_contact": "屑猹不喝茶"},
                                       "xiaotiancai": {"ui": {}}, "webhook": {}},
                                      adb=adb, xtc=xtc, forwarder=fwd, logger=Recorder())
        prefix = f".bugtest{_SEQ['n']}_"
        br.msgs = MessageLog(path=str(root / f"{prefix}msg_log.json"))
        br.history = bridge_mod.HistoryFilter(store_path=str(root / f"{prefix}history.json"))
        br.echo = bridge_mod.EchoFilter(store_path=str(root / f"{prefix}echo.json"))
        br._cmd_done_file = str(root / f"{prefix}cmd_done.json")
        br._forward = lambda contact, text, time_label="", sticker=None: (
            fwd.sent.append(text), True)[1]
        br._queue_forward = bridge_mod.MessageBridge._queue_forward.__get__(br)
        br._job_queue = __import__("queue").Queue()
        br.running = True
        t = threading.Thread(target=br._poll_loop, daemon=True)
        t.start()
        time.sleep(1.0)
        br.running = False
        t.join(3)
        jobs = []
        while not br._job_queue.empty():
            jobs.append(br._job_queue.get_nowait())
        check("轮询里补发真的会跑（contact 为 None 也算）",
              any(j[0] == "forward" and j[2] == "漏掉的一条" for j in jobs), str(jobs))
    finally:
        cleanup(root)


def test_forwarder_wrapper_exposes_send_image() -> None:
    """实机事故：`PluginClient` 有 send_image，但桥接用的是 `PluginForwarder` 包装类 ——
    包装层没暴露这个方法，`getattr(forwarder, "send_image", None)` 就是 None，
    于是**每张表情都退化成文字**（日志："转发器不支持图片，改发文字（插件需一并更新）"）。
    所以这里断言"包装层确实有 send_image，并且表情真的走了图片通道"。
    """
    class _Client:
        def __init__(self):
            self.texts: list = []
            self.images: list = []

        def send_detail(self, t, i, m):
            self.texts.append((t, i, m))
            return True, ""

        def send(self, t, i, m):
            return self.send_detail(t, i, m)[0]

        def send_image(self, t, i, b64, caption=""):
            self.images.append((t, i, b64, caption))
            return True, ""

        def reply_result(self, *a, **k):
            return True

        def qq_search(self, *a, **k):
            return None

        def qq_online(self, *a, **k):
            return None

        def qq_remind(self, *a, **k):
            return None

    root = tmp_root()
    try:
        client = _Client()
        fwd = bridge_mod.PluginForwarder(client, logger=None)
        check("包装层必须暴露 send_image（否则表情永远只能发文字）",
              hasattr(fwd, "send_image"))
        ok, why = fwd.send_image("group", "472805002", "QUJD", "说明")
        check("图片转发委托给客户端",
              ok is True and client.images == [("group", "472805002", "QUJD", "说明")],
              f"{client.images} {why}")

        # 端到端：桥接 _forward 带图片时，必须走图片通道而不是文字
        br = bridge_mod.MessageBridge(
            {"target": {"xtc_contact": "屑猹不喝茶", "qq_private": "2218631043"},
             "xiaotiancai": {"ui": {}}, "webhook": {}},
            adb=None, xtc=None, forwarder=fwd, logger=None)
        br.msgs = MessageLog(path=str(_paths(root)["msgs"]))
        br._cmd_done_file = str(_paths(root)["done"])
        client.images.clear()
        client.texts.clear()
        br._forward("屑猹不喝茶", "表情弹吉他.png", "15:24",
                    sticker={"data": b"GIF89a" + b"z" * 400, "kind": "gif",
                             "animated": True, "source": "cache"})
        check("有表情图时走图片通道（不再退化成文字）",
              len(client.images) == 1 and not client.texts,
              f"images={client.images} texts={client.texts}")
        check("图片是 base64 的 GIF",
              base64.b64decode(client.images[0][2]).startswith(b"GIF89a"),
              client.images[0][2][:12])

        # 客户端不支持图片时如实报告失败（调用方会退回文字，但日志要说清原因）
        class _Old:
            def send_detail(self, t, i, m):
                return True, ""

        old = bridge_mod.PluginForwarder(_Old(), logger=None)
        ok2, why2 = old.send_image("private", "1", "QUJD")
        check("旧客户端不支持图片时返回失败原因",
              ok2 is False and "不支持图片" in why2, why2)
    finally:
        cleanup(root)


def test_cache_pick_prefers_sticker_shape_and_animation() -> None:
    """缓存里同时有照片和贴纸时，要挑**贴纸**：正方形 + 动图，而不是"时间最新的那张"。

    实机数据：同一时刻缓存里有 4 张（JPEG 145x145、GIF 动图 240x240、PNG 243x324、
    PNG 416x416），纯按时间分不出哪张是表情 —— 按"形状与气泡一致 + 是动图"才选得对。
    """
    from emoji_store import EmojiStore

    root = "/sdcard/Android/data/com.xtc.watch"
    jpeg = (b"\xff\xd8\xff\xe0" + b"\x00" * 4
            + b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 145, 145, 3) + b"\x00" * 200)
    gif = (b"GIF89a" + struct.pack("<HH", 240, 240) + b"\x00" * 8
           + b"NETSCAPE2.0" + b"\x21\xf9\x04" * 6 + b";")
    png_wide = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 243, 324)
                + b"\x08\x06\x00\x00\x00")
    png_big = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 416, 416)
               + b"\x08\x06\x00\x00\x00")
    now = 1790322520
    files = {
        f"{root}/cache/big_image/x/1/a.cnt": jpeg,
        f"{root}/cache/big_image/x/1/b.cnt": gif,
        f"{root}/cache/big_image/x/1/c.cnt": png_wide,
        f"{root}/cache/big_image/x/1/d.cnt": png_big,
    }
    listing = "\n".join(f"{now - 460} {len(v)} {k}" for k, v in files.items())

    class FakeAdb:
        def shell(self, cmd, timeout=None):
            return f"{now}\n{listing}" if cmd.startswith("date +%s;") else ""

        def read_file(self, path, timeout=None):
            return files.get(path, b"")

    store = EmojiStore(FakeAdb(), package="com.xtc.watch", logger=None)
    got = store.find("弹吉他", near_epoch=now - 1021, aspect=1.0)
    check("挑出的是那张动图贴纸（不是照片/宽图）",
          bool(got) and got["kind"] == "gif" and got["animated"] is True
          and got["w"] == 240, str({k: v for k, v in (got or {}).items() if k != "data"}))
    check("别的候选（JPEG/宽 PNG）没被选中",
          (got or {}).get("path", "").endswith("b.cnt"), str((got or {}).get("path")))
    # 没有形状信息时也要优先动图（而不是 JPEG 照片）
    got2 = store.find("弹吉他", near_epoch=now - 1021)
    check("没有气泡形状时仍优先动图",
          bool(got2) and got2["animated"] is True, str({k: v for k, v in (got2 or {}).items() if k != "data"}))


def test_time_label_is_absolute_and_stable() -> None:
    """App 的时间标签会**随日期变化**：同一条消息当天是 `16:18`，第二天变成 `昨天 16:18`。

    两个后果（都踩过）：
      * 转发文本里出现"昨天"这种相对时间，而不是绝对的 `09-25 16:18`；
      * 拿原始标签当"消息身份" -> 隔天标签一变，这条**旧消息就被当成新消息重复转发**。
    所以标签必须先统一成绝对时间，既用于显示也用于去重身份。
    """
    br = bridge_mod.MessageBridge.__new__(bridge_mod.MessageBridge)   # 只借用这几个纯函数
    d1 = datetime(2026, 9, 25, 16, 20)
    d2 = datetime(2026, 9, 26, 15, 53)

    check("当天标签 -> 绝对", br._abs_time_label("16:18", d1) == "09-25 16:18",
          br._abs_time_label("16:18", d1))
    check("隔天读到「昨天」-> 同一个绝对时间（身份稳定，不会重复转发）",
          br._abs_time_label("昨天 16:18", d2) == "09-25 16:18",
          br._abs_time_label("昨天 16:18", d2))
    check("两种读法算出的去重键完全相同",
          ("xtc", "", "表情流汗", br._abs_time_label("16:18", d1))
          == ("xtc", "", "表情流汗", br._abs_time_label("昨天 16:18", d2)))
    check("前天/星期X/月日 都能绝对化",
          br._abs_time_label("前天 09:05", d2) == "09-24 09:05"
          and br._abs_time_label("8月30日 16:18", d2) == "08-30 16:18"
          and br._abs_time_label("星期一 16:18", d2) == "09-21 16:18",
          f"{br._abs_time_label('前天 09:05', d2)} "
          f"{br._abs_time_label('8月30日 16:18', d2)} "
          f"{br._abs_time_label('星期一 16:18', d2)}")
    check("已经是绝对时间的标签保持原样",
          br._abs_time_label("09-25 16:18", d2) == "09-25 16:18")
    check("只写月日且落在未来的按去年算（12月31日）",
          br._abs_time_label("12月31日 23:59", d2).endswith("12-31 23:59")
          and datetime.fromtimestamp(br._label_epoch("12月31日 23:59", d2)).year == 2025,
          str(datetime.fromtimestamp(br._label_epoch("12月31日 23:59", d2))))
    check("认不出的标签不硬编（返回空，交给调用方兜底）",
          br._abs_time_label("乱七八糟", d2) == "")

    # 转发文本：永远不带"昨天/前天/今天/星期"
    for raw in ("16:18", "今天 16:18", "昨天 16:18", "前天 16:18", "星期一 16:18",
                "8月30日 16:18", "09-25 16:18", "", "乱七八糟"):
        text = br._format_xtc_time(raw)
        if not re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}", text):
            check(f"转发时间必须是绝对的 MM-DD HH:MM（输入 {raw!r}）", False, text)
            break
    else:
        check("转发时间永远是绝对的 MM-DD HH:MM（含相对标签与空标签）", True)


def test_confirm_sent_rule() -> None:
    """_confirm_sent 的判定规则单测（新气泡 / 失败提示 / 输入框残留 / 读不到界面）。"""
    adb = ChatAdb()
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}}, logger=None)

    adb.set_ui(input_text="晚上回家吃饭")
    ok, why = xtc._confirm_sent("晚上回家吃饭", [], {})
    check("输入框仍有内容 -> 未确认", ok is False, why)

    adb.set_ui(input_text="", tip="网络异常，发送失败")
    ok2, why2 = xtc._confirm_sent("晚上回家吃饭", [], {})
    check("新失败提示 -> 未确认", ok2 is False, why2)

    adb.set_ui(input_text="", tip="网络异常，发送失败")
    ok3, why3 = xtc._confirm_sent("晚上回家吃饭", ["网络异常，发送失败"], {})
    check("同一条旧提示（基线里已有）不算新失败", ok3 is True, why3)

    adb.set_ui(input_text="", bubble="晚上回家吃饭")
    ok4, why4 = xtc._confirm_sent("晚上回家吃饭", [], {})
    check("出现新己方气泡 -> 确认成功", ok4 is True, why4)

    adb.set_ui(input_text="", bubble="晚上回家吃饭")
    ok5, why5 = xtc._confirm_sent("晚上回家吃饭", [], {"晚上回家吃饭": 1})
    check("同文本旧气泡不算新气泡（仍按输入框判定）", ok5 is True, why5)


# ------------------------------------------------------------------ 7. 不重复启动 / 按需自愈
def test_launch_skips_when_foreground() -> None:
    """App 已在前台时 launch() 不能再启动一次（用户报告"已经启动了还启动"）。"""
    adb = FakeAdb(node_xml(n(text="微聊")), focus="com.xtc.watch/.MainActivity")
    xtc = Xiaotiancai(adb, {}, logger=None)
    check("在前台时 launch 直接返回 True", xtc.launch() is True)
    check("没有发出启动命令",
          not any(("am start" in c) or ("monkey" in c) for c in adb.calls), str(adb.calls))

    # 不在前台时才启动
    adb2 = FakeAdb(node_xml(n(text="桌面")), focus="com.android.launcher/.Launcher")
    xtc2 = Xiaotiancai(adb2, {}, logger=None)
    check("不在前台时才启动", xtc2.launch() is True)
    check("确实执行了启动命令", any("am start" in c for c in adb2.calls), str(adb2.calls[:3]))


def test_recover_is_state_driven() -> None:
    """recover() 按需执行：缺什么补什么。"""
    xml = node_xml(
        n(cls="android.widget.EditText", text="",
          rid="com.xtc.watch:id/et_chat_text_content") + n(text="微聊"))
    adb = FakeAdb(xml, focus="com.xtc.watch/.ChatActivity")
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}}, logger=None)
    state = xtc.recover("张三")
    check("已在前台且在聊天页 -> 不启动不导航", "启动" not in state, state)
    check("自愈结果可读", isinstance(state, str) and bool(state), state)


# ------------------------------------------------------------------ 7. 弹窗自动处理
def _dismiss(xml: str, focus: str = "com.xtc.watch/.MainActivity",
             ui_cfg: dict | None = None) -> tuple:
    adb = FakeAdb(xml, focus=focus)
    xtc = Xiaotiancai(adb, {"ui": ui_cfg or {"interaction_delay": 0.1}}, logger=None)
    handled = xtc._dismiss_blockers()
    return handled, [c for c in adb.calls if c.startswith("tap")]


def test_popup_handling() -> None:
    """常见弹窗要能自己关掉（权限/更新/网络/无响应/关闭按钮），正常页面不能误点。"""
    # 权限弹窗
    perm = node_xml(
        n(cls="android.widget.Button", text="允许", bounds="[0,500][100,560]",
          rid="com.android.permissioncontroller:id/permission_allow_foreground_only_button") +
        n(cls="android.widget.Button", text="拒绝", bounds="[0,600][100,660]"))
    handled, taps = _dismiss(perm, focus="com.android.permissioncontroller/.GrantPermissionsActivity")
    check("权限弹窗被处理且点的是『允许』", handled and taps == ["tap [0,500][100,560]"], str(taps))

    # 更新/活动弹窗 -> 点"以后再说"（不能点"立即更新"）
    upd = node_xml(
        n(cls="android.widget.TextView", text="发现新版本", bounds="[0,200][100,260]",
          rid="com.xtc.watch:id/tv_title") +
        n(cls="android.widget.TextView", text="立即更新", bounds="[0,300][100,360]") +
        n(cls="android.widget.TextView", text="以后再说", bounds="[0,400][100,460]"))
    handled2, taps2 = _dismiss(upd)
    check("更新弹窗点『以后再说』", handled2 and taps2 == ["tap [0,400][100,460]"], str(taps2))

    # 网络异常弹窗 -> 关掉提示
    net = node_xml(
        n(cls="android.widget.TextView", text="网络异常，请检查网络", bounds="[0,300][100,360]",
          rid="com.xtc.watch:id/tv_title") +
        n(cls="android.widget.TextView", text="知道了", bounds="[0,700][100,760]"))
    handled3, taps3 = _dismiss(net)
    check("网络弹窗点『知道了』", handled3 and taps3 == ["tap [0,700][100,760]"], str(taps3))

    # 应用无响应 -> 点"等待"（不能点"关闭应用"杀掉 App）
    anr = node_xml(
        n(cls="android.widget.TextView", text="小天才 无响应", bounds="[0,100][100,160]") +
        n(cls="android.widget.Button", text="等待", bounds="[0,200][100,260]") +
        n(cls="android.widget.Button", text="关闭应用", bounds="[0,300][100,360]"))
    handled4, taps4 = _dismiss(anr, focus="com.android.systemui/.AppErrorDialog")
    check("无响应弹窗点『等待』而不是『关闭应用』",
          handled4 and taps4 == ["tap [0,200][100,260]"], str(taps4))

    # 关闭按钮（id）
    close = node_xml(
        n(cls="android.widget.ImageView", text="", bounds="[900,100][960,160]",
          rid="com.xtc.watch:id/iv_close") +
        n(cls="android.widget.TextView", text="广告位招租", bounds="[0,400][100,460]"))
    handled5, taps5 = _dismiss(close)
    check("带 iv_close 的浮层被关闭", handled5 and taps5 == ["tap [900,100][960,160]"], str(taps5))

    # 正常聊天页：绝不能被"处理弹窗"误点/误按返回
    normal = node_xml(
        n(cls="android.widget.EditText", text="", rid="com.xtc.watch:id/et_chat_text_content") +
        n(cls="android.widget.TextView", text="发送", rid="com.xtc.watch:id/tv_send_view") +
        n(cls="android.widget.TextView", text="微聊") + n(cls="android.widget.TextView", text="我的"))
    handled6, taps6 = _dismiss(normal, focus="com.xtc.watch/.ChatActivity")
    check("正常聊天页不误点", (not handled6) and (not taps6), str(taps6))
    check("正常聊天页不按返回键", not any("keyevent 4" in t for t in taps6))


# ---- 自定义 Activity 弹窗（实机抓下来的"升级提醒"） ----
POPUP_FOCUS = "com.xtc.watch/com.xtc.widget.phone.popup.activity.CustomActivity14"


def upgrade_popup_xml(left_text: str = "不更新", right_text: str = "立即安装",
                      title: str = "升级提醒",
                      desc: str = "小天才APP版本已下载完成，现在可以安装并开始体验了。") -> str:
    """实机 dump 到的结构：title/desc + btn_left(负向) + btn_right(正向)。"""
    return node_xml(
        n(cls="android.widget.TextView", text=title, bounds="[979,339][1279,364]",
          rid="com.xtc.watch:id/title") +
        n(cls="android.widget.TextView", text=desc, bounds="[979,374][1275,421]",
          rid="com.xtc.watch:id/desc") +
        n(cls="android.widget.TextView", text=left_text, bounds="[995,503][1125,543]",
          rid="com.xtc.watch:id/btn_left") +
        n(cls="android.widget.TextView", text=right_text, bounds="[1133,503][1263,543]",
          rid="com.xtc.watch:id/btn_right"))


def test_custom_popup_auto_close() -> None:
    """用户报告：小天才"升级提醒"弹窗盖住界面后，消息一直读不到（还老提示找不到联系人）。

    实机抓包（CustomActivity14）：btn_left="不更新" 是负向按钮，btn_right="立即安装"
    是正向按钮。它既不是 PopupWindow 也不是 Dialog，Activity 名里只有 `popup`，
    以前完全认不出来，所以永远不会被自动关掉。要求：能自动关 + 绝不点"立即安装"。
    """
    # (a) 实机结构：点"不更新"，绝不点"立即安装"
    handled, taps = _dismiss(upgrade_popup_xml(), focus=POPUP_FOCUS)
    check("升级提醒弹窗被自动关掉", handled is True)
    check("点的是负向按钮「不更新」", taps == ["tap [995,503][1125,543]"], str(taps))
    check("绝不点「立即安装」", "tap [1133,503][1263,543]" not in taps, str(taps))

    # (b) 文案换成没见过的（按钮="算了"/"马上安装"）：结构认得出来，仍点负向按钮
    unknown = upgrade_popup_xml(left_text="算了", right_text="马上安装",
                                title="需要你的确认", desc="请选择是否继续。")
    handled2, taps2 = _dismiss(unknown, focus=POPUP_FOCUS)
    check("没见过的弹窗文案也能按结构关掉",
          handled2 and taps2 == ["tap [995,503][1125,543]"], str(taps2))

    # (c) 只有正向按钮（没有安全的负向按钮）-> 按返回键关闭，而不是点"立即安装"
    only_pos = node_xml(
        n(cls="android.widget.TextView", text="有新版本", bounds="[0,100][400,160]",
          rid="com.xtc.watch:id/title") +
        n(cls="android.widget.TextView", text="立即安装", bounds="[0,300][400,360]",
          rid="com.xtc.watch:id/btn_right"))
    adb_c = FakeAdb(only_pos, focus=POPUP_FOCUS)
    handled3 = Xiaotiancai(adb_c, {"ui": {"interaction_delay": 0.1}}, logger=None)._dismiss_blockers()
    check("没有负向按钮时按返回键关闭",
          handled3 is True and adb_c.calls == ["keyevent 4"], str(adb_c.calls))

    # (d) 关不掉的弹窗：最多点一次负向按钮 + 按一次返回，之后不再反复点（也不刷屏）
    adb_d = FakeAdb(upgrade_popup_xml(left_text="算了", right_text="马上安装",
                                      title="需要你的确认", desc="请选择是否继续。"),
                    focus=POPUP_FOCUS)
    rec = Recorder()
    xtc_d = Xiaotiancai(adb_d, {"ui": {"interaction_delay": 0.1}}, logger=rec)
    r1 = xtc_d._dismiss_blockers()          # 第 1 轮：点负向按钮
    r2 = xtc_d._dismiss_blockers()          # 第 2 轮：按返回键
    calls_before = list(adb_d.calls)
    r3 = xtc_d._dismiss_blockers()          # 第 3 轮：放弃，不再动作
    r4 = xtc_d._dismiss_blockers()
    check("关不掉时前两轮分别点按钮/按返回",
          r1 and r2 and adb_d.calls[:2] == ["tap [995,503][1125,543]", "keyevent 4"],
          str(adb_d.calls))
    check("第 3 轮起不再反复点", (r3 is False) and (r4 is False))
    check("放弃后没有再发出任何点击/按键", adb_d.calls == calls_before, str(adb_d.calls))
    stuck = [l for l in rec.lines if "无法自动关闭" in l]
    stuck_warn = [l for l in stuck if l.startswith("[warning]")]
    check("关不掉时给出可排查的提示（warning 只报一次，其余降级 debug）",
          len(stuck_warn) == 1 and all(l.startswith("[debug]") for l in stuck[1:]),
          str(stuck))

    # (e) 状态判定：弹窗 -> popup（不是 chat/other），弹窗关掉后 -> chat
    adb_e = FakeAdb(upgrade_popup_xml(), focus=POPUP_FOCUS)
    xtc_e = Xiaotiancai(adb_e, {"ui": {"interaction_delay": 0.1}}, logger=None)
    check("弹窗遮挡 -> STATE_POPUP", xtc_e.app_state() == Xiaotiancai.STATE_POPUP,
          xtc_e.app_state())
    check("弹窗状态有中文说明", Xiaotiancai.STATE_TEXT.get(Xiaotiancai.STATE_POPUP) == "弹窗遮挡界面")
    adb_e.xml = chat_page_xml()
    adb_e.focus = "com.xtc.watch/com.xtc.wechat.view.chatlist.ChatActivity"
    check("弹窗关掉后 -> STATE_CHAT", xtc_e.app_state() == Xiaotiancai.STATE_CHAT,
          xtc_e.app_state())

    # (f) 危险按钮判定：正向禁点，负向安全
    xtc_f = Xiaotiancai(FakeAdb(), {}, logger=None)

    def blocked(text: str, rid: str = "") -> bool:
        root = ET.fromstring(node_xml(n(cls="android.widget.TextView", text=text, rid=rid)))
        return xtc_f._popup_blocked(next(iter(root.iter("node"))))

    check("「立即安装」禁点", blocked("立即安装") is True)
    check("「马上更新」禁点", blocked("马上更新") is True)
    check("btn_right 一律禁点", blocked("继续", "com.xtc.watch:id/btn_right") is True)
    check("「不更新」可点（负向）", blocked("不更新") is False)
    check("「暂不安装」可点（负向）", blocked("暂不安装") is False)

    # (g) 自定义跳过文案（config）生效；没有 id 的按钮也能按文案点到
    custom = node_xml(
        n(cls="android.widget.TextView", text="温馨提示", bounds="[0,100][400,160]",
          rid="com.xtc.watch:id/title") +
        n(cls="android.widget.TextView", text="不了", bounds="[0,300][400,360]") +
        n(cls="android.widget.TextView", text="立即安装", bounds="[0,400][400,460]"))
    handled_g, taps_g = _dismiss(custom, focus=POPUP_FOCUS,
                                 ui_cfg={"interaction_delay": 0.1,
                                         "popup_skip_texts": ["不了"]})
    check("config 自定义跳过文案生效",
          handled_g and taps_g == ["tap [0,300][400,360]"], str(taps_g))
    # 没有配置时「不了」认不出来，但结构 + 无负向按钮 -> 返回键（不会点"立即安装"）
    adb_h = FakeAdb(custom, focus=POPUP_FOCUS)
    handled_h = Xiaotiancai(adb_h, {"ui": {"interaction_delay": 0.1}}, logger=None)._dismiss_blockers()
    check("认不出的按钮不会误点「立即安装」",
          handled_h is True and adb_h.calls == ["keyevent 4"], str(adb_h.calls))


# ------------------------------------------------------------------ 10. 聊天页判定 / 联系人查找
class Recorder:
    """收集日志文本的假 logger（断言"日志里有没有给出可排查的信息"）。"""

    def __init__(self):
        self.lines: list = []

    def _add(self, level, msg):
        self.lines.append(f"[{level}] {msg}")

    def info(self, m):
        self._add("info", m)

    def warning(self, m):
        self._add("warning", m)

    def error(self, m):
        self._add("error", m)

    def debug(self, m):
        self._add("debug", m)


def chat_page_xml(bubble: str = "晚上回家吃饭") -> str:
    """聊天页：有消息气泡 + 输入栏。"""
    return node_xml(
        n(cls="android.widget.TextView", text=bubble, bounds="[60,1200][600,1280]",
          rid="com.xtc.watch:id/chat_msg_item_content", desc=f"王五发的消息,{bubble}") +
        n(cls="android.widget.EditText", text="", bounds="[40,1700][900,1800]",
          rid="com.xtc.watch:id/et_chat_text_content") +
        n(cls="android.widget.TextView", text="发送", bounds="[920,1700][1060,1800]",
          rid="com.xtc.watch:id/tv_send_view"))


def chat_page_bubbles_only() -> str:
    """只认得出消息气泡的聊天页（语音模式 + 输入栏 id 变了）。"""
    return node_xml(
        n(cls="android.widget.TextView", text="在吗", bounds="[60,1200][600,1280]",
          rid="com.xtc.watch:id/chat_msg_item_content", desc="王五发的消息,在吗") +
        n(cls="android.widget.ImageView", text="", bounds="[40,1700][200,1800]",
          rid="com.xtc.watch:id/iv_voice_switch_v2"))


def message_list_xml(rows: list) -> str:
    """消息列表：每行 联系人名 + 消息预览（行本身可点击）。"""
    nodes = ""
    for i, (name, preview) in enumerate(rows):
        y = 200 + i * 200
        nodes += (f'<node class="android.widget.FrameLayout" clickable="true" '
                  f'bounds="[0,{y}][1080,{y + 160}]">'
                  + n(cls="android.widget.TextView", text=name, bounds=f"[40,{y}][400,{y + 60}]",
                      rid="com.xtc.watch:id/tv_chat_dialog_name")
                  + n(cls="android.widget.TextView", text=preview,
                      bounds=f"[40,{y + 60}][800,{y + 120}]",
                      rid="com.xtc.watch:id/tv_chat_dialog_last_msg_content")
                  + "</node>")
    return node_xml(nodes)


def test_chat_page_detection() -> None:
    """用户反馈：明明已经在聊天界面，程序却认为在主页（于是报"找不到联系人"）。"""
    adb = FakeAdb(chat_page_xml(), focus="com.xtc.watch/.ChatActivity")
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}})
    check("ChatActivity：判定在聊天页", xtc.is_in_chat() is True)

    adb2 = FakeAdb(chat_page_bubbles_only(), focus="com.xtc.watch/.SomeUnknownActivity")
    xtc2 = Xiaotiancai(adb2, {"ui": {"interaction_delay": 0.1}})
    check("只有消息气泡、Activity 名不认识 -> 仍判定在聊天页", xtc2.is_in_chat() is True)
    check("聊天页判定可复用传入的 root",
          xtc2.is_in_chat(root=ET.fromstring(chat_page_bubbles_only())) is True)

    adb3 = FakeAdb(message_list_xml([("李四", "早"), ("王五", "晚安")]),
                   focus="com.xtc.watch/.ChatListActivity")
    xtc3 = Xiaotiancai(adb3, {"ui": {"interaction_delay": 0.1}})
    check("聊天列表页不能算聊天页", xtc3.is_in_chat() is False)
    check("ChattingActivity 也算聊天页",
          xtc3._activity_is_chat("com.xtc.watch/.ui.ChattingActivity") is True)
    check("ChatListActivity 不算聊天页",
          xtc3._activity_is_chat("com.xtc.watch/.ChatListActivity") is False)
    check("MainActivity 不算聊天页",
          xtc3._activity_is_chat("com.xtc.watch/.MainActivity") is False)
    check("别的 App 的聊天页不算",
          xtc3._activity_is_chat("com.other.app/.ChatActivity") is False)


def test_open_chat_when_already_in_chat() -> None:
    """已经在聊天页时，open_chat 必须直接成功，绝不能去列表里找联系人。"""
    for focus in ("com.xtc.watch/.ChatActivity", "com.xtc.watch/.WhateverActivity"):
        adb = FakeAdb(chat_page_xml(), focus=focus)
        rec = Recorder()
        xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}}, logger=rec)
        ok = xtc.open_chat("张三")
        check(f"已在聊天页时 open_chat 直接成功（focus={focus.split('.')[-1]}）", ok is True)
        check("没有报『找不到联系人』", not any("找不到联系人" in m for m in rec.lines),
              str(rec.lines[-1:]))


def test_contact_name_matching() -> None:
    """联系人匹配要容忍空格/别名差异，并返回可点击的整行。"""
    xml = message_list_xml([("李四", "早"), ("张 三", "晚上吃啥")])
    root = ET.fromstring(xml)
    adb = FakeAdb(xml, focus="com.xtc.watch/.MainActivity")
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}})
    node = xtc._find_contact_node(root, "张三")
    check("忽略空格差异也能匹配到联系人", node is not None,
          node.get("text") if node is not None else "(无)")
    check("返回的是可点击的整行（而不是小文字）",
          node is not None and (node.get("clickable") == "true" or node.get("bounds") == "[0,200][1080,360]"),
          node.get("class", "") if node is not None else "")
    check("带别名括号时用括号前部分匹配",
          xtc._find_contact_node(root, "张三(爸爸)") is not None)
    check("完全不存在时返回 None", xtc._find_contact_node(root, "王五") is None)
    check("候选名生成正确",
          xtc._contact_candidates("张三（爸爸）")[0] == "张三（爸爸）"
          and "张三" in xtc._contact_candidates("张三（爸爸）"),
          str(xtc._contact_candidates("张三（爸爸）")))


def test_contact_matching_when_ids_differ() -> None:
    """联系人名控件的 id 与预期不同（版本差异）时，仍要能按行结构匹配 + 给出诊断。"""
    rows = ""
    for i, (name, preview) in enumerate([("李四", "早"), ("张三", "在吗")]):
        y = 200 + i * 200
        rows += (f'<node class="android.widget.LinearLayout" clickable="true" '
                 f'bounds="[0,{y}][1080,{y + 160}]">'
                 + n(cls="android.widget.TextView", text=name, bounds=f"[40,{y}][400,{y + 60}]",
                     rid="com.xtc.watch:id/tv_chat_name_v9")          # 不认识的 id
                 + n(cls="android.widget.TextView", text=preview,
                     bounds=f"[40,{y + 60}][800,{y + 120}]",
                     rid="com.xtc.watch:id/tv_chat_dialog_last_msg_content")
                 + "</node>")
    xml = node_xml(rows)
    root = ET.fromstring(xml)
    adb = FakeAdb(xml, focus="com.xtc.watch/.MainActivity")
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}})
    check("名字控件 id 不认识时，按行结构仍能找到联系人",
          xtc._find_contact_node(root, "张三") is not None)
    check("诊断用的可见联系人也按行结构取到",
          set(xtc._visible_contact_names(root)) == {"李四", "张三"},
          str(xtc._visible_contact_names(root)))
    check("该页面仍被识别为消息列表", xtc.looks_like_message_list(root) is True)


def test_contact_not_found_reports_visible_names() -> None:
    """真的找不到时，日志要说明"当前页有什么"，而不是一句干巴巴的找不到。"""
    xml = message_list_xml([("李四", "早"), ("王五", "晚安")])
    rec = Recorder()
    adb = FakeAdb(xml, focus="com.xtc.watch/.MainActivity")
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}}, logger=rec)
    check("找不到联系人返回 False", xtc.open_chat("张三") is False)
    joined = " ".join(rec.lines)
    check("日志给出当前可见联系人", "李四" in joined and "王五" in joined, joined[-200:])
    check("日志提示是消息列表", "消息列表" in joined, joined[-200:])
    check("日志给出可操作建议（改配置）", "xtc_contact" in joined or "contact_name_ids" in joined)


def test_contact_found_after_scroll() -> None:
    """联系人在屏幕外时应滑动查找（列表页才滑）。"""
    first = message_list_xml([("李四", "早"), ("王五", "晚安")])
    second = message_list_xml([("张三", "在吗"), ("王五", "晚安")])

    class ScrollAdb(FakeAdb):
        def __init__(self):
            super().__init__(first, focus="com.xtc.watch/.MainActivity")
            self.swipes = 0

        def swipe(self, *a, **kw):
            self.swipes += 1
            self.xml = second
            self.calls.append("swipe")

        def tap_element(self, node):
            super().tap_element(node)
            self.xml = chat_page_xml()
            self.focus = "com.xtc.watch/.ChatActivity"

    adb = ScrollAdb()
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}})
    check("联系人不在首屏：滑动后找到并进入聊天", xtc.open_chat("张三") is True)
    check("确实滑动过列表", adb.swipes >= 1, f"swipes={adb.swipes}")


def test_message_list_detection() -> None:
    """"当前页到底是不是消息列表"要能判断（用于给出准确诊断）。"""
    adb = FakeAdb(message_list_xml([("李四", "早"), ("王五", "晚安")]))
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}})
    check("有 2 行以上联系人 -> 是消息列表",
          xtc.looks_like_message_list(ET.fromstring(message_list_xml([("李四", "早"), ("王五", "晚安")]))) is True)
    check("聊天页不算消息列表",
          xtc.looks_like_message_list(ET.fromstring(chat_page_xml())) is False)


# ------------------------------------------------------------------ 12. 登录态不再误报
def test_login_state_uses_ui_not_activity_name() -> None:
    """用户反馈：已经登录了还提示未登录。

    旧实现只要 Activity 名里含 "login" 就判定未登录（例如 AccountVerifyLoginActivity），
    现在以**界面证据**为准，Activity 名只作兜底。
    """
    # 已登录：消息列表 + Activity 名里带 login（旧实现会误报未登录）
    logged_in_xml = message_list_xml([("李四", "早"), ("王五", "晚安")])
    adb = FakeAdb(logged_in_xml,
                  focus="com.xtc.watch/com.xtc.choneclick.activity.AccountVerifyLoginActivity")
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}})
    check("界面是消息列表 -> 判定已登录（即使 Activity 名含 login）",
          xtc.login_state(force=True) == "logged_in",
          f"{xtc.login_state(force=True)} @ {xtc.current_activity()}")
    check("is_logged_in 同步为 True",
          xtc.is_logged_in(force=True) is True)

    # 已登录：聊天页 + 陌生 Activity 名
    adb2 = FakeAdb(chat_page_xml(), focus="com.xtc.watch/.SomeUnknownActivity")
    xtc2 = Xiaotiancai(adb2, {"ui": {"interaction_delay": 0.1}})
    check("界面是聊天页 -> 判定已登录", xtc2.login_state(force=True) == "logged_in")

    # 明确未登录：短信登录页（真实抓到的界面）
    sms_login = node_xml(
        n(cls="android.widget.TextView", text="短信验证码登录") +
        n(cls="android.widget.TextView", text="请填写手机号") +
        n(cls="android.widget.TextView", text="获取验证码") +
        n(cls="android.widget.TextView", text="账号密码登录｜注册｜手机号不再使用") +
        n(cls="android.widget.EditText", text="", rid="com.xtc.watch:id/et_verify_account"))
    adb3 = FakeAdb(sms_login,
                   focus="com.xtc.watch/com.xtc.choneclick.activity.AccountVerifyLoginActivity")
    xtc3 = Xiaotiancai(adb3, {"ui": {"interaction_delay": 0.1}})
    check("界面是短信登录页 -> 判定未登录", xtc3.login_state(force=True) == "not_logged_in")
    check("require_login 在明确未登录时返回 False", xtc3.require_login() is False)


def test_login_state_unknown_instead_of_not_logged_in() -> None:
    """读不到界面（息屏/锁屏/null root）时必须是 'unknown'，绝不能报"未登录"。"""
    adb = FakeAdb(message_list_xml([("李四", "早")]), focus="com.xtc.watch/.MainActivity")
    adb.fail_dump = True if hasattr(adb, "fail_dump") else False

    class BlindAdb(FakeAdb):
        def dump_ui(self, retries: int = 3, delay: float = 2.0):
            raise AdbError("模拟界面读不到（null root node）")

    blind = BlindAdb("", focus="com.xtc.watch/.MainActivity")
    xtc = Xiaotiancai(blind, {"ui": {"interaction_delay": 0.1}})
    check("读不到界面 -> unknown", xtc.login_state(force=True) == "unknown",
          xtc.login_state(force=True))
    check("unknown 时 is_logged_in 为 False（但不代表未登录）",
          xtc.is_logged_in(force=True) is False)

    other = FakeAdb(message_list_xml([("李四", "早")]), focus="com.android.launcher/.Launcher")
    xtc2 = Xiaotiancai(other, {"ui": {"interaction_delay": 0.1}})
    check("App 不在前台 -> unknown", xtc2.login_state(force=True) == "unknown")
    check("unknown 不会被打成未登录（不打印未登录告警）",
          xtc2.require_login() is False)


def test_auto_login_decision_skips_unknown() -> None:
    """桥接层：只有明确未登录才触发自动登录（unknown 必须跳过）。"""
    cfg = {"target": {}, "webhook": {},
           "xiaotiancai": {"login": {"phone": "13800000000", "password": "pw"}}}
    br = bridge_mod.MessageBridge(cfg, adb=None, xtc=None, forwarder=None, logger=None)
    check("已登录 -> 不动作", br._auto_login_decision("logged_in") == "none")
    check("无法判断 -> 不动作（修复『已登录却提示未登录』）",
          br._auto_login_decision("unknown") == "none")
    check("明确未登录且有账密 -> 触发登录",
          br._auto_login_decision("not_logged_in") == "login")

    br2 = bridge_mod.MessageBridge({"target": {}, "xiaotiancai": {}, "webhook": {}},
                                   adb=None, xtc=None, forwarder=None, logger=None)
    check("明确未登录但没账密 -> 提示未配置",
          br2._auto_login_decision("not_logged_in") == "no_cred")


def test_activity_level_focus_ignores_ime() -> None:
    """输入法/系统窗口抢占焦点时，App 仍应算"在前台"（否则会误判未登录）。"""
    dump_ime = (
        "  mCurrentFocus=Window{abc u0 com.android.adbkeyboard/com.android.adbkeyboard.AdbIME}\n"
        "  mFocusedApp=ActivityRecord{def u0 com.xtc.watch/.MainActivity t42}\n")
    check("窗口焦点是输入法时，Activity 仍解析为 App",
          ac.ADBController._activity_from_dump(dump_ime) == "com.xtc.watch/.MainActivity",
          ac.ADBController._activity_from_dump(dump_ime))
    check("窗口焦点解析仍然返回输入法（弹窗判断要用）",
          ac.ADBController._parse_focus(dump_ime).endswith("AdbIME"))
    dump_act = ("  mCurrentFocus=null\n"
                "  topResumedActivity=ActivityRecord{d8e1 u0 com.xtc.watch/.MainActivity t123}\n")
    check("Android 13+ topResumedActivity 也能解析",
          ac.ADBController._activity_from_dump(dump_act) == "com.xtc.watch/.MainActivity")
    check("空 dump 返回空串", ac.ADBController._activity_from_dump("") == "")


def test_screen_off_detection_and_wake() -> None:
    """息屏会让 uiautomator 报 null root node：要能识别并唤醒后重试。"""
    def make_ctl(asleep=True):
        ctl = object.__new__(ADBController)
        ctl.logger = ac._silent_logger()
        ctl.focus_ttl = 0.0
        ctl._focus_cache = (0.0, "")
        ctl._activity_cache = (0.0, "")
        state = {"asleep": asleep, "woke": 0}

        def fake(cmd, timeout=None):
            if "dumpsys power" in cmd:
                return ("mWakefulness=Asleep\nmScreenOn=false\n" if state["asleep"]
                        else "mWakefulness=Awake\nmScreenOn=true\n")
            if "keyevent 224" in cmd:
                state["woke"] += 1
                state["asleep"] = False
            return ""

        ctl.try_shell = fake          # type: ignore[method-assign]
        ctl.shell = fake              # type: ignore[method-assign]
        return ctl, state

    ctl, st = make_ctl(asleep=True)
    check("识别息屏", ctl.screen_on() is False)
    check("唤醒后识别为亮屏", ctl.wake_up() is True and st["woke"] == 1, f"woke={st['woke']}")
    ctl2, _ = make_ctl(asleep=False)
    check("亮屏时不误判", ctl2.screen_on() is True)

    ctl3 = object.__new__(ADBController)
    ctl3.logger = ac._silent_logger()
    check("null root node 被识别为『屏幕未点亮』类错误",
          ctl3._looks_like_screen_off(
              ["ERROR: null root node returned by UiTestAutomationBridge."]) is True)
    check("普通 idle 错误不算屏幕问题",
          ctl3._looks_like_screen_off(["could not get idle state"]) is False)


def test_window_recovery_and_compact_dump_error() -> None:
    """WSA 窗口被最小化/关闭 -> mCurrentFocus=null -> 必须自愈，且报错要短而可行动。"""
    adb = FakeAdb("", focus="")        # 没有任何获得焦点的窗口
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}}, logger=None)
    acted = xtc._recover_window()
    check("无焦点窗口时执行恢复动作", acted is True)
    check("确实重新拉起了 App", any("am start" in c for c in adb.calls), str(adb.calls[:3]))

    ctl = object.__new__(ADBController)
    ctl.logger = ac._silent_logger()
    ctl._last_dump_detail = ""
    ctl.get_current_focus = lambda use_cache=True: ""      # type: ignore[method-assign]
    msg = ctl._dump_failure_message(
        ["/dev/tty: ERROR: null root node returned by UiTestAutomationBridge."] * 3)
    check("报错简短（单条日志不再上千字符）", len(msg) < 300, f"len={len(msg)}")
    check("指出根因（窗口/根节点）", ("根节点" in msg) or ("窗口" in msg), msg[:100])
    check("给出可行动建议（保持 WSA 窗口打开）", "WSA 窗口" in msg, msg[:220])
    check("完整细节另存 last_dump_detail（供 --debug adb-info）",
          "null root node" in ctl._last_dump_detail)


def test_open_chat_launches_app_when_not_foreground() -> None:
    """实机发现的坑：WSA 停在主屏时，open_chat 必须先拉起 App，而不是在主屏上找联系人。"""
    list_xml = message_list_xml([("李四", "早"), ("屑猹不喝茶", "晚上吃啥")])

    class HomeThenApp(FakeAdb):
        """一开始前台是 WSA 主屏（launcher），launch_app 后才变成 App 的列表页。"""

        def __init__(self):
            super().__init__(node_xml(n(text="WSA 主屏")),
                             focus="com.microsoft.windows.homeapp/PlaceholderActivity")
            self.launched = 0

        def launch_app(self, package: str, activity: str = "", **kw) -> str:
            self.launched += 1
            self.calls.append(f"am start {package}")
            self.focus = f"{package}/.MainActivity"
            self.xml = list_xml
            return ".MainActivity"

        def tap_element(self, node) -> None:
            super().tap_element(node)
            # 点联系人后就进入聊天页（否则等待进聊天会白等到超时）
            self.xml = chat_page_xml()
            self.focus = "com.xtc.watch/.ChatActivity"

    adb = HomeThenApp()
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}})
    ok = xtc.open_chat("屑猹不喝茶")
    check("App 不在前台时先拉起再找联系人", adb.launched == 1, f"launched={adb.launched}")
    check("拉起后能找到联系人并进入聊天", ok is True, f"ok={ok} focus={adb.focus}")


def test_input_hint_text_is_not_residue() -> None:
    """实机发现：输入框为空时 dump 出的是占位提示（发送文字），不能被当成残留内容。"""
    xml = node_xml(
        n(cls="android.widget.EditText", text="发送文字", bounds="[40,1700][900,1800]",
          rid="com.xtc.watch:id/et_chat_text_content") +
        n(cls="android.widget.TextView", text="发送", bounds="[920,1700][1060,1800]",
          rid="com.xtc.watch:id/tv_send_view"))
    adb = FakeAdb(xml, focus="com.xtc.watch/.ChatActivity")
    xtc = Xiaotiancai(adb, {"ui": {"interaction_delay": 0.1}})
    check("占位提示被当成空输入框", xtc.chat_input_text() == "", repr(xtc.chat_input_text()))
    node = ET.fromstring(xml).iter("node")
    edit = [n for n in node if (n.get("resource-id") or "").endswith("et_chat_text_content")][0]
    check("_input_text_of 把 hint 归一成空串", xtc._input_text_of(edit) == "")
    check("提示文案可配置", Xiaotiancai(adb, {"ui": {"input_hint_texts": ["发送文字"]}})
          ._input_text_of(edit) == "")
    # 真的输入内容时不能被吞掉
    edit2 = ET.fromstring(node_xml(
        n(cls="android.widget.EditText", text="晚上回家吃饭",
          rid="com.xtc.watch:id/et_chat_text_content"))).iter("node")
    e2 = list(edit2)[0]
    check("真实内容不会被当成提示", xtc._input_text_of(e2) == "晚上回家吃饭")


def test_foreground_logic_two_layers() -> None:
    """前台判定要同时看 Activity 与窗口（实机两个坑）：

    * 输入法抢焦点 -> 仍算在前台（否则键盘一弹就误判未登录）；
    * 别的 App（如 WSA 主屏 com.microsoft.windows.homeapp）抢到窗口 -> **不算**在前台
      （否则不会把它拉回前台，uiautomator 只能 dump 到 WSA 主屏，然后报"找不到联系人"）。
    """
    def ctl(activity: str, window: str):
        c = object.__new__(ADBController)
        c.get_current_activity = lambda use_cache=True: activity       # type: ignore[method-assign]
        c.get_current_focus = lambda use_cache=True: window            # type: ignore[method-assign]
        return c

    app = "com.xtc.watch/com.xtc.wechat.view.chatlist.ChatActivity"
    check("Activity 与窗口都是 App -> 在前台", ctl(app, app).is_in_foreground("com.xtc.watch"))
    check("窗口是输入法 -> 仍算在前台",
          ctl(app, "com.android.adbkeyboard/com.android.adbkeyboard.AdbIME")
          .is_in_foreground("com.xtc.watch"))
    check("窗口是系统弹窗 -> 仍算在前台（弹窗清理需要）",
          ctl(app, "com.android.permissioncontroller/.GrantPermissionsActivity")
          .is_in_foreground("com.xtc.watch"))
    check("窗口是别的 App（WSA 主屏）-> 不算在前台（实机日志场景）",
          ctl(app, "com.microsoft.windows.homeapp/com.microsoft.windows.home.Home")
          .is_in_foreground("com.xtc.watch") is False)
    check("App 的 Activity 都不是 -> 不算在前台",
          ctl("com.microsoft.windows.homeapp/Home", "com.microsoft.windows.homeapp/Home")
          .is_in_foreground("com.xtc.watch") is False)
    check("只拿到 Activity、没有窗口信息 -> 按 Activity 判",
          ctl(app, "").is_in_foreground("com.xtc.watch"))


# ------------------------------------------------------------------ 11. 自动登录要会重试
class _FakeXtcLogin:
    def __init__(self, status: str):
        self.status = status
        self.calls = 0

    def login(self, phone: str, password: str) -> str:
        self.calls += 1
        return self.status


def _login_bridge(status: str):
    cfg = {"target": {}, "webhook": {},
           "xiaotiancai": {"login": {"phone": "13800000000", "password": "pw"}}}
    br = bridge_mod.MessageBridge(cfg, adb=None, xtc=None, forwarder=None, logger=None)
    br.xtc = _FakeXtcLogin(status)
    br._login_inflight = True          # 模拟已入队（_do_login_job 由工作线程调用）
    br._do_login_job("")
    return br


def test_auto_login_retry_semantics() -> None:
    """自动登录：失败/超时都要安排后续重试，不能"一次失败就再也不试"。"""
    now = time.monotonic()

    br_timeout = _login_bridge("timeout")
    check("超时不算失败（不置待验证标记）", br_timeout._pending_login_notify is False)
    check("超时后安排了重试",
          br_timeout._login_not_before > now, f"{br_timeout._login_not_before - now:.0f}s")

    br_fail = _login_bridge("fail")
    check("明确失败才置待处理标记", br_fail._pending_login_notify is True)
    check("失败后仍会重试（间隔较长）",
          br_fail._login_not_before - now >= br_fail._login_retry_after_fail - 1,
          f"{br_fail._login_not_before - now:.0f}s")

    br_risk = _login_bridge("risk")
    check("安全验证后等待用户（仍安排重试）",
          br_risk._pending_login_notify is True and br_risk._login_not_before > now,
          f"{br_risk._login_not_before - now:.0f}s")

    br_ok = _login_bridge("ok")
    check("成功 -> 清除待处理标记", br_ok._pending_login_notify is False)
    check("成功 -> 按常规间隔复查",
          abs((br_ok._login_not_before - now) - br_ok._login_check_interval) < 2,
          f"{br_ok._login_not_before - now:.0f}s")

    # 已有一次登录在执行时，不重复入队
    br_ok._login_inflight = True
    before = br_ok._job_queue.qsize()
    br_ok.login_xiaotiancai()
    check("登录进行中不重复入队", br_ok._job_queue.qsize() == before,
          f"queue={br_ok._job_queue.qsize()}")


# ------------------------------------------------------------------ 9. 控制台留痕
def test_webhook_logs_and_forwards() -> None:
    """QQ 回调必须在控制台留痕，并且白名单/放行结果可读。"""
    logs: list = []

    class _Log:
        def info(self, m):
            logs.append(("info", str(m)))

        def warning(self, m):
            logs.append(("warning", str(m)))

        def error(self, m):
            logs.append(("error", str(m)))

        def debug(self, m):
            logs.append(("debug", str(m)))

    class _Bridge:
        def __init__(self, allow=True):
            self.got: list = []
            self.allow = allow

        def qq_sender_allowed(self, qq, group=""):
            return self.allow

        def forward_to_xiaotiancai(self, text, user="", group="", request_id=""):
            self.got.append(text)
            return True

    def post(port: int, payload: dict) -> str:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/qq_callback", data=data,
                                    headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode()

    bridge = _Bridge()
    srv = qq_webhook.create_webhook_server(bridge, host="127.0.0.1", port=0,
                                           token="", logger=_Log())
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = post(port, {"source": "astrbot", "message": "[09-01 10:00] [张三] 你好",
                           "user_id": "10001"})
        for _ in range(40):        # 等后台线程处理
            if bridge.got:
                break
            time.sleep(0.05)
        check("回调返回 OK", body.strip() == "OK", body.strip())
        check("消息已转交给桥接", bridge.got == ["[09-01 10:00] [张三] 你好"], str(bridge.got))
        check("收到回调有日志", any("[QQ回调] 收到" in m for _, m in logs),
              str([m for _, m in logs][:2]))
        check("放行有日志", any("放行" in m for _, m in logs))

        logs.clear()
        bridge2 = _Bridge(allow=False)
        srv2 = qq_webhook.create_webhook_server(bridge2, host="127.0.0.1", port=0,
                                                token="", logger=_Log())
        port2 = srv2.server_address[1]
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        try:
            body2 = post(port2, {"source": "astrbot", "message": "hi", "user_id": "999"})
            check("白名单外返回 IGNORED", body2.strip() == "IGNORED", body2.strip())
            check("拒绝也有日志", any("未转发" in m or "不在白名单" in m for _, m in logs),
                  str([m for _, m in logs][:3]))
        finally:
            srv2.shutdown()
    finally:
        srv.shutdown()


def test_webhook_action_requests_reach_bridge() -> None:
    """QQ 侧 /小天才 历史消息（动作类回调）必须真的被执行到桥接里。

    用户报告："发历史消息提示群不在白名单，明明我添加了"。真因（实机日志）：
    插件转发带 source=astrbot 且 message 为空，而 qq_webhook 的"空消息"分支写在
    action 分派**之前**就 return 了 —— 于是历史消息/登录/初始化/自动登录在插件路径下
    全部被吞掉，日志还把原因写成"未转发（空消息或来源不在白名单）"，误导成白名单问题。
    """
    import qq_webhook as wh_mod

    logs: list = []

    class _Log:
        def info(self, m):
            logs.append(("info", str(m)))

        def warning(self, m):
            logs.append(("warning", str(m)))

        def error(self, m):
            logs.append(("error", str(m)))

        def debug(self, m):
            logs.append(("debug", str(m)))

    class _Bridge:
        def __init__(self, allow: bool = True):
            self.allow = allow
            self.history: list = []
            self.actions: list = []

        def qq_sender_allowed(self, qq, group=""):
            if not self.allow:
                self.actions.append("拒绝")
            return self.allow

        def forward_to_xiaotiancai(self, text, user="", group="", request_id=""):
            self.actions.append(("转发", text))
            return True

        def fetch_xtc_history(self, count=20, request_id="", into_chat=False, source=""):
            self.history.append((count, request_id, into_chat, source))

        def login_xiaotiancai(self, request_id=""):
            self.actions.append(("登录", request_id))

        def toggle_auto_login(self, request_id=""):
            self.actions.append(("自动登录", request_id))

        def init_xiaotiancai(self, request_id=""):
            self.actions.append(("初始化", request_id))

    def post(port: int, payload: dict) -> str:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/qq_callback", data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode()

    def wait_for(pred, timeout: float = 2.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.02)
        return False

    bridge = _Bridge()
    srv = wh_mod.create_webhook_server(bridge, host="127.0.0.1", port=0,
                                       token="", logger=_Log())
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # ① 插件路径的"历史消息"动作：必须落到 fetch_xtc_history，并且返回 OK
        body = post(port, {"source": "astrbot", "action": "history", "user_id": "",
                           "group_id": "472805002", "history_count": 5,
                           "history_source": "QQ群 472805002", "request_id": "req-1"})
        ok = wait_for(lambda: bool(bridge.history))
        check("插件路径的历史消息动作被执行",
              ok and bridge.history[0] == (5, "req-1", False, "QQ群 472805002"),
              f"body={body.strip()} history={bridge.history}")
        check("历史消息动作返回 OK（不是 IGNORED）", body.strip() == "OK", body.strip())
        check("不再把动作请求说成白名单问题",
              not any("不在白名单" in m for _, m in logs), str([m for _, m in logs]))

        # ② 其他动作同样可达
        for act, name in (("login", "登录"), ("auto_login", "自动登录"), ("init", "初始化")):
            before = len(bridge.actions)
            b = post(port, {"source": "astrbot", "action": act, "user_id": "10001",
                            "request_id": f"req-{act}"})
            wait_for(lambda: len(bridge.actions) > before)
            check(f"动作 {act} 被执行",
                  any(a[0] == name for a in bridge.actions[before:]), str(bridge.actions[before:]))
            check(f"动作 {act} 返回 OK", b.strip() == "OK", b.strip())

        # ③ 真的不在白名单：返回 IGNORED，且日志说明是白名单
        bridge2 = _Bridge(allow=False)
        srv2 = wh_mod.create_webhook_server(bridge2, host="127.0.0.1", port=0,
                                            token="", logger=_Log())
        port2 = srv2.server_address[1]
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        try:
            logs.clear()
            b2 = post(port2, {"source": "astrbot", "action": "history", "user_id": "999",
                              "group_id": "111", "request_id": "req-x"})
            check("白名单外的动作返回 IGNORED", b2.strip() == "IGNORED", b2.strip())
            check("白名单外的动作不会被执行", not bridge2.history, str(bridge2.history))
            check("白名单外仍只提示白名单原因",
                  any("不在白名单" in m for _, m in logs), str([m for _, m in logs]))

            # ⑤ 有内容但白名单外：同样只能说白名单，不能说"没有可转发的内容"
            logs.clear()
            b2b = post(port2, {"source": "astrbot", "message": "你好", "user_id": "999",
                               "group_id": "111"})
            check("白名单外的普通消息返回 IGNORED", b2b.strip() == "IGNORED", b2b.strip())
            check("白名单外的普通消息提示白名单原因",
                  any("不在白名单" in m for _, m in logs)
                  and not any("没有可转发的内容" in m for _, m in logs),
                  str([m for _, m in logs]))
        finally:
            srv2.shutdown()

        # ④ 白名单允许但内容为空：日志说明"没有可转发的内容"，不再甩锅白名单
        logs.clear()
        b3 = post(port, {"source": "astrbot", "message": "", "user_id": "10001"})
        check("空消息返回 IGNORED", b3.strip() == "IGNORED", b3.strip())
        check("允许的空消息提示「没有可转发的内容」",
              any("没有可转发的内容" in m for _, m in logs), str([m for _, m in logs]))
        check("允许的空消息不再提白名单",
              not any("不在白名单" in m for _, m in logs), str([m for _, m in logs]))
    finally:
        srv.shutdown()


def test_history_action_end_to_end() -> None:
    """完整链路：QQ(插件) --action=history--> qq_webhook --> 工作线程 --> /api/result 回传。

    用**真的** MessageBridge（不只假桥）跑一遍，确保用户那条
    "/小天才 历史消息" 真能拿到内容并回到 QQ。
    """
    import qq_webhook as wh_mod

    class _Silent:
        def info(self, m):
            pass

        def warning(self, m):
            pass

        def error(self, m):
            pass

        def debug(self, m):
            pass

    class _Fwd:
        def __init__(self):
            self.replies: list = []

        def reply_result(self, request_id, text):
            self.replies.append((request_id, text))
            return True

    root = tmp_root()
    srv = None
    rec = Recorder()
    try:
        cfg = {"target": {"xtc_contact": "张三"},
               "xiaotiancai": {"ui": {}},
               "webhook": {"allow_groups": ["472805002"], "allow_from": ["10001"]}}
        br = bridge_mod.MessageBridge(cfg, adb=None, xtc=None, forwarder=None, logger=rec)
        br.msgs = MessageLog(path=str(_paths(root)["msgs"]))
        br._cmd_done_file = str(_paths(root)["done"])
        fwd = _Fwd()
        br.forwarder = fwd
        br.msgs.append("xtc", "屑猹不喝茶", "晚安", source="手表-屑猹不喝茶")
        br.msgs.append("qq", "张三", "吃饭了", source="QQ群 472805002")
        br.running = True     # _job_worker 是 while self.running 的（真实启动由 br.start() 置位）
        threading.Thread(target=br._job_worker, daemon=True, name="test-jobs").start()

        srv = wh_mod.create_webhook_server(br, host="127.0.0.1", port=0,
                                           token="", logger=_Silent())
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        data = json.dumps({"source": "astrbot", "action": "history", "user_id": "10001",
                           "group_id": "472805002", "history_count": 5,
                           "request_id": "req-e2e"}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/qq_callback", data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode()
        check("动作回执 OK", body.strip() == "OK", body.strip())

        end = time.time() + 3
        while time.time() < end and not fwd.replies:
            time.sleep(0.02)
        check("历史消息结果回传到 /api/result", bool(fwd.replies),
              f"replies={fwd.replies} logs={rec.lines[-4:]}")
        if fwd.replies:
            rid, text = fwd.replies[0]
            check("回传带上了原 request_id", rid == "req-e2e", rid)
            check("回传内容是小天才历史消息", "小天才历史消息" in text, text[:80])
            check("内容里能看到真实记录", ("晚安" in text) and ("吃饭了" in text), text[:200])
    finally:
        if srv is not None:
            srv.shutdown()
        cleanup(root)


def test_logger_tolerant_stream() -> None:
    """GBK 控制台遇到无法编码的字符（emoji / 特殊符号）时不能整条日志丢失。

    这里用 \\u 转义而不是字面 emoji：源码本身保持无 emoji，测试仍然覆盖"不可编码字符"。
    """
    class _GbkStream:
        encoding = "gbk"

        def __init__(self):
            self.data = ""

        def write(self, data):
            data.encode("gbk")     # GBK 编不了的字符会抛 UnicodeEncodeError
            self.data += data
            return len(data)

        def flush(self):
            pass

    raw = _GbkStream()
    stream = logger_mod._tolerant_stream(raw)
    stream.write("普通中文 OK\n")
    try:
        stream.write("带特殊字符 \u2705 与 \u30fb 的日志\n")
        check("不可编码字符被替换而不是抛异常", True)
    except UnicodeEncodeError as e:  # noqa: BLE001
        check("不可编码字符被替换而不是抛异常", False, str(e))
    check("可编码内容仍然写出", "普通中文 OK" in raw.data, raw.data[:40])


def test_find_send_ignores_message_state_icon() -> None:
    """输入框为空时，"发送"不能命中消息状态图标（发送中/发送失败）。

    实机（WSA）实测：输入框为空时没有 tv_send_view，但每条消息右侧有
    iv_chat_msg_item_state（content-desc="发送失败"）。旧的 content-desc 子串匹配
    "发送"会命中它，于是"没输入内容也以为发送按钮存在"——注入其实没成功也继续走，
    点击还落到状态图标上，表现为"莫名其妙发送失败"。
    """
    xml = node_xml(
        n(cls="android.widget.TextView", text="", rid="com.xtc.watch:id/tv_chat_msg_item_date",
          bounds="[945,312][990,337]")
        + n(cls="android.widget.ImageView", text="", desc="发送失败",
            rid="com.xtc.watch:id/iv_chat_msg_item_state", bounds="[1022,614][1042,634]")
        + n(cls="android.widget.EditText", text=" 发送文字",
            rid="com.xtc.watch:id/et_chat_text_content", bounds="[774,678][1120,721]"))
    xtc, _ = make_xtc(xml, ui_cfg={"send_resource_id": "com.xtc.watch:id/tv_send_view"})
    found = xtc._find_send(ET.fromstring(xml))
    check("空输入框时找不到发送按钮（不误认消息状态图标）", found is None,
          f"got={None if found is None else xtc._id_tail(found)}")

    xml2 = node_xml(
        '<node class="android.widget.FrameLayout" text="" '
        'resource-id="com.xtc.watch:id/fl_chat_text_send" content-desc="" '
        'clickable="true" bounds="[1275,677][1323,721]">'
        '<node class="android.widget.TextView" text="发送" '
        'resource-id="com.xtc.watch:id/tv_send_view" content-desc="" '
        'clickable="false" bounds="[1275,683][1317,715]" /></node>')
    xtc2, adb2 = make_xtc(xml2, ui_cfg={"send_resource_id": "com.xtc.watch:id/tv_send_view"})
    root2 = ET.fromstring(xml2)
    send = xtc2._find_send(root2)
    check("有内容时命中真正的发送控件 tv_send_view",
          send is not None and xtc2._id_tail(send) == "tv_send_view",
          f"got={None if send is None else xtc2._id_tail(send)}")
    xtc2._tap_send(send, root2)
    check("点发送时点的是可点击的父容器 fl_chat_text_send",
          any("1275,677" in c for c in adb2.calls), str(adb2.calls[-1:]))


def test_content_desc_exact_match() -> None:
    """content-desc 支持"完全相等"，避免子串误伤。"""
    root = ET.fromstring(node_xml(
        n(cls="android.widget.ImageView", text="", desc="发送失败",
          rid="com.xtc.watch:id/iv_chat_msg_item_state")))
    adb = FakeAdb()
    check("默认仍是子串匹配（不影响弹窗/按钮文案）",
          adb.find_element(root, content_desc="发送") is not None)
    check("显式关闭子串后不命中",
          adb.find_element(root, content_desc="发送", content_desc_contains=False) is None)


def test_time_label_is_per_message() -> None:
    """时间必须是**每条消息自己的**时间，不能把组首的标签发给组内其它消息。

    实测事故（09-20 23:47）：一个时间组里的三条消息
    test / 我去？ / 晚安（检测于 23:50、23:52、23:53）全被安上组首的 23:47。
    小天才只在一个时间组的第一条消息上方画标签，所以标签只能归它下面第一条消息。
    """
    xtc, _ = make_xtc("", ui_cfg={})
    bubble = ET.fromstring(n(bounds="[786,351][922,531]"))
    dates = [(123, 148, "07:06"), (312, 337, "08:08"), (565, 590, "22:20")]
    got = xtc._label_for_bubble(bubble, dates)
    check("取气泡上方最近的标签（不是最上面那个）", got == "08:08", f"got={got!r}")

    # 标签全在气泡下方：必须返回空，让上层用"当前时间"
    below = [(700, 725, "23:59"), (760, 785, "23:58")]
    check("气泡下方/后面的标签一律不用（防张冠李戴）",
          xtc._label_for_bubble(bubble, below) == "")

    # 同一个时间组：标签只属于紧挨它下面的那条消息
    group_label = [(200, 225, "23:47")]
    first = ET.fromstring(n(bounds="[786,250][922,320]"))     # 组首，紧贴标签下方
    second = ET.fromstring(n(bounds="[786,400][922,470]"))    # 组内第二条
    third = ET.fromstring(n(bounds="[786,520][922,590]"))     # 组内第三条
    allb = [first, second, third]
    check("组首拿到组时间", xtc._label_for_bubble(first, group_label, allb) == "23:47",
          repr(xtc._label_for_bubble(first, group_label, allb)))
    check("组内第二条不再共用组首时间（用户报的 bug）",
          xtc._label_for_bubble(second, group_label, allb) == "",
          repr(xtc._label_for_bubble(second, group_label, allb)))
    check("组内第三条同样不共用", xtc._label_for_bubble(third, group_label, allb) == "",
          repr(xtc._label_for_bubble(third, group_label, allb)))

    # 每条消息各有一个标签时，都取到自己的
    per = [(200, 225, "23:47"), (380, 405, "23:52"), (500, 525, "23:53")]
    check("每条各有标签时各取各的",
          (xtc._label_for_bubble(first, per, allb),
           xtc._label_for_bubble(second, per, allb),
           xtc._label_for_bubble(third, per, allb)) == ("23:47", "23:52", "23:53"))

    check("确实没有标签时返回空串", xtc._label_for_bubble(bubble, []) == "")
    check("_date_count 能数出标签数",
          xtc._date_count(ET.fromstring(node_xml(
              n(cls="android.widget.TextView", text="08:08",
                rid="com.xtc.watch:id/tv_chat_msg_item_date")))) == 1)


def compressed_chat_xml() -> str:
    """实机抓下来的**压缩 dump**聊天页：没有 tv_chat_msg_item_date 节点，
    时间只剩父容器 ll_chat_top_layout 的 content-desc（数字是"中文式"写法）。"""
    return node_xml(
        n(cls="android.widget.ImageView", text="",
          desc="屑猹不喝茶发的消息,表情喝娃哈哈.png",
          rid="com.xtc.watch:id/chat_msg_item_content", bounds="[845,105][965,139]") +
        n(cls="android.widget.LinearLayout", text="", desc="十1点十9分",
          rid="com.xtc.watch:id/ll_chat_top_layout", bounds="[1004,153][1049,208]") +
        n(cls="android.widget.ImageView", text="",
          desc="屑猹不喝茶发的消息,表情流汗",
          rid="com.xtc.watch:id/chat_msg_item_content", bounds="[845,208][965,328]") +
        n(cls="android.widget.LinearLayout", text="", desc="十9点十8分",
          rid="com.xtc.watch:id/ll_chat_top_layout", bounds="[1004,342][1049,397]") +
        n(cls="android.widget.ImageView", text="",
          desc="屑猹不喝茶发的消息,表情悄悄看",
          rid="com.xtc.watch:id/chat_msg_item_content", bounds="[845,397][965,517]") +
        n(cls="android.widget.LinearLayout", text="", desc="2十点4十6分",
          rid="com.xtc.watch:id/ll_chat_top_layout", bounds="[1004,531][1049,586]") +
        n(cls="android.widget.TextView", text="晚安",
          desc="你发的消息,晚安",
          rid="com.xtc.watch:id/chat_msg_item_content", bounds="[967,586][1207,658]") +
        n(cls="android.widget.EditText", text="\xa0发送文字",
          rid="com.xtc.watch:id/et_chat_text_content", bounds="[833,678][1179,721]") +
        n(cls="android.widget.TextView", text="发送",
          rid="com.xtc.watch:id/tv_send_view", bounds="[1179,677][1221,721]"))


def test_time_labels_survive_compressed_dump() -> None:
    """用户报告"消息时间还是不对"：根因是 WSA 上默认用的 `--compressed` dump
    **把时间标签节点整片裁掉**（实机同一屏：压缩版 19 节点/0 个时间标签，
    完整版 46 节点/3 个时间标签），于是每条消息都退化成"当前时间"。

    两处修复：① 默认改用完整 dump（见 adb_controller._dump_strategies）；
    ② 万一只拿到压缩版，就从 ll_chat_top_layout 的 content-desc 还原时间。
    """
    xtc, _ = make_xtc("", ui_cfg={})

    # ① desc 里的"中文式"数字还原
    check("desc「十1点十9分」-> 11:19", xtc._label_from_desc("十1点十9分") == "11:19",
          xtc._label_from_desc("十1点十9分"))
    check("desc「十9点十8分」-> 19:18", xtc._label_from_desc("十9点十8分") == "19:18",
          xtc._label_from_desc("十9点十8分"))
    check("desc「2十点4十6分」-> 20:46", xtc._label_from_desc("2十点4十6分") == "20:46",
          xtc._label_from_desc("2十点4十6分"))
    check("普通写法「19点18分」也能认", xtc._label_from_desc("19点18分") == "19:18",
          xtc._label_from_desc("19点18分"))
    check("认不出的 desc 一律返回空（不给错时间）",
          xtc._label_from_desc("刚刚") == "" and xtc._label_from_desc("25点99分") == ""
          and xtc._label_from_desc("和屑猹不喝茶的聊天") == "")

    # ② 压缩 dump：时间标签数量不能是 0（否则会触发"快照不完整"的反复重读）
    root = ET.fromstring(compressed_chat_xml())
    check("压缩 dump 里也能数出时间标签", xtc._date_count(root) == 3, str(xtc._date_count(root)))

    # ③ 每条消息各拿各的时间（标签在它上面 → 归下面第一条气泡）
    bubbles = xtc._chat_bubbles(root, include_own=True)
    got = [(b["text"], b["time_label"]) for b in bubbles]
    check("压缩 dump 下每条消息的时间都对",
          got == [("表情喝娃哈哈.png", ""), ("表情流汗", "11:19"),
                  ("表情悄悄看", "19:18"), ("晚安", "20:46")],
          str(got))

    # ④ 最新一条（别人发的）也要带上自己的时间，而不是"当前时间"
    contact, text, label = xtc._latest_in_chat(root)
    check("最新消息的时间来自压缩 dump 的 desc", label == "19:18", f"label={label!r}")

    # ⑤ 完整 dump 里两种节点都有时，优先用 tv_chat_msg_item_date 的文本
    both = node_xml(
        n(cls="android.widget.LinearLayout", text="", desc="2十点4十6分",
          rid="com.xtc.watch:id/ll_chat_top_layout", bounds="[1004,531][1049,586]") +
        n(cls="android.widget.TextView", text="昨天 20:46",
          rid="com.xtc.watch:id/tv_chat_msg_item_date", bounds="[1004,551][1049,576]"))
    labels = xtc._time_labels(ET.fromstring(both))
    check("完整 dump 优先用 date 节点文本",
          [t for _, _, t in labels] == ["昨天 20:46"], str(labels))


def test_dump_prefers_full_hierarchy() -> None:
    """默认要先用**完整 dump**：压缩版会把时间标签裁掉（用户报的"时间不对"）。

    某些镜像上完整 dump 会被 Killed，所以仍保留压缩版兜底 + "记住成功策略"。
    """
    from adb_controller import ADBController as _C
    ctl = _C(adb_path="adb")
    names = [n for n, _ in ctl._dump_strategies()]
    check("默认先试完整 dump", names[0] == "file-full", str(names))
    check("压缩版仍保留为兜底（镜像不支持完整 dump 时用）",
          "file-compressed" in names and "tty-compressed" in names, str(names))
    ctl._dump_strategy = "file-compressed"
    check("记住的策略仍会被提到最前",
          [n for n, _ in ctl._dump_strategies()][0] == "file-compressed")


class SeqAdb(FakeAdb):
    """按调用次数返回不同界面快照（模拟"偶发不完整 dump"）。"""

    def __init__(self, xmls: list) -> None:
        super().__init__("", "com.xtc.watch/.ChatActivity")
        self.xmls = list(xmls)
        self.i = 0

    def dump_ui(self, retries: int = 3, delay: float = 2.0):
        xml = self.xmls[min(self.i, len(self.xmls) - 1)]
        self.i += 1
        return ET.fromstring(xml)


def test_latest_message_retries_when_labels_missing() -> None:
    """快照里"有消息但一个时间标签都没有"时，要重读拿到消息真实时间。

    不修的话上层会退化成"当前时间"，用户就看到了"8 点发的消息按当前时间转发"。
    """
    no_label = node_xml(
        n(cls="android.widget.TextView", text="早上好", desc="童武洋发的消息,早上好",
          rid="com.xtc.watch:id/chat_msg_item_content", bounds="[786,351][922,531]"))
    with_label = node_xml(
        n(cls="android.widget.TextView", text="早上好", desc="童武洋发的消息,早上好",
          rid="com.xtc.watch:id/chat_msg_item_content", bounds="[786,351][922,531]")
        + n(cls="android.widget.TextView", text="08:08",
            rid="com.xtc.watch:id/tv_chat_msg_item_date", bounds="[945,312][990,337]"))
    adb = SeqAdb([no_label, with_label])
    xtc = Xiaotiancai(adb, {"ui": {}}, logger=None)
    xtc.require_login = lambda *a, **k: True          # 隔离登录态探测的 dump
    _c, text, label, _own, _recent = xtc.get_latest_message()
    check("重读后拿到真实时间标签", label == "08:08", f"label={label!r} text={text!r}")


def test_start_enqueues_auto_init() -> None:
    """启动时自动排队初始化（用户报告"程序不会自动初始化"）。"""
    root = tmp_root()
    cfg = {"target": {"xtc_contact": "张三"},
           "xiaotiancai": {"ui": {"interaction_delay": 0.01}}, "webhook": {}}
    xtc, adb = make_xtc(chat_page_xml(), ui_cfg={"interaction_delay": 0.01})
    br = bridge_mod.MessageBridge(cfg, adb, xtc, forwarder=None, logger=None)
    br.msgs = MessageLog(path=str(_paths(root)["msgs"]))
    seen: list = []
    real_put = br._job_queue.put
    br._job_queue.put = lambda item, *a, **k: (seen.append(item), real_put(item, *a, **k))[1]
    try:
        br.start()
        time.sleep(0.3)
        check("启动时自动排队 init 任务",
              any(j and j[0] == "init" for j in seen), f"seen={seen}")
    finally:
        br.stop()
        cleanup(root)


def test_plugin_send_reports_real_reason() -> None:
    """转发失败必须带出真实原因：QQ 侧发不出去 / 超时 / token 不对 是三种不同的病。

    早先只报"插件未启动？检查 http_port/token"，而插件其实好好的、真正原因是
    QQ/NapCat 侧 ActionFailed（用户照着提示排查不到点上）。
    """
    import socket
    import urllib.error
    import plugin_client as pcmod

    class _Resp:
        def __init__(self, body: str, status: int = 200):
            self._b = body.encode("utf-8")
            self.status = status

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    orig = pcmod.urllib.request.urlopen
    try:
        pcmod.urllib.request.urlopen = lambda *a, **k: _Resp(
            '{"ok": false, "accepted": false, "error": "ActionFailed: retcode=1200 sendMsg Timeout"}')
        ok, why = pcmod.PluginClient().send_detail("private", "1", "hi")
        check("QQ 侧失败时带出插件给的 error", ok is False and "retcode=1200" in why, why)

        def _timeout(*a, **k):
            raise socket.timeout("timed out")

        pcmod.urllib.request.urlopen = _timeout
        ok2, why2 = pcmod.PluginClient().send_detail("private", "1", "hi")
        check("超时提示指向 QQ/NapCat 卡住", ok2 is False and "NapCat" in why2, why2)

        def _401(*a, **k):
            raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)

        pcmod.urllib.request.urlopen = _401
        ok3, why3 = pcmod.PluginClient().send_detail("private", "1", "hi")
        check("401 提示 token 不匹配", ok3 is False and "token" in why3, why3)

        def _refused(*a, **k):
            raise urllib.error.URLError("connection refused")

        pcmod.urllib.request.urlopen = _refused
        ok5, why5 = pcmod.PluginClient().send_detail("private", "1", "hi")
        check("连不上插件时提示检查 AstrBot/插件", ok5 is False and "AstrBot" in why5, why5)

        pcmod.urllib.request.urlopen = lambda *a, **k: _Resp(
            '{"ok": true, "accepted": true, "queued": true}')
        ok6, why6 = pcmod.PluginClient().send_detail("private", "1", "hi")
        check("插件仅排队时标注 queued（不当作已送达）",
              ok6 is True and why6 == "queued", f"ok={ok6} why={why6!r}")

        pcmod.urllib.request.urlopen = lambda *a, **k: _Resp('{"ok": true, "accepted": true}')
        ok4, why4 = pcmod.PluginClient().send_detail("group", "1", "hi")
        check("成功时没有原因文本", ok4 is True and why4 == "", f"ok={ok4} why={why4!r}")
    finally:
        pcmod.urllib.request.urlopen = orig

    check("占位转发器也提供 send_detail（bridge 统一走它）",
          bridge_mod.make_forwarder({"forward": {"mode": "log"}}).send_detail("private", "1", "x")
          == (True, ""))


def test_no_delivery_confirm_on_forward_failure() -> None:
    """转发到 QQ 失败（或只排队）时，绝不能在小天才侧回「发送成功」送达确认。

    用户报告：小天才→QQ 明明发送失败，手表聊天里却出现"发送成功：…"。
    根因是旧客户端把 HTTP 200 当成功（插件失败也是 200 + {"ok": false}）。
    """
    root = tmp_root()

    class _Fwd:
        def __init__(self, ok: bool, detail: str = ""):
            self.ok = ok
            self.detail = detail

        def send(self, t, i, m):
            return self.ok

        def send_detail(self, t, i, m):
            return self.ok, self.detail

    class _Xtc:
        def __init__(self):
            self.sent: list = []

        def is_in_chat(self):
            return True

        def send_message(self, text):
            self.sent.append(text)
            return True

    cfg = {"target": {"xtc_contact": "张三", "qq_private": "2218631043"},
           "xiaotiancai": {"ui": {}}, "webhook": {}}
    cases = ((False, "QQ 侧发送失败: ActionFailed retcode=1200", False, "转发失败"),
             (True, "queued", False, "只排队未确认"),
             (True, "", True, "真成功"))
    for ok, detail, expect, name in cases:
        xtc = _Xtc()
        br = bridge_mod.MessageBridge(cfg, adb=None, xtc=xtc,
                                      forwarder=_Fwd(ok, detail), logger=None)
        br.msgs = MessageLog(path=str(_paths(root)["msgs"]))
        br._cmd_done_file = str(_paths(root)["done"])
        try:
            br._forward("屑猹不喝茶", "测试消息", "23:47")
        except Exception as e:  # noqa: BLE001
            check(f"{name}: 转发链路不抛异常", False, f"{type(e).__name__}: {e}")
            continue
        check(f"{name} -> 送达确认={'有' if expect else '无'}",
              bool(xtc.sent) == expect, f"sent={xtc.sent}")
    cleanup(root)


def test_app_state_machine() -> None:
    """状态判定：聊天页/列表/登录页/不在前台/读不到，五种要分得清。

    用户反馈"不能判断当前状态，还老是提示找不到联系人"——以前轮询只问
    "在不在聊天页"，不在就去 open_chat，于是在登录页上反复找联系人。
    """
    ui = {"interaction_delay": 0.01}

    def state_of(xml: str, focus: str = "com.xtc.watch/.MainActivity") -> str:
        adb = FakeAdb(xml, focus)
        adb.dump_ui = lambda retries=3, delay=2.0: ET.fromstring(xml)
        return Xiaotiancai(adb, {"ui": ui}).app_state()

    chat = chat_page_xml("晚上回家吃饭")
    lst = message_list_xml([("李四", "早"), ("王五", "晚安")])
    check("聊天页 -> chat", state_of(chat) == Xiaotiancai.STATE_CHAT, state_of(chat))
    check("消息列表 -> list", state_of(lst) == Xiaotiancai.STATE_LIST, state_of(lst))
    check("App 不在前台 -> background",
          state_of(chat, "com.microsoft.windows.homeapp/.Home")
          == Xiaotiancai.STATE_BACKGROUND)
    check("空树/读不到 -> blind", state_of(node_xml("")) == Xiaotiancai.STATE_BLIND)

    login_xml = node_xml(
        n(cls="android.widget.EditText", text="", password="true",
          rid="com.xtc.watch:id/et_password", bounds="[100,300][700,380]")
        + n(cls="android.widget.Button", text="登录", bounds="[100,400][700,470]"))
    check("密码框/登录按钮 -> login",
          state_of(login_xml) == Xiaotiancai.STATE_LOGIN, state_of(login_xml))

    other = node_xml(n(cls="android.widget.TextView", text="设置", bounds="[0,0][100,50]")
                     + n(cls="android.widget.TextView", text="关于", bounds="[0,60][100,110]")
                     + n(cls="android.widget.TextView", text="退出", bounds="[0,120][100,170]"))
    check("其它页面 -> other", state_of(other) == Xiaotiancai.STATE_OTHER, state_of(other))
    check("状态有中文说明", Xiaotiancai.STATE_TEXT.get(Xiaotiancai.STATE_LOGIN) == "登录/验证页")


def test_state_logged_once_and_warnings_throttled() -> None:
    """状态只在变化时记一条；同一失败原因不刷屏。"""
    root = tmp_root()
    rec = Recorder()
    br = bridge_mod.MessageBridge({"target": {}, "xiaotiancai": {}, "webhook": {}},
                                  adb=None, xtc=None, forwarder=None, logger=rec)
    br.msgs = MessageLog(path=str(_paths(root)["msgs"]))
    br._cmd_done_file = str(_paths(root)["done"])
    br.xtc = Xiaotiancai(FakeAdb(), {"ui": {}}, logger=rec)
    try:
        br._log_state(Xiaotiancai.STATE_LOGIN)
        br._log_state(Xiaotiancai.STATE_LOGIN)
        n_login = sum(1 for x in rec.lines if "小天才状态: 登录/验证页" in x)
        check("同一状态只记一次", n_login == 1, str(rec.lines))
        check("刚进入该状态时不再立刻重复提醒",
              not any("持续为" in x for x in rec.lines), str(rec.lines))
        br._log_state(Xiaotiancai.STATE_CHAT)
        check("状态变化会再记一条",
              any("小天才状态: 聊天窗口" in x for x in rec.lines), str(rec.lines))

        rec.lines.clear()
        xtc = br.xtc
        for _ in range(5):
            xtc._warn_throttled("k", "同样的失败")
        warns = [x for x in rec.lines if x.startswith("[warning]")]
        check("同类失败 5 分钟内只 warning 一次", len(warns) == 1, str(rec.lines))
        check("其余降为 debug", len([x for x in rec.lines if x.startswith("[debug]")]) == 4,
              str(rec.lines))
    finally:
        cleanup(root)


def test_poll_loop_dismisses_popup_without_cooldown() -> None:
    """轮询层遇到"弹窗遮挡"要**立刻**清弹窗，而且不设冷却。

    用户报告：弹窗盖住界面后消息一直读不到、还老提示"找不到联系人"。
    以前轮询只认"在不在聊天页"，弹窗状态被当成"其它页面"去 open_chat（还有 30 秒
    冷却），弹窗不关就永远进不去聊天页。现在状态机判出 popup，轮询每一轮都清它。
    """
    import threading

    class PollAdb(FakeAdb):
        def is_connected(self) -> bool:
            return True

        def ensure_connected(self) -> bool:
            return True

    calls = {"settle": 0, "states": []}

    class PopupXtc(Xiaotiancai):
        def app_state_with_root(self, attempts: int = 2):
            calls["states"].append(self.STATE_POPUP)
            return (self.STATE_POPUP, None)

        def settle(self, max_passes: int = 6) -> bool:
            calls["settle"] += 1
            return True

    root = tmp_root()
    rec = Recorder()
    try:
        xtc = PopupXtc(PollAdb(), {"ui": {}}, logger=rec)
        br = bridge_mod.MessageBridge({"target": {}, "xiaotiancai": {}, "webhook": {}},
                                      adb=PollAdb(), xtc=xtc, forwarder=None, logger=rec)
        br.msgs = MessageLog(path=str(_paths(root)["msgs"]))
        br._cmd_done_file = str(_paths(root)["done"])
        br._poll_interval = 0.05
        br.running = True
        t = threading.Thread(target=br._poll_loop, daemon=True)
        t.start()
        time.sleep(0.5)
        br.running = False
        t.join(3)
        check("弹窗状态下轮询会去清弹窗", calls["settle"] >= 1, str(calls))
        check("状态日志里看得到「弹窗遮挡界面」",
              any("弹窗遮挡界面" in x for x in rec.lines),
              str([x for x in rec.lines if "状态" in x][:3]))
        check("轮询没有抛异常", not any("轮询异常" in x for x in rec.lines), str(rec.lines[-3:]))
    finally:
        cleanup(root)


def test_monotonic_sentinels_survive_fresh_boot() -> None:
    """刚开机的机器上（monotonic 还很小）该做的事不能被"节流"吞掉。

    真实事故：CI 在新启动的 Linux runner 上跑（monotonic = 开机时长，只有几十秒），
    "上次时刻"初值写 0.0 时 `now - 0.0 >= 间隔` 不成立 -> 该告警的被降级成 debug、
    该进聊天的被跳过。修法：一律用 -inf 作初值。
    """
    import xiaotiancai as xmod
    real_monotonic = xmod.time.monotonic
    rec = Recorder()
    try:
        xmod.time.monotonic = lambda: 5.0          # 模拟"刚开机 5 秒"
        xtc = Xiaotiancai(FakeAdb(), {"ui": {}}, logger=rec)
        for _ in range(3):
            xtc._warn_throttled("k", "同样的失败")
        check("刚开机时第一次失败仍会 warning",
              len([x for x in rec.lines if x.startswith("[warning]")]) == 1,
              str(rec.lines))
        check("_last_dump_warn 初值是 -inf（不是 0.0）",
              xtc._last_dump_warn == float("-inf"), repr(xtc._last_dump_warn))

        br = bridge_mod.MessageBridge({"target": {}, "xiaotiancai": {}, "webhook": {}},
                                      adb=None, xtc=xtc, forwarder=None, logger=rec)
        check("bridge 聊天窗口冷却初值是 -inf",
              br._last_chat_open == float("-inf"), repr(br._last_chat_open))
        # 刚开机 5 秒时也该允许立刻进聊天（而不是等 uptime 超过 30s）
        cooldown = 30.0
        allowed = xmod.time.monotonic() - br._last_chat_open >= cooldown
        check("刚开机时也允许立刻进入聊天", allowed,
              f"monotonic=5.0 last={br._last_chat_open}")
    finally:
        xmod.time.monotonic = real_monotonic


def test_nuitka_version_args_are_numeric() -> None:
    """预发布版本号必须转成纯数字再交给 Nuitka。

    真实事故：打 tag 1.0.0-alpha.1 后 CI 六平台**全部**失败，只有一行
        FATAL: Invalid version number --file-version='1.0.0-alpha.1'.
    Nuitka 的 --file-version/--product-version 只接受数字，产物文件名才用完整版本号。
    """
    import sys as _sys
    _sys.path.insert(0, str(Path.cwd() / "tools"))
    import build_nuitka as b

    check("1.0.0-alpha.1 -> 1.0.0.1", b.numeric_version("1.0.0-alpha.1") == "1.0.0.1",
          b.numeric_version("1.0.0-alpha.1"))
    check("1.0.0 -> 1.0.0.0", b.numeric_version("1.0.0") == "1.0.0.0",
          b.numeric_version("1.0.0"))
    check("2.1-beta.3 -> 2.1.0.3", b.numeric_version("2.1-beta.3") == "2.1.0.3",
          b.numeric_version("2.1-beta.3"))
    check("1.0.0-rc.2 -> 1.0.0.2", b.numeric_version("1.0.0-rc.2") == "1.0.0.2",
          b.numeric_version("1.0.0-rc.2"))
    check("结果全是纯数字点分",
          all(p.isdigit() for p in b.numeric_version("1.0.0-alpha.1").split(".")),
          b.numeric_version("1.0.0-alpha.1"))
    check("异常输入也不炸", b.numeric_version("") == "0.0.0.0", b.numeric_version(""))
    check("产物文件名仍用完整版本号（不带 v）",
          b.product_name("bridge", "1.0.0-alpha.1").endswith("-1.0.0-alpha.1-windows-x86_64")
          or "1.0.0-alpha.1" in b.product_name("bridge", "1.0.0-alpha.1"),
          b.product_name("bridge", "1.0.0-alpha.1"))


def test_guard_is_windows_only() -> None:
    """WSA 网络守护只应在 Windows 上编译（WSA 是 Windows 独有组件）。

    用户指出：给 Linux/macOS 也构建守护进程产物是错的。
    """
    import sys as _sys
    _sys.path.insert(0, str(Path.cwd() / "tools"))
    import build_nuitka as b

    t_all_win, note_win = b.select_targets("all", True)
    check("Windows 上 --target all 编 bridge + guard",
          t_all_win == ["bridge", "guard"] and note_win == "", f"{t_all_win} {note_win!r}")

    t_all_nix, note_nix = b.select_targets("all", False)
    check("非 Windows 上 --target all 只编主程序",
          t_all_nix == ["bridge"] and "只适用于 Windows" in note_nix,
          f"{t_all_nix} {note_nix!r}")

    t_guard_nix, note_guard = b.select_targets("guard", False)
    check("非 Windows 上显式要 guard 会被拒绝",
          t_guard_nix == [] and "不提供该产物" in note_guard, f"{t_guard_nix} {note_guard!r}")

    t_bridge_nix, note_bridge = b.select_targets("bridge", False)
    check("非 Windows 上编 bridge 不受影响",
          t_bridge_nix == ["bridge"] and note_bridge == "", f"{t_bridge_nix}")

    t_guard_win, _ = b.select_targets("guard", True)
    check("Windows 上显式要 guard 正常", t_guard_win == ["guard"])


def _backlog_bridge(root: Path, fwd, bubbles: list, known: list | None = None,
                    catchup_max: int = 0):
    """搭一个只测补发逻辑的桥接：假 xtc 提供气泡列表，消息库预先塞入 known。"""
    class _Xtc:
        STATE_CHAT = "chat"

        def __init__(self, items):
            self.bubbles = list(items)

        def _chat_bubbles(self, root, include_own=False):
            return list(self.bubbles)

        def _is_system_msg(self, text):
            return text.startswith("发送成功")

        def is_in_chat(self):
            return False

    cfg = {"target": {"xtc_contact": "张三", "qq_private": "2218631043"},
           "xiaotiancai": {}, "webhook": {}}
    br = bridge_mod.MessageBridge(cfg, adb=None, xtc=_Xtc(bubbles), forwarder=fwd, logger=None)
    # 用本用例专属的状态文件：否则会读到真实 data/ 里的历史/回声缓存
    # （之前就因此让"撞库"判定误命中，用例之间互相污染）
    prefix = f".bugtest{_SEQ['n']}_"
    br.msgs = MessageLog(path=str(root / f"{prefix}msg_log.json"))
    br.history = bridge_mod.HistoryFilter(store_path=str(root / f"{prefix}history.json"))
    br.echo = bridge_mod.EchoFilter(store_path=str(root / f"{prefix}echo.json"))
    br._cmd_done_file = str(root / f"{prefix}cmd_done.json")
    br._catchup_max = catchup_max
    # 线上是"入队 + 工作线程异步转发"；测试里没有工作线程，
    # 这里把入队替换成同步执行，逻辑（撞库判定/入库/顺序）保持一致。
    br._queue_forward = lambda c, t, l, sticker=None: br._do_forward_job(
        c, t, l, sticker=sticker)
    for t in (known or []):
        br.msgs.append("xtc", "屑猹不喝茶", t)
    return br


def test_backlog_walk_until_known() -> None:
    """补发逻辑：从最新往回走，库里没有的补齐，撞到库里已有的就停。

    用户要求的就是这个：发完最新一条后看上一条，和库里一样就停；
    不一样就转发再往上，直到撞上库里已有的一条。
    """
    root = tmp_root()

    class _Fwd:
        def __init__(self, ok=True):
            self.sent: list = []
            self.ok = ok

        def send(self, t, i, m):
            self.sent.append(m)
            return self.ok

        def send_detail(self, t, i, m):
            self.sent.append(m)
            return self.ok, ""

    def bubbles(*texts):
        return [{"text": t, "time_label": "19:52"} for t in texts]

    try:
        fwd = _Fwd()
        br = _backlog_bridge(root, fwd, bubbles("老消息", "漏掉的1", "漏掉的2", "最新一条"),
                             known=["老消息"])
        n = br._forward_backlog(None, "屑猹不喝茶")
        check("撞到库里已有的那条就停，只补它上面的", n == 3, f"n={n} sent={fwd.sent}")
        check("按 旧 -> 新 顺序补发",
              ["漏掉的1" in fwd.sent[0], "漏掉的2" in fwd.sent[1], "最新一条" in fwd.sent[2]]
              == [True, True, True], str(fwd.sent))
        check("已经有的那条不会被重复补发",
              all("老消息" not in m for m in fwd.sent), str(fwd.sent))

        fwd.sent.clear()
        n2 = br._forward_backlog(None, "屑猹不喝茶")
        check("补发过的都进了库，再跑一次不再补", n2 == 0 and not fwd.sent,
              f"n={n2} sent={fwd.sent}")

        # 启动前积压：库是空的（等于全新装），可见的消息按用户要求也要补
        fwd2 = _Fwd()
        br2 = _backlog_bridge(root, fwd2, bubbles("积压1", "积压2"))
        n3 = br2._forward_backlog(None, "屑猹不喝茶")
        check("启动前积压的消息也补（不再按启动时刻过滤）",
              n3 == 2 and len(fwd2.sent) == 2, f"n={n3} sent={fwd2.sent}")

        # 命令文本与系统提示：跳过、不作为停止边界
        fwd3 = _Fwd()
        br3 = _backlog_bridge(root, fwd3,
                              bubbles("已知", "发送成功：x", "/小天才 帮助", "新消息"),
                              known=["已知"])
        n4 = br3._forward_backlog(None, "屑猹不喝茶")
        check("命令与系统提示被跳过且不算边界",
              n4 == 1 and "新消息" in fwd3.sent[0], f"n={n4} sent={fwd3.sent}")

        # 转发失败：不入库（所以之后还会被当成"库里没有"重试），但受 120 秒节流保护
        fwd4 = _Fwd(ok=False)
        br4 = _backlog_bridge(root, fwd4, bubbles("会失败的"))
        check("转发失败时不入库",
              br4._forward_backlog(None, "屑猹不喝茶") == 1
              and br4.msgs.seen("会失败的", "xtc") is False)
        check("失败的消息没进消息库（之后还会重试）",
              br4.msgs.seen("会失败的", "xtc") is False)
        br4.dedup = bridge_mod.Deduplicator()      # 模拟 120 秒节流窗口过去
        fwd4.ok = True
        n5 = br4._forward_backlog(None, "屑猹不喝茶")
        check("节流窗口过后会重试", n5 == 1, f"n={n5}")

        # 上限：只补最早的一批，剩下的下一轮继续
        fwd5 = _Fwd()
        br5 = _backlog_bridge(root, fwd5, bubbles("b1", "b2", "b3"), catchup_max=1)
        n6 = br5._forward_backlog(None, "屑猹不喝茶")
        check("超过上限时先补最早的，其余的下一轮继续",
              n6 == 1 and "b1" in fwd5.sent[0], f"n={n6} sent={fwd5.sent}")
        n7 = br5._forward_backlog(None, "屑猹不喝茶")
        check("下一轮接着补（顺序不乱）",
              n7 == 1 and "b2" in fwd5.sent[1], f"n={n7} sent={fwd5.sent}")

        br5._catchup_enabled = False
        fwd5.sent.clear()
        check("开关关闭时不补发",
              br5._forward_backlog(None, "屑猹不喝茶") == 0 and not fwd5.sent)

        # 线上路径：转发是异步入队（QQ 慢时不再卡住读屏）
        fwd6 = _Fwd()
        br6 = _backlog_bridge(root, fwd6, bubbles("异步消息"))
        br6._queue_forward = bridge_mod.MessageBridge._queue_forward.__get__(br6)
        n8 = br6._forward_backlog(None, "屑猹不喝茶")
        jobs = []
        while not br6._job_queue.empty():
            jobs.append(br6._job_queue.get_nowait())
        check("补发走的是异步队列（不会阻塞轮询）",
              n8 == 1 and jobs and jobs[0][0] == "forward", f"n={n8} jobs={jobs}")
        lbl_abs = br6._abs_time_label("19:52")
        check("入队时就短期去重，避免下一轮重复入队",
              br6.dedup.seen(("xtc", "屑猹不喝茶", "异步消息", lbl_abs)) is True, lbl_abs)
        check("去重键带上了时间标签（同文本不同时间算两条）",
              br6.dedup.seen(("xtc", "屑猹不喝茶", "异步消息",
                              br6._abs_time_label("19:53"))) is False)
        br6._do_forward_job(*jobs[0][1:4])          # 工作线程真正执行
        check("工作线程执行后才入长期历史/消息库",
              fwd6.sent and br6.msgs.seen("异步消息", "xtc") is True, str(fwd6.sent))
    finally:
        cleanup(root)


def test_msg_log_sharding_and_cap() -> None:
    """消息库：分库（只读最新库）+ 条数上限（0 = 不限制）。"""
    root = tmp_root()
    try:
        # 分库：每 3 条换一个新文件，只加载最新那个
        ml = MessageLog(path=str(root / ".bugtest-shard.json"), cap=0, shard_size=3)
        for i in range(7):
            ml.append("xtc", "张三", f"m{i}")
        check("按分库大小切开", ml.shard_count() == 3, f"shards={ml.shard_count()}")
        check("默认只加载最新分库", len(ml.recent(1)) == 1 and ml.count() == 7,
              f"count={ml.count()}")
        check("最新分库里的消息能查到", ml.seen("m6") is True)
        check("更新前的同一分库也能查到（同库内）", ml.seen("m4") is True)
        check("翻更老的分库由 recent 惰性加载",
              [e["text"] for e in ml.recent(7)] == [f"m{i}" for i in range(7)],
              str([e["text"] for e in ml.recent(7)]))

        # 重开一个实例：只加载最新分库，但 recent 能翻到老的
        ml2 = MessageLog(path=str(root / ".bugtest-shard.json"), cap=0, shard_size=3)
        check("重启后仍只加载最新分库", ml2.seen("m6") is True, "m6")
        check("重启后 recent 仍能翻出全量", ml2.count() == 7, f"count={ml2.count()}")

        # 条数上限（不分库）
        ml3 = MessageLog(path=str(root / ".bugtest-cap.json"), cap=5, shard_size=0)
        for i in range(12):
            ml3.append("xtc", "张三", f"c{i}")
        check("不分库时按条数上限截断", ml3.count() == 5, f"count={ml3.count()}")
        check("保留的是最新的", [e["text"] for e in ml3.recent(5)] ==
              [f"c{i}" for i in range(7, 12)], str([e["text"] for e in ml3.recent(5)]))

        # 0 = 不限制
        ml4 = MessageLog(path=str(root / ".bugtest-unlimited.json"), cap=0, shard_size=0)
        for i in range(30):
            ml4.append("xtc", "张三", f"u{i}")
        check("cap=0 表示不限制", ml4.count() == 30, f"count={ml4.count()}")

        # 分库时的上限按分库粒度控制：最多 ceil(cap/shard_size) 个分库
        ml5 = MessageLog(path=str(root / ".bugtest-shardcap.json"), cap=6, shard_size=3)
        for i in range(30):
            ml5.append("xtc", "张三", f"s{i}")
        check("分库时按上限删掉最老的分库", ml5.shard_count() == 2,
              f"shards={ml5.shard_count()}")
    finally:
        cleanup(root)


def main() -> int:
    for fn in (test_history_source_tags, test_history_source_from_plugin_payload,
               test_command_not_repeated, test_login_detection,
               test_password_field_masked, test_login_progress_not_failure,
               test_send_result_is_honest, test_send_speed_fast_path,
               test_recent_snapshot_window, test_plain_injection_skips_dump,
               test_ime_check_is_cached, test_launch_app_skips_hard_failures_fast,
               test_poll_loop_yields_to_pending_send,
               test_png_encoder_and_crop, test_screencap_header_parsing,
               test_sticker_detection_and_capture, test_sticker_forward_one_way,
               test_emoji_store_reads_original_file, test_blind_send_fast_typing,
               test_forwarder_wrapper_exposes_send_image,
               test_cache_pick_prefers_sticker_shape_and_animation,
               test_time_label_is_absolute_and_stable,
               test_wake_before_relaunch, test_keep_awake_heartbeat, test_confirm_sent_rule,
               test_launch_skips_when_foreground, test_recover_is_state_driven,
               test_popup_handling, test_custom_popup_auto_close,
               test_chat_page_detection, test_open_chat_when_already_in_chat,
               test_contact_name_matching, test_contact_matching_when_ids_differ,
               test_contact_not_found_reports_visible_names,
               test_contact_found_after_scroll, test_message_list_detection,
               test_login_state_uses_ui_not_activity_name,
               test_login_state_unknown_instead_of_not_logged_in,
               test_auto_login_decision_skips_unknown,
               test_activity_level_focus_ignores_ime,
               test_screen_off_detection_and_wake,
               test_window_recovery_and_compact_dump_error,
               test_open_chat_launches_app_when_not_foreground,
               test_input_hint_text_is_not_residue,
               test_foreground_logic_two_layers,
               test_auto_login_retry_semantics, test_webhook_logs_and_forwards,
               test_webhook_action_requests_reach_bridge, test_history_action_end_to_end,
               test_find_send_ignores_message_state_icon, test_content_desc_exact_match,
               test_time_label_is_per_message, test_time_labels_survive_compressed_dump,
               test_dump_prefers_full_hierarchy, test_latest_message_retries_when_labels_missing,
               test_start_enqueues_auto_init, test_plugin_send_reports_real_reason,
               test_no_delivery_confirm_on_forward_failure,
               test_app_state_machine, test_state_logged_once_and_warnings_throttled,
               test_poll_loop_dismisses_popup_without_cooldown,
               test_monotonic_sentinels_survive_fresh_boot,
               test_nuitka_version_args_are_numeric, test_guard_is_windows_only,
               test_backlog_walk_until_known,
               test_repeated_message_is_forwarded_again, test_backlog_runs_without_contact,
               test_msg_log_sharding_and_cap,
               test_logger_tolerant_stream):
        print(f"--- {fn.__name__} ---")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            check(f"{fn.__name__} 未抛异常", False, f"{type(e).__name__}: {e}")
    print(f"\n===== 回归测试：{RC['pass']} 通过 / {RC['fail']} 失败 =====")
    return 1 if RC["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
