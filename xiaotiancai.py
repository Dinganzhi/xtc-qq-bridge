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
# 聊天页 resource-id 末段的**包含**特征：不同版本/机型 id 会有前缀后缀差异，
# 只认全等会漏判（用户反馈"明明在聊天页却以为我在主页"）。命中任一即视为聊天页。
_CHAT_ID_HINTS = ("chat_input", "chat_edit", "et_chat_text", "chat_text_content",
                  "chat_record", "record_button", "send_view", "btn_send", "tv_send",
                  "chat_msg_item", "chatting_input", "edit_chat")
# 聊天页 Activity 名的判断：含 chat，且不含这些"列表/首页"字样（避免把聊天列表当聊天页）
_ACTIVITY_CHAT_EXCLUDE = ("list", "main", "welcome", "login", "register", "signin",
                          "watchmsg", "msglist", "message")
# 消息列表行的联系人名 id（找不到联系人时用来给出"当前可见联系人"诊断）
_CONTACT_NAME_IDS = ("tv_chat_dialog_name", "tv_chat_dialog_title", "chat_dialog_name")
# 消息列表行的预览 id（用来判断"当前页面到底是不是消息列表"）
_CONTACT_PREVIEW_IDS = ("tv_chat_dialog_last_msg_content", "tv_chat_dialog_last_msg")
# 语音模式 -> 文字模式的切换按钮
_SWITCH_TO_TEXT_IDS = ("iv_left_img_view", "chat_record_button")
# 发送控件 id 线索（实测 tv_send_view，可点的是父 fl_chat_text_send）
_SEND_ID_HINTS = ("tv_send_view", "fl_chat_text_send", "btn_send", "send_view", "iv_send",
                  "chat_send", "send_button")
# 这些是"消息状态"图标（发送中/发送失败/重发），**不是**发送按钮，必须排除
_SEND_EXCLUDE_IDS = ("msg_item_state", "resend", "send_state", "send_fail", "msg_state")
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
_SKIP_TEXTS = ("以后再说", "稍后再说", "暂不更新", "不更新", "稍后更新", "下次再说", "暂不升级",
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

# ---- 通用弹窗（自定义 Activity 弹窗 / Dialog / PopupWindow）----
# 弹窗类 Activity 的名字里常带这些词：小天才的"升级提醒"就是
# com.xtc.watch/com.xtc.widget.phone.popup.activity.CustomActivity14
# （它既不是 PopupWindow 也不是 Dialog，以前认不出来，所以永远不会被自动关掉）
_POPUP_ACTIVITY_HINTS = ("popup", "dialog", "alert")
# 弹窗标题/说明控件（聊天页、消息列表页不会有）
_POPUP_TITLE_TAILS = ("title", "tv_title", "dialog_title", "tv_dialog_title", "alert_title")
# 弹窗容器（自定义弹窗常用 ll_center / iv_banner 这类 id）
_POPUP_CONTAINER_TAILS = ("ll_center", "fl_center", "iv_banner", "dialog_root",
                          "dialog_container", "alert_root", "popup_root")
# 负向（安全）按钮：点它只是"不做这件事"，不会触发下载/安装/重启
_POPUP_NEG_TAILS = ("btn_left", "btn_cancel", "tv_cancel", "btn_no", "tv_no",
                    "btn_negative", "tv_negative", "btn_disagree", "btn_later", "tv_left")
# 正向/危险按钮：**绝不点**（立即安装/立即更新/确定安装 这类会真的动系统）
_POPUP_POS_TAILS = ("btn_right", "btn_sure", "btn_ok", "btn_confirm", "tv_right",
                    "btn_positive", "btn_install", "btn_update", "btn_restart")
# 危险按钮文案（命中且不含"不/稍后/取消"这类否定词时才禁点）
_POPUP_BLOCK_TEXTS = ("立即安装", "马上安装", "现在安装", "去安装", "确认安装", "下载安装",
                      "立即更新", "马上更新", "立即升级", "马上升级", "现在升级", "去升级",
                      "立即重启", "去评分", "给个好评", "同意并安装")
# 否定词：带这些词的按钮一律视为"安全的负向按钮"
_POPUP_SAFE_WORDS = ("不", "稍后", "以后", "下次", "暂", "取消", "关闭", "忽略",
                     "拒绝", "返回", "放弃")

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
# 输入框为空时会显示"占位提示"（hint），dump 出来的 text 就是这段文案；
# 必须当成"空"，否则清残留/发送确认都会把它误当成用户输入（实机测试发现）。
_DEFAULT_INPUT_HINTS = ("发送文字", "发送消息", "输入内容", "说点什么", "输入消息", "发消息")

# 登录态三态（模块级常量，bridge 等模块直接引用，避免依赖具体实例）
LOGIN_LOGGED_IN = "logged_in"
LOGIN_NOT_LOGGED_IN = "not_logged_in"
LOGIN_UNKNOWN = "unknown"


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
        # 注意：这些"上次做某事"的时间戳一律用 -inf 作初值，**不能写 0.0**。
        # time.monotonic() 的零点任意（Linux 上是开机时长），刚开机/刚启动的机器上
        # now - 0.0 可能小于节流间隔，导致该做的事被当成"刚做过"而跳过。
        self._net_retry_ts = float("-inf")     # 网络弹窗"重试"按钮的点击冷却
        self._last_dump_warn = float("-inf")   # "读不到界面"告警的节流时间戳
        self._last_label_retry = float("-inf")  # "快照里没有时间标签"的补救重读节流
        self._throttle: dict[str, float] = {}   # 同类失败告警节流：key -> 上次打印时刻
        self._last_verify_root = None   # 注入校验时的界面快照（复用给"找发送按钮"，省一次 dump）
        # 弹窗自动关闭的"同一弹窗最多试几轮"状态（空 key 表示当前没有弹窗）
        self._popup_key = ""            # 当前弹窗的指纹（Activity + 标题 + 说明前若干字）
        self._popup_tries = 0           # 这个弹窗已经尝试关闭的轮数
        # 最近一次界面快照（轮询每 2 秒就会更新）：发送等交互复用它，省一次 ~3 秒的 dump
        self._snapshot = None
        self._snapshot_ts = float("-inf")
        # 发送按钮坐标缓存 + 快路径开关（省掉"注入后 dump 一次找发送按钮"的 ~3 秒）
        self._send_cache: dict | None = None
        # "先手打字"的中间状态（begin_blind_send -> end_blind_send）
        self._blind: dict | None = None
        self._blind_enabled = bool(self.ui.get("blind_send", True))
        self._fast_send = bool(self.ui.get("fast_send", True))
        try:
            self._snapshot_reuse = max(0.0, float(self.ui.get("snapshot_reuse", 3.0)))
        except (TypeError, ValueError):
            self._snapshot_reuse = 3.0
        # 点发送后等多久再 dump 确认：太短会白 dump 一次（约 3 秒），太长就只是慢一点
        try:
            self._confirm_delay = max(0.0, float(self.ui.get("confirm_delay", 0.45)))
        except (TypeError, ValueError):
            self._confirm_delay = 0.45
        self.last_open_reason = ""   # 最近一次 open_chat 失败的原因（桥接据此去重打印）

    def log(self, level: str, msg: str):
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(msg)

    def _warn_throttled(self, key: str, msg: str, interval: float = 300.0) -> None:
        """同一类失败在 interval 秒内只打一次 warning，其余降为 debug。

        轮询每 2 秒跑一轮，失败原因又往往是"状态问题"（未登录、不在前台、
        联系人不在当前页），不节流就会把日志刷成"老是提示找不到联系人"。
        """
        now = time.monotonic()
        if now - self._throttle.get(key, float("-inf")) >= interval:
            self._throttle[key] = now
            self.log("warning", msg)
        else:
            self.log("debug", msg)

    # ------------------------------------------------------------------ 状态判定
    def app_state_with_root(self, attempts: int = 2) -> tuple:
        """读一次界面并判定状态，返回 (state, root)。

        state 取值见 STATE_* 常量。读不到界面时 root 可能为 None。
        """
        root = self._dump_with_retry(attempts)
        return self.app_state(root), root

    @staticmethod
    def _tree_is_empty(root: ET.Element) -> bool:
        """界面快照是不是"空的"（UI 还没渲染好/dump 到了空树）。

        不能用"节点数 < 3"来判断：登录页有时只有两三个控件，会被误判成读不到。
        真正的空树是所有节点都没有文本、描述、id、也不可点击。
        """
        for n in root.iter("node"):
            if (n.get("text") or "").strip() or (n.get("content-desc") or "").strip():
                return False
            if (n.get("resource-id") or "").strip():
                return False
            if str(n.get("clickable", "")).lower() == "true":
                return False
        return True

    def app_state(self, root: ET.Element | None = None) -> str:
        """判定"App 现在到底处于什么状态"，供桥接做状态机与日志。

        为什么需要它：以前轮询只问"在不在聊天页"，不在就去 open_chat，
        于是在登录页/别的页面上反复找联系人，报一堆"找不到联系人"，
        既没有判断状态也没法对症处理。

        顺序：先看前台（不在前台时界面根本不是小天才的）-> 再看能不能读到界面
        -> 登录/安全验证页 -> 聊天页 -> 消息列表 -> 其它页面。
        """
        try:
            if not self.adb.is_in_foreground(self.package):
                return self.STATE_BACKGROUND
        except AdbError:
            return self.STATE_BLIND
        if root is None:
            root = self._dump_with_retry(1)
        if root is None or self._tree_is_empty(root):
            return self.STATE_BLIND
        try:
            # 弹窗优先：弹窗盖在上面时，dump 出来的往往只有弹窗本身（下面的聊天页
            # 读不到），若先判聊天页会得出错误结论，所以先认弹窗、把它关掉再说。
            if self._looks_like_popup(root, self.current_activity()):
                return self.STATE_POPUP
            if self.looks_like_chat_page(root):
                return self.STATE_CHAT
            if self._looks_like_login_page(root):
                return self.STATE_LOGIN
            if self.looks_like_message_list(root):
                return self.STATE_LIST
        except Exception:  # noqa: BLE001 判定异常按"读不到"处理
            return self.STATE_BLIND
        return self.STATE_OTHER

    def _dump_fast(self) -> ET.Element:
        """交互路径的快速 UI dump（2 次尝试、很短的等待）。

        比默认 dump（2 次 / 0.8s）快，又比"只试 1 次"稳——单次 uiautomator 偶发失败
        时不至于把整轮发送/登录直接判成失败。

        每次成功都记进"最近快照"（见 recent_snapshot）：轮询每 2 秒就会 dump 一次，
        发送等交互可以直接复用那份，省掉一次 ~3 秒的 dump。
        """
        root = self.adb.dump_ui(retries=2, delay=0.3)
        self._snapshot = root
        self._snapshot_ts = time.monotonic()
        return root

    def recent_snapshot(self, max_age: float | None = None) -> ET.Element | None:
        """最近一次界面快照（默认 max_age = ui.snapshot_reuse，秒），过期返回 None。

        只用于"基线/控件位置"这类容错信息（例如发送前记下已有气泡数、输入框位置），
        调用方拿到后仍会做自己的判断；过期就拿不到，自己 dump 即可。
        """
        if self._snapshot is None:
            return None
        age = self._snapshot_reuse if max_age is None else max_age
        if age <= 0:
            return None
        if time.monotonic() - self._snapshot_ts > age:
            return None
        return self._snapshot

    def _dump_with_retry(self, attempts: int = 2) -> ET.Element | None:
        """读界面并容错：失败时（收键盘 / 找回窗口）再试，仍失败返回 None。

        为什么要收键盘：软键盘/光标闪烁是 uiautomator "could not get idle state"
        的常见原因之一，收起后再 dump 往往就成功了。
        为什么要"找回窗口"：WSA 窗口被最小化/关闭或息屏后，Android 侧
        `mCurrentFocus=null`，uiautomator 会一直报 "null root node"——这时先唤醒屏幕、
        再重新拉起 App，通常就能恢复读取。
        日志节流：同一类失败 30 秒内只打一次 warning，其余降级为 debug（避免刷屏）。
        """
        for i in range(max(1, attempts)):
            try:
                return self._dump_fast()
            except AdbError as e:
                now = time.monotonic()
                level = "warning" if now - self._last_dump_warn >= 30 else "debug"
                self._last_dump_warn = now
                self.log(level, f"读取界面失败（第 {i + 1}/{attempts} 次）: {e}")
                if i + 1 >= attempts:
                    return None
                self._recover_window()
                time.sleep(0.8)
        return None

    def _recover_window(self) -> bool:
        """把"窗口"找回来：先收键盘，再（必要时）唤醒屏幕 + 重新拉起 App。

        返回是否执行了恢复动作。没有焦点窗口时（WSA 窗口被最小化/关闭、息屏），
        `launch()` 因为 Activity 仍是 resumed 会直接跳过，所以这里直接调 adb 拉起。
        """
        acted = False
        try:
            self.close_keyboard()
        except Exception:  # noqa: BLE001 个别实现没有 ime_shown
            pass
        try:
            # 两种情况都要把窗口找回来：
            #   ① 没有任何窗口有焦点（mCurrentFocus=null，见实时日志）
            #   ② 前台是**别的 App**（例如 WSA 主屏 com.microsoft.windows.homeapp，
            #      用户日志里就是这个场景）——此时 dump 同样会失败
            foreign = False
            if self.adb.has_focus_window():
                foreign = not self.adb.is_in_foreground(self.package)
            needs = (not self.adb.has_focus_window()) or foreign
            if needs:
                why = "前台是其它窗口" if foreign else "没有任何窗口获得焦点"
                self.log("info", f"{why}（可能是 WSA 窗口被最小化/关闭、屏幕未点亮，"
                                 f"或 App 没在前台），正在唤醒屏幕并重新拉起小天才 App ...")
                if self.adb.screen_on() is not True:
                    self.adb.wake_up()
                self.adb.invalidate_focus()
                self.adb.launch_app(self.package, self.main_activity, wait=8.0, attempts=1)
                self.invalidate_login_state()
                acted = True
        except AdbError as e:
            self.log("debug", f"找回窗口失败: {e}")
        return acted

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
        """当前 **Activity**（忽略输入法/弹窗窗口）。"""
        return self.adb.get_current_activity() or ""

    def current_window(self) -> str:
        """当前最上层窗口（含输入法/弹窗），用于弹窗判断。"""
        return self.adb.get_current_focus() or ""

    # 登录态三态：明确已登录 / 明确未登录 / 无法判断（与模块级常量同值）
    LOGGED_IN = LOGIN_LOGGED_IN
    NOT_LOGGED_IN = LOGIN_NOT_LOGGED_IN
    LOGIN_UNKNOWN = LOGIN_UNKNOWN

    # App 状态机（app_state 的返回值）：桥接据此决定"该做什么"，而不是一味重试
    STATE_CHAT = "chat"              # 已在聊天窗口
    STATE_LIST = "list"              # 消息列表（这里才该找联系人）
    STATE_LOGIN = "login"            # 登录页/安全验证页（等登录，别找联系人）
    STATE_OTHER = "other"            # 其它页面（首页/设置/详情等）
    STATE_POPUP = "popup"            # 弹窗遮挡界面（升级提醒等，读不到下面的内容）
    STATE_BACKGROUND = "background"  # App 不在前台
    STATE_BLIND = "blind"            # 读不到界面（息屏/界面不空闲/dump 失败）

    STATE_TEXT = {
        STATE_CHAT: "聊天窗口",
        STATE_LIST: "消息列表",
        STATE_LOGIN: "登录/验证页",
        STATE_OTHER: "小天才其它页面",
        STATE_POPUP: "弹窗遮挡界面",
        STATE_BACKGROUND: "App 不在前台",
        STATE_BLIND: "读不到界面",
    }

    def login_state(self, force: bool = False, root: ET.Element | None = None) -> str:
        """判断登录态，返回 'logged_in' / 'not_logged_in' / 'unknown'。

        **以界面为准，Activity 名只作兜底**（用户反馈过"明明已登录却提示未登录"）：
        - App 不在前台 / 界面读不到（息屏、锁屏、dump 失败）→ 'unknown'
          （绝不当成"未登录"，否则会去点登录按钮、甚至误触发自动登录）；
        - 界面像登录页（密码框 / 获取验证码 / 短信验证码登录 / login_markers）→ 'not_logged_in'；
        - 界面像主界面（聊天页 / 消息列表 / 微聊·我的 等）→ 'logged_in'；
        - 都不像时才看 Activity 名：含 welcome/register/signin 或以 LoginActivity 结尾 → 'not_logged_in'，
          否则按已登录处理（保守，避免谎报未登录）。

        force=False 时结果按 login_state_ttl 秒缓存（轮询每 2s 调用，缓存可省掉大量 dump）；
        root 传入时复用调用方已经 dump 好的界面，不再重复 dump。
        """
        if not force and root is None and self._login_state is not None:
            ts, val = self._login_state
            if self._login_state_ttl > 0 and (time.monotonic() - ts) < self._login_state_ttl:
                return val
        act = self.current_activity()
        if not act.startswith(self.package):
            # 拿不到 Activity 信息，或 App 真的不在前台：无法判断登录态
            return self._remember_login(self.LOGIN_UNKNOWN)
        if root is None:
            try:
                root = self.adb.dump_ui(retries=2, delay=0.4)
            except AdbError:
                # 读不到界面（息屏/空闲性/安全窗口）→ 无法判断，不缓存为"未登录"
                return self.LOGIN_UNKNOWN
        # 首启隐私协议弹窗 = 尚未进入 App
        joined = "".join(n.get("text", "") or "" for n in root.iter("node"))
        if "温馨提示" in joined and ("同意" in joined or "不同意" in joined):
            return self._remember_login(self.NOT_LOGGED_IN)
        if self._looks_like_login_page(root):
            return self._remember_login(self.NOT_LOGGED_IN)
        if self._looks_like_main_ui(root):
            return self._remember_login(self.LOGGED_IN)
        # 界面读到了但两种都不像：用 Activity 名兜底（只认明确的登录/欢迎页）
        low = act.lower()
        if any(m in low for m in ("welcome", "register", "signin")) or low.endswith("loginactivity"):
            return self._remember_login(self.NOT_LOGGED_IN)
        return self._remember_login(self.LOGGED_IN)

    def is_logged_in(self, force: bool = False, root: ET.Element | None = None) -> bool:
        """是否已登录（True 仅当**明确**判定为已登录；无法判断时为 False）。

        需要区分"未登录"和"无法判断"的场景请直接用 login_state()。
        """
        return self.login_state(force=force, root=root) == self.LOGGED_IN

    def _looks_like_main_ui(self, root: ET.Element) -> bool:
        """主界面证据：聊天页（输入栏/消息气泡）、消息列表行、微聊/我的 等入口。

        这是修复"已登录却提示未登录"的关键：**登录后界面有这些特征就认为已登录**，
        不再因为 Activity 名里带 login（如 AccountVerifyLoginActivity）而误判。
        """
        if self.looks_like_chat_page(root):
            return True
        if self._row_anchors(root):                     # 消息列表有"联系人+预览"行
            return True
        markers = set(self.ui.get("main_ui_markers",
                                  ["微聊", "消息", "我的", "联系人", "聊天", "设置", "发现"]))
        texts = {(n.get("text") or "").strip() for n in root.iter("node")}
        hits = {t for t in texts if t in markers}
        return len(hits) >= 2

    def _remember_login(self, state: str) -> str:
        self._login_state = (time.monotonic(), state)
        return state

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

    def require_login(self, root: ET.Element | None = None) -> bool:
        """能否读消息：只有**明确未登录**才算不能读。

        注意：'unknown'（App 不在前台 / 界面读不到，例如息屏）不再当成"未登录"刷日志，
        也不会（在桥接层）触发自动登录 —— 这正是"已经登录了还提示未登录"的修复点之一。
        """
        state = self.login_state(root=root)
        if state == self.NOT_LOGGED_IN:
            if not self._warned_not_login:
                self.log("warning", "小天才 App 未登录家长账号：请在目标 Android 环境"
                                    "（模拟器/WSA/Waydroid/真机）里完成登录（登录前无法读取/发送消息）")
                self._warned_not_login = True
            return False
        if state == self.LOGIN_UNKNOWN:
            self.log("debug", "无法确认登录态（App 不在前台或界面暂时读不到），本轮跳过读取")
            return False
        self._warned_not_login = False
        return True

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
            # 5) 勾选协议：只有**明确未勾选**（checked=false）才点，
            #    属性缺失时不乱点（避免把已勾选的又取消，实机上会被"请勾选协议"拦住）
            root = self._dump_fast()
            cb = self._first(
                self.adb.find_element(root, class_name="CheckBox"),
                self.adb.find_element(root, resource_id="com.xtc.watch:id/cb_protocol"))
            if cb is not None and (cb.get("checked", "") or "").lower() == "false":
                self.log("debug", "勾选用户协议")
                self.adb.tap_element(cb)
                time.sleep(0.4)
            # 6) 点登录（先收起键盘——输入密码后键盘仍打开，会挡住/截获点击）
            #    未离开账密登录页说明点击被吞，重试最多 3 次；
            #    若被"请勾选协议"拦住，则补勾一次再点。
            self.adb.keyevent(4)
            time.sleep(0.5)
            tapped = False
            for attempt in range(1, 4):
                root = self._dump_fast()
                if self._needs_protocol_agree(root):
                    cb2 = self._first(
                        self.adb.find_element(root, class_name="CheckBox"),
                        self.adb.find_element(root, resource_id="com.xtc.watch:id/cb_protocol"))
                    if cb2 is not None:
                        self.log("info", "登录提示需要同意协议，补勾一次后重试")
                        self.adb.tap_element(cb2)
                        time.sleep(0.5)
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
        """登录按钮：精确文本"登录" -> id 末段是 login/btn_login 的控件 -> content-desc。

        实机（WSA + 小天才）实测：登录按钮是**无文本的可点击 RelativeLayout**
        （`rl_login_login`，clickable=true）。旧实现要求 class 含 "Text"/"Button"，
        于是"表单填好了却说未找到登录按钮"。现在只要 id 命中且**可点击**就认。
        （仍不能用"id 含 login"——tv_login_area_title / tv_login_bottom 等标签也含 login。）
        """
        n = self.adb.find_element(root, text="登录")
        if n is not None:
            return n
        for node in root.iter("node"):
            tail = self._id_tail(node).lower()
            if not (tail.endswith("login") or "login_btn" in tail or "btn_login" in tail):
                continue
            cls = node.get("class", "")
            if ("Text" in cls or "Button" in cls
                    or (node.get("clickable", "") or "") == "true"):
                return node
        return self._first(
            self.adb.find_element(root, content_desc="登录"),
            None)

    @staticmethod
    def _needs_protocol_agree(root: ET.Element) -> bool:
        """是否提示"需要勾选同意协议"（实机上未勾选时点登录会这样拦）。"""
        joined = "".join((n.get("text") or "") for n in root.iter("node"))
        return ("协议" in joined) and any(w in joined for w in ("请勾选", "请先", "未勾选", "勾选同意"))

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
        小天才警告弹窗 -> 关闭类按钮（id/desc）-> 通用对话框文本按钮 ->
        **通用弹窗兜底（负向按钮/返回键，认结构不认文案）** -> 弹窗窗口 BACK 兜底。

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
            # 弹窗判定（认结构/Activity 名，不认文案）；不是弹窗就把"同一弹窗重试计数"清零
            popup = self._looks_like_popup(root, focus)
            if not popup:
                self._popup_key, self._popup_tries = "", 0
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
            # 5) 已知的"跳过/稍后"类弹窗：点配置里的跳过按钮。
            #    命中条件放宽到"只要是弹窗就试"——小天才的"升级提醒"标题是"升级提醒"、
            #    按钮是"不更新"，以前靠 _UPDATE_WORDS 匹配碰运气，现在结构认出来就够了；
            #    按钮文案仍必须是白名单里的（绝不点"立即安装/立即更新"）。
            if popup or any(w in texts_all for w in _UPDATE_WORDS) \
                    or any(w in descs_all for w in _UPDATE_WORDS):
                for t in self.ui.get("popup_skip_texts", list(_SKIP_TEXTS)):
                    btn = self.adb.find_element(root, text=t)
                    if btn is not None and not self._popup_blocked(btn) \
                            and self._looks_like_dialog(root, focus):
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
            # 10.5) 通用弹窗兜底（小天才"升级提醒"这类**自定义 Activity 弹窗**：
            #       com.xtc.watch/...popup.activity.CustomActivity14，按钮是
            #       btn_left="不更新" / btn_right="立即安装"）。
            #       以前只认 PopupWindow/Dialog 和固定文案，认不出它，于是弹窗永远
            #       关不掉、消息一直读不到。这里按"结构"处理，与具体文案无关：
            #       先点负向按钮（不更新/稍后/取消），找不到就按返回键；绝不点正向按钮。
            if popup:
                key = self._popup_signature(root, focus)
                if key != self._popup_key:
                    self._popup_key, self._popup_tries = key, 0
                self._popup_tries += 1
                title = self._popup_title(root) or focus or "(无标题)"
                if self._popup_tries == 1:
                    neg = self._find_popup_negative(root)
                    if neg is not None:
                        label = (neg.get("text") or "").strip() or self._id_tail(neg)
                        self.adb.tap_element(neg)
                        self.log("info", f"已关闭弹窗「{title}」（点「{label}」，未点确认/安装类按钮）")
                        return True
                    self.adb.keyevent(4)
                    self.log("info", f"弹窗「{title}」没有可点的负向按钮，按返回键关闭")
                    return True
                if self._popup_tries == 2:
                    self.adb.keyevent(4)
                    self.log("warning", f"弹窗「{title}」仍在，直接按返回键关闭")
                    return True
                # 点过负向按钮、也按过返回键都关不掉：不再反复点，交给人工（10 分钟提醒一次）
                self._warn_throttled(
                    f"popup_stuck:{key[:48]}",
                    f"弹窗「{title}」无法自动关闭（已尝试负向按钮与返回键）；"
                    "可在 config.yaml -> xiaotiancai.ui.popup_skip_texts 里加上它的跳过按钮文案"
                    "（例如「不更新」「稍后」）", interval=600.0)
                return False
            # 11) BACK 兜底：仅当前台是独立弹窗/对话框窗口时（如 PopupWindow），
            #     普通 Activity 页面即使有 NAF 节点也不按返回，防止 App 退到桌面。
            if "popupwindow" in low_focus or "dialog" in low_focus:
                self.adb.keyevent(4)
                self.log("info", "检测到弹窗窗口，按返回键关闭")
                return True
        except AdbError:
            pass
        return False

    # ---- 通用弹窗：识别 / 找负向按钮 / 指纹 ----
    def _looks_like_popup(self, root: ET.Element, focus: str = "") -> bool:
        """是不是"弹窗盖在界面上"（自定义 Activity 弹窗 / Dialog / PopupWindow）。

        判定必须**保守**：误判会把正常页面当弹窗去点按钮/按返回键，把 App 退到桌面。
        强证据（任一成立才算）：
        * 前台 Activity 名里带 popup/dialog/alert（小天才是 ...popup.activity.CustomActivity14）；
        * 同时存在左/右两个对话按钮（btn_left + btn_right，聊天页/列表页不会有）；
        * 存在弹窗容器（ll_center 等）且容器里有对话按钮或"标题+说明"。
        """
        low = (focus or "").lower()
        if any(k in low for k in _POPUP_ACTIVITY_HINTS):
            return True
        tails = {self._id_tail(nd).lower() for nd in root.iter("node")}
        if "btn_left" in tails and "btn_right" in tails:
            return True
        if tails & set(_POPUP_CONTAINER_TAILS):
            buttons = tails & (set(_POPUP_NEG_TAILS) | set(_POPUP_POS_TAILS))
            has_desc = bool(tails & {"desc", "tv_desc", "dialog_desc", "alert_desc"})
            has_title = bool(tails & set(_POPUP_TITLE_TAILS))
            if buttons or (has_title and has_desc):
                return True
        return False

    def _popup_title(self, root: ET.Element) -> str:
        """弹窗标题（用于日志，让用户一眼看出是哪个弹窗）。"""
        for rid in _POPUP_TITLE_TAILS:
            nd = self.adb.find_element(root, resource_id=f"{self.package}:id/{rid}")
            if nd is not None and (nd.get("text") or "").strip():
                return (nd.get("text") or "").strip()[:24]
        return ""

    def _popup_signature(self, root: ET.Element, focus: str = "") -> str:
        """弹窗指纹：Activity + 标题 + 说明前 24 字。用来判断"是不是同一个弹窗"，
        从而限制同一弹窗的重试次数（否则会一直点/一直按返回）。"""
        desc = ""
        for rid in ("desc", "tv_desc", "dialog_desc", "alert_desc", "message"):
            nd = self.adb.find_element(root, resource_id=f"{self.package}:id/{rid}")
            if nd is not None and (nd.get("text") or "").strip():
                desc = (nd.get("text") or "").strip()[:24]
                break
        return f"{focus}|{self._popup_title(root)}|{desc}"

    def _popup_block_texts(self) -> tuple:
        """禁点按钮文案（config -> xiaotiancai.ui.popup_block_texts 可覆盖）。"""
        val = self.ui.get("popup_block_texts")
        if isinstance(val, (list, tuple)) and val:
            return tuple(str(v) for v in val)
        return _POPUP_BLOCK_TEXTS

    def _popup_blocked(self, node) -> bool:
        """这个控件是不是"危险按钮"（点了会真的下载/安装/更新/重启）。

        判定顺序：id 末段是正向按钮 -> 禁点；文案带否定词（不/稍后/取消…）-> 安全；
        文案命中禁点词 -> 禁点。
        """
        if node is None:
            return True
        if self._id_tail(node).lower() in _POPUP_POS_TAILS:
            return True
        text = (node.get("text") or "").strip()
        if not text:
            return False
        if any(w in text for w in _POPUP_SAFE_WORDS):
            return False
        return any(w in text for w in self._popup_block_texts())

    def _find_popup_negative(self, root: ET.Element):
        """找弹窗里"安全的负向按钮"（不更新/稍后/取消），找不到返回 None。

        优先按 id（btn_left/btn_cancel…，小天才会话框的约定是左负右正），
        再按配置/默认的跳过文案。**永远不会返回"立即安装"这类正向按钮。**
        """
        for rid in _POPUP_NEG_TAILS:
            nd = self.adb.find_element(root, resource_id=f"{self.package}:id/{rid}")
            # clickable 属性缺失时按"可点"处理（dump 里通常有，测试构造的树里可能没有）
            if nd is not None and str(nd.get("clickable", "true")).lower() != "false" \
                    and not self._popup_blocked(nd):
                return nd
        for t in self.ui.get("popup_skip_texts", list(_SKIP_TEXTS)):
            nd = self.adb.find_element(root, text=t)
            if nd is not None and not self._popup_blocked(nd):
                return nd
        return None

    def _looks_like_dialog(self, root: ET.Element, focus: str = "") -> bool:
        """是否像"弹窗/对话框"场景：独立弹窗窗口、对话框标题控件、
        或界面上同时出现多个对话框按钮文本。用于给"点关闭/跳过"类动作兜底证据，
        避免在正常聊天/列表页误点。"""
        if self._looks_like_popup(root, focus):
            return True
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
            if n is not None and not self._popup_blocked(n):
                self.adb.tap_element(n)
                self.log("info", f"已点击对话框按钮: {key}")
                return True
        for key in confirm_keys:
            n = self.adb.find_element(root, text=key)
            if n is not None and not self._popup_blocked(n):
                self.adb.tap_element(n)
                self.log("info", f"已点击对话框按钮: {key}")
                return True
        return False

    # ------------------------------------------------------------------ 界面判定
    def _activity_is_chat(self, act: str) -> bool:
        """前台 Activity 名是否就是聊天页。

        旧实现只认 `endswith("chatactivity")`，机型/版本一换（ChattingActivity、
        带子类后缀等）就漏判，于是"人已经在聊天页却以为在主页"。
        后来改成"含 chat 且不含列表/首页类字样"，但那段判断用的是**整个组件名**——
        实机上报的是 `com.xtc.wechat.view.chatlist.ChatActivity`：**包名里的
        `chatlist` 含 "list"**，于是聊天页永远被排除，`is_in_chat()` 每次都要再
        dump 一次界面（WSA 上一次 ~3 秒，发送链路白等 3 秒）。现在只拿**类名**
        （最后一段）做判断，`ui.activity_chat_exclude` 也是按类名匹配。
        """
        a = (act or "").lower()
        if not a or not a.startswith(self.package.lower()):
            return False
        cls = a.split("/")[-1].rsplit(".", 1)[-1]      # ...chatlist.ChatActivity -> chatactivity
        if any(w in cls for w in self.ui.get("activity_chat_exclude",
                                             list(_ACTIVITY_CHAT_EXCLUDE))):
            return False
        return "chat" in cls

    def _find_chat_bar(self, root: ET.Element):
        """聊天输入栏 / 聊天页特征控件。

        先精确匹配已知 id，再按"包含"匹配（`send_view`/`chat_msg_item`/`record_button` …），
        这样即使输入栏 id 变了、或当前是语音模式，也能认出"这就是聊天页"。
        """
        hints = tuple(self.ui.get("chat_id_hints", list(_CHAT_ID_HINTS)))
        fallback = None
        for n in root.iter("node"):
            tail = self._id_tail(n).lower()
            if not tail:
                continue
            if tail in _CHAT_IDS:
                return n
            if fallback is None and any(h in tail for h in hints):
                fallback = n
        return fallback

    def looks_like_chat_page(self, root: ET.Element) -> bool:
        """聊天页判定（只看界面）：输入栏特征或**消息气泡**出现即算。

        消息气泡（chat_msg_item_*）只在聊天页出现，是最可靠的信号之一；
        聊天列表页只有 tv_chat_dialog_* 行。
        """
        if self._find_chat_bar(root) is not None:
            return True
        for n in root.iter("node"):
            tail = self._id_tail(n).lower()
            if "chat_msg_item" in tail or tail in ("tv_chat_msg_item_date",):
                return True
        return False

    def looks_like_message_list(self, root: ET.Element) -> bool:
        """是否像"消息列表"页（用于找不到联系人时给出准确诊断）。"""
        names = set(self.ui.get("contact_name_ids", list(_CONTACT_NAME_IDS)))
        previews = set(self.ui.get("contact_preview_ids", list(_CONTACT_PREVIEW_IDS)))
        rows = 0
        for n in root.iter("node"):
            tail = self._id_tail(n)
            if tail in previews:
                rows += 1
            elif tail in names:
                rows += 1
        return rows >= 2

    def is_in_chat(self, root: ET.Element | None = None) -> bool:
        """聊天窗口判定。

        快路径：前台 Activity 名就是聊天页（见 _activity_is_chat）；
        兜底：按界面特征确认（输入栏 / 消息气泡），避免 Activity 名误判或漏判。
        root 传入时复用调用方已经 dump 好的界面，避免重复 dump。"""
        if self._activity_is_chat(self.current_activity()):
            return True
        if root is not None:
            return self.looks_like_chat_page(root)
        try:
            root = self._dump_fast()
        except AdbError:
            return False
        return self.looks_like_chat_page(root)

    # ------------------------------------------------------------------ 打开聊天
    @staticmethod
    def _id_tail(node) -> str:
        """resource-id 的末段（如 com.xtc.watch:id/tv_send_view -> tv_send_view）。"""
        rid = node.get("resource-id", "") or ""
        return rid.split("/")[-1]

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
        # 0) App 不在前台（例如 WSA 停在主屏/别的窗口）-> 先把它拉到前台，
        #    否则会在 WSA 主屏上找联系人，然后报"找不到联系人"（实机测试发现的坑）。
        if not self.adb.is_in_foreground(self.package):
            # 0.1) 先排除"只是息屏/窗口没焦点"：WSA 睡过去时 App 同样看起来不在前台，
            #      但窗口还在、Activity 还是 resumed 的 —— 叫醒即可，不必重新拉起 App
            #      （实测"重新拉起"要 20 秒，唤醒只要 ~1.5 秒）。
            if self.adb.wake_if_asleep() and self.adb.is_in_foreground(self.package):
                self.log("info", "子系统息屏导致 App 不在前台，唤醒后已回到前台（无需重启 App）")
                self._dismiss_blockers()
                if self.is_in_chat():
                    return True
            else:
                self.log("info", f"小天才 App 不在前台（当前={self.current_activity() or '未知'}），先启动它")
                if not self.launch():
                    self.log("warning", "打开聊天失败：小天才 App 没能拉到前台")
                    return False
                self._dismiss_blockers()
                if self.is_in_chat():
                    return True
        self._dismiss_blockers()
        # 读到界面之前先看一眼登录态：未登录时没必要去找联系人
        # （实机测试：未登录时在登录页反复找联系人会白等 40 多秒）
        if self.login_state(force=True) == self.NOT_LOGGED_IN:
            self.last_open_reason = (f"小天才 App 未登录（当前在登录页），"
                                     f"登录后才能进入与 {contact!r} 的聊天")
            self._warn_throttled("open_chat_not_logged_in", "打开聊天失败：" + self.last_open_reason)
            return False
        # 读界面（失败自动收键盘重试；仍失败就本轮放弃，交给外层稍后重试，
        # 不再抛出 "cat: ... No such file" 这种误导性 ERROR）
        root = self._dump_with_retry(2)
        if root is None:
            self.last_open_reason = (f"连续读不到界面控件（联系人 {contact}），稍后会自动重试"
                                     "（若反复出现，多为界面一直不空闲，"
                                     "可调大 adb.dump_retries / adb.dump_delay）")
            self._warn_throttled("open_chat_no_dump", "打开聊天暂缓：" + self.last_open_reason)
            return False
        if len(list(root.iter("node"))) < 3:
            # 刚登录完/页面切换中的空树：别照着它去"找联系人"，那只会得出错结论
            self.last_open_reason = "界面还在切换/加载（只读到空树）"
            self.log("info", f"打开聊天暂缓：{self.last_open_reason}，稍后自动重试")
            return False
        try:
            # 进来先确认一次：万一 Activity 名不认识，界面特征也能证明"已经在聊天页"
            if self.looks_like_chat_page(root):
                return True
            # 消息列表通常就是首页（"微聊"）：先在当前页找，找不到再切 Tab / 滑动查找
            node = self._find_contact_node(root, contact)
            if node is None:
                root = self._switch_to_message_tab(root) or root
                node = self._find_contact_node(root, contact)
            if node is None and self.looks_like_message_list(root):
                # 联系人可能在屏幕外：列表页才滑动（避免在别的页面误滑）
                node, _visible = self._scroll_find_contact(contact, root)
            if node is None:
                visible = self._visible_contact_names(root)
                if not self.adb.is_in_foreground(self.package):
                    page = f"小天才 App 不在前台（当前={self.current_activity() or '未知'}）"
                elif self.login_state(force=True) == self.NOT_LOGGED_IN:
                    page = "小天才 App 未登录（当前在登录页），登录后才能进入聊天"
                elif visible:
                    page = "消息列表"
                else:
                    page = f"当前页面不像消息列表（前台={self.current_activity() or '未知'}）"
                self.last_open_reason = (
                    f"找不到联系人 {contact!r}：{page}；"
                    f"当前可见联系人: {'、'.join(visible) or '(无)'}"
                    "（可改 config.yaml -> target.xtc_contact 或 ui.contact_name_ids）")
                # 轮询每 2 秒一次，这类失败会连续发生 -> 同因 5 分钟只报一次
                self._warn_throttled("open_chat_not_found", self.last_open_reason)
                return False
            self.adb.tap_element(node)

            # 等待进入聊天；若误入别的页面/弹窗遮挡，先清弹窗再点一次
            deadline = time.time() + 12
            reclick = 0
            while time.time() < deadline:
                if self.is_in_chat():
                    return True
                act = self.current_activity()
                if (not act.startswith(self.package)) or "WatchMsg" in act or reclick < 2:
                    if self._dismiss_blockers():
                        time.sleep(0.5)
                    if reclick < 2:
                        root = self._dump_with_retry(1)   # 读不到不影响本轮继续等待
                        if root is not None:
                            node = self._find_contact_node(root, contact)
                            if node is not None:
                                self.adb.tap_element(node)   # 弹窗关掉后重新点联系人
                        reclick += 1
                time.sleep(0.8)
            self.log("warning", f"点击联系人 {contact} 后未检测到聊天窗口（可能 App 界面有弹窗）")
            return False
        except AdbError as e:
            self.log("warning", f"打开聊天失败: {e}")
            return False

    # ---- 联系人查找辅助 ----
    @staticmethod
    def _norm_name(text: str) -> str:
        """比较用归一化：去空白（含全角空格）、统一小写。"""
        return re.sub(r"[\s\u3000]+", "", (text or "")).casefold()

    def _contact_candidates(self, contact: str) -> list[str]:
        """联系人名的候选写法：原名/去空格；"张三(爸爸)" 这类再取括号前的部分。"""
        raw = (contact or "").strip()
        cands = [raw]
        for sep in ("(", "（", "[", "【", "<", "《", "-", "_", "|"):
            if sep in raw:
                head = raw.split(sep, 1)[0].strip()
                if head:
                    cands.append(head)
        cands += [c.replace(" ", "").replace("\u3000", "") for c in list(cands)]
        out: list[str] = []
        for c in cands:
            if c and c not in out:
                out.append(c)
        return out

    @staticmethod
    def _name_matches(row_name: str, candidates: list[str]) -> bool:
        """行内名字与候选名是否算同一个：全等，或（双方都不太短时）互相包含。"""
        n = Xiaotiancai._norm_name(row_name)
        if not n:
            return False
        for c in candidates:
            nc = Xiaotiancai._norm_name(c)
            if not nc:
                continue
            if n == nc:
                return True
            short, long_ = (n, nc) if len(n) <= len(nc) else (nc, n)
            if len(short) >= 2 and short in long_:
                return True
        return False

    def _clickable_ancestor(self, node, root: ET.Element, max_up: int = 5):
        """往上找可点击的祖先（列表行本身往往才是可点击节点）。"""
        cur = node
        for _ in range(max_up):
            if (cur.get("clickable", "") or "") == "true":
                return cur
            parent = self._parent(cur, root)
            if parent is None:
                break
            cur = parent
        return None

    def _visible_contact_names(self, root: ET.Element) -> list[str]:
        """当前界面上的联系人名（诊断用：找不到时告诉用户"我看到了谁"）。

        先按已知 id 取；再按"预览行"结构取（不同版本联系人名控件的 id 可能不一样，
        只认 id 会让诊断信息变成"空"，反而误导排查）。
        """
        ids = set(self.ui.get("contact_name_ids", list(_CONTACT_NAME_IDS)))
        out: list[str] = []
        for n in root.iter("node"):
            if self._id_tail(n) in ids:
                t = (n.get("text") or "").strip()
                if t and t not in out:
                    out.append(t)
        if not out:
            for anchor in self._row_anchors(root):
                name = self._row_name_of(anchor, root)
                if name and name not in out:
                    out.append(name)
        return out

    def _row_anchors(self, root: ET.Element) -> list:
        """消息列表行的锚点节点（"最后一条消息预览"）。"""
        previews = set(self.ui.get("contact_preview_ids", list(_CONTACT_PREVIEW_IDS)))
        return [n for n in root.iter("node") if self._id_tail(n) in previews]

    def _row_name_of(self, anchor, root: ET.Element) -> str:
        """从"预览"节点所在的行里取出联系人名。

        ① 同行里已知的联系人名 id；② 退化为"行内位于预览上方、最靠上的文本"
        （列表行结构：名字在上、预览在下），这样即使 id 变了也能匹配/诊断。
        """
        parent = self._parent(anchor, root)
        if parent is None:
            return ""
        name_ids = set(self.ui.get("contact_name_ids", list(_CONTACT_NAME_IDS)))
        for n in parent.iter("node"):
            if self._id_tail(n) in name_ids and (n.get("text") or "").strip():
                return (n.get("text") or "").strip()
        preview_text = (anchor.get("text") or "").strip()
        anchor_b = self._bounds(anchor)
        best = None
        best_y = None
        for n in parent.iter("node"):
            t = (n.get("text") or "").strip()
            if not t or t == preview_text:
                continue
            b = self._bounds(n)
            if b is None:
                continue
            if anchor_b is not None and b[3] > anchor_b[1]:
                continue                      # 在预览下方的不算（多为时间/未读角标）
            if best is None or (best_y is not None and b[1] < best_y):
                best, best_y = n, b[1]
        return ((best.get("text") or "").strip() if best is not None else "")

    def _find_contact_node(self, root: ET.Element, contact: str):
        """在消息列表里找联系人节点。

        顺序：① 列表行的联系人名节点（id 命中，最可靠，支持别名/空格差异）
              ② 按"预览行"结构找（联系人名 id 与预期不同也能命中）
              ③ 任意节点的 text / content-desc 包含匹配
        找到后尽量返回**可点击的祖先**（点行本身才稳定，点小文字有时无效）。
        """
        candidates = self._contact_candidates(contact)
        name_ids = set(self.ui.get("contact_name_ids", list(_CONTACT_NAME_IDS)))
        for n in root.iter("node"):
            if self._id_tail(n) not in name_ids:
                continue
            if self._name_matches(n.get("text", ""), candidates):
                return self._clickable_ancestor(n, root) or n
        for anchor in self._row_anchors(root):
            if self._name_matches(self._row_name_of(anchor, root), candidates):
                return self._clickable_ancestor(anchor, root) or anchor
        for c in candidates:
            n = self._first(
                self.adb.find_element(root, text=c, text_contains=True),
                self.adb.find_element(root, content_desc=c))
            if n is not None:
                return self._clickable_ancestor(n, root) or n
        return None

    def _switch_to_message_tab(self, root: ET.Element):
        """切到"微聊/消息"页；成功返回新的界面快照，否则 None。

        文案可通过 ui.message_tab_text / ui.message_tab_texts 配置（不同版本叫法不同）。
        """
        texts = self.ui.get("message_tab_texts") or [self.ui.get("message_tab_text", "微聊")]
        for t in [x for x in texts if x]:
            tab = self._first(
                self.adb.find_element(root, text=t),
                self.adb.find_element(root, content_desc=t))
            if tab is None:
                continue
            self.log("debug", f"切换到 {t} 页")
            self.adb.tap_element(tab)
            time.sleep(self._delay)
            return self._dump_with_retry(2)
        return None

    def _scroll_find_contact(self, contact: str, root: ET.Element,
                             max_swipes: int = 3) -> tuple:
        """联系人可能在屏幕外：在消息列表里向上滑几次找它。返回 (节点或None, 见过的名字)。

        只在"确实是消息列表"时调用（调用方已判断），避免在别的页面乱滑。
        """
        seen = self._visible_contact_names(root)
        try:
            w, h = self.adb.get_screen_size()
        except AdbError:
            return None, seen
        for i in range(max_swipes):
            try:
                self.adb.swipe(w // 2, int(h * 0.72), w // 2, int(h * 0.32), 300)
            except Exception as e:  # noqa: BLE001 个别实现/假设备没有 swipe 时不影响查找
                self.log("debug", f"滑动查找联系人失败（跳过）: {e}")
                break
            time.sleep(0.5)
            cur = self._dump_with_retry(1)
            if cur is None:
                break
            for name in self._visible_contact_names(cur):
                if name not in seen:
                    seen.append(name)
            node = self._find_contact_node(cur, contact)
            if node is not None:
                self.log("info", f"向上滑动 {i + 1} 次后在列表里找到联系人 {contact!r}")
                return node, seen
        return None, seen

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
        return self._input_text_of(edit)

    def input_verifier(self, text: str):
        """给 adb.input_text 用的校验回调：输入框里已出现目标文本，或发送按钮已出现
        （小天才 App 只在输入框有内容时才显示发送按钮）即视为注入成功。"""
        needle = (text or "").strip()

        def _verify() -> bool:
            try:
                root = self._dump_fast()
                self._last_verify_root = root        # 供 _send_once 复用，省一次 dump
            except AdbError:
                return True  # dump 失败无法判定，不阻塞发送流程
            edit = self._find_input(root)
            if edit is not None:
                cur = self._input_text_of(edit)
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
        cur = self._input_text_of(edit).strip()
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

        速度（WSA 上一次 uiautomator dump 约 3 秒，所以**数 dump 次数**才是关键）：
        - 发送前的基线优先复用轮询刚 dump 的那份快照（少 1 次）；
        - 布局没变时走**快路径**：直接广播注入 + 点缓存的发送按钮坐标，
          再用一次 dump 同时确认"注入成功 + 发送成功"（少 1 次）；
        - 快路径不成立/没确认成功时退回"注入并校验"的稳妥流程（首次发送、布局变了、
          或注入没生效时走这条），结果照样如实判定。
        """
        try:
            # 0) 先确保屏幕是亮的：WSA 大约每分钟就会把虚拟屏睡一次（stayOn 也挡不住），
            #    息屏时 dump 必失败（null root node），整条发送就要多花 10 秒以上。
            #    这一步只花 ~0.2 秒，能把"发送撞上息屏"的概率压到很低。
            try:
                self.adb.wake_if_asleep()
            except Exception:  # noqa: BLE001 唤醒失败也照常往下走
                pass
            # 1) 发送前基线：优先复用很新的界面快照（轮询每 2 秒 dump 一次）
            root = self.recent_snapshot()
            if root is None or self._find_input(root) is None:
                root = self._dump_with_retry(2)   # 读不到界面（界面不空闲）时自动收键盘重试
                if root is None or self._find_input(root) is None:
                    # 找不到输入框：可能有弹窗盖住了聊天页，先清理再重读界面
                    if self._dismiss_blockers():
                        root = self._dump_with_retry(1)
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
            input_bounds = self._bounds(input_node)
            base_fail = self._fail_signature(root)
            base_own = self._own_bubble_counter(root)
            self._clear_chat_input(root)
            self._focus_input(input_node)
            # 2) 快路径：布局与上次一致 -> 直接用缓存的发送按钮坐标
            point = self._cached_send_point(input_bounds) if self._fast_send else None
            if point is not None and self.adb.input_text_plain(text):
                time.sleep(0.25)
                self.adb.tap(point[0], point[1])
                ok, why = self._confirm_sent(text, base_fail, base_own)
                if ok:
                    return True, why
                # 只有"输入框仍留有内容"才能安全重来（说明没发出去）；
                # 其它情况（气泡/界面不明）不重试，免得把消息发两遍。
                if "输入框仍留有内容" not in why:
                    return False, why
                self.log("debug", f"快路径发送未确认（{why}），改用带校验的流程重试")
                self._send_cache = None
                # 盲点可能点到了别的控件（例如打开了表情面板）：先清一遍弹层，
                # 再重读界面按残留处理，避免拼在新消息前面。
                self._dismiss_blockers()
                try:
                    root = self._dump_fast()
                    fresh_input = self._find_input(root)
                    if fresh_input is not None:
                        self._clear_chat_input(root)
                        input_node = fresh_input
                        input_bounds = self._bounds(fresh_input)
                except AdbError:
                    pass
            elif point is not None:
                self.log("debug", "纯注入广播未发出，改用带校验的注入流程")
            # 3) 稳妥路径：注入并校验（这次 dump 同时用来找发送按钮），点完再确认
            self._focus_input(input_node)
            if not self.adb.input_text(text, verify=self.input_verifier(text)):
                # 注入失败最常见的原因是"输入框没拿到输入连接"（点一下没生效、
                # 输入法没挂上）。这时反复发 ADBKeyBoard 广播是没用的（没人接收），
                # 必须重新点击输入框拿到焦点后再注入一次。
                self.log("warning", "文本注入未生效，重新聚焦输入框后再注入一次")
                try:
                    fresh = self._dump_fast()
                except AdbError:
                    fresh = root
                self._focus_input(self._find_input(fresh) or input_node)
                if not self.adb.input_text(text, verify=self.input_verifier(text)):
                    return False, "文本注入失败（输入框未收到内容）"
            time.sleep(0.3)
            # 复用注入校验时那次界面快照来找发送按钮，省掉一次 dump（WSA 上约 3 秒）
            send_root = self._last_verify_root
            self._last_verify_root = None
            if send_root is None:
                try:
                    send_root = self._dump_fast()
                except AdbError:
                    send_root = root
            send_node = self._find_send(send_root)
            if send_node is None:
                # 快照可能是息屏/过渡帧里读到的（没有发送按钮）：先唤醒再重读一次，
                # 别拿旧快照去点，也别糊里糊涂按回车（实测这一下能省一整轮 10 秒）。
                try:
                    if self.adb.wake_if_asleep():
                        again = self._dump_with_retry(1)
                        if again is not None:
                            send_root = again
                            send_node = self._find_send(send_root)
                except Exception:  # noqa: BLE001 重读失败就按原逻辑兜底
                    pass
            if send_node is None:
                self.log("warning", "未找到发送按钮，改用回车发送")
                self.adb.keyevent(66)  # KEYCODE_ENTER
            else:
                self._tap_send(send_node, send_root)
                self._remember_send_point(input_bounds, send_node, send_root)
            return self._confirm_sent(text, base_fail, base_own)
        except AdbError as e:
            return False, f"ADB 异常: {e}"

    # ---- 发送按钮坐标缓存（快路径用；布局变了就失效） ----
    @staticmethod
    def _same_bounds(a, b, tol: int = 12) -> bool:
        """两处控件位置是否基本一致（允许 tol 像素误差）。"""
        if not a or not b or len(a) != 4 or len(b) != 4:
            return False
        try:
            return all(abs(int(x) - int(y)) <= tol for x, y in zip(a, b))
        except (TypeError, ValueError):
            return False

    def _cached_send_point(self, input_bounds):
        """缓存的发送按钮可点坐标；**仅当输入框位置与缓存时一致**才返回

        为什么要这个判断：WSA 窗口能被缩放/最大化，布局一变，缓存下来的绝对坐标
        可能落到别的控件上（例如"更多"）。位置对不上就返回 None，退回
        "注入后 dump 找按钮"的稳妥流程，宁可慢一点也不乱点。

        已知残余风险（可接受）：位置比对用的是**最近快照**，所以"刚缩放完窗口、还没
        轮到下一次轮询 dump 就发消息"这一种情况仍可能按旧坐标点一次；点完的确认 dump
        会发现没发出去，随后清掉缓存并退回稳妥流程（多余点开的弹层也会被清掉），
        所以最坏是多花一次 dump，不会静默发错。
        """
        cache = self._send_cache
        if not cache or not input_bounds:
            return None
        if not self._same_bounds(cache.get("input"), input_bounds):
            return None
        point = cache.get("point")
        if not point:
            return None
        try:
            w, h = self.adb.get_screen_size()
            if not (0 <= int(point[0]) <= int(w) and 0 <= int(point[1]) <= int(h)):
                return None
        except Exception:  # noqa: BLE001 拿不到屏幕尺寸就不冒险
            return None
        return (int(point[0]), int(point[1]))

    def _remember_send_point(self, input_bounds, send_node, root: ET.Element) -> None:
        """记下这次点成功的发送按钮坐标 + 输入框坐标，供下次"先手打字"用。"""
        try:
            target = send_node
            if str(send_node.get("clickable", "")).lower() != "true":
                target = self._clickable_ancestor(send_node, root) or send_node
            center = self.adb.node_center(target) or self.adb.node_center(send_node)
            if center and input_bounds:
                x1, y1, x2, y2 = (int(v) for v in input_bounds)
                self._send_cache = {
                    "input": (x1, y1, x2, y2),
                    "point": (int(center[0]), int(center[1])),
                    "input_point": ((x1 + x2) // 2, (y1 + y2) // 2),
                }
        except Exception as e:  # noqa: BLE001 缓存失败不影响发送
            self.log("debug", f"发送按钮坐标缓存失败: {e}")

    # ---- "先手打字"（免 dump 预输入）：QQ -> 小天才 的可见延迟从 ~10 秒降到 ~1 秒 ----
    def chat_title(self, root: ET.Element | None = None) -> str:
        """当前聊天页的标题（联系人名）。用于确认"现在打开的确实是目标联系人"。"""
        try:
            if root is None:
                root = self._dump_fast()
            for n in root.iter("node"):
                if self._id_tail(n) in ("tv_titleBar_title", "tv_title", "chat_title"):
                    t = (n.get("text") or "").strip()
                    if t:
                        return t
                    d = (n.get("content-desc") or "").strip()
                    if d.startswith("和") and d.endswith("的聊天"):
                        return d[1:-3].strip()
        except Exception:  # noqa: BLE001 标题读不到就算了（调用方据此不放行快发）
            pass
        return ""

    def blind_send_ready(self) -> bool:
        """能不能"先手打字"：需要上次成功发送留下的坐标缓存 + 一份可用的界面快照。

        坐标缓存由**上一次真正发成功**的发送留下（重启后第一条走稳妥流程），
        快照用轮询刚 dump 的那份（只为拿"发送前基线"，不做任何 UI 操作）。
        `ui.blind_send: false` 可彻底关掉这条快路径。
        """
        if not self._blind_enabled:
            return False
        cache = self._send_cache or {}
        if not cache.get("point") or not cache.get("input_point"):
            return False
        return self.recent_snapshot() is not None

    def begin_blind_send(self, text: str) -> tuple[bool, str]:
        """先手把文字打进去（**不占桥接的操作锁、不读界面**）：文字 ~1 秒内出现在输入框。

        为什么快：稳妥流程要先等轮询那次 ~3.8 秒的 dump（操作锁）再自己读一次界面，
        文字才被注入；这里直接用缓存坐标点输入框 -> 广播注入 -> 点缓存坐标的发送按钮。
        正确性靠两点保证：
          * 调用方（桥接）只在"最近一次轮询确认过就是目标聊天页"时才走这条（含标题校验）；
          * 随后的 `end_blind_send()` 会用一次 dump 复核，失败就退回稳妥流程 + 清缓存。
        """
        cache = self._send_cache or {}
        point_in = cache.get("input_point")
        point_send = cache.get("point")
        if not point_in or not point_send:
            return False, "还没有发送坐标缓存（先走稳妥流程建立）"
        base = self.recent_snapshot()
        if base is None:
            return False, "没有可用的界面快照（拿不到发送前基线）"
        self._blind = {
            "text": text,
            "base_fail": self._fail_signature(base),
            "base_own": self._own_bubble_counter(base),
            "retryable": False,
        }
        try:
            # 屏可能刚睡过去（WSA 随宿主窗口状态休眠虚拟屏）：先确认点亮，
            # 否则点下去是打在 WSA 主屏的空壳上、文字进不了输入框（白费一轮复核）。
            try:
                self.adb.wake_if_asleep()
            except Exception:  # noqa: BLE001 唤醒失败也照常试
                pass
            self.adb.tap(point_in[0], point_in[1])          # 点输入框拿焦点
            time.sleep(min(self._delay, 0.4))
            if not self.adb.input_text_plain(text):         # 广播注入（不校验）
                self._blind = None
                return False, "纯注入广播未发出"
            time.sleep(0.15)
            self.adb.tap(point_send[0], point_send[1])      # 点发送（缓存坐标）
            self._blind["retryable"] = True
            return True, ""
        except AdbError as e:
            self._blind = None
            return False, f"ADB 异常: {e}"

    def end_blind_send(self, text: str) -> tuple[bool, str, bool]:
        """复核"先手打字"的结果：返回 (是否确认发出, 说明, 失败时能否安全重发)。

        用一次 dump 同时判断：出现新己方气泡（最强证据）/ 出现新失败提示 /
        输入框仍留有内容（没发出去，可安全重发）/ 输入框已清空且无新失败提示。
        """
        blind = self._blind or {}
        self._blind = None
        if not blind:
            return False, "没有待复核的先手发送", False
        try:
            ok, why = self._confirm_sent(text, blind.get("base_fail") or [],
                                         blind.get("base_own") or {})
        except AdbError as e:
            return False, f"复核失败: {e}", False
        retryable = "输入框仍留有内容" in (why or "")
        if not ok and retryable:
            # 没发出去（文字还在输入框里）：清掉缓存坐标，让稳妥流程重来
            self._send_cache = None
        return ok, why, retryable

    def learn_send_point(self, probe: str = "a") -> bool:
        """启动时用一次"探针注入"学出发送按钮坐标（不会发出任何消息）。

        为什么需要：先手打字靠**发送按钮的坐标**，而它只在输入框有内容时才出现 ——
        不先学一次的话，**重启后的第一条** QQ 消息只能走稳妥流程（两次 dump）。
        这里往输入框注入一个探针字符 -> dump 一次 -> 记下输入框与发送按钮坐标 ->
        立刻清空输入框。全程只输入、不点发送，所以不会有任何消息被发出去。
        """
        try:
            root = self.recent_snapshot() or self._dump_with_retry(1)
            edit = self._find_input(root) if root is not None else None
            if edit is None:
                return False
            bounds = self._bounds(edit)
            self._focus_input(edit)
            if not self.adb.input_text_plain(probe):
                return False
            time.sleep(0.25)
            snap = self._dump_with_retry(1)          # 有内容了 -> 发送按钮出现
            if snap is None:
                return False
            send_node = self._find_send(snap)
            if send_node is not None and bounds:
                self._remember_send_point(bounds, send_node, snap)
            # 清掉探针字符：绝不留内容在输入框里（否则会被拼进下一条消息）
            self._clear_chat_input(self._dump_with_retry(1) or snap)
            done = self._send_cache is not None
            self.log("info" if done else "debug",
                     "已学出发送按钮坐标（先手打字可用）" if done
                     else "没能学出发送按钮坐标（首次发送仍走稳妥流程）")
            return done
        except Exception as e:  # noqa: BLE001 学不到就退回稳妥流程
            self.log("debug", f"学习发送按钮坐标失败: {e}")
            return False

    def _focus_input(self, edit) -> None:
        """点击输入框让它获得焦点，并等软键盘/输入连接就绪。

        输入框没有焦点时，ADBKeyBoard 的广播会"发出去了但没人接收"，
        表现就是"文本注入失败（输入框未收到内容）"。
        """
        if edit is None:
            return
        self.adb.tap_element(edit)
        time.sleep(self._delay)

    def _confirm_sent(self, text: str, base_fail: list, base_own: dict) -> tuple[bool, str]:
        """发送后确认（通常只 dump 一次，约 3 秒）。返回 (是否确认发出, 说明)。

        第一次 dump 前先等 `_confirm_delay`：点发送后 App 要一点时间才把气泡画出来，
        等太短会白 dump 一次（WSA 上一次 ~3 秒，白等就变成"发一条要十几秒"）。
        后面几次只在前一次证据不足时才继续。
        """
        last = "界面读取失败"
        base_fail_cnt = self._counter(base_fail)
        for i in range(3):
            time.sleep(self._confirm_delay if i == 0 else 0.5)
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
            cur = self._input_text_of(edit)
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

    def _input_text_of(self, edit) -> str:
        """输入框的**真实**文本：把空输入框显示的占位提示（hint）当成空串。

        实机（WSA + 小天才）测到：输入框为空时 dump 出的 text 是 "发送文字"，
        旧逻辑会把它当作用户残留内容，于是反复清空/影响发送确认判定。
        """
        t = (edit.get("text", "") or "") if edit is not None else ""
        hints = tuple(self.ui.get("input_hint_texts", _DEFAULT_INPUT_HINTS))
        return "" if t.strip() in hints else t

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
        """找"发送"控件。必须**只**认真正的发送控件，不能误认消息状态图标。

        实机（WSA + 小天才，1366x768）实测：
        - 输入框有内容时：`tv_send_view`（TextView text=发送，本身 clickable=false，
          真正可点的是它的父 `fl_chat_text_send`）；
        - 输入框为空时：**没有** tv_send_view；此时旧的 content-desc 子串匹配会命中
          `iv_chat_msg_item_state`（每条消息右侧的"发送中/发送失败"状态图标），
          于是"没输入内容也以为发送按钮存在"——注入其实没成功却继续点它，
          表现为"莫名其妙发送失败"。所以这里排除状态图标，且 desc 必须完全相等。
        """
        rid = self.ui.get("send_resource_id", "")
        if rid:
            n = self.adb.find_element(root, resource_id=rid)
            if n is not None:
                return n
        # 1) 已知发送控件 id（不依赖文案，最稳）
        for n in root.iter("node"):
            tail = self._id_tail(n).lower()
            if any(x in tail for x in _SEND_EXCLUDE_IDS):
                continue
            if any(x in tail for x in _SEND_ID_HINTS):
                return n
        # 2) 精确文本"发送"（不能用 text_contains：会命中输入框"发送文字"提示）
        for t in self.ui.get("send_texts", ["发送"]):
            n = self.adb.find_element(root, text=t)
            if n is not None:
                return n
        # 3) 最后才看 content-desc，且必须完全相等 + 不是消息状态图标
        for t in self.ui.get("send_texts", ["发送"]):
            n = self.adb.find_element(root, content_desc=t, content_desc_contains=False)
            if n is not None and not any(
                    x in self._id_tail(n).lower() for x in _SEND_EXCLUDE_IDS):
                return n
        return None

    def _tap_send(self, send_node, root: ET.Element) -> None:
        """点发送：优先点它自己，不可点则点最近的可点祖先（实测 tv_send_view
        的 clickable=false，父 fl_chat_text_send 才接收点击）。"""
        target = send_node
        try:
            if str(send_node.get("clickable", "")).lower() != "true":
                target = self._clickable_ancestor(send_node, root) or send_node
        except Exception:  # noqa: BLE001 拿不到祖先就点自己
            target = send_node
        self.adb.tap_element(target)

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
            # 已经空了就不要重复点键盘（少一次点击/40 次按键）；占位提示文案算"空"
            if self._input_text_of(edit).strip():
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
            state = self.login_state(force=True)
            if state == self.NOT_LOGGED_IN:
                steps.append("未登录（等待自动登录/手动登录）")
                return "，".join(steps)
            if state == self.LOGIN_UNKNOWN:
                steps.append("无法确认登录态（界面读不到/App 不在前台），未做登录动作")
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
    def get_latest_message(self, root: ET.Element | None = None):
        """返回 (contact, text, time_label, own_text, own_recent)；无法确定时
        (None, None, "", "", [])。

        root: 可传入调用方已经读好的界面快照（轮询层会先 app_state 判状态再复用），
              省掉一次 uiautomator dump——WSA 上单次 dump 要 3 秒左右。

        own_text = 最新一条"自己发的"消息文本；
        own_recent = 最近若干条自己发的消息 [(text, time_label)]（新->旧，供 xtc 侧
        命令检测；命令可能被送达确认等新消息盖过，需扫最近几条；时间标签用于区分
        同文本的再次输入）。复用同一次 dump，避免额外开 uiautomator dump。不抛异常。

        只在聊天窗口内读取——列表预览无法可靠判断发送方（家长侧手动发送的消息
        也会出现在预览里），会被误当成对方消息转发。轮询层负责确保聊天窗口已打开。"""
        try:
            if not self.require_login(root):
                return (None, None, "", "", [])
            # 一次 dump 同时用于登录态判断与消息解析（登录态有缓存，避免重复 dump）；
            # 读不到界面（界面一直不空闲等）→ 返回空，交给轮询层稍后重试/自愈，
            # 不抛异常也不刷 ERROR 日志。
            root = root if root is not None else self._dump_with_retry(2)
            if root is None:
                return (None, None, "", "", [])
            if self.is_in_chat(root):
                contact, text, time_label = self._latest_in_chat(root)
                # 偶发的"不完整界面快照"：聊天里有消息，但一个时间标签都没有。
                # 这时直接转发会退化成"当前时间"（用户报告过 8 点的消息被按当前时间
                # 转发）。重读一两次通常就有标签了；20 秒内只补救一次，避免拖慢轮询。
                if (text and not time_label and self._date_count(root) == 0
                        and time.monotonic() - self._last_label_retry >= 20):
                    self._last_label_retry = time.monotonic()
                    for _ in range(2):
                        time.sleep(0.4)
                        again = self._dump_with_retry(1)
                        if again is None or not self.is_in_chat(again):
                            break
                        c2, t2, l2 = self._latest_in_chat(again)
                        if l2:
                            root = again                     # 用更完整的那次快照
                            contact, text, time_label = (c2 or contact), t2, l2
                            break
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
        # 收集时间标签（按 y 排序，供气泡取最近上方标签）；压缩 dump 里退化成从
        # 父容器 desc 还原（见 _time_labels），否则时间会全变成"当前时间"
        dates = self._time_labels(root)
        # 发送失败提示条（网络异常等）：下方带该提示的气泡 = 未送达，跳过
        fail_hints = []
        for n in root.iter("node"):
            if self._id_tail(n) in _TIP_IDS:
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
        # 归属判定要知道**所有**气泡（含自己发的），所以单独收集一份
        all_bubbles = [x for x in root.iter("node")
                       if self._id_tail(x) == "chat_msg_item_content"
                       and self._bounds(x) is not None]
        return (None, t, self._label_for_bubble(n, dates, all_bubbles))

    def _label_for_bubble(self, bubble, dates: list, all_bubbles: list | None = None) -> str:
        """取**这条消息自己的**时间（dates = [(top, bottom, text)]，按 top 升序）。

        小天才只在一个"时间组"的第一条消息上方画一个时间标签，组内后续消息
        **没有**自己的标签。所以规则是两条：
        ① 找位于该气泡上方、最靠下的那个标签；
        ② 这个标签必须**真的属于它**——标签下方第一条气泡必须是它自己。
           否则说明标签是上面那条消息的，本条没有自己的时间，返回 ""。

        为什么必须做 ②：没有它就会出现"三条消息全是 23:47"——实测日志
            [09-20 23:47] test / 我去？ / 晚安
        三条不同消息（检测时刻 23:50、23:52、23:53）都被安上了组首的 23:47。
        返回 "" 时上层用"当前时间"，对刚收到的消息就是它自己的到达时间，
        每条各不相同（这也是 09-15 老版本的行为）。
        """
        b = self._bounds(bubble)
        if not b or not dates:
            return ""
        top = b[1]
        chosen = None
        for d_top, d_bottom, d_text in reversed(dates):   # dates 按 top 升序
            if d_bottom <= top + 5:                       # 标签底边在气泡上方
                chosen = (d_top, d_bottom, d_text)
                break
        if chosen is None:
            return ""
        if not all_bubbles:            # 拿不到其它气泡时退回旧行为
            return chosen[2]
        label_bottom = chosen[1]
        below = [ob[1] for ob in (self._bounds(x) for x in all_bubbles)
                 if ob is not None and ob[1] >= label_bottom - 5]
        if below and min(below) != top:
            return ""                  # 这个标签是它上面那条消息的
        return chosen[2]

    def _date_count(self, root: ET.Element) -> int:
        return len(self._time_labels(root))

    @staticmethod
    def _cn_numeral(token: str) -> str:
        """还原 app 无障碍文案里的"中文式"数字：十1->11、十9->19、2十->20、4十6->46。

        实机 ll_chat_top_layout 的 content-desc 就是这种写法（'十1点十9分' 对应 11:19）。
        认不出来返回 ""（宁可不给，也不给错的时间）。
        """
        token = (token or "").strip()
        if not token:
            return ""
        if "十" not in token:
            return token if token.isdigit() else ""
        if token == "十":
            return "10"
        head, _, tail = token.partition("十")
        if head and not head.isdigit():
            return ""
        tens = int(head) if head else 1
        if not tail:
            return str(tens * 10)
        if not tail.isdigit():
            return ""
        return str(tens * 10 + int(tail))

    @classmethod
    def _label_from_desc(cls, desc: str) -> str:
        """把时间标签的 content-desc 还原成 "HH:MM"（认不出来返回 ""）。

        为什么需要：WSA 上默认的 `--compressed` dump **不含** tv_chat_msg_item_date
        节点（实测同一屏：压缩版 0 个时间标签，完整版 3 个），只剩父容器
        ll_chat_top_layout 的 content-desc；不还原的话每条消息的时间都会退化成
        "当前时间"（用户报的"消息时间不对"）。
        """
        s = (desc or "").strip()
        if not s:
            return ""
        m = re.match(r"^([0-9十]{1,3})点(?:([0-9十]{1,3})分?)?$", s)
        if not m:
            return ""
        hh = cls._cn_numeral(m.group(1))
        mm = cls._cn_numeral(m.group(2)) if m.group(2) else "0"
        if not hh or not mm:
            return ""
        hi, mi = int(hh), int(mm)
        if not (0 <= hi <= 23 and 0 <= mi <= 59):
            return ""
        return f"{hi:02d}:{mi:02d}"

    def _time_labels(self, root: ET.Element) -> list[tuple[int, int, str]]:
        """聊天页里的时间标签 [(top, bottom, text)]，按从上到下排序。

        首选 tv_chat_msg_item_date 的文本（完整 dump 里有，形如 "11:19"/"昨天 23:42"）；
        没有这些节点时（压缩 dump）退化成从 ll_chat_top_layout 的 content-desc 还原。
        """
        labels = []
        for n in root.iter("node"):
            if self._id_tail(n) != "tv_chat_msg_item_date":
                continue
            b = self._bounds(n)
            t = (n.get("text") or "").strip()
            if b and t:
                labels.append((b[1], b[3], t))
        if labels:
            return sorted(labels)
        for n in root.iter("node"):
            if self._id_tail(n) != "ll_chat_top_layout":
                continue
            b = self._bounds(n)
            t = self._label_from_desc(n.get("content-desc") or "")
            if b and t:
                labels.append((b[1], b[3], t))
        return sorted(labels)

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
        dates: list[tuple[int, int, str]] = self._time_labels(root)
        fail_hints: list[tuple[int, int]] = []
        for n in root.iter("node"):
            if self._id_tail(n) in _TIP_IDS:
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
            desc_body = ""
            if "发的消息" in desc:
                if desc.startswith("你发的"):
                    is_own = True
                else:
                    contact = desc.split("发的消息", 1)[0].strip()
                if "," in desc:
                    desc_body = desc.split(",", 1)[1].strip()
                if not t and desc_body:
                    t = desc_body                      # 表情/语音等从 desc 取类型
            else:
                # 无标注：右侧气泡 = 自己发的消息
                is_own = filter_own and center_x > screen_w * 0.55
            if not t or t in junk:
                continue
            # 表情/贴纸：气泡本身没有文字（文字消息的 text 一定非空），
            # content-desc 给的是"表情<名字>"，例如 '屑猹不喝茶发的消息,表情啊啊啊'。
            # 只按这个判，避免把"表情包发我"这种真的文字消息当成表情。
            is_sticker = bool(desc_body.startswith("表情")) and not (n.get("text", "") or "").strip()
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
                        "time_label": time_label, "y_bottom": b[3],
                        "sticker": is_sticker, "bounds": b})
        out.sort(key=lambda it: it["y_bottom"])
        return out

    def sticker_of_latest(self, root: ET.Element, text: str = "") -> dict | None:
        """最新一条**对方发来的表情**气泡信息 {text, bounds, time_label}；没有则 None。

        给桥接用：轮询读到"表情X"时，据此拿到气泡位置，把贴纸原样截图发给 QQ
        （见 capture_sticker）。text 传了就按文本匹配那一条（避免读到别的表情）。
        """
        try:
            items = self._chat_bubbles(root, include_own=False)
        except Exception as e:  # noqa: BLE001 判定失败不影响文字转发
            self.log("debug", f"解析表情气泡失败: {e}")
            return None
        want = (text or "").strip()
        hit = None
        for it in items:                      # 已按 y 从小到大（旧->新）
            if not it.get("sticker"):
                continue
            if want and it.get("text") != want:
                continue
            hit = it
        return hit

    def capture_sticker(self, bounds) -> bytes | None:
        """把表情气泡那块**截图抠成 PNG**（失败返回 None，调用方退回发文字）。

        为什么截图而不是拿原图：贴纸是 App 内部资源（可能在 so/apk 里、也可能是网络图），
        ADB 拿不到；而贴纸在屏幕上就是它本身，按气泡区域裁剪即得原图。
        只裁气泡本身的区域（不加内边距）：实机实测 ImageView 的 bounds 正好是贴纸范围。

        截图偶发失败（刚唤醒/界面正在重绘）时**重试一次**：实机遇到过"明明气泡就在屏幕上，
        却因为这一次 screencap 没成功而整条退化成了文字"。
        """
        if not bounds:
            return None
        for attempt in (1, 2):
            try:
                w, h = self.adb.get_screen_size()
                x1, y1, x2, y2 = (int(v) for v in bounds)
                if x2 - x1 < 12 or y2 - y1 < 12:
                    self.log("debug", f"表情气泡太小，放弃截图: {bounds}")
                    return None
                if x2 <= 0 or y2 <= 0 or x1 >= w or y1 >= h:
                    self.log("debug", f"表情气泡不在屏幕内，放弃截图: {bounds}")
                    return None
                png = self.adb.screencap_crop_png((max(0, x1), max(0, y1),
                                                   min(w, x2), min(h, y2)))
                if png and len(png) >= 200:
                    return png
                self.log("debug", f"表情截图内容异常（{len(png or b'')} 字节）"
                                  + ("，重试一次" if attempt == 1 else ""))
            except Exception as e:  # noqa: BLE001 截图失败不影响文字转发
                self.log("debug", f"表情截图失败（第 {attempt} 次）: {e}")
            if attempt == 1:
                time.sleep(0.4)
        return None

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
