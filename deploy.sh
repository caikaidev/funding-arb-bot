#!/bin/bash
# ============================================
# 一键部署脚本 — 在云服务器上运行
#
# 使用方式:
#   1. 将项目上传到服务器
#   2. 编辑 config.yaml 填入 API Key
#   3. 运行: bash deploy.sh
#
# 功能:
#   - 安装 Python 3.11 + 依赖
#   - 创建 systemd 服务（后台运行+开机自启）
#   - 默认以模拟模式启动
# ============================================

set -e

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }

# 项目目录
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="arb-bot"
PYTHON_BIN=""

echo ""
echo "=================================="
echo "  资金费率套利机器人 — 部署脚本"
echo "=================================="
echo ""

# ------------------------------------------------------------------
# Step 1: 检测/安装 Python
# ------------------------------------------------------------------
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
        warn "Python $PY_VER 版本过低，正在安装 3.11..."
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
        error "无法自动安装 Python，请手动安装 Python 3.10+"
    fi
    info "Python 安装完成"
fi

# ------------------------------------------------------------------
# Step 2: 创建虚拟环境 + 安装依赖
# ------------------------------------------------------------------
echo ""
echo "📦 Step 2: 安装依赖"

cd "$PROJECT_DIR"

if [ ! -d "venv" ]; then
    $PYTHON_BIN -m venv venv
    info "虚拟环境已创建"
fi

source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
info "依赖安装完成"

# ------------------------------------------------------------------
# Step 3: 检查配置
# ------------------------------------------------------------------
echo ""
echo "🔑 Step 3: 检查配置"

if [ ! -f "config.yaml" ]; then
    error "config.yaml 不存在! 请先配置 API Key"
fi

# 检查是否已填入 API Key
if grep -q "YOUR_BINANCE_API_KEY" config.yaml; then
    error "请先编辑 config.yaml 填入 Binance API Key"
fi

info "配置文件已就绪"

# 运行预检
echo ""
python preflight.py --monitor
echo ""

# ------------------------------------------------------------------
# Step 4: 读取资金参数
# ------------------------------------------------------------------
CAPITAL=$(python -c "
import yaml
with open('config.yaml') as f:
    c = yaml.safe_load(f)
print(c.get('initial_capital', 10000))
")

# ------------------------------------------------------------------
# Step 5: 创建 systemd 服务
# ------------------------------------------------------------------
echo "⚙️  Step 4: 配置后台服务"

VENV_PYTHON="$PROJECT_DIR/venv/bin/python"

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=Funding Rate Arbitrage Bot (Simulate Mode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_PYTHON} main.py --simulate --capital ${CAPITAL}
Restart=always
RestartSec=30
StartLimitIntervalSec=300
StartLimitBurst=5

# 日志
StandardOutput=append:${PROJECT_DIR}/logs/service.log
StandardError=append:${PROJECT_DIR}/logs/service.log

# 环境
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}

info "systemd 服务已创建并设为开机自启"

# ------------------------------------------------------------------
# Step 6: 启动
# ------------------------------------------------------------------
echo ""
echo "🚀 Step 5: 启动服务"

sudo systemctl start ${SERVICE_NAME}
sleep 2

if sudo systemctl is-active --quiet ${SERVICE_NAME}; then
    info "服务已启动!"
else
    error "服务启动失败，请检查: journalctl -u ${SERVICE_NAME} -n 50"
fi

# ------------------------------------------------------------------
# 完成
# ------------------------------------------------------------------
echo ""
echo "=================================="
echo "  ✅ 部署完成!"
echo "=================================="
echo ""
echo "  模式: 🧪 模拟交易 (不下真单)"
echo "  资金: \$${CAPITAL}"
echo ""
echo "  常用命令:"
echo "  ──────────────────────────────────────────"
echo "  查看状态    sudo systemctl status ${SERVICE_NAME}"
echo "  查看日志    tail -f ${PROJECT_DIR}/logs/service.log"
echo "  实时日志    journalctl -u ${SERVICE_NAME} -f"
echo "  查看结果    cd ${PROJECT_DIR} && source venv/bin/activate && python results.py"
echo "  导出 CSV    python results.py --export"
echo "  停止服务    sudo systemctl stop ${SERVICE_NAME}"
echo "  重启服务    sudo systemctl restart ${SERVICE_NAME}"
echo ""
echo "  一周后查看模拟结果:"
echo "  python results.py"
echo ""
echo "  确认收益满意后，切换到实盘:"
echo "  1. 给 API Key 加交易权限"
echo "  2. 编辑 /etc/systemd/system/${SERVICE_NAME}.service"
echo "     把 --simulate 改成去掉（直接 --capital ${CAPITAL}）"
echo "  3. sudo systemctl daemon-reload && sudo systemctl restart ${SERVICE_NAME}"
echo ""
