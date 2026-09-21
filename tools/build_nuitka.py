# -*- coding: utf-8 -*-
"""Nuitka 编译驱动：把桥接主程序与 WSA 守护编译成**机器码单文件**（Windows / Linux / macOS）。

为什么用一个 Python 驱动而不是一堆命令行
----------------------------------------
* 同一份构建逻辑跨平台复用（Windows 用 build.bat 调它，Linux/macOS 用 build.sh 调它）；
* 会自动探测 Nuitka 版本/接口（4.2+ 用 `--mode=onefile`，老版本用 `--onefile`）；
* 自动校验要用到的每个 Nuitka 选项（缺失就跳过，不再因为 Nuitka 升级改名而构建失败）；
* 内置前置检查（Python 版本 / pyyaml / C 编译器），错误信息直接给出解决办法；
* 统一产物命名 `<名字>-v<版本>-<系统>-<架构>[.exe]` 并生成 sha256。

用法
----
  python tools/build_nuitka.py --check-env            # 只检查编译环境
  python tools/build_nuitka.py --dry-run              # 只打印将要执行的命令（不构建）
  python tools/build_nuitka.py                        # 默认 onefile + bridge + guard
  python tools/build_nuitka.py --mode standalone      # 目录模式（启动更快，便于调试）
  python tools/build_nuitka.py --target bridge        # 只编主程序
  python tools/build_nuitka.py --target guard         # 只编 WSA 守护
  python tools/build_nuitka.py --out dist --jobs 8    # 指定输出目录与并行度
  python tools/build_nuitka.py --lto yes              # 开启 LTO（更慢的构建、更快的程序）

Windows 需要 Visual Studio 2022+（勾选"使用 C++ 的桌面开发"）；Python 3.13+ **不支持**
MinGW64，所以本驱动在 Windows 上默认走 MSVC。Linux 需要 gcc 与 python3-dev。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 非 UTF-8 控制台兜底：Windows CI 的 cp1252 下 print 中文会抛 UnicodeEncodeError，
# 直接把整个编译判失败（Windows 两个平台的 CI 就是这么挂的）。
try:
    from utils.logger import make_console_tolerant
    make_console_tolerant()
except Exception:  # noqa: BLE001
    pass

# ---------------------------------------------------------------- 常量
TARGETS = {
    # 名字: (入口脚本, 是否带控制台, 说明)
    "bridge": ("main.py", True, "小天才 <-> QQ 桥接主程序"),
    # 守护只适用于 Windows（WSA 是 Windows 独有组件）；其它平台会自动跳过
    "guard": ("tools/wsa_net_guard.py", True, "WSA / WSABuilds 网络守护（仅 Windows）"),
}
# 只读资源：源 -> 包内目标（--include-data-files 的 "源=目标" 形式）
#   注意：目标不能写 "."（Nuitka 会报 illegal suffix），必须是文件名/子目录名。
DATA_FILES = {
    "bridge": [
        ("config.example.yaml", "config.example.yaml"),
        ("keyboardservice-debug.apk", "keyboardservice-debug.apk"),
        ("README.md", "README.md"),
        ("LICENSE", "LICENSE"),
        ("requirements.txt", "requirements.txt"),
    ],
    "guard": [
        ("config.example.yaml", "config.example.yaml"),
        ("README.md", "README.md"),
        ("LICENSE", "LICENSE"),
    ],
}
# 需要整目录打进包里的资源（**逐文件**添加，见 _data_dir_args 的原因说明）
DATA_DIRS = {
    "bridge": [("astrbot_plugin_xtc_bridge", "astrbot_plugin_xtc_bridge")],
    "guard": [],
}
# 函数内 import / 可选 import 的模块，显式声明，避免被静态分析漏掉（按目标裁剪，控制体积）
INCLUDE_MODULES = {
    "bridge": [
        "adb_controller", "bridge", "xiaotiancai", "plugin_client", "qq_webhook", "msg_log",
        "runtime_paths", "version", "utils.logger", "utils.deduplicate", "tools.dump_ui",
    ],
    "guard": [
        "adb_controller", "runtime_paths", "version", "utils.logger",
    ],
}
# 不需要进产物（开发/测试用），显式排除以缩小体积、避免拉进 pytest 之类
EXCLUDE_MODULES = {
    "bridge": [
        "tools.test_wsa", "tools.test_integration", "tools.test_reported_bugs",
        "tools.test_paths", "tools.build_nuitka", "tools.wsa_net_guard", "tools.selftest",
    ],
    "guard": [
        "tools.test_wsa", "tools.test_integration", "tools.test_reported_bugs",
        "tools.test_paths", "tools.build_nuitka", "tools.dump_ui", "tools.selftest",
        "bridge", "xiaotiancai", "plugin_client", "qq_webhook", "msg_log",
    ],
}
PRODUCT = "XTC QQ Bridge"
COMPANY = "xtc-bridge"


def read_version() -> str:
    ns: dict = {}
    exec((ROOT / "version.py").read_text(encoding="utf-8"), ns)  # noqa: S102 本仓库自有文件
    return str(ns.get("__version__", "0.0.0"))


def host_os() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def host_arch() -> str:
    m = platform.machine().lower()
    if m in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("i386", "i686", "x86"):
        return "x86"
    return re.sub(r"[^a-z0-9]+", "_", m) or "unknown"


def select_targets(requested: str, is_windows: bool | None = None) -> tuple:
    """按平台决定编译哪些目标，返回 (targets, 提示文本)。

    WSA 网络守护只适用于 Windows：WSA（Windows Subsystem for Android）是 Windows
    独有组件，Linux / macOS 上既没有 WSA 也没有对应的宿主网络，编出来毫无意义
    （之前 CI 给 macOS/Linux 也编了守护产物，是错的）。
    - `--target all`   非 Windows -> 只编 bridge，并给出跳过提示
    - `--target guard` 非 Windows -> 返回空列表（调用方按"明确拒绝"处理）
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    targets = ["bridge", "guard"] if requested == "all" else [requested]
    if "guard" in targets and not is_windows:
        if requested == "guard":
            return [], ("WSA 网络守护只适用于 Windows（WSA 是 Windows 独有组件），"
                        "本平台不提供该产物。")
        return ["bridge"], "WSA 网络守护只适用于 Windows，本平台只编译主程序。"
    return targets, ""


def product_name(target: str, version: str) -> str:
    base = "xtc-qq-bridge" if target == "bridge" else "xtc-wsa-guard"
    # 版本号不加 v 前缀（与 tag 一致：tag 就叫 1.0.0-alpha.1）
    return f"{base}-{version}-{host_os()}-{host_arch()}"


def numeric_version(version: str) -> str:
    """把 `1.0.0-alpha.1` 这类版本号转成 Nuitka 的 --file-version 能接受的形式。

    Nuitka 的 --file-version / --product-version 只接受**纯数字**（最多 4 段），
    带 `-alpha.1` 后缀会直接报
        FATAL: Invalid version number --file-version='1.0.0-alpha.1'.
    并让整个编译立刻失败（CI 六平台全挂就是这么来的）。
    产物文件名仍然用完整版本号；这里只给 Windows 版本资源用数字形式：
        1.0.0-alpha.1 -> 1.0.0.1     1.0.0 -> 1.0.0.0     2.1-beta.3 -> 2.1.0.3
    """
    raw = (version or "").strip()
    m = re.match(r"^(\d+(?:\.\d+)*)", raw)
    parts = [int(x) for x in (m.group(1).split(".") if m else ["0"])][:3]
    while len(parts) < 3:
        parts.append(0)
    # 预发布序号放进第 4 段，正式版为 0（Windows 版本字段上限 65535）
    pre = re.search(r"(?:alpha|beta|rc)\.?(\d+)", raw, re.I)
    parts.append(int(pre.group(1)) if pre else 0)
    return ".".join(str(max(0, min(p, 65535))) for p in parts)


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001 可选依赖
        return False


# ---------------------------------------------------------------- Nuitka 探测
class NuitkaInfo:
    def __init__(self) -> None:
        self.available = False
        self.version = ""
        self.help_text = ""
        self.compiler = ""
        self.raw_version = ""
        self.mode_style = "onefile"     # "mode" = 用 --mode=xxx；"legacy" = 用 --onefile/--standalone
        self.nuitka_cmd: list[str] = []

    def probe(self) -> "NuitkaInfo":
        self.nuitka_cmd = [sys.executable, "-m", "nuitka"]
        try:
            out = subprocess.run([*self.nuitka_cmd, "--version"], capture_output=True,
                                 text=True, errors="replace", timeout=180, env=nuitka_env())
        except (OSError, subprocess.SubprocessError) as e:
            self.help_text = f"(nuitka --version 失败: {e})"
            return self
        blob = (out.stdout or "") + (out.stderr or "")
        self.raw_version = blob.strip()
        m = re.search(r"^(\d+\.\d+(?:\.\d+)?)", blob.strip())
        self.version = m.group(1) if m else ""
        self.available = bool(self.version)
        cm = re.search(r"Version C compiler:\s*(.+)", blob)
        self.compiler = cm.group(1).strip() if cm else ""
        if "Not found" in self.compiler or self.compiler.lower().startswith("none"):
            self.compiler = ""
        try:
            h = subprocess.run([*self.nuitka_cmd, "--help"], capture_output=True, text=True,
                               timeout=180, env=nuitka_env())
            self.help_text = (h.stdout or "") + (h.stderr or "")
        except (OSError, subprocess.SubprocessError):
            self.help_text = ""
        # 4.2 起用 --mode=xxx（--standalone/--onefile 变成兼容写法）；老版本只有后者
        self.mode_style = "mode" if "--mode=" in self.help_text else "legacy"
        return self

    def supports(self, flag: str) -> bool:
        """选项是否存在于当前 Nuitka（--mode= 这类带等号的按前缀搜）。"""
        if not self.help_text:
            return True                      # 拿不到 help 就不乱删选项
        return flag in self.help_text

    def mode_args(self, mode: str) -> list[str]:
        if self.mode_style == "mode":
            return [f"--mode={mode}"]
        return [f"--{mode}"]                 # onefile / standalone


def nuitka_env() -> dict:
    """Nuitka 缓存目录：默认位置不可写时（沙箱/受限环境）自动改到项目内。"""
    env = dict(os.environ)
    if env.get("NUITKA_CACHE_DIR"):
        return env
    default = (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Nuitka" if os.name == "nt"
               else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "Nuitka")
    try:
        default.mkdir(parents=True, exist_ok=True)
        probe = default / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        cache = ROOT / ".nuitka-cache"
        cache.mkdir(parents=True, exist_ok=True)
        env["NUITKA_CACHE_DIR"] = str(cache)
    return env


# ---------------------------------------------------------------- 前置检查
def preflight(info: NuitkaInfo, *, strict: bool = False) -> tuple[bool, list[str]]:
    """返回 (是否通过, 提示行列表)。

    **只有硬性条件不满足才算失败**（Python 版本、pyyaml、Nuitka 缺失）；
    "检测不到 C 编译器"只作为警告 —— 各平台检测方式不同（Windows 上 Nuitka 通过 vswhere
    找 VS，某些 CI 镜像里 --version 里不打印编译器），真正缺编译器时 Nuitka 自己会在编译
    阶段给出更准确的错误。`--strict` 可让它也变成失败（本地想快速失败时用）。
    """
    lines: list[str] = []
    ok = True
    lines.append(f"平台        : {host_os()}-{host_arch()}  Python {platform.python_version()}")
    if sys.version_info < (3, 10):
        ok = False
        lines.append("[错误] 需要 Python 3.10+")
    try:
        import yaml  # noqa: F401
        lines.append("pyyaml      : 已安装（配置用 YAML，编译时必须装）")
    except ImportError:
        ok = False
        lines.append("[错误] 缺 pyyaml：编译产物将无法读 YAML 配置。"
                     "请先 `python -m pip install pyyaml`")
    if not info.available:
        ok = False
        lines.append("[错误] 未安装 Nuitka：`python -m pip install -U nuitka`（Python 3.14 需 4.1+）")
        return ok, lines
    lines.append(f"nuitka      : {info.version}"
                 f"（{'--mode=' if info.mode_style == 'mode' else '--onefile/--standalone'} 接口）")
    if info.compiler:
        lines.append(f"C 编译器    : {info.compiler}")
    else:
        note = {
            "windows": "安装 Visual Studio 2022+ 并勾选“使用 C++ 的桌面开发”"
                       "（Python 3.13+ 不支持 MinGW64）",
            "linux": "安装 gcc/g++ 与 python3-dev（Debian/Ubuntu: sudo apt install gcc g++ python3-dev）",
            "macos": "安装 Xcode Command Line Tools：xcode-select --install",
        }[host_os()]
        (lines.append if not strict else lines.append)(
            f"[{'错误' if strict else '警告'}] 未从 `nuitka --version` 检测到 C 编译器：{note}"
            f"（编译阶段会给出更准确的报错）")
        if strict:
            ok = False
    if info.raw_version:
        lines.append("nuitka --version 原始输出（前 6 行，排障用）：")
        for line in info.raw_version.splitlines()[:6]:
            lines.append(f"    {line}")
    return ok, lines


# ---------------------------------------------------------------- 命令组装
def _data_dir_args(rel_src: str, rel_dst: str) -> list[str]:
    """把一个目录里的文件**逐个**用 --include-data-files 打进包。

    为什么不用 --include-data-dir：Nuitka 会把数据目录里的 `.py` 当"Python 模块"处理并
    **从数据里剔除**（实测：datadir 里只剩 json/yaml/md，main.py 消失），而 AstrBot 插件
    必须原样带 .py 才能安装。逐文件声明就不会被剔除。
    """
    args: list[str] = []
    base = ROOT / rel_src
    if not base.is_dir():
        return args
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.suffix in (".pyc", ".pyo"):
            continue
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(base).as_posix()
        args.append(f"--include-data-files={p.relative_to(ROOT).as_posix()}={rel_dst}/{rel}")
    return args


def build_command(info: NuitkaInfo, target: str, mode: str, out_dir: Path,
                  args: argparse.Namespace) -> list[str]:
    script, _console, desc = TARGETS[target]
    version = read_version()
    final = product_name(target, version)
    cmd = [*info.nuitka_cmd, *info.mode_args(mode)]
    # 注意：Nuitka 4.2 的选项解析要求带等号（`--output-dir=xxx`），空格分隔会报
    # "The '--output-dir' option requires an argument with '--output-dir='"。
    cmd += [f"--output-dir={out_dir}"]
    cmd += [f"--output-filename={final}.exe" if os.name == "nt" else f"--output-filename={final}"]
    cmd += ["--assume-yes-for-downloads", "--remove-output"]
    cmd += ["--nofollow-import-to=" + m for m in EXCLUDE_MODULES.get(target, [])]
    cmd += ["--include-module=" + m for m in INCLUDE_MODULES.get(target, [])]
    # pyyaml 在 try/except 里 import，显式带上整个包（含 C 扩展）
    if info.supports("--include-package"):
        cmd += ["--include-package=yaml"]
    if _has_module("_yaml"):                       # PyYAML 的 libyaml 加速模块（可选）
        cmd += ["--include-module=_yaml"]
    for src, dst in DATA_FILES.get(target, []):
        if (ROOT / src).is_file():
            cmd += [f"--include-data-files={src}={dst}"]
    for src, dst in DATA_DIRS.get(target, []):
        cmd += _data_dir_args(src, dst)
    if info.supports("--company-name"):
        cmd += [f"--company-name={COMPANY}", f"--product-name={PRODUCT}"]
        # 版本资源必须是纯数字：1.0.0-alpha.1 -> 1.0.0.1（否则 Nuitka 直接 FATAL）
        num_ver = numeric_version(version)
        cmd += [f"--file-version={num_ver}", f"--product-version={num_ver}"]
        cmd += [f"--copyright=Apache-2.0"]
    if os.name == "nt" and info.supports("--windows-console-mode"):
        cmd += ["--windows-console-mode=force"]        # CLI 工具必须保留控制台
    if os.name == "nt" and info.supports("--msvc"):
        cmd += ["--msvc=latest"]                       # 明确用 MSVC（Python 3.13+ 不支持 MinGW64）
    jobs = args.jobs or (os.cpu_count() or 2)
    if info.supports("--jobs"):
        cmd += [f"--jobs={jobs}"]
    if args.lto != "default" and info.supports("--lto"):
        cmd += [f"--lto={args.lto}"]
    if args.quiet:
        cmd += ["--quiet"]
    if args.extra:
        cmd += list(args.extra)
    cmd += [script]
    return cmd


def run(cmd: list[str]) -> int:
    print("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    t0 = time.time()
    rc = subprocess.run(cmd, cwd=str(ROOT), env=nuitka_env()).returncode
    print(f"  -> 退出码 {rc}，用时 {time.time() - t0:.0f}s")
    return rc


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 中间目录 / 并行保护
TEMP_DIR_SUFFIXES = (".build", ".onefile-build", ".dist")


def _pid_alive(pid: int) -> bool:
    """进程是否还活着（只查询，不发任何信号）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            k32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_build_lock(out_dir: Path):
    """同一个输出目录不允许并行编译，用锁文件挡住（并行会撞 Nuitka 的断言）。

    返回锁文件路径；若确认已有另一个编译在跑则返回 None。
    上次异常退出留下的锁不会被永久卡住：pid 已经不存在的锁会被自动接管。
    """
    lock = out_dir / ".build.lock"
    if lock.exists():
        data = {}
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        pid = int(data.get("pid") or 0)
        if _pid_alive(pid):
            print(f"[中止] 另一个编译正在进行（pid={pid}，开始于 {data.get('started', '未知')}）。")
            print("       同一个输出目录不能并行编译，等它结束再跑；")
            print(f"       若确认它已经死了，删掉 {lock} 后重试。")
            return None
        print(f"[提示] 忽略上次残留的编译锁（pid={pid} 已不在）")
    lock.write_text(json.dumps({"pid": os.getpid(),
                                "started": time.strftime("%Y-%m-%d %H:%M:%S")},
                               ensure_ascii=False), encoding="utf-8")
    return lock


def release_build_lock(lock) -> None:
    if lock is None:
        return
    try:
        lock.unlink()
    except OSError:
        pass


def clean_stale_dirs(out_dir: Path, target: str) -> list[str]:
    """删掉该目标上次编译留下的中间目录。

    Nuitka 写 C 源文件时会断言"文件不存在"，所以只要上次编译被打断
    （Ctrl+C、崩溃、或两个编译并行写同一个输出目录），残留的
    `module.__main__.c` 就会让下一次编译直接崩在 AssertionError 上：
        AssertionError: ...\\dist\\main.build\\module.__main__.c
    """
    stem = Path(TARGETS[target][0]).stem
    removed: list[str] = []
    for suffix in TEMP_DIR_SUFFIXES:
        p = out_dir / (stem + suffix)
        if not p.exists():
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                pass
        if not p.exists():
            removed.append(p.name)
    return removed


def run_smoke(target: str, artifact: Path, mode: str) -> bool:
    """编译后冒烟测试（不需要设备）：把产物复制到干净目录里运行自检命令。

    - bridge: `--version` + `--verify`（校验依赖与捆绑资源是否真的打进包了）
    - guard : `--test`（守护自身的分级修复逻辑自测）
    在干净目录里跑还能顺带验证"数据写在 exe 旁边"这条路径逻辑。
    """
    smoke_dir = ROOT / ".smoke_run"
    shutil.rmtree(smoke_dir, ignore_errors=True)
    smoke_dir.mkdir(parents=True, exist_ok=True)
    if artifact.is_dir():                       # standalone：整个目录一起复制
        dst_dir = smoke_dir / artifact.name
        shutil.copytree(artifact, dst_dir)
        exe_name = product_name(target, read_version()) + (".exe" if os.name == "nt" else "")
        exe = dst_dir / exe_name
        if not exe.exists():                    # 名字对不上就取目录里第一个可执行文件
            for f in dst_dir.iterdir():
                if f.is_file() and (f.suffix == ".exe" or os.access(f, os.X_OK)):
                    exe = f
                    break
    else:
        exe = smoke_dir / artifact.name
        shutil.copy2(artifact, exe)
    if not exe or not exe.exists():
        print(f"[冒烟] 找不到可执行文件（artifact={artifact}），跳过")
        return False
    # guard 没有 --version（它只有 --test 等参数），所以两个目标的自检命令不一样
    cmds = [["--test"]] if target == "guard" else [["--version"], ["--verify"]]
    ok = True
    for extra in cmds:
        try:
            p = subprocess.run([str(exe), *extra], capture_output=True, text=True,
                               errors="replace", timeout=300)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[冒烟] {' '.join(extra)} 运行失败: {e}")
            ok = False
            continue
        tail = (p.stdout or "").strip().splitlines()
        last = tail[-1] if tail else (p.stderr or "").strip()[-200:]
        print(f"[冒烟] {' '.join(extra):<12} 退出码={p.returncode}  {last}")
        if p.returncode != 0:
            ok = False
    shutil.rmtree(smoke_dir, ignore_errors=True)
    return ok


def collect_artifacts(out_dir: Path, target: str, mode: str) -> list[Path]:
    """找出本次产物并统一命名。

    onefile: Nuitka 直接产出 `<名字>.exe`（受 --output-filename 控制）。
    standalone: Nuitka 用**脚本名**给目录命名（如 main.dist），这里按里面的 exe 名找到它，
    再重命名为 `<名字>.dist`，让两种模式的命名一致。
    """
    version = read_version()
    base = product_name(target, version)
    produced: list[Path] = []
    if mode == "onefile":
        exe = out_dir / (base + (".exe" if os.name == "nt" else ""))
        if exe.is_file():
            produced.append(exe)
        return produced
    exe_name = base + (".exe" if os.name == "nt" else "")
    for d in sorted(out_dir.glob("*.dist")):
        if not d.is_dir():
            continue
        if not (d / exe_name).exists() and d.name != f"{base}.dist":
            continue
        want = out_dir / f"{base}.dist"
        if d != want:
            try:
                if want.exists():
                    shutil.rmtree(want, ignore_errors=True)
                d.rename(want)
            except OSError as e:
                print(f"[warn] 重命名 {d.name} -> {want.name} 失败: {e}")
                produced.append(d)
                continue
        produced.append(want)
    return produced


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nuitka 编译驱动（单文件机器码）")
    ap.add_argument("--mode", choices=["onefile", "standalone"], default="onefile",
                    help="onefile=单文件（默认）；standalone=目录（启动更快）")
    ap.add_argument("--target", choices=["all", "bridge", "guard"], default="all")
    ap.add_argument("--out", default="dist", help="输出目录（默认 dist）")
    ap.add_argument("--jobs", type=int, default=0, help="并行编译进程数（默认 CPU 核数）")
    ap.add_argument("--lto", choices=["default", "yes", "no"], default="default",
                    help="链接时优化（默认跟随 Nuitka）")
    ap.add_argument("--quiet", action="store_true", help="只输出关键信息")
    ap.add_argument("--check-env", action="store_true", help="只做前置检查后退出")
    ap.add_argument("--strict", action="store_true",
                    help="严格模式：连『检测不到 C 编译器』也算失败（本地想快速失败时用）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令")
    ap.add_argument("--no-smoke", action="store_true",
                    help="编译后不做冒烟测试（默认会在干净目录里跑 --verify/--test）")
    ap.add_argument("--extra", action="append", default=[],
                    help="追加 Nuitka 参数（可重复，例如 --extra=--low-memory）")
    args = ap.parse_args(argv)

    version = read_version()
    out_dir = (ROOT / args.out).resolve()
    info = NuitkaInfo().probe()
    ok, lines = preflight(info, strict=args.strict)
    print("---- 编译环境 ----")
    for line in lines:
        print("  " + line)
    print(f"  版本号      : {version}")
    print(f"  输出目录    : {out_dir}")
    print("------------------")

    targets, note = select_targets(args.target)
    if note:
        print(("\n[中止] " if not targets else "\n[跳过] ") + note)
    if not targets:
        return 2
    if args.dry_run:
        for t in targets:
            cmd = build_command(info, t, args.mode, out_dir, args)
            print(f"\n[{t}] {TARGETS[t][2]}（{args.mode}）")
            print("  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        print("\n（--dry-run：未执行任何编译）")
        return 0
    if args.check_env:
        print("\n环境检查：通过" if ok else "\n环境检查：未通过（见上面的 [错误]）")
        return 0 if ok else 1
    if not ok:
        print("\n[中止] 请先解决上面的 [错误]（Windows 需要 Visual Studio 2022+ 的 C++ 组件）")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    lock = acquire_build_lock(out_dir)
    if lock is None:
        return 1
    rc_all = 0
    for t in targets:
        print(f"\n===== 编译 {t}：{TARGETS[t][2]} =====")
        stale = clean_stale_dirs(out_dir, t)
        if stale:
            print(f"[清理] 上次残留的中间目录：{', '.join(stale)}")
        rc = run(build_command(info, t, args.mode, out_dir, args))
        if rc != 0:
            rc_all = rc
            print(f"[失败] {t} 编译失败（退出码 {rc}）")
            # 失败往往把中间目录写了一半；留着下次就会撞 Nuitka 的断言，所以清掉
            if clean_stale_dirs(out_dir, t):
                print(f"[清理] 已清掉 {t} 的半成品中间目录，修好问题后可直接重跑")
            continue
        for art in collect_artifacts(out_dir, t, args.mode):
            if art.is_file():
                digest = sha256_file(art)
                (art.parent / (art.name + ".sha256")).write_text(
                    f"{digest}  {art.name}\n", encoding="utf-8")
                size_mb = art.stat().st_size / (1024 * 1024)
                print(f"[产物] {art}  ({size_mb:.1f} MB)  sha256={digest[:16]}...")
            else:
                print(f"[产物] {art}\\  (目录模式)")
            if not args.no_smoke and art.exists():
                if not run_smoke(t, art, args.mode):
                    rc_all = rc_all or 1
                    print(f"[冒烟] {t} 冒烟测试未通过（可用 --no-smoke 跳过）")
    release_build_lock(lock)
    if rc_all == 0:
        print(f"\n全部完成。产物在 {out_dir}")
        print("提示：把 exe 放到任意目录运行 `--paths` 可确认配置/日志位置；"
              "首次运行会在 exe 旁边生成 config.yaml。")
    return rc_all


if __name__ == "__main__":
    sys.exit(main())
