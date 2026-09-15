# -*- coding: utf-8 -*-
"""回归测试：针对用户报告的具体问题（不需要设备，全部离线）。

覆盖：
  1. 历史消息带来源（手表 / QQ私聊 / QQ群），并附来源统计
  2. 同一条 /小天才 命令不会被反复执行/反复回复
  3. 已登录界面不再被误判为"未登录"（泛化"登录"字样不再触发）
  4. 登录表单按行精确校验；密码框是掩码时按长度校验（不再重复输入密码）

用法：python tools/test_reported_bugs.py
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import bridge as bridge_mod  # noqa: E402
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
    """只需要 dump_ui / get_current_focus / shell 的最小实现。"""

    def __init__(self, xml: str = "", focus: str = "com.xtc.watch/.MainActivity"):
        self.xml = xml
        self.focus = focus
        self.calls: list[str] = []

    def dump_ui(self, retries: int = 3, delay: float = 2.0):
        return ET.fromstring(self.xml or node_xml(""))

    def get_current_focus(self) -> str:
        return self.focus

    def shell(self, cmd: str, timeout=None) -> str:
        self.calls.append(cmd)
        return ""

    def tap_element(self, node) -> None:
        self.calls.append(f"tap {node.get('bounds')}")

    def keyevent(self, code: int) -> None:
        self.calls.append(f"keyevent {code}")

    def clear_text_field(self) -> None:
        self.calls.append("clear_text_field")

    def input_text(self, text, verify=None, retries=0, ensure_ime=True) -> bool:
        self.calls.append(f"input_text {text}")
        self.xml = self.xml.replace("%INPUT%", text)
        return True if verify is None else bool(verify())


def make_xtc(xml: str = "", focus: str = "com.xtc.watch/.MainActivity",
             ui_cfg: dict | None = None, adb: FakeAdb | None = None) -> tuple:
    adb = adb or FakeAdb(xml, focus)
    xtc = Xiaotiancai(adb, {"ui": ui_cfg or {}}, logger=None)
    return xtc, adb


# ------------------------------------------------------------------ 1. 来源
def make_bridge(root: Path):
    cfg = {"target": {"xtc_contact": "宝贝"}, "xiaotiancai": {}, "webhook": {}}
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
        br._archive_qq_send("[09-01 10:00] [小明] 中午吃什么", user_id="10001")
        br._archive_qq_send("[09-01 10:01] [小红] 群里说", user_id="10002", group_id="999")
        br.msgs.append("xtc", "宝贝", "我吃了", source="手表・宝贝", source_id="宝贝")
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
        br._archive_qq_send("[09-01 10:00] [小明] 私聊消息", user_id="10001")
        br._archive_qq_send("[09-01 10:01] [小红] 群消息", user_id="10002", group_id="999")
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
        # 1) 同一分钟内轮询 5 次（时间标签相同）→ 只执行一次
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

        # 4) 命令仍是"最新一条"、标签不变 → 再轮询多次也不再执行
        for _ in range(4):
            br._maybe_xtc_cmd("own", cmd, "09:42")
        check("执行完后同标签不再重复", br._job_queue.qsize() == 0,
              f"queue={br._job_queue.qsize()}")

        # 5) 用户重新输入同一条命令（新消息 → 新时间标签）→ 允许再次执行
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

    # 掩码字段：dump 出来是圆点、长度与明文一致 → 判定成功（不再重复输入）
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

    # 明文页面：文本与目标不一致 → 报失败（而不是无限重输）
    wrong_xml = node_xml(
        n(cls="android.widget.EditText", text="13800000000") +
        n(cls="android.widget.EditText", text="别的密码", password="true"))
    xtc3, adb3 = make_xtc(wrong_xml)
    edits3 = [nd for nd in adb3.dump_ui().iter("node")]
    ok3, rows3 = xtc3.fill_login_form(edits3, ["13800000000", "secret"])
    check("内容不符时报失败", ok3 is False, f"rows={rows3}")
    check("重试次数有限（≤2 次/字段）",
          sum(1 for c in adb3.calls if c.startswith("input_text")) <= 4,
          str([c for c in adb3.calls if c.startswith("input_text")]))


def main() -> int:
    for fn in (test_history_source_tags, test_history_source_from_plugin_payload,
               test_command_not_repeated, test_login_detection,
               test_password_field_masked):
        print(f"--- {fn.__name__} ---")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            check(f"{fn.__name__} 未抛异常", False, f"{type(e).__name__}: {e}")
    print(f"\n===== 回归测试：{RC['pass']} 通过 / {RC['fail']} 失败 =====")
    return 1 if RC["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
