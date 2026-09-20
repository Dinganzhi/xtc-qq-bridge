#!/usr/bin/env bash
# =============================================================================
#  在 Docker 里编译 Linux 版单文件可执行程序（宿主机不用装 gcc/工具链）
#
#  用法（Linux / macOS / WSL 主机）：
#    bash tools/build_in_docker.sh                     # x86_64，onefile，两个目标
#    bash tools/build_in_docker.sh --mode standalone   # 目录模式
#    IMAGE=python:3.11-slim bash tools/build_in_docker.sh
#    PLATFORM=linux/arm64 bash tools/build_in_docker.sh   # 编 arm64（需 QEMU/binfmt，较慢）
#
#  说明：
#  * 默认用轻量的 python:3.12-slim + 临时装 gcc/patchelf（产物 glibc 门槛 = Debian 12，2.36）；
#    想要"老系统也能跑"（glibc 2.17）可换 Nuitka 官方编译镜像：
#      IMAGE=androsh7/nuitka-compiler:latest bash tools/build_in_docker.sh
#  * 产物落在宿主机 dist/（工作目录被挂载进容器）。
#  * Nuitka 不能交叉编译：Windows 产物必须在 Windows 上编，macOS 必须在 macOS 上编。
# =============================================================================
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SRC"

IMAGE="${IMAGE:-python:3.12-slim}"
PLATFORM="${PLATFORM:-}"
ARGS="${*:-}"

if ! command -v docker >/dev/null 2>&1; then
    echo "[错误] 没找到 docker。装 Docker Desktop / docker-ce 后再试；"
    echo "        或者直接在 Linux 上跑： bash build.sh"
    exit 1
fi

PLATFORM_ARG=()
[ -n "$PLATFORM" ] && PLATFORM_ARG=(--platform "$PLATFORM")

echo "[信息] 镜像=$IMAGE 平台=${PLATFORM:-默认} 参数=${ARGS:-（默认 onefile all）}"

docker run --rm "${PLATFORM_ARG[@]}" -v "$SRC":/app -w /app -e NUITKA_CACHE_DIR=/app/.nuitka-cache \
    "$IMAGE" bash -lc '
        set -e
        if ! command -v gcc >/dev/null 2>&1; then
            echo "[容器] 安装 gcc/g++/patchelf ..."
            (apt-get update -qq && apt-get install -y -qq --no-install-recommends gcc g++ patchelf) \
              >/dev/null 2>&1 || echo "[容器] 警告：apt 安装失败，若镜像自带编译器可忽略"
        fi
        python -m pip install -q -U -r requirements-build.txt
        python tools/build_nuitka.py '"$ARGS"'
    '

echo
echo "[完成] 产物在 $SRC/dist/"
ls -la "$SRC/dist" 2>/dev/null || true
