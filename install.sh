#!/usr/bin/env bash
# =============================================================================
#  XTC QQ Bridge - Linux / macOS 安装脚本（install.bat 的对应版本）
#  做四件事：
#    1. 检查 Python 3.10+，并确保 pyyaml（缺失时装到虚拟环境或用户目录）
#    2. 复制 AstrBot 插件到 ~/.astrbot/data/plugins/xtc_qq_bridge/
#    3. 从 config.example.yaml 生成 config.yaml（已存在则不动）
#    4. 生成插件初始配置（已存在则不动）
#  用法：  bash install.sh          （或 ./install.sh）
# =============================================================================
set -u

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SRC" || exit 1

# 颜色（非终端时自动不输出）
if [ -t 1 ]; then C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_0=$'\033[0m'
else C_OK=""; C_WARN=""; C_ERR=""; C_0=""; fi
ok()   { printf '%s[OK]%s %s\n'   "$C_OK"   "$C_0" "$*"; }
warn() { printf '%s[WARN]%s %s\n' "$C_WARN" "$C_0" "$*"; }
err()  { printf '%s[ERROR]%s %s\n' "$C_ERR" "$C_0" "$*"; }

echo "============================================"
echo "  XTC QQ Bridge - Installer (Linux / macOS)"
echo "============================================"
echo

# ---------- 0. 基本环境 ----------
case "$(uname -s)" in
    Linux*)  OS=linux ;;
    Darwin*) OS=macos ;;
    *)       OS=other ;;
esac
ok "平台: $(uname -s) $(uname -m)"

# ---------- 1. Python ----------
PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
            PY="$cand"; break
        fi
        # 记录一个版本过低的候选，便于给出准确提示
        [ -n "${PY_LOW:-}" ] || PY_LOW="$cand $("$cand" --version 2>&1)"
    fi
done
if [ -z "$PY" ]; then
    err "找不到 Python 3.10+。"
    [ -n "${PY_LOW:-}" ] && err "已安装的版本过低: $PY_LOW"
    if [ "$OS" = "macos" ]; then
        echo "  macOS: brew install python@3.12"
    else
        echo "  Debian/Ubuntu: sudo apt install python3 python3-pip python3-venv"
        echo "  Fedora/RHEL:   sudo dnf install python3 python3-pip"
        echo "  Arch:          sudo pacman -S python python-pip"
    fi
    exit 1
fi
ok "Python: $("$PY" --version 2>&1)  ($PY)"

# ---------- 2. 依赖 pyyaml（缺失时尽力安装） ----------
PKG_PY=""
if "$PY" -c "import yaml" >/dev/null 2>&1; then
    ok "pyyaml 已就绪"
else
    echo "[..] 缺少 pyyaml，尝试安装 ..."
    # 优先 venv（Debian/Ubuntu 的 PEP 668 externally-managed 会拒绝全局 pip 安装）
    if "$PY" -m venv .venv >/dev/null 2>&1 && [ -x ".venv/bin/python" ]; then
        if .venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1 \
           && .venv/bin/python -m pip install -q pyyaml >/dev/null 2>&1; then
            PKG_PY="$SRC/.venv/bin/python"
            ok "pyyaml 已装入项目虚拟环境 .venv/（启动请用 .venv/bin/python main.py）"
        fi
    fi
    if [ -z "$PKG_PY" ]; then
        if "$PY" -m pip install -q --user pyyaml >/dev/null 2>&1 \
           || "$PY" -m pip install -q --break-system-packages pyyaml >/dev/null 2>&1 \
           || "$PY" -m pip install -q pyyaml >/dev/null 2>&1; then
            PKG_PY="$PY"
            ok "pyyaml 已安装"
        else
            warn "pyyaml 安装失败（可能没网或缺 pip）。"
            warn "可手动安装：$PY -m pip install --user pyyaml"
            warn "或把 config.yaml 写成 JSON 格式（同目录，加载器会自动降级）。"
        fi
    fi
fi
RUN_PY="${PKG_PY:-$PY}"

# ---------- 3. adb（不自动安装，只提示） ----------
if command -v adb >/dev/null 2>&1; then
    ok "adb: $(command -v adb)"
else
    warn "PATH 里没有 adb。"
    if [ "$OS" = "macos" ]; then
        echo "  brew install --cask android-platform-tools"
    else
        echo "  Debian/Ubuntu: sudo apt install adb"
        echo "  Fedora:        sudo dnf install android-tools"
        echo "  Arch:          sudo pacman -S android-tools"
        echo "  或下载 platform-tools 后在 config.yaml → adb.path 指定绝对路径"
    fi
fi

# ---------- 4. 复制 AstrBot 插件 ----------
DEST="$HOME/.astrbot/data/plugins/xtc_qq_bridge"
mkdir -p "$DEST" || { err "无法创建 $DEST"; exit 1; }
copied=0
for f in main.py metadata.yaml _conf_schema.json README.md; do
    if [ -f "$SRC/astrbot_plugin_xtc_bridge/$f" ]; then
        cp -f "$SRC/astrbot_plugin_xtc_bridge/$f" "$DEST/" && copied=$((copied + 1))
    fi
done
if [ "$copied" -gt 0 ]; then ok "插件已复制到 $DEST"; else warn "插件源码缺失（$SRC/astrbot_plugin_xtc_bridge/）"; fi

# ---------- 5. 插件初始配置 ----------
PCFG="$HOME/.astrbot/data/config/xtc_qq_bridge_config.json"
if [ -f "$PCFG" ]; then
    ok "插件配置已存在 - 保持原样: $PCFG"
else
    mkdir -p "$(dirname "$PCFG")"
    if [ -f "$SRC/astrbot_plugin_xtc_bridge/plugin_config.example.json" ]; then
        cp -f "$SRC/astrbot_plugin_xtc_bridge/plugin_config.example.json" "$PCFG"
        ok "插件配置已初始化: $PCFG"
    else
        warn "找不到 plugin_config.example.json，插件首次启动时会自己生成默认配置"
    fi
fi

# ---------- 6. 桥接 config.yaml ----------
if [ -f "$SRC/config.yaml" ]; then
    ok "config.yaml 已存在 - 保持原样"
else
    cp -f "$SRC/config.example.yaml" "$SRC/config.yaml"
    ok "config.yaml 已从模板生成 - 运行前请先编辑"
fi

# ---------- 7. ADBKeyBoard 本地 APK（仅提示，不联网下载） ----------
if [ -f "$SRC/keyboardservice-debug.apk" ] || [ -f "$SRC/ADBKeyBoard.apk" ]; then
    ok "ADBKeyBoard APK 已就位（设备上没有时会自动安装这个本地文件）"
else
    warn "项目目录里没有 ADBKeyBoard 的 APK（keyboardservice-debug.apk / ADBKeyBoard.apk）。"
    warn "设备上已安装时可忽略；否则中文无法输入，请把 APK 放回项目根目录。"
fi

# ---------- 完成 ----------
cat <<EOF

============================================
  后续步骤
  1. 编辑 config.yaml：QQ 号、联系人、昵称、账密、token
     （token 需与插件配置一致）；按需设置 adb.port / adb.serial
  2. 启动一次 AstrBot，在 WebUI「插件管理」里启用 xtc_qq_bridge
  3. 配置 NapCat 适配器并登录 QQ 机器人
  4. 给机器人发一条消息（让插件学到平台 ID）
  5. 启动桥接：
       $RUN_PY main.py
     或先自检：
       $RUN_PY main.py --check
============================================
EOF

if [ "$RUN_PY" != "$PY" ]; then
    warn "注意：pyyaml 装在项目虚拟环境里，请用 $RUN_PY 运行（不要用系统的 $PY）"
fi
