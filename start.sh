#!/usr/bin/env bash
# =============================================================================
#  XTC QQ Bridge - Linux / macOS 启动器（start.bat 的对应版本）
#  用法：
#    ./start.sh                 # 交互菜单
#    ./start.sh --check         # 参数透传：直接跑 python main.py --check 后退出
#    ./start.sh --debug adb-info
# =============================================================================
set -u

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SRC" || exit 1

# 强制 UTF-8：中文配置/日志在非 UTF-8 locale 下会乱码
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

if [ -t 1 ]; then C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_0=$'\033[0m'
else C_OK=""; C_WARN=""; C_ERR=""; C_0=""; fi
say_ok()   { printf '%s[ok]%s %s\n'   "$C_OK"   "$C_0" "$*"; }
say_warn() { printf '%s[!]%s %s\n'    "$C_WARN" "$C_0" "$*"; }
say_err()  { printf '%s[x]%s %s\n'    "$C_ERR"  "$C_0" "$*"; }

echo "============================================"
echo "  小天才 <-> QQ 桥接   启动器"
echo "  XTC QQ Bridge Launcher (Linux / macOS)"
echo "============================================"
echo

# ---------- 0. 选择 Python 解释器（优先项目虚拟环境） ----------
PY=""
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
    say_ok "使用项目虚拟环境 .venv"
else
    for cand in python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then
            PY="$cand"
            break
        fi
    done
fi
if [ -z "$PY" ]; then
    say_err "找不到 python3。请先安装 Python 3.10+（Debian/Ubuntu: sudo apt install python3）"
    exit 1
fi
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
    say_err "Python 版本过低（需要 3.10+）：$("$PY" --version 2>&1)"
    exit 1
fi
say_ok "环境 $("$PY" --version 2>&1)"

# ---------- 1. pyyaml ----------
if "$PY" -c "import yaml" >/dev/null 2>&1; then
    say_ok "pyyaml 已就绪"
else
    say_warn "缺少 pyyaml，尝试安装..."
    if "$PY" -m pip install -q --user pyyaml >/dev/null 2>&1 \
       || "$PY" -m pip install -q --break-system-packages pyyaml >/dev/null 2>&1 \
       || "$PY" -m pip install -q pyyaml >/dev/null 2>&1; then
        say_ok "pyyaml 已安装"
    else
        say_warn "pyyaml 安装失败（可能没网）；可把 config.yaml 写成 JSON 格式"
    fi
fi

# ---------- 2. adb（提示，不自动安装） ----------
if command -v adb >/dev/null 2>&1; then
    say_ok "adb: $(command -v adb)"
elif [ -f "config.yaml" ]; then
    say_warn "PATH 里没有 adb；若 config.yaml → adb.path 已指定绝对路径可忽略"
else
    say_warn "PATH 里没有 adb（macOS: brew install --cask android-platform-tools）"
fi

# ---------- 3. config.yaml ----------
if [ ! -f "config.yaml" ]; then
    if [ -f "config.example.yaml" ]; then
        cp -f config.example.yaml config.yaml
        say_ok "已从模板生成 config.yaml"
        echo
        echo "请先编辑 config.yaml，至少填写："
        echo "  target.qq_private / qq_group、target.xtc_contact、"
        echo "  forward.plugin.token、webhook.token / allow_from（与插件配置一致）"
        echo "  以及 adb.port / adb.serial（Waydroid 一般是 127.0.0.1:5555）"
        echo
        printf "现在用编辑器打开 config.yaml 吗？[y/N] "
        read -r ans
        case "$ans" in
            [Yy]*) "${EDITOR:-nano}" config.yaml ;;
        esac
        echo "填好后重新运行本脚本即可。"
        exit 0
    else
        say_err "找不到 config.yaml 与 config.example.yaml"
        exit 1
    fi
else
    say_ok "config.yaml 已就绪"
fi

# ---------- 4. ADBKeyBoard 本地 APK（仅提示） ----------
if [ -f "keyboardservice-debug.apk" ] || [ -f "ADBKeyBoard.apk" ]; then
    say_ok "ADBKeyBoard APK 已就位"
else
    say_warn "项目目录里没有 ADBKeyBoard 的 APK；设备上已安装可忽略，否则中文无法输入"
fi

# ---------- 5. 参数透传 ----------
if [ "$#" -gt 0 ]; then
    echo "[运行] main.py $*"
    echo
    "$PY" main.py "$@"
    rc=$?
    echo
    echo "[退出] 返回码 $rc"
    exit "$rc"
fi

menu() {
    echo
    echo "--------------------------------------------"
    echo "  1) 启动桥接           (main.py)"
    echo "  2) 环境自检           (--check)"
    echo "  3) 干跑一轮           (--once)"
    echo "  4) 打印当前界面控件   (--debug dump-ui)"
    echo "  5) 编辑 config.yaml"
    echo "  6) 查看日志           (logs/bridge.log 末尾 40 行)"
    echo "  0) 退出"
    echo "--------------------------------------------"
}

while true; do
    menu
    printf "请选择 [1]: "
    read -r ch || exit 0
    [ -z "$ch" ] && ch=1
    case "$ch" in
        1)
            echo
            echo "[运行] 启动桥接（Ctrl+C 退出）"
            echo
            "$PY" main.py
            rc=$?
            echo
            echo "[退出] 返回码 $rc"
            [ "$rc" != "0" ] && say_warn "启动失败，建议先选 2 做一次环境自检"
            exit "$rc"
            ;;
        2)
            echo
            "$PY" main.py --check
            echo
            ;;
        3)
            echo
            "$PY" main.py --once
            echo
            ;;
        4)
            echo
            "$PY" main.py --debug dump-ui
            echo
            ;;
        5)
            "${EDITOR:-nano}" config.yaml
            ;;
        6)
            if [ -f "logs/bridge.log" ]; then
                echo
                tail -n 40 logs/bridge.log
                echo
            else
                say_warn "还没有日志文件 logs/bridge.log（先启动一次桥接）"
            fi
            ;;
        0|q|Q)
            exit 0
            ;;
        *)
            say_warn "无效选择，请重新输入"
            ;;
    esac
done
