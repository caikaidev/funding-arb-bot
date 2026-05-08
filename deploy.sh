#!/bin/bash
# ============================================
# 一键部署脚本 — 在云服务器上运行
#
# 使用方式:
#   bash deploy.sh simulate [capital] [--t3]   # 模拟（阶段 2）
#   bash deploy.sh live <capital> [--t3]       # 实盘（阶段 3）
#
# 阶段 1（--monitor 数据采集）请手动跑, 不进 systemd:
#   nohup python main.py --monitor --capital 10000 \
#       > logs/monitor-$(date +%Y%m%d).log 2>&1 &
#
# 功能:
#   - 安装 Python 3.11 + 依赖
#   - 模式独立的 systemd 服务（arb-bot-simulate / arb-bot-live）
#   - 实盘模式强制二次确认 + 资金上限 + 完整预检
#   - 切换模式时自动停用另一个模式, 防止并行
# ============================================

set -e

# ==================== 参数 & 常量 ====================

# 实盘资金安全上限（超过此值直接拒绝部署, 防止手滑/误操作）
# 如需突破, 请编辑本脚本, 并自觉承担风险
LIVE_CAPITAL_MAX=2000

# 颜色
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
    bash deploy.sh simulate [capital] [--t3 | --no-t3]
    bash deploy.sh live <capital> [--t3 | --no-t3]

模式:
    simulate    模拟交易。capital 省略时从 config.yaml 的 initial_capital 读取
    live        实盘交易。capital 必填且 <= \$${LIVE_CAPITAL_MAX}（安全上限）

示例:
    bash deploy.sh simulate              # 阶段 2: 模拟, 资金从 config
    bash deploy.sh simulate 5000 --t3    # 阶段 2: 模拟 5000 + 启用 T3
    bash deploy.sh live 500              # 阶段 3: 实盘 500（需二次确认）

阶段 1（monitor 数据采集）不走本脚本, 直接:
    mkdir -p logs
    nohup python main.py --monitor --capital 10000 \\
        > logs/monitor-\$(date +%Y%m%d).log 2>&1 &

EOF
}

# ---------- 解析 mode ----------
if [ $# -lt 1 ]; then usage; exit 1; fi

MODE="$1"; shift
case "$MODE" in
    simulate|live) ;;
    -h|--help) usage; exit 0 ;;
    *) error "未知模式: '$MODE'（只接受 simulate 或 live）" ;;
esac

# ---------- 解析 capital（可选, 必须是数字） ----------
CAPITAL=""
if [ $# -gt 0 ] && [[ "$1" =~ ^[0-9]+$ ]]; then
    CAPITAL="$1"; shift
fi

# ---------- 解析剩余 flag ----------
EXTRA_FLAGS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --t3|--no-t3) EXTRA_FLAGS="$EXTRA_FLAGS $1"; shift ;;
        *) error "未知参数: '$1'（支持 --t3 / --no-t3）" ;;
    esac
done

# ---------- 模式级规则校验 ----------
if [ "$MODE" = "live" ]; then
    [ -z "$CAPITAL" ] && error "实盘模式必须显式指定资金: bash deploy.sh live <capital>"
    if [ "$CAPITAL" -gt "$LIVE_CAPITAL_MAX" ]; then
        error "实盘资金 \$${CAPITAL} 超过安全上限 \$${LIVE_CAPITAL_MAX}。如需更高, 请编辑脚本 LIVE_CAPITAL_MAX"
    fi
fi

SERVICE_NAME="arb-bot-${MODE}"
OTHER_MODE=$([ "$MODE" = "simulate" ] && echo "live" || echo "simulate")
OTHER_SERVICE="arb-bot-${OTHER_MODE}"

# 项目目录
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN=""

echo ""
echo "=================================="
echo "  资金费率套利机器人 — 部署脚本"
echo "  模式: $MODE"
echo "=================================="
echo ""

# ==================== Step 1: Python ====================
echo "📦 Step 1: Python 环境"

if command -v python3.11 &>/dev/null; then
    PYTHON_BIN="python3.11"
    info "已安装 Python 3.11"
elif command -v python3 &>/dev/null; then
    PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$(echo $PY_VER | cut -d. -f1)
    PY_MINOR=$(echo $PY_VER | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
        PYTHON_BIN="python3"
        info "已安装 Python $PY_VER"
    else
        warn "Python $PY_VER 版本过低, 正在安装 3.11..."
    fi
fi

if [ -z "$PYTHON_BIN" ]; then
    if command -v apt-get &>/dev/null; then
        sudo apt-get update -qq
        sudo apt-get install -y -qq python3.11 python3.11-venv python3-pip
        PYTHON_BIN="python3.11"
    elif command -v yum &>/dev/null; then
        sudo yum install -y python3.11
        PYTHON_BIN="python3.11"
    else
        error "无法自动安装 Python, 请手动安装 Python 3.10+"
    fi
    info "Python 安装完成"
fi

# ==================== Step 2: 虚拟环境 + 依赖 ====================
echo ""
echo "📦 Step 2: 安装依赖"

cd "$PROJECT_DIR"

if [ ! -d ".venv" ]; then
    $PYTHON_BIN -m venv .venv
    info "虚拟环境已创建"
fi

source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
info "依赖安装完成"

# ==================== Step 3: 配置检查 ====================
echo ""
echo "🔑 Step 3: 检查配置"

[ ! -f "config.yaml" ] && error "config.yaml 不存在! 请先配置 API Key"

# 强化的 API Key 检查（不再只是 grep 占位符字符串）
API_KEY=$(python -c "
import yaml, sys
try:
    c = yaml.safe_load(open('config.yaml'))
    k = ((c.get('exchanges') or {}).get('binance') or {}).get('api_key', '') or ''
    print(k.strip())
except Exception as e:
    sys.exit(1)
" 2>/dev/null || echo "")

if [ -z "$API_KEY" ]; then
    error "config.yaml 里 exchanges.binance.api_key 未设置"
fi
if [ "$API_KEY" = "YOUR_BINANCE_API_KEY" ]; then
    error "config.yaml 里 exchanges.binance.api_key 还是占位符, 请填入真实 Key"
fi
if [ "${#API_KEY}" -lt 20 ]; then
    error "config.yaml 里 exchanges.binance.api_key 长度异常（${#API_KEY} 位）, 请检查"
fi

info "API Key 格式检查通过"

# 如果没手动指定 CAPITAL（simulate 模式）, 从 config 读
if [ -z "$CAPITAL" ]; then
    CAPITAL=$(python -c "import yaml; print(yaml.safe_load(open('config.yaml')).get('initial_capital', 10000))")
    hint "未指定资金, 使用 config.yaml 的 initial_capital = \$$CAPITAL"
fi

# ==================== Step 4: 预检 ====================
echo ""
echo "🔍 Step 4: 运行预检"

# transfer.enabled=true 时（默认）需要 API 开启 "Enable Internal Transfer"
TRANSFER_ENABLED=$(python -c "
import yaml
c = yaml.safe_load(open('config.yaml'))
print(str((c.get('transfer') or {}).get('enabled', False)).lower())
" 2>/dev/null || echo "false")

if [ "$TRANSFER_ENABLED" = "true" ]; then
    hint "transfer.enabled=true：需要 API Key 开启「Enable Internal Transfer」（不需提现权限）"
    hint "  用途：开/平仓后 spot↔futures 钱包再平衡 + 强平防护补保证金"
fi

if [ "$MODE" = "live" ]; then
    hint "实盘模式: 运行完整预检（含下单权限 / 安全模式 / 余额 ≥ \$$CAPITAL 硬卡）"
    python preflight.py --live --capital "$CAPITAL"
else
    hint "模拟模式: 只跑只读预检"
    python preflight.py --monitor
fi

# ==================== Step 5: 实盘二次确认 ====================
if [ "$MODE" = "live" ]; then
    echo ""
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}  ⚠️  你正在部署【实盘】交易服务${NC}"
    echo -e "${RED}========================================${NC}"
    echo -e "  模式:   ${RED}LIVE（下真实订单）${NC}"
    echo -e "  资金:   ${YELLOW}\$$CAPITAL${NC}"
    echo -e "  额外:   ${EXTRA_FLAGS:-（无）}"
    echo -e "  账户:   由 config.yaml 的 API Key 决定"
    echo -e "  服务:   $SERVICE_NAME.service"
    echo ""
    echo -e "  ${YELLOW}一旦启动, 机器人会按扫描周期真金白银下单。${NC}"
    echo -e "  ${YELLOW}确认前请复核 preflight 输出, 特别是账户余额与权限。${NC}"
    echo ""
    read -p "请精确输入 'YES-LIVE' 继续, 其他任何输入都会取消: " CONFIRM
    if [ "$CONFIRM" != "YES-LIVE" ]; then
        error "未收到 YES-LIVE 确认, 已取消部署"
    fi
    info "实盘确认通过"
fi

# ==================== Step 6: 停用对立模式服务 ====================
echo ""
echo "🔄 Step 6: 处理另一模式的服务"

if sudo systemctl list-unit-files 2>/dev/null | grep -q "^${OTHER_SERVICE}.service"; then
    if sudo systemctl is-active --quiet "$OTHER_SERVICE"; then
        warn "检测到 $OTHER_SERVICE 正在运行, 将停止并禁用（防止与 $SERVICE_NAME 并行）"
        sudo systemctl stop "$OTHER_SERVICE"
    fi
    if sudo systemctl is-enabled --quiet "$OTHER_SERVICE" 2>/dev/null; then
        sudo systemctl disable "$OTHER_SERVICE" 2>/dev/null || true
        info "已禁用 $OTHER_SERVICE 开机自启"
    fi
else
    info "无另一模式的历史服务, 跳过"
fi

# ==================== Step 7: 创建 systemd 服务 ====================
echo ""
echo "⚙️  Step 7: 配置 systemd 服务"

mkdir -p "${PROJECT_DIR}/logs"

VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

if [ "$MODE" = "simulate" ]; then
    EXEC_ARGS="--simulate --capital $CAPITAL$EXTRA_FLAGS"
    DESC="Funding Rate Arbitrage Bot (SIMULATE, \$$CAPITAL)"
else
    EXEC_ARGS="--capital $CAPITAL$EXTRA_FLAGS"
    DESC="Funding Rate Arbitrage Bot (LIVE, \$$CAPITAL)"
fi

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=$DESC
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_PYTHON} main.py $EXEC_ARGS
Restart=always
RestartSec=30
StartLimitIntervalSec=300
StartLimitBurst=5

# 内存约束：912 MiB VPS, RAM 50/65% 上限 + 1.5G swap 兜底
# 实测启动尖峰 ~334MB（ccxt load_markets + fetch_funding_rates + exchangeInfo），
# 原 300M/400M 配额导致 cgroup memory reclaim 触发剧烈 swap thrashing,
# 进程进入 D 态（uninterruptible disk sleep），asyncio 事件循环死锁。
# 当前配额给启动尖峰 1.8× 余量；swap 兜底防 cgroup reclaim 死锁。
MemoryHigh=500M
MemoryMax=600M

# 关闭超时：默认 90s；缩短到 15s 让 systemctl restart 时旧进程更快退出，
# 避免新旧进程同时存在的尖峰双吞内存
TimeoutStopSec=15s
KillMode=mixed

# 日志
StandardOutput=append:${PROJECT_DIR}/logs/${SERVICE_NAME}.log
StandardError=append:${PROJECT_DIR}/logs/${SERVICE_NAME}.log

# 环境
Environment=PYTHONUNBUFFERED=1
# 日志展示时区：SGT (UTC+8)。业务代码所有时间戳显式 datetime.now(timezone.utc)，不受影响。
Environment=TZ=Asia/Singapore

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}

info "systemd 服务已创建: $SERVICE_NAME"

# ==================== Step 8: 启动 ====================
echo ""
echo "🚀 Step 8: 启动服务"

# 先 restart, 避免已存在的服务吃老命令
sudo systemctl restart ${SERVICE_NAME}
sleep 2

if sudo systemctl is-active --quiet ${SERVICE_NAME}; then
    info "服务已启动!"
else
    error "服务启动失败, 请检查: journalctl -u ${SERVICE_NAME} -n 50"
fi

# ==================== 完成 ====================
echo ""
echo "=================================="
echo "  ✅ 部署完成!"
echo "=================================="
echo ""
if [ "$MODE" = "simulate" ]; then
    echo "  模式: 🧪 模拟交易（不下真单）"
else
    echo -e "  模式: ${RED}💰 实盘交易（下真实订单）${NC}"
fi
echo "  资金: \$${CAPITAL}"
[ -n "$EXTRA_FLAGS" ] && echo "  附加: $EXTRA_FLAGS"
echo "  服务: $SERVICE_NAME"
echo ""
echo "  常用命令:"
echo "  ──────────────────────────────────────────"
echo "  查看状态    sudo systemctl status ${SERVICE_NAME}"
echo "  查看日志    tail -f ${PROJECT_DIR}/logs/${SERVICE_NAME}.log"
echo "  实时日志    journalctl -u ${SERVICE_NAME} -f"
echo "  查看结果    source .venv/bin/activate && python results.py$([ "$MODE" = "live" ] && echo " --db arbitrage.db")"
echo "  停止服务    bash stop.sh ${MODE}"
echo "  快速重启    bash stop.sh ${MODE} --restart"
echo ""
echo "  切换模式（直接重跑本脚本, 无需手改 service 文件）:"
if [ "$MODE" = "simulate" ]; then
    echo "    升阶段 3（实盘小额）: bash deploy.sh live 500"
else
    echo "    回退到模拟:         bash deploy.sh simulate"
fi
echo ""
echo "  阶段 1（monitor 数据采集, 不进 systemd）:"
echo "    nohup python main.py --monitor --capital 10000 \\"
echo "        > logs/monitor-\$(date +%Y%m%d).log 2>&1 &"
echo ""
