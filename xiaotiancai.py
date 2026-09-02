# -*- coding: utf-8 -*-
"""小天才 App 操作层：启动 / 登录检测 / 打开聊天 / 发送消息 / 读取最新消息。

UI 适配策略（基于真机实测，com.xtc.watch v 登录后界面）：
- 聊天窗口 ChatActivity 有两种输入模式：
    * 语音模式：底部是"按住说话"按钮（chat_record_button），没有 EditText；
    * 文字模式：点击底部左侧图标 iv_left_img_view（单次点击）切出
      EditText（et_chat_text_content）+ 发送按钮（tv_send_view，输入内容后才出现）。
- 聊天窗口判定：看聊天输入栏特征 id（et_chat_text_content / chat_record_button /
  chat_input），不再用 Activity 名猜（会误判弹窗/接收画面）。
- 弹窗处理：系统录音权限弹窗（允许前台使用）、小天才"警告"弹窗（点取消）。
- 读取消息：聊天窗口内取输入栏上方最靠下的一条文本气泡（剔除 UI 文案）；
  聊天列表取联系人行内的消息预览。
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

from adb_controller import ADBController, AdbError

# 聊天窗口输入栏的特征 resource-id 末段（出现其一即认为在聊天页）
_CHAT_IDS = ("et_chat_text_content", "chat_record_button", "chat_input")
# 语音模式 → 文字模式的切换按钮
_SWITCH_TO_TEXT_IDS = ("iv_left_img_view", "chat_record_button")
# 系统录音权限弹窗按钮
_PERMISSION_ALLOW_ID = (
    "com.android.permissioncontroller:id/permission_allow_foreground_only_button"
)
# 小天才内部警告弹窗"取消"
_CANCEL_DIALOG_ID = "com.xtc.watch:id/btn_cancel"
# 网络提示条（会混进聊天文本，读取时排除）
_TIP_IDS = ("tv_weichat_uninstall_hint", "iv_tips_content")
# 发送失败弹窗标题
_SEND_FAIL_TITLE = "消息发送"

_DEFAULT_JUNK = ["发送", "表情", "语音", "拍照", "更多", "已读", "撤回", "按住说话",
                 "试试和宝贝聊天吧", "试试将作业要求发送给宝贝吧"]


class Xiaotiancai:
    def __init__(self, adb: ADBController, cfg: dict | None = None, logger=None):
        self.adb = adb
        cfg = cfg or {}
        self.package = cfg.get("package", "com.xtc.watch")
        self.main_activity = cfg.get("main_activity", ".MainActivity")
        self.ui = cfg.get("ui", {}) or {}
        self.logger = logger
        self._warned_not_login = False
        # 坐标缓存：进入聊天后输入框/发送按钮位置基本固定，避免每次全量 dump
        self._cache = {"input_xy": None, "send_xy": None, "ts": 0.0}

    def log(self, level: str, msg: str):
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(msg)

    # ------------------------------------------------------------------ 生命周期
    def launch(self) -> bool:
        self.log("info", f"启动小天才 App: {self.package}")
        try:
            self.adb.launch_app(self.package, self.main_activity)
            ok = self.adb.wait_for_activity(self.package, timeout=20)
            if not ok:
                self.log("warning", "小天才 App 启动后未检测到前台窗口")
            return ok
        except AdbError as e:
            self.log("error", f"启动失败: {e}")
            return False

    def current_activity(self) -> str:
        return self.adb.get_current_focus() or ""

    def is_logged_in(self) -> bool:
        """通过 Activity 名 + 界面文案判断是否已登录家长账号。
        小天才 App 不在前台（如桌面）时一律视为未登录。"""
        act = self.current_activity()
        if not act.startswith(self.package):
            return False
        for marker in ("welcome", "login", "register", "signin"):
            if marker in act.lower():
                return False
        try:
            root = self.adb.dump_ui()
        except AdbError:
            return False
        texts = [n.get("text", "") for n in root.iter("node")]
        joined = "".join(texts)
        for marker in self.ui.get("login_markers", ["注册/登录", "立即登录", "登录"]):
            if marker in joined:
                return False
        # 首启隐私协议弹窗 = 尚未进入 App，视为未登录
        if "温馨提示" in joined and ("同意" in joined or "不同意" in joined):
            return False
        return True

    def require_login(self) -> bool:
        ok = self.is_logged_in()
        if not ok and not self._warned_not_login:
            self.log("warning", "小天才 App 未登录家长账号：请在模拟器中完成登录（登录前无法读取/发送消息）")
            self._warned_not_login = True
        return ok

    # ------------------------------------------------------------------ 账密登录
    def login(self, phone: str, password: str) -> str:
        """账密登录小天才（手机号 + 密码，非验证码）。

        返回状态：
        - 'already' 已经登录，无需操作
        - 'ok'      登录成功
        - 'risk'    触发安全验证，需要用户手动在模拟器操作
        - 'fail'    账号/密码错误等（登录失败）
        - 'error'   流程异常（未找到控件/未配置账密）
        """
        if not phone or not password:
            self.log("error", "账密登录需要 config.yaml → xiaotiancai.login.phone / password")
            return "error"
        if self.is_logged_in():
            return "already"
        try:
            # 0) 确保 App 在前台 + 处理首启隐私弹窗/权限等
            self.launch()
            time.sleep(2.0)
            self._dismiss_blockers()
            # 1) 进入登录页（欢迎页 → 点"注册/登录"）
            root = self.adb.dump_ui()
            if not self._is_login_page(root):
                entry = self._first(
                    self.adb.find_element(root, text="注册/登录", text_contains=True),
                    self.adb.find_element(root, content_desc="注册/登录"))
                if entry is None:
                    entry = self._first(
                        self.adb.find_element(root, text="登录", text_contains=True),
                        self.adb.find_element(root, content_desc="登录"))
                if entry is not None:
                    self.log("debug", f"点登录入口 {entry.get('bounds')}")
                    self.adb.tap_element(entry)
                    time.sleep(2.0)
                    root = self.adb.dump_ui()
            # 2) 切到"账号密码登录"（短信登录页底部多段链接的最左段）
            if not self._is_account_login_page(root):
                switch = self._find_account_login_entry(root)
                if switch is not None:
                    self.log("debug", f"点账密入口 {switch.get('bounds')}")
                    self._tap_entry_left(switch)
                    time.sleep(2.0)
                    root = self.adb.dump_ui()
                else:
                    self.log("debug", "未找到账密入口")
            self.log("debug",
                     f"切换后前台={self.current_activity()} "
                     f"EditText={len(self.adb.find_elements(root, class_name='EditText'))}")
            # 3) 找手机号 + 密码两个输入框
            edits = self.adb.find_elements(root, class_name="EditText")
            edits.sort(key=lambda n: (self._bounds(n) or (0, 0, 0, 0))[1])
            if len(edits) < 2:
                self.log("error", "未找到账密输入框（页面结构变化？运行 python tools/dump_ui.py 查看登录页）")
                return "error"
            # 4) 输入手机号、密码（先清空再输入）
            for edit, value in ((edits[0], phone), (edits[1], password)):
                self.adb.tap_element(edit)
                time.sleep(1.5)
                for _ in range(20):
                    self.adb.keyevent(67)  # 清空可能残留的内容
                if not self.adb.input_text(value):
                    self.log("error", "文本注入失败（ADBKeyBoard 未就绪？）")
                    return "error"
                time.sleep(0.5)
            # 5) 勾选协议（若存在且未勾选）
            root = self.adb.dump_ui()
            cb = self._first(
                self.adb.find_element(root, class_name="CheckBox"),
                self.adb.find_element(root, resource_id="com.xtc.watch:id/cb_protocol"))
            if cb is not None and cb.get("checked") != "true":
                self.adb.tap_element(cb)
                time.sleep(0.5)
            # 6) 点登录（先收起键盘——输入密码后键盘仍打开，会挡住/截获点击）
            #    未离开账密登录页说明点击被吞，重试最多 3 次
            self.adb.keyevent(4)
            time.sleep(0.8)
            tapped = False
            for attempt in range(1, 4):
                root = self.adb.dump_ui()
                btn = self._find_login_button(root)
                if btn is None:
                    break  # 页面已跳转（登录中/验证页/成功）
                self.log("debug", f"点登录按钮（第 {attempt} 次）{btn.get('bounds')}")
                self.adb.tap_element(btn)
                tapped = True
                time.sleep(2.5)
                act = self.current_activity()
                if not act.endswith("LoginActivity"):
                    break  # 已离开账密登录页，请求已发出
                self.log("debug", "点击后仍在登录页，重试")
            if not tapped:
                self.log("error", "未找到登录按钮（页面结构变化？运行 dump_ui 查看）")
                return "error"
            # 7) 等待结果（最多 25s）
            deadline = time.time() + 25
            while time.time() < deadline:
                time.sleep(2.5)
                try:
                    if self.is_logged_in():
                        self.log("info", "小天才账密登录成功")
                        return "ok"
                    root = self.adb.dump_ui()
                    risk = self._detect_risk(root)
                    if risk:
                        self.log("warning", f"登录触发安全验证（{risk}），需要用户手动操作")
                        return "risk"
                    err = self._detect_login_error(root)
                    if err:
                        self.log("error", f"登录失败: {err}")
                        return "fail"
                except AdbError:
                    continue
            self.log("warning", "登录结果超时未确定")
            return "fail"
        except AdbError as e:
            self.log("error", f"登录流程异常: {e}")
            return "error"

    # ------------------------------------------------------------------ 登录页判定/工具
    def _is_login_page(self, root: ET.Element) -> bool:
        """当前页面是否为登录页（短信/账密均可）。
        以输入框/获取验证码等登录页专属元素为准——欢迎页虽有"注册/登录"文案，但不是登录页。"""
        if self.adb.find_elements(root, class_name="EditText"):
            return True
        texts = "".join(n.get("text", "") for n in root.iter("node"))
        return "获取验证码" in texts or "短信验证码登录" in texts

    def _is_account_login_page(self, root: ET.Element) -> bool:
        """是否为账密登录页：至少两个 EditText（手机号+密码）。"""
        return len(self.adb.find_elements(root, class_name="EditText")) >= 2

    def _find_account_login_entry(self, root: ET.Element):
        """切到账密登录的入口（短信页底部文案 '账号密码登录' 等）。"""
        return self._first(
            self.adb.find_element(root, text="账号密码登录", text_contains=True),
            self.adb.find_element(root, content_desc="账号密码登录"))

    def _tap_entry_left(self, node) -> None:
        """多段链接（'账号密码登录｜注册｜忘记密码…'）点最左段；普通节点点中心。"""
        b = self._bounds(node)
        if b is None:
            self.adb.tap_element(node)
            return
        x1, y1, x2, y2 = b
        text = node.get("text", "")
        if "｜" in text or "|" in text:
            self.adb.tap(x1 + int((x2 - x1) * 0.2), (y1 + y2) // 2)
        else:
            self.adb.tap((x1 + x2) // 2, (y1 + y2) // 2)

    def _find_login_button(self, root: ET.Element):
        """登录按钮：优先精确文本"登录"；其次 id 结尾为 login 的按钮
        （注意：不能用"id 含 login"——tv_login_area_title/tv_login_account 等
        标签控件 id 也含 login，会误命中）。"""
        n = self.adb.find_element(root, text="登录")
        if n is not None:
            return n
        for node in root.iter("node"):
            tail = self._id_tail(node)
            if tail.endswith("login") or "login_btn" in tail or "btn_login" in tail:
                if "Text" in node.get("class", "") or "Button" in node.get("class", ""):
                    return node
        return self._first(
            self.adb.find_element(root, content_desc="登录"),
            None)

    def _detect_risk(self, root: ET.Element) -> str:
        """检测安全验证（账号风险）界面，返回命中的标记文案；无则返回 ''。
        注意：不要用 "验证码" 这类宽泛词——登录页底部固定有"短信验证码登录"文案会误判。"""
        markers = self.ui.get("risk_markers",
                              ["安全验证", "风险", "滑块", "拖动滑块", "图形验证",
                               "完成验证", "请完成验证", "滑动验证"])
        texts = [n.get("text", "") for n in root.iter("node")]
        joined = "".join(texts)
        for m in markers:
            if m in joined:
                return m
        return ""

    def _detect_login_error(self, root: ET.Element) -> str:
        """检测登录失败提示（账号/密码错误等），返回命中文案；无则返回 ''。"""
        markers = self.ui.get("login_error_markers",
                              ["密码错误", "账号不存在", "不存在", "错误", "失败", "次数过多"])
        texts = [n.get("text", "") for n in root.iter("node")]
        joined = "".join(texts)
        for m in markers:
            if m in joined:
                return m
        return ""

    # ------------------------------------------------------------------ 弹窗处理
    def dismiss_blockers(self) -> bool:
        """公开别名：多轮清理所有可识别的弹窗/遮挡，直到界面干净。"""
        return self.settle()

    def settle(self, max_passes: int = 6) -> bool:
        """多轮弹窗清理：权限/隐私协议/警告/通用对话框按钮/BACK 兜底。
        任何一轮处理了内容就继续下一轮，直到界面干净或达到轮数上限。"""
        handled_any = False
        for _ in range(max_passes):
            if self._dismiss_blockers():
                handled_any = True
                time.sleep(0.8)
                continue
            break
        return handled_any

    def _dismiss_blockers(self) -> bool:
        """处理单轮可识别的弹窗/遮挡，返回是否处理过。
        覆盖：系统权限 / 首启隐私协议 / 通话面板弹层 / 小天才警告 /
        通用对话框文本按钮 / 弹窗窗口 BACK 兜底。
        注意：普通页面的 NAF 节点（图片等）不算遮挡，绝不能按 BACK（会把 App 退到桌面）。"""
        focus = self.current_activity()
        try:
            if "permissioncontroller" in focus.lower():
                root = self.adb.dump_ui()
                btn = self.adb.find_element(root, resource_id=_PERMISSION_ALLOW_ID)
                if btn is not None:
                    self.adb.tap_element(btn)
                    self.log("info", "已允许录音权限（前台使用）")
                    return True
                # 允许失败的兜底：允许一次 / 允许（部分镜像按钮不同）
                btn = self.adb.find_element(
                    root, resource_id="com.android.permissioncontroller:id/permission_allow_button")
                if btn is not None:
                    self.adb.tap_element(btn)
                    self.log("info", "已允许录音权限")
                    return True
                return False
            root = self.adb.dump_ui()
            # 通话面板弹层（视频通话/拨打电话 + 取消；聊天/联系人页 "+" 菜单误触出现）
            texts_all = "".join((n.get("text") or "") for n in root.iter("node"))
            if "视频通话" in texts_all and "拨打电话" in texts_all:
                cancel = (self.adb.find_element(
                    root, resource_id="com.xtc.watch:id/tv_cancel")
                    or self.adb.find_element(root, text="取消"))
                if cancel is not None:
                    self.adb.tap_element(cancel)
                    self.log("info", "已关闭通话面板弹层")
                    return True
            # 首启隐私协议弹窗（温馨提示 → 点"同意"；必须先于通用取消按钮）
            title = self.adb.find_element(root, resource_id="com.xtc.watch:id/tv_title")
            if title is not None and "温馨提示" in title.get("text", ""):
                sure = self.adb.find_element(root, resource_id="com.xtc.watch:id/btn_sure")
                if sure is not None:
                    self.adb.tap_element(sure)
                    self.log("info", "已同意隐私协议（首启弹窗）")
                    return True
            # 小天才警告弹窗（取消 / 确认）：先取取消，没有则取确认/确定
            btn = self.adb.find_element(root, resource_id=_CANCEL_DIALOG_ID)
            if btn is not None:
                self.adb.tap_element(btn)
                self.log("info", "已关闭小天才弹窗")
                return True
            # 通用对话框文本按钮（按需取确认类或取消类，避免误关）
            clicked = self._tap_any_dialog_button(root)
            if clicked:
                return True
            # BACK 兜底：仅当前台是独立弹窗/对话框窗口时（如 PopupWindow），
            # 普通 Activity 页面即使有 NAF 节点也不按返回，防止 App 退到桌面。
            if "PopupWindow" in focus or "Dialog" in focus:
                self.adb.keyevent(4)
                self.log("info", "检测到弹窗窗口，按返回键关闭")
                return True
        except AdbError:
            pass
        return False

    def _tap_any_dialog_button(self, root: ET.Element) -> bool:
        """点通用对话框按钮。优先确认类（同意/确定/知道了/好的），其次取消类。
        仅当界面存在"对话框特征"（有多个按钮文本）时才动作，避免误点正常界面按钮。"""
        texts = [n.get("text", "").strip() for n in root.iter("node") if n.get("text", "").strip()]
        confirm_keys = ("同意", "确定", "知道了", "好的", "确认", "允许")
        cancel_keys = ("取消", "关闭", "不同意", "暂不", "以后再说")
        hit = [t for t in texts if t in confirm_keys or t in cancel_keys]
        if len(hit) < 2 and not any("温馨提示" in t or "警告" in t or "提示" in t for t in texts):
            return False  # 非对话框场景不动作
        for key in confirm_keys:
            n = self.adb.find_element(root, text=key)
            if n is not None:
                self.adb.tap_element(n)
                self.log("info", f"已点击对话框按钮: {key}")
                return True
        for key in cancel_keys:
            n = self.adb.find_element(root, text=key)
            if n is not None:
                self.adb.tap_element(n)
                self.log("info", f"已点击对话框按钮: {key}")
                return True
        return False

    # ------------------------------------------------------------------ 界面判定
    def is_in_chat(self) -> bool:
        """聊天窗口判定。
        快路径：前台 Activity 以 ChatActivity 结尾（该 App 聊天窗固定类名，快且准）；
        兜底：按输入栏特征 id 确认（防 Activity 名误判弹窗/接收画面）。"""
        act = self.current_activity().lower()
        if act.endswith("chatactivity"):
            return True
        try:
            root = self.adb.dump_ui()
        except AdbError:
            return False
        return self._find_chat_bar(root) is not None

    @staticmethod
    def _id_tail(node) -> str:
        rid = node.get("resource-id", "")
        return rid.split("/")[-1]

    def _find_chat_bar(self, root: ET.Element):
        """聊天输入栏（任意特征 id 的节点）。"""
        for n in root.iter("node"):
            if self._id_tail(n) in _CHAT_IDS:
                return n
        return None

    # ------------------------------------------------------------------ 打开聊天
    @staticmethod
    def _first(*nodes):
        """返回第一个非 None 节点（Element 真值判断已弃用，禁止用 or 链）。"""
        for n in nodes:
            if n is not None:
                return n
        return None

    def open_chat(self, contact: str) -> bool:
        if not contact:
            self.log("error", "open_chat 缺少联系人昵称（config.yaml → target.xtc_contact）")
            return False
        if self.is_in_chat():
            return True
        self._dismiss_blockers()
        try:
            # 主页即"微聊"列表：先直接在当前页找联系人，找不到再尝试切 Tab
            root = self.adb.dump_ui()
            node = self._find_contact_node(root, contact)
            if node is None:
                tab_text = self.ui.get("message_tab_text", "微聊")
                # 精确匹配，避免命中"消息动态"等含"消息"的入口
                tab = self._first(
                    self.adb.find_element(root, text=tab_text),
                    self.adb.find_element(root, content_desc=tab_text))
                if tab is not None:
                    self.adb.tap_element(tab)
                    time.sleep(1.5)
                    root = self.adb.dump_ui()
                    node = self._find_contact_node(root, contact)
            if node is None:
                self.log("error", f"在消息列表找不到联系人: {contact}")
                return False
            self.adb.tap_element(node)

            # 等待进入聊天；若误入"手表消息"等页面，按返回后重试一次
            deadline = time.time() + 12
            retried = False
            while time.time() < deadline:
                if self.is_in_chat():
                    return True
                act = self.current_activity()
                if not act.startswith(self.package) or "WatchMsg" in act:
                    self.adb.keyevent(4)  # 返回
                    time.sleep(1.0)
                    root = self.adb.dump_ui()
                    node = self._find_contact_node(root, contact)
                    if node is not None:
                        self.adb.tap_element(node)
                    retried = True
                time.sleep(1)
            self.log("warning", f"点击联系人 {contact} 后未检测到聊天窗口（可能 App 界面有弹窗）")
            return False
        except AdbError as e:
            self.log("error", f"打开聊天失败: {e}")
            return False

    def _find_contact_node(self, root: ET.Element, contact: str):
        """在消息列表里找联系人节点：优先精确/包含文本匹配，再按 content-desc。"""
        return self._first(
            self.adb.find_element(root, text=contact, text_contains=True),
            self.adb.find_element(root, content_desc=contact))

    # ------------------------------------------------------------------ 发送消息
    def send_message(self, text: str) -> bool:
        try:
            # 快路径：缓存命中且在聊天页 → 用缓存的输入框/发送按钮坐标，不 dump
            if self._fast_path_available():
                ix, iy = self._cache["input_xy"]
                sx, sy = self._cache["send_xy"]
                self.adb.tap(ix, iy)
                time.sleep(1.5)
                if self.adb.input_text(text):
                    time.sleep(0.8)
                    self.adb.tap(sx, sy)
                    time.sleep(1.0)
                    if self._send_confirmed(text):
                        return True
                self.log("warning", "快路径发送未确认，回退完整流程")

            self._dismiss_blockers()
            root = self.adb.dump_ui()
            input_node = self._find_input(root)
            if input_node is None:
                # 语音模式没有输入框：单击左侧图标切到文字模式
                if self._switch_to_text_mode(root):
                    self.log("info", "已在语音模式，切换到文字输入")
                    time.sleep(1.5)
                    root = self.adb.dump_ui()
                    input_node = self._find_input(root)
            if input_node is None:
                self.log("error", "未找到输入框（请确认当前在聊天页）")
                return False
            self.adb.tap_element(input_node)
            time.sleep(1.5)  # 等待软键盘弹出完成，避免输入被吞
            if not self.adb.input_text(text):
                return False
            time.sleep(0.5)
            root = self.adb.dump_ui()
            send_node = self._find_send(root)
            if send_node is None:
                self.log("warning", "未找到发送按钮，改用回车发送")
                self.adb.keyevent(66)  # KEYCODE_ENTER
            else:
                self.adb.tap_element(send_node)
            time.sleep(1.0)
            if not self._send_confirmed(text):
                self.log("warning", "消息可能未发出（输入框仍保留内容），请检查小天才 App 与手表网络")
                return False
            return True
        except AdbError as e:
            self.log("error", f"发送消息失败: {e}")
            return False

    def _send_confirmed(self, text: str) -> bool:
        """发送后确认：输入框里已不含刚输入的文本 → 视为发出。
        不做网络提示条判定（历史失败留下的提示条会误报）。"""
        try:
            root = self.adb.dump_ui()
        except AdbError:
            return True  # dump 失败无法确认，不判失败（避免误报）
        edit = self._find_input(root)
        if edit is not None:
            content = edit.get("text", "")
            if text and content and text in content:
                return False  # 文本还留在输入框 = 没发出去
        return True

    def _switch_to_text_mode(self, root: ET.Element) -> bool:
        """语音模式 → 文字模式：单击 iv_left_img_view（实测单击即可切换）。"""
        for n in root.iter("node"):
            if self._id_tail(n) in _SWITCH_TO_TEXT_IDS:
                self.adb.tap_element(n)
                return True
        return False

    def _fast_path_available(self) -> bool:
        """快路径可用：缓存未过期 + 确认仍在聊天页（快速 Activity 判断）。"""
        if self._cache["input_xy"] is None or self._cache["send_xy"] is None:
            return False
        if time.monotonic() - self._cache["ts"] > 120:
            return False
        return self.is_in_chat()

    def _find_input(self, root: ET.Element):
        rid = self.ui.get("input_resource_id", "")
        if rid:
            n = self.adb.find_element(root, resource_id=rid)
            if n is not None:
                self._cache_xy("input_xy", n)
                return n
        # 特征 id 优先（实测 et_chat_text_content）
        for n in root.iter("node"):
            if self._id_tail(n) == "et_chat_text_content":
                self._cache_xy("input_xy", n)
                return n
        nodes = self.adb.find_elements(root, class_name="EditText")
        if nodes:
            # 取最靠下的 EditText，通常是聊天输入框
            n = max(nodes, key=lambda n: (self._bounds(n) or (0, 0, 0, 0))[3])
            self._cache_xy("input_xy", n)
            return n
        return None

    def _find_send(self, root: ET.Element):
        rid = self.ui.get("send_resource_id", "")
        if rid:
            n = self.adb.find_element(root, resource_id=rid)
            if n is not None:
                self._cache_xy("send_xy", n)
                return n
        # resource-id 含 send（实测 tv_send_view）
        for n in root.iter("node"):
            if "send" in self._id_tail(n).lower() and "Text" in n.get("class", ""):
                self._cache_xy("send_xy", n)
                return n
        # 精确文本"发送"（不能用 text_contains：会命中输入框"发送文字"提示）
        for t in self.ui.get("send_texts", ["发送"]):
            n = self._first(
                self.adb.find_element(root, text=t),
                self.adb.find_element(root, content_desc=t))
            if n is not None:
                self._cache_xy("send_xy", n)
                return n
        return None

    def _cache_xy(self, key: str, node) -> None:
        try:
            x, y = self.adb.node_center(node)
            self._cache[key] = (x, y)
            self._cache["ts"] = time.monotonic()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 界面初始化/恢复
    def ensure_input_clean(self) -> str:
        """确保文字模式（有输入框），并清空输入框内容。返回状态描述。"""
        try:
            root = self.adb.dump_ui()
            edit = self._find_input(root)
            if edit is None:
                if self._switch_to_text_mode(root):
                    time.sleep(1.5)
                    root = self.adb.dump_ui()
                    edit = self._find_input(root)
            if edit is None:
                return "未找到输入框（可能不在聊天页或界面异常）"
            self.adb.tap_element(edit)
            time.sleep(1.2)
            for _ in range(40):
                self.adb.keyevent(67)  # 清空可能残留的文字
            return "文字模式已就绪，输入框已清空"
        except AdbError as e:
            return f"输入框处理失败: {e}"

    def keyboard_visible(self) -> bool:
        """软键盘是否弹出（dumpsys input_method 判断）。"""
        try:
            return "mInputShown=true" in (self.adb.shell("dumpsys input_method") or "")
        except AdbError:
            return False

    def close_keyboard(self) -> bool:
        """收起软键盘（若可见）。返回是否执行了收起动作。"""
        try:
            if self.keyboard_visible():
                self.adb.keyevent(4)
                time.sleep(0.8)
                return True
        except AdbError:
            pass
        return False

    # ------------------------------------------------------------------ 读取消息
    def get_latest_message(self):
        """返回 (contact, text, time_label, own_text, own_recent)；无法确定时
        (None, None, "", "", [])。

        own_text = 最新一条"自己发的"消息文本；own_recent = 最近若干条自己发的消息
        （新→旧，供 xtc 侧命令检测；命令可能被送达确认等新消息盖过，需扫最近几条）。
        复用同一次 dump，避免命令轮询额外开 uiautomator dump。不抛异常。

        只在聊天窗口内读取——列表预览无法可靠判断发送方（家长侧手动发送的消息
        也会出现在预览里），会被误当成对方消息转发。轮询层负责确保聊天窗口已打开。"""
        try:
            if not self.require_login():
                return (None, None, "", "", [])
            root = self.adb.dump_ui()
            if self.is_in_chat():
                contact, text, time_label = self._latest_in_chat(root)
                own_recent = self._own_texts_in_chat(root)
                return (contact, text, time_label,
                        own_recent[0] if own_recent else "", own_recent)
            return (None, None, "", "", [])  # 不在聊天页不读列表（防误转发家长侧消息）
        except Exception as e:  # noqa: BLE001 读取失败不致命
            self.log("warning", f"读取消息异常: {e}")
            return (None, None, "", "", [])

    def _latest_in_chat(self, root: ET.Element):
        """聊天窗口内：取最新一条"别人发来的"消息，返回 (contact, text, time_label)。

        识别依据（真机实测）：
        - 消息气泡 id=chat_msg_item_content（文本为 TextView，表情/语音为 ImageView，text 可能为空）；
        - content-desc 标注发送方：'童武洋发的消息,内容'（手表发） vs '你发的消息,内容'（自己发，跳过）；
        - **双重判断**：desc 标注为主，气泡左右位置为辅（自己发=右侧、别人发=左侧），
          desc 缺失时按位置判断（右侧=自己发跳过）；
        - 时间标签取气泡上方最近的日期节点（tv_chat_msg_item_date，如 "06:56" / "昨天 23:42"）。
        注意：最新消息可能被输入栏遮挡（y 超出输入栏顶部），因此不做输入栏边界裁剪。
        """
        screen_w = self.adb.get_screen_size()[0]
        filter_own = bool(self.ui.get("filter_own_bubbles", True))
        junk = set(self.ui.get("chat_junk_texts", _DEFAULT_JUNK))
        # 收集日期标签（按 y 排序，供气泡取最近上方标签）
        dates = []
        # 发送失败提示条（网络异常等）：下方带该提示的气泡 = 未送达，跳过
        fail_hints = []
        for n in root.iter("node"):
            tail = self._id_tail(n)
            if tail == "tv_chat_msg_item_date":
                b = self._bounds(n)
                if b and n.get("text", "").strip():
                    dates.append((b[1], b[3], n.get("text", "").strip()))
            elif tail in ("tv_weichat_uninstall_hint", "iv_tips_content"):
                b = self._bounds(n)
                if b:
                    fail_hints.append((b[1], b[3]))
        candidates = []
        for n in root.iter("node"):
            if self._id_tail(n) != "chat_msg_item_content":
                continue  # 只关心消息气泡
            t = n.get("text", "").strip()
            desc = n.get("content-desc", "")
            b = self._bounds(n)
            if b is None:
                continue
            # 下方带"发送失败"提示的气泡 = 未送达（手动发送失败等），跳过
            if any(fh_top - 60 <= b[3] <= fh_top + 30 for fh_top, _ in fail_hints):
                continue
            center_x = (b[0] + b[2]) / 2
            if "发的消息" in desc:
                # App 标注了发送方：'XX发的消息,内容'（别人） / '你发的消息,内容'（自己）
                if desc.startswith("你发的"):
                    continue
                if not t and "," in desc:
                    t = desc.split(",", 1)[1].strip()  # 表情/语音等从 desc 取内容类型
            else:
                # 无标注：按左右位置判断（自己发=右侧跳过）
                if t in junk:
                    continue
                if filter_own and center_x > screen_w * 0.55:
                    continue  # 右侧气泡 = 自己发的消息
            if not t:
                continue
            # 桥接系统提示（如送达确认 ✅/❌）一律不转发，防止循环
            if self._is_system_msg(t):
                continue
            candidates.append((n, t, b[3]))
        if not candidates:
            return (None, None, "")
        n, t, y_bottom = max(candidates, key=lambda c: c[2])
        # 取气泡上方最近的日期标签作为时间
        time_label = ""
        bubble_top = (self._bounds(n) or (0, 0, 0, 0))[1]
        # dates 按 y 升序；取"最靠下且仍在气泡上方"的标签 = 最近的上方标签
        # （注意不能用第一个满足的——那是最上面的标签，会取到更早消息的时间）
        for d_top, d_bottom, d_text in reversed(dates):
            if d_bottom <= bubble_top + 5:  # 标签在气泡上方
                time_label = d_text
                break
        return (None, t, time_label)

    # ------------------------------------------------------------------ 历史消息 / 命令轮询
    def _chat_bubbles(self, root: ET.Element, include_own: bool = False) -> list[dict]:
        """解析一次 UI dump 的聊天消息气泡，按屏幕从上到下（旧→新）排序。

        识别逻辑与 _latest_in_chat 一致（气泡 id=chat_msg_item_content）：
        - content-desc 标注发送方（'XX发的消息'/'你发的消息'）优先；无标注按左右位置
          （右侧=自己发）；
        - 下方带发送失败提示（网络异常）的气泡剔除；
        - 垃圾 UI 文案 / 桥接系统提示（发送成功 等前缀）剔除；
        - include_own=False 时跳过自己发的消息。
        返回 [{text, is_own, contact, time_label, y_bottom}]。
        """
        screen_w = self.adb.get_screen_size()[0]
        filter_own = bool(self.ui.get("filter_own_bubbles", True))
        junk = set(self.ui.get("chat_junk_texts", _DEFAULT_JUNK))
        dates: list[tuple[int, int, str]] = []
        fail_hints: list[tuple[int, int]] = []
        for n in root.iter("node"):
            tail = self._id_tail(n)
            if tail == "tv_chat_msg_item_date":
                b = self._bounds(n)
                if b and n.get("text", "").strip():
                    dates.append((b[1], b[3], n.get("text", "").strip()))
            elif tail in _TIP_IDS:
                b = self._bounds(n)
                if b:
                    fail_hints.append((b[1], b[3]))
        out: list[dict] = []
        for n in root.iter("node"):
            if self._id_tail(n) != "chat_msg_item_content":
                continue
            t = (n.get("text", "") or "").strip()
            desc = (n.get("content-desc", "") or "").strip()
            b = self._bounds(n)
            if b is None:
                continue
            if any(fh_top - 60 <= b[3] <= fh_top + 30 for fh_top, _ in fail_hints):
                continue  # 气泡下方是发送失败提示 = 未送达
            center_x = (b[0] + b[2]) / 2
            is_own = False
            contact = ""
            if "发的消息" in desc:
                if desc.startswith("你发的"):
                    is_own = True
                else:
                    contact = desc.split("发的消息", 1)[0].strip()
                if not t and "," in desc:
                    t = desc.split(",", 1)[1].strip()  # 表情/语音等从 desc 取类型
            else:
                # 无标注：右侧气泡 = 自己发的消息
                is_own = filter_own and center_x > screen_w * 0.55
            if not t or t in junk:
                continue
            if self._is_system_msg(t):
                continue
            if is_own and not include_own:
                continue
            time_label = ""
            for d_top, d_bottom, d_text in reversed(dates):  # 取气泡上方最近的日期标签
                if d_bottom <= b[1] + 5:
                    time_label = d_text
                    break
            out.append({"text": t, "is_own": is_own, "contact": contact,
                        "time_label": time_label, "y_bottom": b[3]})
        out.sort(key=lambda it: it["y_bottom"])
        return out

    def _latest_own_in_chat(self, root: ET.Element) -> str:
        """聊天页内最新一条"自己发的"消息文本（系统/垃圾已过滤）；无则 ""。"""
        own = self._own_texts_in_chat(root)
        return own[0] if own else ""

    def _own_texts_in_chat(self, root: ET.Element, limit: int = 8) -> list[str]:
        """聊天页内最近若干条"自己发的"消息文本（新→旧，系统/垃圾已过滤）。
        命令可能被送达确认等后续消息盖过（不再是"最新一条"），检测时扫最近几条。"""
        items = self._chat_bubbles(root, include_own=True)
        return [it["text"] for it in reversed(items) if it["is_own"]][:max(1, limit)]

    def get_chat_history(self, count: int = 20,
                         skip_own_prefixes: tuple = ()) -> list[dict]:
        """聊天窗口内向上滚动，读取最近 count 条对话消息（不含系统/送达确认等）。

        返回 list[dict]，按时间从旧到新：
          {text, is_own, contact, time_label}
        - contact: 对方（手表侧）发送方名字；无标注为 ""
        - is_own:   是否家长侧（自己）发的消息（含从 QQ 转发进来的消息）
        - time_label: 气泡上方的 App 日期标签（如 "06:56" / "昨天 23:42"）
        skip_own_prefixes: 自己发的、以这些前缀开头的消息（如 xtc 命令）不计入历史。
        滚动到顶部或连续两屏无新消息即停止。必须在聊天页调用（调用方保证），
        键盘需已收起（否则滚动区域被遮挡）。失败返回 []。
        """
        try:
            count = max(1, min(int(count), 100))
        except (TypeError, ValueError):
            count = 20
        prefixes = tuple(p for p in (skip_own_prefixes or ()) if p)

        def skipped(it: dict) -> bool:
            return bool(it["is_own"] and prefixes
                        and it["text"].startswith(prefixes))
        try:
            screen_h = self.adb.get_screen_size()[1]
            x = self.adb.get_screen_size()[0] // 2
        except AdbError:
            return []
        # 去重只按 (文本, 发送方) —— 时间标签会随滚动移出屏幕而消失，
        # 若把标签并入 key，同一条消息跨屏会被重复统计。
        seen_prev: set[tuple[str, bool]] = set()
        oldest_first: list[dict] = []  # 时间从旧到新（更旧的新内容整体排在前面）
        empty_streak = 0
        max_screens = min(40, 2 + count)
        try:
            for _ in range(max_screens):
                root = self.adb.dump_ui()
                items = self._chat_bubbles(root, include_own=True)
                new_items: list[dict] = []
                for it in items:
                    if skipped(it):
                        continue
                    key = (it["text"], it["is_own"])
                    if key in seen_prev:
                        continue
                    new_items.append(it)
                for it in items:
                    if skipped(it):
                        continue
                    seen_prev.add((it["text"], it["is_own"]))
                if new_items:
                    # 向下翻页 = 露出更早内容（从屏幕上方进入）→ 整体比已收集内容更旧
                    oldest_first[0:0] = new_items
                    empty_streak = 0
                    if len(oldest_first) >= count:
                        break
                else:
                    empty_streak += 1
                    if empty_streak >= 2:
                        break  # 已到顶部 / 没有更多历史
                # 真机实测：手指自屏幕中上滑到下方 = 看更早消息
                self.adb.swipe(x, int(screen_h * 0.25), x, int(screen_h * 0.85), 350)
                time.sleep(1.0)
            return oldest_first[-count:]
        except Exception as e:  # noqa: BLE001 读取历史失败不致命
            self.log("warning", f"读取历史消息异常: {e}")
            return oldest_first[-count:]

    def _latest_from_list(self, root: ET.Element):
        """聊天列表（主页微聊列表）：取最顶部（最新）聊天行的消息预览，返回 (contact, text, time_label)。

        实测列表行结构：tv_chat_dialog_name（联系人名）+ tv_chat_dialog_last_msg_content（预览）。
        watch_contact 配置后只取该联系人的行。时间取行内时间标签（如 "06:56" / "昨天 23:42"）。
        """
        preview_tail = "tv_chat_dialog_last_msg_content"
        watch_contact = self.ui.get("watch_contact", "") or ""
        rows = []
        for n in root.iter("node"):
            if self._id_tail(n) == preview_tail:
                rows.append(n)
        if not rows:
            return (None, None, "")
        if watch_contact:
            target = None
            for row in rows:
                contact = self._row_contact(row, root)
                if contact == watch_contact:
                    target = row
                    break
            if target is None:
                return (None, None, "")
        else:
            target = min(rows, key=lambda n: (self._bounds(n) or (0, 0, 0, 0))[1])
        text = target.get("text", "").strip()
        if self._is_system_msg(text):
            return (None, None, "")  # 列表预览是桥接系统提示（送达确认等），不转发
        contact = self._row_contact(target, root)
        time_label = self._row_time(target, root)
        return (contact or None, text or None, time_label)

    def _row_time(self, preview_node, root: ET.Element) -> str:
        """取预览节点所在行的日期/时间标签（如 "06:56"、"昨天 23:42"、"8月30日"）。"""
        parent = self._parent(preview_node, root)
        if parent is None:
            return ""
        for n in parent.iter("node"):
            t = n.get("text", "").strip()
            if not t or t == preview_node.get("text", ""):
                continue
            if ":" in t or "昨天" in t or "前天" in t or "日" in t or "月" in t:
                return t
        return ""

    def _is_system_msg(self, text: str) -> bool:
        """桥接系统提示消息（送达确认等）按前缀识别，防止被当成接收消息转发。"""
        prefixes = self.ui.get("system_msg_prefixes", ["发送成功", "发送失败", "✅", "❌"])
        return any(str(text).startswith(p) for p in prefixes)

    def _row_contact(self, preview_node, root: ET.Element) -> str:
        """取预览节点同行的联系人名（同父节点的 tv_chat_dialog_name）。"""
        parent = self._parent(preview_node, root)
        if parent is None:
            return ""
        for n in parent.iter("node"):
            if self._id_tail(n) == "tv_chat_dialog_name":
                return n.get("text", "").strip()
        return ""

    def _row_ancestor(self, node, root: ET.Element):
        """向上找“一行”祖先：宽度接近屏宽、高度小于 300px。"""
        width = self.adb.get_screen_size()[0]
        cur = node
        for _ in range(6):
            parent = self._parent(cur, root)
            if parent is None:
                break
            b = self._bounds(parent)
            if b and b[2] - b[0] > width * 0.6 and 0 < b[3] - b[1] < 300:
                return parent
            cur = parent
        return None

    @staticmethod
    def _parent(node, root: ET.Element):
        for p in root.iter("node"):
            if node in list(p):
                return p
        return None

    # ------------------------------------------------------------------ 未读数
    def get_unread_count(self):
        """尽力而为：统计小数字角标数量，返回 int 或 None。"""
        try:
            root = self.adb.dump_ui()
        except AdbError:
            return None
        badge_ids = set(self.ui.get("badge_resource_ids", []))
        count = 0
        for n in root.iter("node"):
            if badge_ids and n.get("resource-id", "") in badge_ids:
                count += 1
                continue
            t = n.get("text", "").strip()
            if re.fullmatch(r"\d{1,3}", t):
                b = self._bounds(n)
                if b and 0 < b[2] - b[0] <= 80:  # 角标通常是小尺寸文本
                    count += 1
        return count or None

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _bounds(node) -> tuple[int, int, int, int] | None:
        return ADBController.node_bounds(node) if node is not None else None
