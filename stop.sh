#!/bin/bash
# ============================================
# 运行时控制脚本 — 停止 / 重启 / 禁用 / 卸载 systemd 服务
#
# 使用方式:
#   bash stop.sh <live|simulate>              # 仅停止（保留开机自启）
#   bash stop.sh <live|simulate> --restart    # 重启（不重新部署/不预检/不二次确认）
#   bash stop.sh <live|simulate> --disable    # 停止 + 取消开机自启
#   bash stop.sh <live|simulate> --uninstall  # 停止 + 取消自启 + 删除 service 文件
#
# 数据库 / 日志 / 配置 / 代码 均不会被删除
# 部署 / 修改资金或 flag, 请用 deploy.sh
# ============================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
hint()  { echo -e "${BLUE}[i]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }

usage() {
    cat <<EOF

用法:
    bash stop.sh <live|simulate> [操作]

模式:
    live          对应 arb-bot-live.service
    simulate      对应 arb-bot-simulate.service

操作 (默认: 仅停止):
    --restart     重启服务（跳过 pip / preflight / 二次确认）
    --disable     停止 + 取消开机自启
    --uninstall   停止 + 取消自启 + 删除 service 文件

部署 / 修改资金或 flag, 请用 deploy.sh
EOF
}

# ---------- 解析参数 ----------
if [ $# -lt 1 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    usage
    exit 0
fi

MODE="$1"; shift
case "$MODE" in
    live|simulate) ;;
    *) error "未知模式: '$MODE'（只接受 live 或 simulate）" ;;
esac

ACTION="stop"
if [ $# -gt 0 ]; then
    case "$1" in
        --restart)   ACTION="restart" ;;
        --disable)   ACTION="disable" ;;
        --uninstall) ACTION="uninstall" ;;
        *) error "未知操作: '$1'（支持 --restart / --disable / --uninstall）" ;;
    esac
fi

SERVICE_NAME="arb-bot-${MODE}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "=================================="
echo "  资金费率套利机器人 — 运行时控制"
echo "  服务: $SERVICE_NAME"
echo "  操作: $ACTION"
echo "=================================="
echo ""

# ---------- 服务存在性检查 ----------
if [ ! -f "$SERVICE_FILE" ]; then
    warn "未检测到 ${SERVICE_NAME} 服务（${SERVICE_FILE} 不存在）"
    if [ "$ACTION" = "uninstall" ]; then
        info "已是卸载状态，无需操作"
        exit 0
    fi
    if [ "$ACTION" = "restart" ]; then
        error "服务不存在，无法重启。请先运行 deploy.sh 部署"
    fi
    # stop / disable 仍尝试执行，万一是残留进程
fi

# ---------- restart：单独路径，做完即返回 ----------
if [ "$ACTION" = "restart" ]; then
    echo "🔄 重启服务"
    sudo systemctl restart "$SERVICE_NAME"
    sleep 2
    if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
        info "重启完成"
        echo ""
        echo "  查看状态    sudo systemctl status ${SERVICE_NAME}"
        echo "  查看日志    tail -f ${PROJECT_DIR}/logs/${SERVICE_NAME}.log"
    else
        error "重启失败, 请检查: journalctl -u ${SERVICE_NAME} -n 50"
    fi
    echo ""
    exit 0
fi

# ---------- 停止 ----------
echo "🛑 停止服务"
if sudo systemctl is-active --quiet ${SERVICE_NAME} 2>/dev/null; then
    sudo systemctl stop ${SERVICE_NAME}
    info "服务已停止"
else
    info "服务当前未运行"
fi

# ---------- 禁用 / 卸载（可选） ----------
if [ "$ACTION" = "disable" ] || [ "$ACTION" = "uninstall" ]; then
    if sudo systemctl is-enabled --quiet ${SERVICE_NAME} 2>/dev/null; then
        sudo systemctl disable ${SERVICE_NAME}
        info "已取消开机自启"
    else
        info "开机自启未启用"
    fi
fi

if [ "$ACTION" = "uninstall" ]; then
    if [ -f "$SERVICE_FILE" ]; then
        sudo rm -f "$SERVICE_FILE"
        sudo systemctl daemon-reload
        sudo systemctl reset-failed ${SERVICE_NAME} 2>/dev/null || true
        info "已删除 ${SERVICE_FILE}"
    fi
fi

# ---------- 完成 ----------
echo ""
echo "=================================="
case "$ACTION" in
    stop)
        echo "  ✅ 服务已停止（保留配置 & 自启）"
        echo "=================================="
        echo ""
        echo "  常用命令:"
        echo "  ──────────────────────────────────────────"
        echo "  再次启动    sudo systemctl start ${SERVICE_NAME}"
        echo "  快速重启    bash stop.sh ${MODE} --restart"
        echo "  查看状态    sudo systemctl status ${SERVICE_NAME}"
        echo "  取消自启    bash stop.sh ${MODE} --disable"
        echo "  彻底卸载    bash stop.sh ${MODE} --uninstall"
        ;;
    disable)
        echo "  ✅ 服务已停止并取消开机自启"
        echo "=================================="
        echo ""
        echo "  再次启用    sudo systemctl enable --now ${SERVICE_NAME}"
        echo "  彻底卸载    bash stop.sh ${MODE} --uninstall"
        ;;
    uninstall)
        echo "  ✅ 服务已彻底卸载"
        echo "=================================="
        echo ""
        echo "  代码 / 配置 / 数据库 / 日志 均未删除"
        echo "  重新部署    bash deploy.sh ${MODE} <capital>"
        ;;
esac
echo ""
