#!/bin/bash
# ============================================
# 停止脚本 — 停止/禁用/卸载 systemd 服务
#
# 使用方式:
#   bash stop.sh              # 仅停止服务（保留开机自启）
#   bash stop.sh --disable    # 停止 + 取消开机自启
#   bash stop.sh --uninstall  # 停止 + 取消自启 + 删除 service 文件
#
# 数据库 / 日志 / 配置 / 代码 均不会被删除
# ============================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }

SERVICE_NAME="arb-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

MODE="stop"
case "${1:-}" in
    ""|-h|--help)
        [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && {
            sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
        }
        MODE="stop" ;;
    --disable)   MODE="disable" ;;
    --uninstall) MODE="uninstall" ;;
    *) error "未知参数: $1（可用: --disable / --uninstall）" ;;
esac

echo ""
echo "=================================="
echo "  资金费率套利机器人 — 停止脚本"
echo "=================================="
echo ""

# ------------------------------------------------------------------
# Step 1: 检查 service 是否存在
# ------------------------------------------------------------------
if [ ! -f "$SERVICE_FILE" ]; then
    warn "未检测到 ${SERVICE_NAME} 服务（${SERVICE_FILE} 不存在）"
    if [ "$MODE" = "uninstall" ]; then
        info "已是卸载状态，无需操作"
        exit 0
    fi
    # 仍尝试 stop，万一是残留进程
fi

# ------------------------------------------------------------------
# Step 2: 停止服务
# ------------------------------------------------------------------
echo "🛑 停止服务"
if sudo systemctl is-active --quiet ${SERVICE_NAME} 2>/dev/null; then
    sudo systemctl stop ${SERVICE_NAME}
    info "服务已停止"
else
    info "服务当前未运行"
fi

# ------------------------------------------------------------------
# Step 3: 禁用 / 卸载（可选）
# ------------------------------------------------------------------
if [ "$MODE" = "disable" ] || [ "$MODE" = "uninstall" ]; then
    if sudo systemctl is-enabled --quiet ${SERVICE_NAME} 2>/dev/null; then
        sudo systemctl disable ${SERVICE_NAME}
        info "已取消开机自启"
    else
        info "开机自启未启用"
    fi
fi

if [ "$MODE" = "uninstall" ]; then
    if [ -f "$SERVICE_FILE" ]; then
        sudo rm -f "$SERVICE_FILE"
        sudo systemctl daemon-reload
        sudo systemctl reset-failed ${SERVICE_NAME} 2>/dev/null || true
        info "已删除 ${SERVICE_FILE}"
    fi
fi

# ------------------------------------------------------------------
# 完成
# ------------------------------------------------------------------
echo ""
echo "=================================="
case "$MODE" in
    stop)
        echo "  ✅ 服务已停止（保留配置 & 自启）"
        echo "=================================="
        echo ""
        echo "  常用命令:"
        echo "  ──────────────────────────────────────────"
        echo "  再次启动    sudo systemctl start ${SERVICE_NAME}"
        echo "  查看状态    sudo systemctl status ${SERVICE_NAME}"
        echo "  取消自启    bash stop.sh --disable"
        echo "  彻底卸载    bash stop.sh --uninstall"
        ;;
    disable)
        echo "  ✅ 服务已停止并取消开机自启"
        echo "=================================="
        echo ""
        echo "  再次启用    sudo systemctl enable --now ${SERVICE_NAME}"
        echo "  彻底卸载    bash stop.sh --uninstall"
        ;;
    uninstall)
        echo "  ✅ 服务已彻底卸载"
        echo "=================================="
        echo ""
        echo "  代码 / 配置 / 数据库 / 日志 均未删除"
        echo "  重新部署    bash deploy.sh"
        ;;
esac
echo ""
