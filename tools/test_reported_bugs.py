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

import json
import os
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import bridge as bridge_mod  # noqa: E402
import qq_webhook  # noqa: E402
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

    def is_in_foreground(self, package: str, use_cache: bool = True) -> bool:
        return bool(self.focus) and self.focus.startswith(package)

    def get_screen_size(self) -> tuple:
        return (1080, 1920)

    def shell(self, cmd: str, timeout=None) -> str:
        self.calls.append(cmd)
        return ""

    def ime_shown(self) -> bool:
        return False

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
        self.set_ui(input_text=input_text, tip=tip)

    def set_ui(self, input_text: str = "", tip: str = "", bubble: str = "") -> None:
        nodes = n(cls="android.widget.EditText", text=input_text,
                  rid="com.xtc.watch:id/et_chat_text_content",
                  bounds="[40,1700][900,1800]", focusable="true")
        nodes += n(cls="android.widget.TextView", text="发送",
                   rid="com.xtc.watch:id/tv_send_view", bounds="[920,1700][1060,1800]")
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


# ------------------------------------------------------------------ 8. 自动登录要会重试
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


def main() -> int:
    for fn in (test_history_source_tags, test_history_source_from_plugin_payload,
               test_command_not_repeated, test_login_detection,
               test_password_field_masked, test_login_progress_not_failure,
               test_send_result_is_honest, test_confirm_sent_rule,
               test_launch_skips_when_foreground, test_recover_is_state_driven,
               test_popup_handling,
               test_auto_login_retry_semantics, test_webhook_logs_and_forwards,
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
