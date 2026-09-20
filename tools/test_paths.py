# -*- coding: utf-8 -*-
"""离线测试：运行路径解析（源码 / Nuitka 冻结）。

编译成单文件后，"配置/日志/数据写哪里"完全依赖 runtime_paths 的判断，判断错了会
出现"日志和消息库写进临时目录、退出就丢"这类难查的问题。这些用例不需要设备，
也不需要真的编译：直接构造目录验证解析顺序与兜底逻辑。

用法：python tools/test_paths.py
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import runtime_paths as rp  # noqa: E402

RC = {"pass": 0, "fail": 0}
# 临时目录放工作目录下：受限沙箱里系统 %TEMP% 子目录可能不可写（项目已有测试同样处理）
_WORK = Path.cwd()
_DIRS: list[Path] = []


def tmpdir(name: str) -> Path:
    d = _WORK / f".pathtest_{name}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    _DIRS.append(d)
    return d


def cleanup() -> None:
    for d in _DIRS:
        shutil.rmtree(d, ignore_errors=True)
    _DIRS.clear()
    for old in _WORK.glob(".pathtest*"):
        shutil.rmtree(old, ignore_errors=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")
    RC["pass" if ok else "fail"] += 1


def test_paths_shape() -> None:
    check("BUNDLE_DIR 是绝对路径且存在", rp.BUNDLE_DIR.is_absolute() and rp.BUNDLE_DIR.exists(),
          str(rp.BUNDLE_DIR))
    check("APP_DIR 是绝对路径", rp.APP_DIR.is_absolute(), str(rp.APP_DIR))
    check("DATA_DIR/LOG_DIR 在 APP_DIR 下",
          rp.DATA_DIR.parent == rp.APP_DIR and rp.LOG_DIR.parent == rp.APP_DIR,
          f"{rp.DATA_DIR} | {rp.LOG_DIR}")
    check("源码运行时不报告冻结", not rp.IS_FROZEN, f"mode={rp.FROZEN_MODE!r}")


def test_log_path_resolution() -> None:
    rel = rp.log_path("logs/bridge.log")
    check("相对日志路径按 APP_DIR 解析",
          rel.is_absolute() and rel.parent.parent == rp.APP_DIR and rel.name == "bridge.log",
          str(rel))
    abs_in = tmpdir("abs") / "xtc-abs.log"
    check("绝对日志路径原样保留", rp.log_path(str(abs_in)) == abs_in, str(rp.log_path(str(abs_in))))
    check("data_path 落在 DATA_DIR", rp.data_path("msg_log.json") == rp.DATA_DIR / "msg_log.json")


def test_resource_lookup() -> None:
    p = rp.resource(rp.CONFIG_EXAMPLE_NAME)
    check("能定位到 config.example.yaml", p.is_file(), str(p))
    plug = rp.resource_dir(rp.PLUGIN_SRC_DIR)
    check("能定位到 AstrBot 插件目录", plug is not None and (plug / "main.py").is_file(),
          str(plug))
    missing = rp.resource("this-file-does-not-exist.xyz")
    check("资源缺失时返回路径而不是抛异常",
          isinstance(missing, Path) and not missing.exists(), str(missing))


def test_apk_detection() -> None:
    """捆绑 APK 只有约 18KB：旧实现用 >50KB 判断会误报"找不到 APK"。"""
    apk = rp.bundled_apk()
    check("能识别捆绑的 ADBKeyBoard APK", apk is not None, str(apk))
    td = tmpdir("apk")
    fake = td / "keyboardservice-debug.apk"
    fake.write_bytes(b"not-a-zip" * 200)
    check("非 zip 内容不算 APK", rp.looks_like_apk(fake) is False)
    fake.write_bytes(b"PK\x03\x04" + b"\0" * 2000)
    check("zip 魔数 + 体积足够即算 APK", rp.looks_like_apk(fake) is True)
    tiny = td / "tiny.apk"
    tiny.write_bytes(b"PK\x03\x04")
    check("过小文件不算 APK", rp.looks_like_apk(tiny) is False)


def test_config_resolution_order() -> None:
    td = tmpdir("cfg")
    old_cwd = Path.cwd()
    try:
        os.chdir(td)
        explicit = td / "custom.yaml"
        check("命令行指定的配置优先",
              rp.resolve_config(str(explicit)) == explicit,
              str(rp.resolve_config(str(explicit))))
        # 模拟"APP_DIR 里已有 config.yaml"（源码运行时 = 项目根）
        fake_root = td / "app"
        fake_root.mkdir()
        (fake_root / "config.yaml").write_text("target: {}\n", encoding="utf-8")
        old_app = rp.APP_DIR
        try:
            rp.APP_DIR = fake_root
            got = rp.resolve_config()
            check("APP_DIR 有配置时优先用 APP_DIR 的", got == fake_root / "config.yaml", str(got))
        finally:
            rp.APP_DIR = old_app
        # 不指定 --config 时总能解析出一个路径（这里 APP_DIR 里就有项目自带的 config.yaml）
        check("不指定时也能解析出可用配置路径",
              rp.resolve_config().is_absolute(), str(rp.resolve_config()))
    finally:
        os.chdir(old_cwd)


def test_ensure_config_copies_template() -> None:
    target = tmpdir("ensure") / "sub" / "config.yaml"
    created = rp.ensure_config(target)
    check("缺失时自动生成配置", created and target.is_file(), str(target))
    text = target.read_text(encoding="utf-8")
    check("生成内容来自模板（含 webhook 段）", "webhook:" in text, text[:40].replace("\n", " "))
    check("已存在时不覆盖", rp.ensure_config(target) is False)


def test_writable_probe_and_fallback() -> None:
    d = tmpdir("writable") / "writable"
    check("可写目录判定为 True", rp._writable(d) is True, str(d))
    check("探测后不留临时文件", not (d / ".xtc_write_probe").exists())
    ud = rp._user_data_dir()
    check("用户数据目录兜底路径在当前用户目录下",
          Path.home() in ud.parents or ud.parent == Path.home(), str(ud))
    check("describe() 能生成排障信息",
          all(k in rp.describe() for k in ("资源目录", "数据目录", "配置")),
          rp.describe().splitlines()[0])


def test_no_writes_into_bundle_for_source_run() -> None:
    """源码运行时 APP_DIR 就是项目目录（不能跑到临时目录去）。"""
    check("源码运行时 APP_DIR == BUNDLE_DIR", rp.APP_DIR == rp.BUNDLE_DIR,
          f"{rp.APP_DIR} vs {rp.BUNDLE_DIR}")


def main() -> int:
    try:
        for fn in (test_paths_shape, test_log_path_resolution, test_resource_lookup,
                   test_apk_detection, test_config_resolution_order,
                   test_ensure_config_copies_template, test_writable_probe_and_fallback,
                   test_no_writes_into_bundle_for_source_run):
            print(f"--- {fn.__name__} ---")
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                check(f"{fn.__name__} 未抛异常", False, f"{type(e).__name__}: {e}")
    finally:
        cleanup()
    print(f"\n===== 路径测试：{RC['pass']} 通过 / {RC['fail']} 失败 =====")
    return 1 if RC["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
