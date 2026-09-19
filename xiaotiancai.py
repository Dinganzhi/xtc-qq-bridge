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
# 语音模式 -> 文字模式的切换按钮
_SWITCH_TO_TEXT_IDS = ("iv_left_img_view", "chat_record_button")
# 系统录音权限弹窗按钮（不同 Android 版本 id 不同，全部尝试）
_PERMISSION_ALLOW_IDS = (
    "com.android.permissioncontroller:id/permission_allow_foreground_only_button",
    "com.android.permissioncontroller:id/permission_allow_button",
    "com.android.permissioncontroller:id/permission_allow_one_time_button",
    "com.android.permissioncontroller:id/permission_allow_always_button",
    "com.android.packageinstaller:id/permission_allow_button",
)
# 小天才内部警告弹窗"取消"
_CANCEL_DIALOG_ID = "com.xtc.watch:id/btn_cancel"
# 网络提示条（会混进聊天文本，读取时排除）
_TIP_IDS = ("tv_weichat_uninstall_hint", "iv_tips_content")
# 发送失败弹窗标题
_SEND_FAIL_TITLE = "消息发送"

# ---- 弹窗/遮挡处理用的短语表（均可被 config -> xiaotiancai.ui 覆盖） ----
# 关闭类按钮：resource-id 末段命中即点（这些 id 只出现在弹窗/浮层上）
_CLOSE_ID_TAILS = ("btn_close", "iv_close", "img_close", "tv_close", "close_btn",
                   "dialog_close", "btn_cancel", "iv_cancel", "btn_no", "tv_cancel")
# content-desc 命中且当前是弹窗窗口时才点
_CLOSE_DESCS = ("关闭", "取消", "close", "Cancel", "×")
# 更新/评价/公告类弹窗：优先点"稍后/以后再说"，避免误触下载
_UPDATE_WORDS = ("发现新版本", "版本更新", "立即更新", "马上更新", "升级", "去评分",
                 "给个好评", "评价一下", "公告", "活动", "福利", "签到有礼")
_SKIP_TEXTS = ("以后再说", "稍后再说", "暂不更新", "稍后更新", "下次再说", "暂不升级",
               "忽略此版本", "先逛逛", "放弃", "不同意", "取消", "关闭")
# 系统无响应/崩溃弹窗
_ANR_WORDS = ("无响应", "没有响应", "已停止运行", "屡次停止运行", "反复停止")
_ANR_WAIT = ("等待", "等待响应", "继续等待")
# 网络类临时弹窗：点"重试/知道了"（点重试有冷却，避免死循环）
_NET_WORDS = ("网络异常", "网络连接失败", "连接失败", "网络不可用", "请检查网络",
              "服务器繁忙", "加载失败")
_NET_RETRY = ("重试", "再试一次", "重新加载", "刷新")
_NET_DISMISS = ("知道了", "确定", "好的", "取消", "关闭")
# 权限弹窗的文本按钮兜底
_PERMISSION_TEXTS = ("允许", "始终允许", "仅在使用中允许", "使用应用时允许", "同意", "确定")

# 登录页"正在进行"的文案：出现这些一律**不判失败**，继续等待
_DEFAULT_LOGIN_PROGRESS = ("登录中", "正在登录", "正在验证", "验证中", "正在提交", "提交中",
                           "请稍候", "请稍后", "正在加载", "加载中", "处理中", "请等待",
                           "登录中...", "正在登录...")
# 明确的账号/密码类错误（命中即判定登录失败）
_STRONG_LOGIN_ERRORS = ("密码错误", "密码不正确", "账号或密码错误", "账号不存在", "用户不存在",
                        "手机号未注册", "手机号不存在", "验证码错误", "验证码已失效",
                        "验证码不正确", "次数过多", "账号异常", "账号已被", "已被封禁",
                        "密码格式", "手机号格式")

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
        # 登录态缓存：is_logged_in() 需要 dump 界面（1~2s），轮询每 2s 调一次会明显变慢；
        # 这里按 login_state_ttl 秒缓存，登录动作/启动动作后主动失效。
        self._login_state: tuple[float, bool] | None = None
        self._login_state_ttl = float(cfg.get("login_state_ttl", 5.0) or 0.0)
        # 交互路径的等待时间（秒）：默认偏短，可在 ui.interaction_delay 调整
        try:
            self._delay = max(0.1, float(self.ui.get("interaction_delay", 0.6)))
        except (TypeError, ValueError):
            self._delay = 0.6
        # 发送重试轮数（每次仅在"输入框仍留有内容"等可安全重试的情况下重发）
        try:
            self._send_retries = max(1, int(self.ui.get("send_retries", 2)))
        except (TypeError, ValueError):
            self._send_retries = 2
        self._net_retry_ts = 0.0     # 网络弹窗"重试"按钮的点击冷却

    def log(self, level: str, msg: str):
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(msg)

    def _dump_fast(self) -> ET.Element:
        """交互路径的快速 UI dump（2 次尝试、很短的等待）。

        比默认 dump（2 次 / 0.8s）快，又比"只试 1 次"稳——单次 uiautomator 偶发失败
        时不至于把整轮发送/登录直接判成失败。
        """
        return self.adb.dump_ui(retries=2, delay=0.3)

    # ------------------------------------------------------------------ 生命周期
    def launch(self) -> bool:
        """确保小天才 App 在前台；**已在前台时不做任何启动动作**（避免"定死操作"）。

        WSA 上 `am start -n` 偶发失败，adb_controller.launch_app() 内部已做多策略
        启动 + 前台轮询；这里只负责前置判断、重试与日志。
        """
        if self.adb.is_in_foreground(self.package):
            self.log("debug", "小天才 App 已在前台，跳过启动")
            return True
        self.log("info", f"小天才 App 不在前台，启动: {self.package}")
        if not self.adb.package_installed(self.package):
            self.log("error", f"设备上没有安装 {self.package}（请先在模拟器/WSA 里安装并登录小天才 App）")
            return False
        last = ""
        for attempt in range(1, 4):
            try:
                used = self.adb.launch_app(self.package, self.main_activity)
                if used and self.adb.is_in_foreground(self.package):
                    self._login_state = None
                    return True
                last = f"activity={used or '(未解析)'} 前台={self.current_activity() or '(未知)'}"
            except AdbError as e:
                last = str(e)
            self.log("warning", f"启动小天才未确认（第 {attempt}/3 次）：{last}")
            time.sleep(1.5 * attempt)
        self.log("error", f"启动失败: {last}")
        return False

    def current_activity(self) -> str:
        return self.adb.get_current_focus() or ""

    def is_logged_in(self, force: bool = False, root: ET.Element | None = None) -> bool:
        """通过 Activity 名 + 登录页专属控件判断是否已登录家长账号。

        判定逻辑（避免误报"未登录"）：
        - App 不在前台 -> False（无法判断）；
        - Activity 名含 welcome/login/register/signin -> False；
        - **出现密码输入框**（账号密码登录页才有的 inputType=password / 密码提示文案）-> False；
        - 出现"获取验证码/短信验证码登录"等短信登录页专属元素 -> False；
        - 出现配置的 login_markers（默认「注册/登录」「立即登录」，**不含泛化的"登录"**）
          -> False；
        - 其余情况（例如聊天列表/微聊主页里恰好有"登录"字样）-> True。

        force=False 时结果按 login_state_ttl 秒缓存（轮询每 2s 调用，缓存可省掉大量 dump）；
        root 传入时复用调用方已经 dump 好的界面，不再重复 dump。
        """
        if not force and root is None and self._login_state is not None:
            ts, val = self._login_state
            if self._login_state_ttl > 0 and (time.monotonic() - ts) < self._login_state_ttl:
                return val
        act = self.current_activity()
        if not act.startswith(self.package):
            return self._remember_login(False)
        low = act.lower()
        if any(marker in low for marker in ("welcome", "login", "register", "signin")):
            return self._remember_login(False)
        if root is None:
            try:
                # 需要较可靠的结果：2 次尝试；读不到界面时不缓存（避免一次偶发失败
                # 被当成"未登录"而触发自动登录流程）。
                root = self.adb.dump_ui(retries=2, delay=0.4)
            except AdbError:
                return False
        if self._looks_like_login_page(root):
            return self._remember_login(False)
        # 首启隐私协议弹窗 = 尚未进入 App，视为未登录
        joined = "".join(n.get("text", "") or "" for n in root.iter("node"))
        if "温馨提示" in joined and ("同意" in joined or "不同意" in joined):
            return self._remember_login(False)
        return self._remember_login(True)

    def _remember_login(self, ok: bool) -> bool:
        self._login_state = (time.monotonic(), bool(ok))
        return bool(ok)

    def invalidate_login_state(self) -> None:
        self._login_state = None

    def _looks_like_login_page(self, root: ET.Element) -> bool:
        """登录页判定：只认登录页**专属**特征，避免把已登录界面里的"登录"字样误判。

        专属特征：
        1) 密码输入框（EditText 的 password="true"，或提示文案含"密码"）；
        2) 短信登录页元素（获取验证码 / 短信验证码登录）；
        3) 配置的 login_markers（默认只含「注册/登录」「立即登录」这类强标记）。
        """
        texts = [(n.get("text", "") or "") for n in root.iter("node")]
        hint = "".join(texts) + "".join(
            (n.get("content-desc", "") or "") for n in root.iter("node"))
        for marker in ("获取验证码", "短信验证码登录", "验证码登录"):
            if marker in hint:
                return True
        for n in root.iter("node"):
            if not (n.get("class", "") or "").endswith("EditText"):
                continue
            if (n.get("password", "") or "").lower() == "true":
                return True
            if "密码" in (n.get("text", "") or "") or "密码" in (n.get("content-desc", "") or ""):
                return True
        for marker in self.ui.get("login_markers", ["注册/登录", "立即登录"]):
            if not marker:
                continue
            if any(marker in (t or "") for t in texts):
                return True
        return False

    def require_login(self) -> bool:
        ok = self.is_logged_in()
        if not ok and not self._warned_not_login:
            self.log("warning", "小天才 App 未登录家长账号：请在目标 Android 环境（模拟器/WSA/Waydroid/真机）里完成登录（登录前无法读取/发送消息）")
            self._warned_not_login = True
        return ok

    # ------------------------------------------------------------------ 表单输入（登录页用）
    def _field_verifier(self, field_node=None):
        """生成 input_text 的校验回调工厂：必须**按行精确匹配**目标文本。

        为什么不用"输入框非空即成功"：登录页有多个 EditText，把手机号写进密码框、
        或残留上一次的内容时，非空判定会误报成功，导致后续逻辑"找不到控件"反复重输
        （用户遇到的密码被输好几遍）。这里要求某一行的文本恰好等于目标值；
        **密码框显示为掩码**（••••/圆点）时按"已清空且长度一致"判断，
        否则掩码文本永远不等于明文密码 -> 会被误判为没输进去而重复输入。
        """
        masked = self._is_masked_field(field_node)

        def _verify_text(expected: str):
            want = (expected or "").strip()

            def _v() -> bool:
                try:
                    root = self.adb.dump_ui(retries=1, delay=0.0)
                except AdbError:
                    return True  # 读不到界面时不阻塞流程
                rows = self._input_row_texts(root)
                if not rows:
                    return True
                for r in rows:
                    cur = (r or "").strip()
                    if cur == want:
                        return True
                    if masked and cur and self._is_mask_text(cur) and len(cur) == len(want):
                        return True
                return False

            return _v

        return _verify_text

    @staticmethod
    def _is_masked_field(node) -> bool:
        if node is None:
            return False
        try:
            if (node.get("password", "") or "").lower() == "true":
                return True
            hint = (node.get("text", "") or "") + (node.get("content-desc", "") or "")
            return "密码" in hint
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _is_mask_text(text: str) -> bool:
        """掩码文本判定：全部由圆点/星号/实心点构成（允许尾部空格）。

        注意：下面这串字符是**功能字符**（App 可能用其中任意一种渲染密码掩码），
        不是装饰符号，清理"特殊符号"时不要删；它们都是 GBK 可编码的。
        """
        s = (text or "").strip()
        if not s:
            return False
        return all(ch in "•·*＊●○□■" for ch in s)

    def _input_row_texts(self, root: ET.Element) -> list[str]:
        """当前界面上所有输入类控件的文本（EditText 优先，其次聚焦节点）。"""
        rows: list[str] = []
        for n in root.iter("node"):
            cls = n.get("class", "") or ""
            if cls.endswith("EditText"):
                rows.append(n.get("text", "") or "")
        if rows:
            return rows
        for n in root.iter("node"):
            if (n.get("focusable", "") or "") == "true" and (n.get("text", "") or ""):
                rows.append(n.get("text", "") or "")
        return rows

    def fill_login_form(self, edits: list, values: list) -> tuple[bool, list[str]]:
        """按行填入登录表单，返回 (是否全部成功, 最终各行文本)。

        每个字段：点击聚焦 -> 清空（必要时删两行，避免残留）-> 注入 -> 按行校验。
        校验失败只"清空重填一次"，绝不重复追加，因此不会出现"密码被输好几遍"。
        密码框显示为掩码时按长度校验（见 _field_verifier）。
        """
        final: list[str] = []
        for edit, value in zip(edits, values):
            if not value:
                continue
            ok_field = False
            for attempt in (1, 2):
                self.adb.tap_element(edit)
                time.sleep(1.2)
                self.adb.clear_text_field()   # 尽量清空前后文本（含掩码字段的残留）
                time.sleep(0.4)
                ok_field = self.adb.input_text(
                    value, verify=self._field_verifier(edit)(value), retries=2)
                time.sleep(0.4)
                final = self._input_row_texts(self._current_root())
                if self._field_looks_ok(edit, value, final):
                    ok_field = True
                    break
                self.log("warning",
                         f"第 {attempt} 次输入未确认（当前输入框内容={final!r}），清空后重试")
            if not ok_field:
                return False, final
        return True, final

    def _field_looks_ok(self, edit, value: str, rows: list[str]) -> bool:
        """最终判定：明文逐行精确匹配；掩码字段按"非空且长度等于目标长度"。"""
        want = (value or "").strip()
        if any((r or "").strip() == want for r in rows):
            return True
        if self._is_masked_field(edit):
            for r in rows:
                cur = (r or "").strip()
                if cur and self._is_mask_text(cur) and len(cur) == len(want):
                    return True
                if cur == want:
                    return True
        return False

    def _current_root(self) -> ET.Element:
        try:
            return self.adb.dump_ui(retries=1, delay=0.0)
        except AdbError:
            return ET.Element("hierarchy")

    # ------------------------------------------------------------------ 账密登录
    def login(self, phone: str, password: str) -> str:
        """账密登录小天才（手机号 + 密码，非验证码）。

        返回状态：
        - 'already' 已经登录，无需操作
        - 'ok'      登录成功
        - 'risk'    触发安全验证，需要用户手动在模拟器操作
        - 'fail'    确认的账号/密码错误等（登录失败）
        - 'timeout' 等待超时/网络类临时问题（**不能当成"密码错误"**，稍后应重试）
        - 'error'   流程异常（未找到控件/未配置账密）

        重要：登录中的界面（"登录中/正在验证/请稍候"）**绝不判失败**——历史版本因为把
        泛化的"失败/错误"当成密码错误，导致"明明在登录中却提示登录失败"，随后还被
        当成"已处理"而不再重试。
        """
        if not phone or not password:
            self.log("error", "账密登录需要 config.yaml -> xiaotiancai.login.phone / password")
            return "error"
        # 只有明确重新读界面后仍显示未登录才会走登录流程（避免缓存/偶发 dump 失败误判）
        if self.is_logged_in(force=True):
            return "already"
        try:
            # 0) 确保 App 在前台 + 处理首启隐私弹窗/权限等（已在前台则完全不重启 App）
            self.launch()
            self._dismiss_blockers()
            # 1) 进入登录页（欢迎页 -> 点"注册/登录"）
            root = self._dump_fast()
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
                    time.sleep(self._delay)
                    root = self._dump_fast()
            # 2) 切到"账号密码登录"（短信登录页底部多段链接的最左段）
            if not self._is_account_login_page(root):
                switch = self._find_account_login_entry(root)
                if switch is not None:
                    self.log("debug", f"点账密入口 {switch.get('bounds')}")
                    self._tap_entry_left(switch)
                    time.sleep(self._delay)
                    root = self._dump_fast()
                else:
                    self.log("debug", "未找到账密入口（可能已在账密页）")
            # 3) 找手机号 + 密码两个输入框（确认确实在账密页，避免把密码写进短信页）
            edits = self.adb.find_elements(root, class_name="EditText")
            edits.sort(key=lambda n: (self._bounds(n) or (0, 0, 0, 0))[1])
            if len(edits) < 2:
                self.log("error", "未找到账密输入框（页面结构变化？运行 python tools/dump_ui.py 查看登录页）")
                return "error"
            self.log("debug",
                     f"账密页就绪：前台={self.current_activity()} 输入框={len(edits)}")
            # 4) 输入手机号、密码（点击聚焦 -> 清空 -> 注入 -> **按行精确校验**）
            ok_fill, final_rows = self.fill_login_form(
                [edits[0], edits[1]], [phone, password])
            if not ok_fill:
                self.log("error",
                         f"登录表单输入未确认：输入框内容={final_rows!r}"
                         "（运行 python tools/dump_ui.py 查看登录页控件）")
                return "error"
            # 5) 勾选协议（若存在且未勾选）
            root = self._dump_fast()
            cb = self._first(
                self.adb.find_element(root, class_name="CheckBox"),
                self.adb.find_element(root, resource_id="com.xtc.watch:id/cb_protocol"))
            if cb is not None and cb.get("checked") != "true":
                self.adb.tap_element(cb)
                time.sleep(0.4)
            # 6) 点登录（先收起键盘——输入密码后键盘仍打开，会挡住/截获点击）
            #    未离开账密登录页说明点击被吞，重试最多 3 次
            self.adb.keyevent(4)
            time.sleep(0.5)
            tapped = False
            for attempt in range(1, 4):
                root = self._dump_fast()
                btn = self._find_login_button(root)
                if btn is None:
                    break  # 页面已跳转（登录中/验证页/成功）
                self.log("debug", f"点登录按钮（第 {attempt} 次）{btn.get('bounds')}")
                self.adb.tap_element(btn)
                tapped = True
                time.sleep(1.5)
                act = self.current_activity()
                if "loginactivity" not in act.lower():
                    break  # 已离开账密登录页，请求已发出
                self.log("debug", "点击后仍在登录页，重试")
            if not tapped:
                self.log("error", "未找到登录按钮（页面结构变化？运行 dump_ui 查看）")
                return "error"
            return self._await_login_result()
        except AdbError as e:
            self.log("error", f"登录流程异常: {e}")
            return "error"

    def _await_login_result(self) -> str:
        """等待登录结果。**区分**"登录中"、"确证的失败"、"网络类临时问题"与"超时"。

        - 出现"登录中/正在验证/请稍候"等进度文案 -> 不判失败，继续等；
        - 明确账号/密码错误 -> 'fail'；
        - 网络类提示 -> 继续等到超时，返回 'timeout'（稍后重试，而不是当成密码错）；
        - 一直无法确证 -> 'timeout'。
        """
        try:
            wait = max(15.0, float(self.ui.get("login_timeout", 45) or 45))
        except (TypeError, ValueError):
            wait = 45.0
        hard_deadline = time.time() + wait + 30
        deadline = time.time() + wait
        progress_logged = False
        soft = ""
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                if self.is_logged_in(force=True):
                    self.log("info", "小天才账密登录成功")
                    return "ok"
                root = self._dump_fast()
                risk = self._detect_risk(root)
                if risk:
                    self.log("warning", f"登录触发安全验证（{risk}），需要用户手动操作")
                    return "risk"
                if self._detect_login_progress(root):
                    if not progress_logged:
                        self.log("info", "登录进行中（界面提示登录中/验证中），继续等待结果")
                        progress_logged = True
                    deadline = min(hard_deadline, max(deadline, time.time() + 15))
                    continue
                soft = self._detect_soft_error(root) or soft
                err = self._detect_login_error(root)
                if err:
                    self.log("error", f"登录失败（明确错误）: {err}")
                    return "fail"
            except AdbError:
                continue
        if soft:
            self.log("warning", f"登录未在时限内完成（疑似临时问题：{soft}）——稍后会自动重试")
        else:
            self.log("warning", "登录结果超时未确定（界面既没有成功也没有明确错误）——稍后会自动重试")
        return "timeout"

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

    def _detect_login_progress(self, root: ET.Element) -> str:
        """检测"登录正在进行"的界面文案；命中返回文案，否则 ''。

        这类文案存在时**绝不能**判失败（用户报告的"登录中却提示登录失败"）。
        """
        markers = self.ui.get("login_progress_markers", list(_DEFAULT_LOGIN_PROGRESS))
        for n in root.iter("node"):
            blob = (n.get("text", "") or "") + (n.get("content-desc", "") or "")
            if not blob:
                continue
            for m in markers:
                if m and m in blob:
                    return m
        return ""

    def _detect_login_error(self, root: ET.Element) -> str:
        """检测**确证的**登录失败提示（账号/密码错误等）；不确定时返回 ''。

        规则（修复"登录中误报失败"）：
        1. 出现"登录中/正在验证/请稍候"等进度文案 -> 一律返回 ''（交给调用方继续等待）；
        2. 强标记（密码错误 / 账号不存在 / 次数过多 …）命中即返回；
        3. 配置里的弱标记（"失败"/"错误"/"不存在"这类泛化词）只有出现在**短文本节点**
           （toast/对话框文案，<=30 字）且**不含网络类词**时才采纳——
           这样"网络连接失败""加载失败，请重试"之类的临时问题不会被当成密码错误。
        """
        if self._detect_login_progress(root):
            return ""
        joined = "".join((n.get("text", "") or "") for n in root.iter("node"))
        joined += "".join((n.get("content-desc", "") or "") for n in root.iter("node"))
        for m in _STRONG_LOGIN_ERRORS:
            if m in joined:
                return m
        markers = self.ui.get("login_error_markers",
                              ["密码错误", "账号不存在", "手机号不存在", "错误", "失败", "次数过多"])
        for m in markers:
            if not m or m not in joined:
                continue
            if m in _STRONG_LOGIN_ERRORS:
                return m
            if self._weak_error_context(root, m):
                return m
        return ""

    def _weak_error_context(self, root: ET.Element, marker: str) -> bool:
        """泛化错误词（失败/错误/不存在）是否出现在可信的"短提示"节点里。"""
        for n in root.iter("node"):
            blob = ((n.get("text", "") or "") + (n.get("content-desc", "") or "")).strip()
            if not blob or marker not in blob or len(blob) > 30:
                continue
            if any(w in blob for w in _NET_WORDS) or any(w in blob for w in ("网络", "超时", "连接", "服务器")):
                continue  # 网络类 = 临时问题，不是密码错误
            cls = n.get("class", "") or ""
            if "Button" in cls or "EditText" in cls:
                continue  # 按钮/输入框上的文字不算错误提示
            return True
        return False

    def _detect_soft_error(self, root: ET.Element) -> str:
        """检测网络类/临时性提示（登录超时后用来说明原因，不作为失败依据）。"""
        for n in root.iter("node"):
            blob = ((n.get("text", "") or "") + (n.get("content-desc", "") or "")).strip()
            if not blob or len(blob) > 40:
                continue
            if any(w in blob for w in _NET_WORDS) or any(w in blob for w in ("网络", "超时", "服务器繁忙")):
                return blob
        return ""

    # ------------------------------------------------------------------ 弹窗处理
    def dismiss_blockers(self) -> bool:
        """公开别名：多轮清理所有可识别的弹窗/遮挡，直到界面干净。"""
        return self.settle()

    def settle(self, max_passes: int = 6) -> bool:
        """多轮弹窗清理：权限/无响应/更新/网络/警告/通用对话框/BACK 兜底。
        任何一轮处理了内容就继续下一轮，直到界面干净或达到轮数上限。"""
        handled_any = False
        for i in range(max_passes):
            if self._dismiss_blockers():
                handled_any = True
                time.sleep(0.5)
                continue
            break
        if handled_any:
            self.log("debug", f"弹窗清理完成（共处理 {i + 1} 轮内的可识别遮挡）")
        return handled_any

    def _dismiss_blockers(self) -> bool:
        """处理单轮可识别的弹窗/遮挡，返回是否处理过。

        覆盖（按优先级）：系统权限 -> 应用无响应/崩溃 -> 通话面板 -> 隐私协议 ->
        更新/评价/公告类（点"以后再说"）-> 网络类（点"重试"，带冷却）->
        小天才警告弹窗 -> 关闭类按钮（id/desc）-> 通用对话框文本按钮 -> 弹窗窗口 BACK 兜底。

        注意：普通页面的 NAF 节点（图片等）不算遮挡，绝不能按 BACK（会把 App 退到桌面）；
        关闭类按钮只在"弹窗特征"成立时才点，避免误关正常页面。
        """
        focus = self.current_activity()
        low_focus = focus.lower()
        try:
            # 1) 系统权限弹窗（前台/一次性/始终允许等，不同镜像 id 与文案都试）
            if "permissioncontroller" in low_focus or "packageinstaller" in low_focus:
                root = self._dump_fast()
                for rid in _PERMISSION_ALLOW_IDS:
                    btn = self.adb.find_element(root, resource_id=rid)
                    if btn is not None:
                        self.adb.tap_element(btn)
                        self.log("info", "已允许系统权限弹窗请求")
                        return True
                for t in _PERMISSION_TEXTS:
                    btn = self.adb.find_element(root, text=t)
                    if btn is not None:
                        self.adb.tap_element(btn)
                        self.log("info", f"已点击权限弹窗按钮: {t}")
                        return True
                return False
            root = self._dump_fast()
            texts_all = "".join((n.get("text") or "") for n in root.iter("node"))
            descs_all = "".join((n.get("content-desc") or "") for n in root.iter("node"))
            # 2) 应用无响应/崩溃弹窗：优先"等待"（不杀进程），否则关闭
            if any(w in texts_all for w in _ANR_WORDS):
                for t in self.ui.get("anr_wait_texts", list(_ANR_WAIT)):
                    btn = self.adb.find_element(root, text=t)
                    if btn is not None:
                        self.adb.tap_element(btn)
                        self.log("warning", "检测到应用无响应弹窗，已点「等待」继续")
                        return True
                for t in ("确定", "关闭应用", "知道了"):
                    btn = self.adb.find_element(root, text=t)
                    if btn is not None:
                        self.adb.tap_element(btn)
                        self.log("warning", "检测到应用无响应弹窗，已关闭提示")
                        return True
            # 3) 通话面板弹层（视频通话/拨打电话 + 取消；误触 "+" 菜单时出现）
            if "视频通话" in texts_all and "拨打电话" in texts_all:
                cancel = self._first(
                    self.adb.find_element(root, resource_id="com.xtc.watch:id/tv_cancel"),
                    self.adb.find_element(root, text="取消"))
                if cancel is not None:
                    self.adb.tap_element(cancel)
                    self.log("info", "已关闭通话面板弹层")
                    return True
            # 4) 隐私协议/首启温馨提示（点"同意"；必须先于更新/关闭类，否则会点成"不同意"）
            title = self.adb.find_element(root, resource_id="com.xtc.watch:id/tv_title")
            if title is not None and "温馨提示" in title.get("text", ""):
                sure = self.adb.find_element(root, resource_id="com.xtc.watch:id/btn_sure")
                if sure is not None:
                    self.adb.tap_element(sure)
                    self.log("info", "已同意隐私协议（首启弹窗）")
                    return True
            # 5) 更新/评价/公告/活动类弹窗：只点"稍后/以后再说"这类跳过按钮
            if any(w in texts_all for w in _UPDATE_WORDS) or any(w in descs_all for w in _UPDATE_WORDS):
                for t in self.ui.get("popup_skip_texts", list(_SKIP_TEXTS)):
                    btn = self.adb.find_element(root, text=t)
                    if btn is not None and self._looks_like_dialog(root, focus):
                        self.adb.tap_element(btn)
                        self.log("info", f"已跳过更新/活动类弹窗（点「{t}」）")
                        return True
            # 6) 网络类临时弹窗：带冷却地点"重试"，否则关掉提示
            if any(w in texts_all for w in _NET_WORDS):
                if self._looks_like_dialog(root, focus) and \
                        time.monotonic() - self._net_retry_ts >= 30:
                    for t in _NET_RETRY:
                        btn = self.adb.find_element(root, text=t)
                        if btn is not None:
                            self._net_retry_ts = time.monotonic()
                            self.adb.tap_element(btn)
                            self.log("info", f"网络异常弹窗：已点「{t}」重新尝试")
                            return True
                for t in _NET_DISMISS:
                    btn = self.adb.find_element(root, text=t)
                    if btn is not None and self._looks_like_dialog(root, focus):
                        self.adb.tap_element(btn)
                        self.log("info", f"已关闭网络提示弹窗（点「{t}」）")
                        return True
            # 7) 小天才警告弹窗（取消 / 关闭）
            btn = self.adb.find_element(root, resource_id=_CANCEL_DIALOG_ID)
            if btn is not None:
                self.adb.tap_element(btn)
                self.log("info", "已关闭小天才弹窗")
                return True
            # 8) 关闭类按钮：resource-id 末段命中即点（这些 id 只出现在弹窗/浮层上）
            for n in root.iter("node"):
                tail = self._id_tail(n).lower()
                if tail and tail in _CLOSE_ID_TAILS:
                    self.adb.tap_element(n)
                    self.log("info", f"已点击关闭按钮（id={self._id_tail(n)}）")
                    return True
            # 9) content-desc 关闭类：仅当当前确实像弹窗窗口时
            if self._looks_like_dialog(root, focus):
                for n in root.iter("node"):
                    blob = ((n.get("content-desc") or "") + (n.get("text") or "")).strip()
                    if blob and any(d in blob for d in _CLOSE_DESCS) and len(blob) <= 8:
                        self.adb.tap_element(n)
                        self.log("info", f"已点击弹窗关闭控件（{blob}）")
                        return True
            # 10) 通用对话框文本按钮（按需取确认类或取消类，避免误关）
            clicked = self._tap_any_dialog_button(root, focus)
            if clicked:
                return True
            # 11) BACK 兜底：仅当前台是独立弹窗/对话框窗口时（如 PopupWindow），
            #     普通 Activity 页面即使有 NAF 节点也不按返回，防止 App 退到桌面。
            if "popupwindow" in low_focus or "dialog" in low_focus:
                self.adb.keyevent(4)
                self.log("info", "检测到弹窗窗口，按返回键关闭")
                return True
        except AdbError:
            pass
        return False

    def _looks_like_dialog(self, root: ET.Element, focus: str = "") -> bool:
        """是否像"弹窗/对话框"场景：独立弹窗窗口、对话框标题控件、
        或界面上同时出现多个对话框按钮文本。用于给"点关闭/跳过"类动作兜底证据，
        避免在正常聊天/列表页误点。"""
        low = (focus or "").lower()
        if "popupwindow" in low or "dialog" in low:
            return True
        for n in root.iter("node"):
            if self._id_tail(n).lower() in ("tv_title", "dialog_title", "btn_sure",
                                            "btn_cancel", "tv_cancel", "alert_title"):
                return True
        keys = ("同意", "确定", "知道了", "好的", "确认", "允许", "取消", "关闭", "不同意",
                "以后再说", "稍后再说", "重试")
        hit = 0
        for n in root.iter("node"):
            t = (n.get("text") or "").strip()
            if t in keys:
                hit += 1
                if hit >= 2:
                    return True
        return False

    def _tap_any_dialog_button(self, root: ET.Element, focus: str = "") -> bool:
        """点通用对话框按钮。优先"跳过/取消"类（避免误触下载、更新、支付），
        其次确认类。仅当界面存在"对话框特征"时才动作，避免误点正常界面按钮。"""
        if not self._looks_like_dialog(root, focus):
            return False
        skip_keys = ("以后再说", "稍后再说", "暂不更新", "稍后更新", "下次再说", "暂不升级",
                     "取消", "关闭", "不同意", "暂不", "忽略")
        confirm_keys = ("同意", "确定", "知道了", "好的", "确认", "允许")
        for key in skip_keys:
            n = self.adb.find_element(root, text=key)
            if n is not None:
                self.adb.tap_element(n)
                self.log("info", f"已点击对话框按钮: {key}")
                return True
        for key in confirm_keys:
            n = self.adb.find_element(root, text=key)
            if n is not None:
                self.adb.tap_element(n)
                self.log("info", f"已点击对话框按钮: {key}")
                return True
        return False

    # ------------------------------------------------------------------ 界面判定
    def is_in_chat(self, root: ET.Element | None = None) -> bool:
        """聊天窗口判定。
        快路径：前台 Activity 以 ChatActivity 结尾（该 App 聊天窗固定类名，快且准）；
        兜底：按输入栏特征 id 确认（防 Activity 名误判弹窗/接收画面）。
        root 传入时复用调用方已经 dump 好的界面，避免重复 dump。"""
        act = self.current_activity().lower()
        if act.endswith("chatactivity"):
            return True
        if root is not None:
            return self._find_chat_bar(root) is not None
        try:
            root = self._dump_fast()
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
            self.log("error", "open_chat 缺少联系人昵称（config.yaml -> target.xtc_contact）")
            return False
        if self.is_in_chat():
            return True
        self._dismiss_blockers()
        try:
            # 主页即"微聊"列表：先直接在当前页找联系人，找不到再尝试切 Tab
            root = self._dump_fast()
            node = self._find_contact_node(root, contact)
            if node is None:
                tab_text = self.ui.get("message_tab_text", "微聊")
                # 精确匹配，避免命中"消息动态"等含"消息"的入口
                tab = self._first(
                    self.adb.find_element(root, text=tab_text),
                    self.adb.find_element(root, content_desc=tab_text))
                if tab is not None:
                    self.adb.tap_element(tab)
                    time.sleep(self._delay)
                    root = self._dump_fast()
                    node = self._find_contact_node(root, contact)
            if node is None:
                self.log("error", f"在消息列表找不到联系人: {contact}")
                return False
            self.adb.tap_element(node)

            # 等待进入聊天；若误入别的页面/弹窗遮挡，先清弹窗再点一次
            deadline = time.time() + 12
            reclick = 0
            while time.time() < deadline:
                root = None
                if self.is_in_chat():
                    return True
                act = self.current_activity()
                if (not act.startswith(self.package)) or "WatchMsg" in act or reclick < 2:
                    if self._dismiss_blockers():
                        time.sleep(0.5)
                    if reclick < 2:
                        root = self._dump_fast()
                        node = self._find_contact_node(root, contact)
                        if node is not None:
                            self.adb.tap_element(node)   # 弹窗关掉后重新点联系人
                        reclick += 1
                time.sleep(0.8)
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
    def chat_input_text(self) -> str:
        """读取聊天输入框当前文本；读不到返回 ''。"""
        try:
            root = self._dump_fast()
        except AdbError:
            return ""
        edit = self._find_input(root)
        if edit is None:
            return ""
        return edit.get("text", "") or ""

    def input_verifier(self, text: str):
        """给 adb.input_text 用的校验回调：输入框里已出现目标文本，或发送按钮已出现
        （小天才 App 只在输入框有内容时才显示发送按钮）即视为注入成功。"""
        needle = (text or "").strip()

        def _verify() -> bool:
            try:
                root = self._dump_fast()
            except AdbError:
                return True  # dump 失败无法判定，不阻塞发送流程
            edit = self._find_input(root)
            if edit is not None:
                cur = edit.get("text", "") or ""
                if needle and needle in cur:
                    return True
                if cur.strip():
                    # 输入框里有内容但和目标不一致（例如剪贴板粘错），
                    # 说明注入通道是通的，由调用方清空重输，不在这里当作失败
                    self.log("warning", f"输入框内容与预期不一致: {cur[:40]!r}")
                    return True
                return False
            # 没有输入框（切走了/语音模式）：退而看发送按钮是否出现
            return self._find_send(root) is not None

        return _verify

    def _clear_chat_input(self, root: ET.Element | None = None) -> None:
        """清空聊天输入框，避免上一次失败残留的内容被拼在新消息前面。
        root 传入时复用调用方的界面快照，少 dump 一次。"""
        if root is None:
            try:
                root = self._dump_fast()
            except AdbError:
                return
        edit = self._find_input(root)
        if edit is None:
            return
        cur = (edit.get("text", "") or "").strip()
        if not cur:
            return
        self.adb.tap_element(edit)
        time.sleep(self._delay)
        self.adb.clear_text_field()
        time.sleep(0.3)
        left = self.chat_input_text().strip()
        if left:
            for _ in range(len(left) + 5):   # 兜底：逐字符删除
                self.adb.keyevent(67)
            time.sleep(0.2)

    # ---- 发送失败/成功判定用的小工具 ----
    def _send_fail_markers(self) -> tuple:
        return tuple(self.ui.get("send_fail_markers",
                                 ["发送失败", "发送不成功", "未发送", "网络异常", "网络不可用",
                                  "发送异常", "重发", "无法发送"]))

    def _fail_signature(self, root: ET.Element) -> list[str]:
        """界面上"发送失败"类提示的文本清单（用于发送前后对比，识别**新出现**的失败提示）。"""
        markers = self._send_fail_markers()
        out: list[str] = []
        for n in root.iter("node"):
            blob = ((n.get("text") or "") + " " + (n.get("content-desc") or "")).strip()
            if not blob:
                continue
            if any(m in blob for m in markers):
                out.append(blob)
            elif _SEND_FAIL_TITLE in blob and ("失败" in blob or "重发" in blob):
                out.append(blob)
        return out

    @staticmethod
    def _counter(items) -> dict:
        out: dict = {}
        for it in items:
            out[it] = out.get(it, 0) + 1
        return out

    def _own_bubble_counter(self, root: ET.Element) -> dict:
        """界面上"自己发的"消息气泡文本计数（用于确认新气泡真的出现了）。"""
        try:
            items = self._chat_bubbles(root, include_own=True)
        except Exception:  # noqa: BLE001 解析失败不影响发送
            return {}
        return self._counter(it["text"] for it in items if it["is_own"])

    def _has_new_own_bubble(self, root: ET.Element, text: str, baseline: dict) -> bool:
        """发送后是否出现了包含目标文本的**新**己方气泡（避免上次同文本误判）。"""
        needle = (text or "").strip()
        if not needle:
            return False
        first_line = needle.splitlines()[0].strip()[:20] or needle[:20]
        after = self._own_bubble_counter(root)
        for blob, cnt in after.items():
            if blob.startswith(self._send_fail_markers()) or self._is_system_msg(blob):
                continue
            if (needle in blob or (first_line and first_line in blob)) \
                    and cnt > baseline.get(blob, 0):
                return True
        return False

    def send_message(self, text: str) -> bool:
        """发送消息并**确认真实结果**（不再"发失败也报成功"）。

        返回 True 仅当能确认消息已交出去：
        - 出现包含该文本的**新**己方气泡；或
        - 输入框已不再包含该文本，且没有出现新的"发送失败"类提示。
        读不到界面、找不到输入框、出现新的失败提示 -> 返回 False（调用方如实回报失败）。

        重试策略：只有"输入框里仍留着这段文本"（点击发送没生效）才安全重发，
        最多 xiaotiancai.ui.send_retries 轮；无法确认时绝不重发（避免重复消息）。
        """
        if not text or not text.strip():
            return False
        last_reason = ""
        for attempt in range(1, self._send_retries + 1):
            ok, reason = self._send_once(text)
            if ok:
                return True
            last_reason = reason
            self.log("warning", f"发送未确认（{reason}）"
                                + (f"，准备重试 {attempt}/{self._send_retries}" if attempt < self._send_retries else ""))
            if "输入框仍留有内容" not in reason:
                break  # 其他情况重发有重复风险，直接如实报失败
            time.sleep(self._delay)
        self.log("error", f"发送失败：{last_reason or '未知原因'}"
                          "（请检查小天才 App 聊天窗口与手表网络）")
        return False

    def _send_once(self, text: str) -> tuple[bool, str]:
        """执行一轮"读界面 -> 清残留 -> 注入 -> 点发送 -> 确认"。返回 (是否确认成功, 说明)。

        速度：全部用 `_dump_fast()`（单次 uiautomator dump，走 /dev/tty 快路径）+
        较短的固定等待，一次发送通常 3~5 秒（旧实现 10 秒以上）。
        """
        try:
            try:
                root = self._dump_fast()
            except AdbError:
                root = None
            if root is None or self._find_input(root) is None:
                # 找不到输入框：可能有弹窗盖住了聊天页，先清理再重读界面
                if self._dismiss_blockers():
                    try:
                        root = self._dump_fast()
                    except AdbError:
                        root = None
            if root is None:
                return False, "界面读取失败，无法确认聊天页状态"
            input_node = self._find_input(root)
            if input_node is None:
                # 语音模式没有输入框：单击左侧图标切到文字模式
                if self._switch_to_text_mode(root):
                    self.log("info", "已在语音模式，切换到文字输入")
                    time.sleep(self._delay)
                    try:
                        root = self._dump_fast()
                    except AdbError:
                        return False, "界面读取失败（切换文字模式后）"
                    input_node = self._find_input(root)
            if input_node is None:
                return False, "未找到输入框（请确认当前在聊天页）"
            # 发送前基线：旧的失败提示 / 已有己方气泡（用于识别"新出现"的失败与新气泡）
            base_fail = self._fail_signature(root)
            base_own = self._own_bubble_counter(root)
            self._clear_chat_input(root)
            self.adb.tap_element(input_node)
            time.sleep(self._delay)   # 等软键盘弹出，避免输入被吞
            if not self.adb.input_text(text, verify=self.input_verifier(text)):
                return False, "文本注入失败（输入框未收到内容）"
            time.sleep(0.3)
            try:
                send_root = self._dump_fast()
            except AdbError:
                send_root = root
            send_node = self._find_send(send_root)
            if send_node is None:
                self.log("warning", "未找到发送按钮，改用回车发送")
                self.adb.keyevent(66)  # KEYCODE_ENTER
            else:
                self.adb.tap_element(send_node)
            return self._confirm_sent(text, base_fail, base_own)
        except AdbError as e:
            return False, f"ADB 异常: {e}"

    def _confirm_sent(self, text: str, base_fail: list, base_own: dict) -> tuple[bool, str]:
        """发送后确认（最多 3 次快速 dump，约 2s）。返回 (是否确认发出, 说明)。"""
        last = "界面读取失败"
        base_fail_cnt = self._counter(base_fail)
        for i in range(3):
            if i:
                time.sleep(0.5)
            try:
                root = self._dump_fast()
            except AdbError as e:
                last = f"界面读取失败({e})"
                continue
            # 新出现的"发送失败"提示 = 明确失败
            now_fail = self._counter(self._fail_signature(root))
            new_fail = [k for k, v in now_fail.items() if v > base_fail_cnt.get(k, 0)]
            if new_fail:
                return False, f"出现发送失败提示（{new_fail[0][:30]}）"
            if self._has_new_own_bubble(root, text, base_own):
                return True, "已出现己方消息气泡"     # 最强证据：消息真的进了聊天记录
            edit = self._find_input(root)
            if edit is None:
                last = "未找到输入框，无法确认"
                continue
            cur = (edit.get("text", "") or "")
            needle = (text or "").strip()
            if needle and cur and needle in cur:
                return False, "输入框仍留有内容"
            # 输入框已清空且没有新的失败提示：视为已发出（App 点发送后立即清空输入框）
            return True, "输入框已清空且无失败提示"
        return False, last

    def _switch_to_text_mode(self, root: ET.Element) -> bool:
        """语音模式 -> 文字模式：单击 iv_left_img_view（实测单击即可切换）。"""
        for n in root.iter("node"):
            if self._id_tail(n) in _SWITCH_TO_TEXT_IDS:
                self.adb.tap_element(n)
                return True
        return False

    def _find_input(self, root: ET.Element):
        rid = self.ui.get("input_resource_id", "")
        if rid:
            n = self.adb.find_element(root, resource_id=rid)
            if n is not None:
                return n
        # 特征 id 优先（实测 et_chat_text_content）
        for n in root.iter("node"):
            if self._id_tail(n) == "et_chat_text_content":
                return n
        nodes = self.adb.find_elements(root, class_name="EditText")
        if nodes:
            # 取最靠下的 EditText，通常是聊天输入框
            n = max(nodes, key=lambda n: (self._bounds(n) or (0, 0, 0, 0))[3])
            return n
        return None

    def _find_send(self, root: ET.Element):
        rid = self.ui.get("send_resource_id", "")
        if rid:
            n = self.adb.find_element(root, resource_id=rid)
            if n is not None:
                return n
        # resource-id 含 send（实测 tv_send_view）
        for n in root.iter("node"):
            if "send" in self._id_tail(n).lower() and "Text" in n.get("class", ""):
                return n
        # 精确文本"发送"（不能用 text_contains：会命中输入框"发送文字"提示）
        for t in self.ui.get("send_texts", ["发送"]):
            n = self._first(
                self.adb.find_element(root, text=t),
                self.adb.find_element(root, content_desc=t))
            if n is not None:
                return n
        return None

    # ------------------------------------------------------------------ 界面初始化/恢复
    def ensure_input_clean(self) -> str:
        """确保文字模式（有输入框），并清空输入框内容。返回状态描述。"""
        try:
            root = self._dump_fast()
            edit = self._find_input(root)
            if edit is None:
                if self._switch_to_text_mode(root):
                    time.sleep(self._delay)
                    root = self._dump_fast()
                    edit = self._find_input(root)
            if edit is None:
                return "未找到输入框（可能不在聊天页或界面异常）"
            # 已经空了就不要重复点键盘（少一次点击/40 次按键）
            if (edit.get("text", "") or "").strip():
                self.adb.tap_element(edit)
                time.sleep(self._delay)
                for _ in range(40):
                    self.adb.keyevent(67)  # 清空可能残留的文字
                return "文字模式已就绪，输入框已清空"
            return "文字模式已就绪（输入框本来就是空的）"
        except AdbError as e:
            return f"输入框处理失败: {e}"

    def recover(self, contact: str = "") -> str:
        """"随机应变"的界面自愈：清弹窗 -> 必要时启动 App -> 检查登录 -> 回到聊天页。

        与旧的"每次都从头启动一遍"不同：每一步都先判断当前状态，只做缺的那一步。
        返回一句可读的状态说明（供日志/初始化命令使用）。
        """
        steps: list[str] = []
        try:
            if self.settle():
                steps.append("已清理弹窗")
            if not self.adb.is_in_foreground(self.package):
                ok = self.launch()
                steps.append("已启动 App" if ok else "启动失败")
                if not ok:
                    return "，".join(steps)
                self.settle()
            if not self.is_logged_in(force=True):
                steps.append("未登录（等待自动登录/手动登录）")
                return "，".join(steps)
            if contact and not self.is_in_chat():
                if self.open_chat(contact):
                    steps.append("已回到聊天页")
                else:
                    steps.append("未能回到聊天页")
            return "，".join(steps) or "界面正常"
        except AdbError as e:
            return f"界面恢复异常: {e}"

    def keyboard_visible(self) -> bool:
        """软键盘是否弹出（dumpsys input_method 判断）。"""
        try:
            return self.adb.ime_shown()
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

        own_text = 最新一条"自己发的"消息文本；
        own_recent = 最近若干条自己发的消息 [(text, time_label)]（新->旧，供 xtc 侧
        命令检测；命令可能被送达确认等新消息盖过，需扫最近几条；时间标签用于区分
        同文本的再次输入）。复用同一次 dump，避免额外开 uiautomator dump。不抛异常。

        只在聊天窗口内读取——列表预览无法可靠判断发送方（家长侧手动发送的消息
        也会出现在预览里），会被误当成对方消息转发。轮询层负责确保聊天窗口已打开。"""
        try:
            if not self.require_login():
                return (None, None, "", "", [])
            # 一次 dump 同时用于登录态判断与消息解析（登录态有缓存，避免重复 dump）
            root = self._dump_fast()
            if self.is_in_chat(root):
                contact, text, time_label = self._latest_in_chat(root)
                own_recent = self._own_texts_in_chat(root)
                return (contact, text, time_label,
                        own_recent[0][0] if own_recent else "", own_recent)
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
            # 桥接系统提示（如"发送成功/发送失败"送达确认）一律不转发，防止循环
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
        """解析一次 UI dump 的聊天消息气泡，按屏幕从上到下（旧->新）排序。

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
        return own[0][0] if own else ""

    def _own_texts_in_chat(self, root: ET.Element, limit: int = 8) -> list[tuple[str, str]]:
        """聊天页内最近若干条"自己发的"消息（新->旧，系统/垃圾已过滤）。
        返回 [(text, time_label)]。命令可能被送达确认等后续消息盖过（不再是
        "最新一条"），检测时扫最近几条；时间标签用于区分同文本的再次输入。"""
        items = self._chat_bubbles(root, include_own=True)
        return [(it["text"], it["time_label"]) for it in reversed(items)
                if it["is_own"]][:max(1, limit)]

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
                    # 向下翻页 = 露出更早内容（从屏幕上方进入）-> 整体比已收集内容更旧
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
        """桥接系统提示消息（送达确认等）按前缀识别，防止被当成接收消息转发。

        空前缀要过滤掉：`str.startswith("")` 恒为真，会把所有消息都当成系统提示。
        """
        prefixes = self.ui.get("system_msg_prefixes", ["发送成功", "发送失败"])
        return any(str(p).strip() and str(text).startswith(str(p)) for p in prefixes)

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
