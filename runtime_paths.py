# -*- coding: utf-8 -*-
"""运行路径解析：**源码运行 / Nuitka 冻结（standalone、onefile）都可用**。

为什么需要它
------------
Nuitka 的两种打包模式对"文件在哪"的答案完全不同（已实测）：

| 模式 | `__file__` 所在目录 | `sys.argv[0]` | 临时目录 |
|---|---|---|---|
| 源码运行 | 项目根目录 | `main.py` | 无 |
| `--mode=standalone` | `<dist>/xxx.dist/`（数据文件也在这里） | `<dist>/xxx.dist/xxx.exe` | 无 |
| `--mode=onefile` | **临时解包目录**（退出即删除） | **用户手上那个 exe 的真实路径** | 有 |

所以必须区分两种目录：

- **BUNDLE_DIR（只读资源）**：`Path(__file__).resolve().parent`。模板、APK、插件源码等
  用 `--include-data-files/-dir` 打进来的东西都在这里，两种模式都成立。
- **APP_DIR（可写数据）**：exe 所在目录（`sys.argv[0]`）。`config.yaml`、`logs/`、`data/`
  都放这里；**绝不能**用 `__file__`，否则 onefile 会写进临时目录并在退出时丢掉。
  APP_DIR 不可写时（例如放在 Program Files）退回到用户数据目录。

注意：`__compiled__.containing_dir` 在 standalone 模式下返回的是**上层输出目录**而不是
`.dist` 目录（实测），因此这里不用它，统一以 `sys.argv[0]` 为准。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "xtc-qq-bridge"
PLUGIN_NAME = "xtc_qq_bridge"
CONFIG_NAME = "config.yaml"
CONFIG_EXAMPLE_NAME = "config.example.yaml"
PLUGIN_SRC_DIR = "astrbot_plugin_xtc_bridge"
APK_NAMES = ("keyboardservice-debug.apk", "ADBKeyBoard.apk")


def _detect_frozen() -> tuple[bool, str]:
    """是否 Nuitka 冻结运行，以及模式（onefile / standalone）。"""
    compiled = globals().get("__compiled__")
    if compiled is not None:
        return True, ("onefile" if getattr(compiled, "onefile", False) else "standalone")
    if getattr(sys, "frozen", False):     # 其它打包器（PyInstaller 等）的兜底
        return True, "onefile"
    return False, ""


IS_FROZEN, FROZEN_MODE = _detect_frozen()

# 只读资源目录（打包进来的 config.example.yaml / *.apk / 插件源码都在这里）
BUNDLE_DIR: Path = Path(__file__).resolve().parent


def _launcher_exe() -> Path | None:
    """用户实际双击/执行的那个可执行文件（onefile 下是真实 exe，不是临时目录里的 python）。"""
    argv0 = (sys.argv[0] if sys.argv else "") or ""
    if argv0:
        try:
            p = Path(argv0)
            if p.is_file():
                return p.resolve()
        except OSError:
            pass
        found = shutil.which(argv0)
        if found:
            return Path(found).resolve()
    return None


def _writable(d: Path) -> bool:
    """目录是否可写（写一个临时文件试一下；失败不抛异常）。"""
    probe = d / ".xtc_write_probe"
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False
    finally:
        try:
            if probe.exists():
                probe.unlink()
        except OSError:
            pass


def _user_data_dir() -> Path:
    """exe 所在目录不可写时的兜底数据目录（Windows: %LOCALAPPDATA%，Linux: XDG，macOS: Application Support）。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def _resolve_app_dir() -> tuple[Path, str]:
    """返回 (可写数据目录, 说明)。冻结时优先 exe 所在目录，源码运行时用项目根目录。"""
    if IS_FROZEN:
        exe = _launcher_exe()
        base = exe.parent if exe else Path(sys.executable).resolve().parent
        if _writable(base):
            return base, f"exe 所在目录（{FROZEN_MODE}）"
        fallback = _user_data_dir()
        return fallback, "exe 所在目录不可写，改用用户数据目录"
    base = BUNDLE_DIR
    if _writable(base):
        return base, "项目目录（源码运行）"
    return _user_data_dir(), "项目目录不可写，改用用户数据目录"


APP_DIR, APP_DIR_REASON = _resolve_app_dir()
DATA_DIR = APP_DIR / "data"
LOG_DIR = APP_DIR / "logs"


def ensure_dirs() -> None:
    """确保可写目录存在（日志/消息库/状态文件要用）。"""
    for d in (APP_DIR, DATA_DIR, LOG_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def resource(*parts: str) -> Path:
    """只读资源路径：BUNDLE_DIR -> APP_DIR -> 当前工作目录，返回第一个存在的。"""
    for base in (BUNDLE_DIR, APP_DIR, Path.cwd()):
        p = base.joinpath(*parts)
        if p.exists():
            return p
    return BUNDLE_DIR.joinpath(*parts)


def resource_dir(name: str) -> Path | None:
    p = resource(name)
    return p if p.is_dir() else None


def data_path(name: str) -> Path:
    return DATA_DIR / name


def log_path(name: str) -> Path:
    """相对路径按 APP_DIR 解析（保证 onefile 下日志写在 exe 旁边而不是临时目录）。"""
    p = Path(name)
    return p if p.is_absolute() else APP_DIR / p


def default_config_path() -> Path:
    return APP_DIR / CONFIG_NAME


def resolve_config(cli_value: str = "") -> Path:
    """配置文件路径：命令行 > APP_DIR > 当前目录 > 打包资源目录。"""
    if cli_value:
        return Path(cli_value).expanduser()
    for cand in (APP_DIR / CONFIG_NAME, Path.cwd() / CONFIG_NAME, BUNDLE_DIR / CONFIG_NAME):
        if cand.exists():
            return cand
    return APP_DIR / CONFIG_NAME


def ensure_config(path: Path) -> bool:
    """配置不存在时从打包的模板生成一份；返回是否新建了文件。

    生成的配置里是空值（占位），需要用户自己填 —— 与源码版的 install 脚本行为一致。
    """
    if path.exists():
        return False
    template = resource(CONFIG_EXAMPLE_NAME)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if template.exists():
            path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.write_text("# 配置模板缺失，请参考仓库里的 config.example.yaml\n",
                            encoding="utf-8")
        return True
    except OSError:
        return False


def looks_like_apk(p: Path) -> bool:
    """是不是一个（至少像样的）APK：zip 魔数 + 最小体积。

    之前用"体积 > 50KB"判断，而仓库里捆绑的 keyboardservice-debug.apk 只有约 18KB，
    导致**明明有 APK 却被判定为找不到**。这里改成校验 zip 魔数，避免再踩这个坑。
    """
    try:
        if not p.is_file() or p.stat().st_size < 1024:
            return False
        with p.open("rb") as fh:
            return fh.read(2) == b"PK"
    except OSError:
        return False


def bundled_apk() -> Path | None:
    """打包/项目目录里捆绑的 ADBKeyBoard APK（没有则 None，绝不联网下载）。"""
    for name in APK_NAMES:
        for base in (BUNDLE_DIR, APP_DIR, Path.cwd()):
            p = base / name
            if looks_like_apk(p):
                return p
    return None


def plugin_dir() -> Path:
    """AstrBot 插件安装目录（--install-plugin 的目标）。"""
    return Path.home() / ".astrbot" / "data" / "plugins" / PLUGIN_NAME


def describe() -> str:
    """打印运行环境与路径（`--paths` / 排障用）。"""
    lines = [
        "---- 运行路径 ----",
        f"运行方式      : {'Nuitka ' + FROZEN_MODE if IS_FROZEN else '源码（python）'}",
        f"Python        : {sys.version.split()[0]} ({sys.executable})",
        f"可执行文件    : {_launcher_exe() or '(未知)'}",
        f"资源目录      : {BUNDLE_DIR}",
        f"数据目录      : {APP_DIR}  [{APP_DIR_REASON}]",
        f"  可写        : {_writable(APP_DIR)}",
        f"配置          : {resolve_config()}",
        f"  已存在      : {resolve_config().exists()}",
        f"日志          : {LOG_DIR / 'bridge.log'}",
        f"消息库        : {DATA_DIR / 'msg_log.json'}",
        f"捆绑 APK      : {bundled_apk() or '(未找到)'}",
        f"插件目录      : {plugin_dir()}",
        "------------------",
    ]
    return "\n".join(lines)


if __name__ == "__main__":       # python runtime_paths.py 也能直接看路径
    ensure_dirs()
    print(describe())
