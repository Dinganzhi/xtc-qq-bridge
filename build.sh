#!/usr/bin/env bash
# =============================================================================
#  Build machine-code single-file executables with Nuitka (Linux / macOS)
#
#  用法：
#    bash build.sh                     # onefile，两个目标，产物在 dist/
#    bash build.sh --mode standalone   # 目录模式（启动更快）
#    bash build.sh --target guard      # 只编 WSA 守护
#    bash build.sh --check-env         # 只检查 Python/pyyaml/Nuitka/gcc
#    bash build.sh --dry-run           # 只打印 nuitka 命令行
#
#  依赖（Debian/Ubuntu）：sudo apt install gcc g++ python3-dev
#  依赖（macOS）：xcode-select --install
# =============================================================================
set -u

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SRC" || exit 1

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

PY=""
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    for cand in python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
    done
fi
if [ -z "$PY" ]; then
    echo "[error] 找不到 python3（需要 3.10+）"
    exit 1
fi

echo "============================================"
echo "  Nuitka 编译（Linux / macOS）"
echo "============================================"
echo "[1/2] 检查编译环境 ..."
if ! "$PY" tools/build_nuitka.py --check-env; then
    echo
    echo "[提示] 需要：python3 -m pip install -U nuitka pyyaml"
    echo "       Linux 还要装 gcc/g++/python3-dev；macOS 需要 xcode-select --install"
    exit 1
fi

echo
echo "[2/2] 开始编译 ..."
"$PY" tools/build_nuitka.py "$@"
rc=$?
echo
if [ "$rc" != "0" ]; then
    echo "[退出] 编译失败，返回码 $rc"
else
    echo "[退出] 编译完成，产物在 dist/"
fi
exit "$rc"
