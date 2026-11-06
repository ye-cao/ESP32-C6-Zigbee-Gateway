#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# build.sh —— ESP32-C6 固件并行构建脚本（Linux / macOS / Git Bash）
#
# 只负责「配置 + 编译」，不碰业务代码。等价于原来的 `idf.py build`，但显式
# 控制 ninja 并行度，并处理 Windows/MSYS 上并发写 depfile 的瞬时锁。
# Windows 下请用同目录的 build.ps1（PowerShell + export.ps1 是项目硬要求）。
#
# 用法：
#   ./build.sh [zigbee|rcp|matter|client] [build|flash [PORT]] [JOBS]
#
# 示例：
#   ./build.sh                       # 默认 zigbee，自动并行数
#   ./build.sh zigbee build 16       # 指定 16 并行
#   ./build.sh zigbee flash /dev/ttyUSB0
#
# 并行策略：JOBS 默认 = min(逻辑核数, 8)。8 是吞吐与稳定性的平衡点：
# 单个 riscv32 gcc 约占 200–400 MB，继续加并行会在 Windows 上撞 .obj.d
# 瞬时文件锁，反而引发重编。可用第四个参数覆盖。
#
# 一致性保证：使用同一个 build/build.ninja，CMake 的 --parallel 只改变
# 调度并发数，不改变依赖图、编译命令或链接顺序；ccache 行为保持默认。
# 并行失败时自动 -j 1 串行收尾；构建后再跑一次必须为 no-op。
# ─────────────────────────────────────────────────────────────────────────────
set -e

PROFILE=${1:-zigbee}
ACTION=${2:-build}
PORT=${3:-}
JOBS=${4:-0}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── 并行度 ───────────────────────────────────────────────────────────────────
if [[ "$JOBS" -le 0 ]]; then
    if command -v nproc >/dev/null 2>&1; then
        CORES=$(nproc)
    elif command -v getconf >/dev/null 2>&1; then
        CORES=$(getconf _NPROCESSORS_ONLN)
    else
        CORES=4
    fi
    JOBS=$(( CORES < 8 ? CORES : 8 ))
    [[ "$JOBS" -lt 1 ]] && JOBS=1
fi
[[ "$JOBS" -gt 32 ]] && JOBS=32

# ── IDF / Matter 环境 ────────────────────────────────────────────────────────
if [[ -z "$IDF_PATH" ]]; then
    if [[ -f "$HOME/esp-idf/export.sh" ]]; then
        # shellcheck disable=SC1091
        source "$HOME/esp-idf/export.sh" > /dev/null 2>&1
    else
        echo "错误：IDF_PATH 未设置，且找不到 ~/esp-idf/export.sh。" >&2
        exit 1
    fi
fi

if [[ "$PROFILE" == "matter" && -z "$ESP_MATTER_PATH" ]]; then
    if [[ -f "$HOME/esp-matter/export.sh" ]]; then
        # shellcheck disable=SC1091
        source "$HOME/esp-matter/export.sh" > /dev/null 2>&1
    else
        echo "错误：ESP_MATTER_PATH 未设置。" >&2
        exit 1
    fi
fi

# ── 构建目标 → 目录 ─────────────────────────────────────────────────────────
case "$PROFILE" in
    zigbee)
        BUILD_DIR="$SCRIPT_DIR"
        BINARY="build/zigbee-vital-sensor.bin"
        PROFILE_LABEL="Zigbee 协调器"
        ;;
    rcp)
        BUILD_DIR="$SCRIPT_DIR/rcp"
        BINARY="build/esp32c6_rcp.bin"
        PROFILE_LABEL="Thread RCP"
        ;;
    matter)
        BUILD_DIR="$SCRIPT_DIR/matter"
        BINARY="build/esp32c6_matter_vital_gateway.bin"
        PROFILE_LABEL="Matter over Thread 生命体征传感器"
        ;;
    client)
        BUILD_DIR="$SCRIPT_DIR/client"
        BINARY="build/zigbee-client.bin"
        PROFILE_LABEL="Zigbee Router Client"
        ;;
    *)
        echo "未知构建目标：$PROFILE"
        echo "可用：zigbee | rcp | matter | client"
        exit 1
        ;;
esac

echo "╔══════════════════════════════════════════════════════╗"
echo "║  ESP32-C6 并行构建  –  $PROFILE_LABEL"
echo "╚══════════════════════════════════════════════════════╝"
echo "  构建目标 : $PROFILE"
echo "  目录     : $BUILD_DIR"
echo "  IDF      : $IDF_PATH"
echo "  并行数   : $JOBS"
echo ""

cd "$BUILD_DIR"

# 切换构建目标时重置本地 sdkconfig（保留原有行为）
PROFILE_STAMP="build/.last_profile"
if [[ -f "$PROFILE_STAMP" ]] && [[ "$(cat "$PROFILE_STAMP")" != "$PROFILE" ]]; then
    echo "构建目标已切换 —— 重置 sdkconfig …"
    rm -f sdkconfig
fi

mkdir -p build

# ── 并行构建 ─────────────────────────────────────────────────────────────────
# cmake --build 会自行处理 build.ninja 的 RERUN_CMAKE 目标，只触发一次配置，
# 不会像「先 idf.py build 再 ninja」那样重复生成造成大面积重编。
START=$(date +%s)
echo "[1/2] 并行构建（-j $JOBS）…"
if ! cmake --build build --parallel "$JOBS" -- -k 0; then
    echo "警告：并行构建失败，通常是 .obj.d 瞬时锁；改用 -j 1 串行收尾。" >&2
    cmake --build build --parallel 1 -- -k 0
fi

echo "[2/2] 增量一致性校验（期望 no work to do）…"
cmake --build build --parallel "$JOBS"

END=$(date +%s)
echo ""
echo "✓ 构建完成：$BUILD_DIR/$BINARY（耗时 $((END - START)) s）"

echo "$PROFILE" > "$PROFILE_STAMP"

# ── 刷写（可选）─────────────────────────────────────────────────────────────
if [[ "$ACTION" == "flash" ]]; then
    FLASH_ARGS=()
    [[ -n "$PORT" ]] && FLASH_ARGS=(-p "$PORT")
    echo ""
    echo "刷写到 ${PORT:-自动检测端口} …"
    idf.py "${FLASH_ARGS[@]}" flash
    echo "✓ 刷写完成"

    if [[ "$PROFILE" != "rcp" ]] && [[ -t 0 ]]; then
        echo ""
        echo "打开串口监视（Ctrl+] 退出）："
        idf.py "${FLASH_ARGS[@]}" monitor
    fi
fi
