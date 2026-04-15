# 从零开始：准备清单 & 上手步骤

---

## 你需要准备什么

### 第一步：纯监控（只看不下单）

只需要 **2 样东西**：

| 序号 | 准备项 | 说明 | 获取方式 |
|------|--------|------|----------|
| 1 | **Binance API Key** (只读) | 用于读取费率和行情 | Binance → 账户 → API 管理 |
| 2 | **Python 3.10+** | 运行环境 | 本地电脑即可 |

Telegram 通知、交易权限、云服务器 —— **全部不需要**。

API Key 创建时：
```
✅ 勾选: 读取（Enable Reading）
❌ 不勾选: 现货交易、合约交易、提币
```
这样即使 Key 泄露也不会有任何损失。

### 第二步：实盘交易（加上这些）

| 序号 | 准备项 | 说明 | 必须? |
|------|--------|------|-------|
| 3 | **API Key 开启交易权限** | 现货+合约 | ✅ |
| 4 | **Telegram Bot** | 手机接收通知 | 推荐但非必须 |
| 5 | **云服务器** | 7×24 运行 | 推荐但非必须 |

---

## 详细步骤

### Step 1: 创建 Binance API Key

```
1. 登录 Binance → 右上角头像 → API 管理
2. 点击 "创建 API" → 选择 "系统生成"
3. 输入标签名，比如 "arb-bot-monitor"
4. 完成验证（邮箱+手机+2FA）
5. 记录下 API Key 和 Secret Key
   ⚠️ Secret Key 只显示一次，务必保存!

权限设置（监控阶段）:
  ✅ Enable Reading
  ❌ Enable Spot & Margin Trading    ← 先不开
  ❌ Enable Futures                  ← 先不开
  ❌ Enable Withdrawals              ← 永远不开

IP 限制（推荐）:
  填入你当前电脑的公网 IP
  查你的 IP: 浏览器打开 https://ifconfig.me
```

### Step 2: 安装 Python 环境

```bash
# macOS
brew install python@3.11

# Ubuntu / Debian
sudo apt update && sudo apt install python3.11 python3.11-venv

# Windows
# 从 https://python.org 下载安装，勾选 "Add to PATH"
```

### Step 3: 下载并配置项目

```bash
# 解压项目
tar xzf funding-arb-bot-v4.tar.gz
cd funding-arb-bot

# 创建虚拟环境（推荐）
python3 -m venv venv
source venv/bin/activate        # macOS/Linux
# venv\Scripts\activate         # Windows

# 安装依赖
pip install -r requirements.txt
```

编辑配置文件:
```bash
# 打开 config.yaml，只需改这两行:
vim config.yaml
```

```yaml
exchanges:
  binance:
    api_key: "你的 API Key 粘贴到这里"
    api_secret: "你的 Secret Key 粘贴到这里"

initial_capital: 10000   # 你计划投入的资金（监控阶段随便填一个数）
```

### Step 4: 运行预检

```bash
python preflight.py --monitor
```

输出示例:
```
📦 1. Python 环境
  ✅ Python 版本 — 3.11.8
  ✅ ccxt
  ✅ yaml
  ✅ aiohttp
  ✅ loguru
  ⏭️ 交易依赖 — 监控模式不需要

🔑 2. 配置文件
  ✅ config.yaml 存在
  ✅ Binance API Key — abc12345...6789
  ✅ Binance API Secret — 已配置
  ⚠️ Telegram Bot Token — 未配置（可选）
  ✅ 初始资金 — $10,000

🌐 3. 网络连通性
  ✅ Binance API 连通 — 已连接，326 个永续合约

  ⏭️ 4. API 权限 — 监控模式不需要

📁 5. 目录
  ✅ logs/ 目录

==================================================
  ✅ 全部检查通过! 可以启动 (监控模式)
     python main.py --monitor
     python main.py --monitor --capital 10000
==================================================
```

### Step 5: 启动监控

```bash
python main.py --monitor --capital 10000
```

你会看到:
```
=======================================================
  资金分配方案 | 模式: T1+T2
  初始资金:     $ 10,000.00
-------------------------------------------------------
  可套利资金:   $  8,000.00  (80%)
  链上底仓:     $  1,500.00  (15%)
  应急备用:     $    500.00  (5%)
-------------------------------------------------------
  T1 核心        $ 3,000.00/仓 × 2仓
  T2 主流        $ 2,500.00/仓 × 2仓
-------------------------------------------------------

  📡 监控模式 — 只扫描不下单，可放心运行

  扫描 22 个币种 (活跃等级: [1, 2])
  费率初筛: 5 个

  ======================================================================
   模拟开仓方案 (可用 $8,000)
  ======================================================================
   [T1 核] BTCUSDT | 做空 | 费率 +0.0210% | 年化  23.0% | 仓位 $ 3,000 | 日收 $1.89
   [T2 主] SOLUSDT | 做空 | 费率 +0.0350% | 年化  38.3% | 仓位 $ 2,500 | 日收 $2.63
  ──────────────────────────────────────────────────────────────────────
   已分配 $5,500 / $8,000 | 预估日收入 $4.52 | 预估月收入 $135
  ======================================================================
```

每 5 分钟自动刷新一次。你可以观察几天，了解：
- 哪些币种经常出现
- 费率波动规律
- 不同时段的机会多少

### Step 6: 准备上实盘时…

```
1. Binance API 管理 → 编辑你的 Key:
   ✅ Enable Spot & Margin Trading
   ✅ Enable Futures
   ❌ Enable Withdrawals （永远不开！）
   
   设置 IP 白名单（云服务器 IP）

2. (推荐) 创建 Telegram Bot:
   打开 Telegram → 搜索 @BotFather → 发送 /newbot
   → 记录 bot_token
   搜索 @userinfobot → 记录你的 chat_id
   填入 config.yaml

3. (推荐) 部署到云服务器:
   腾讯云/阿里云 新加坡节点 ~¥30/月
   
4. 运行完整预检:
   python preflight.py
   
5. 启动实盘:
   python main.py --capital 10000
```

---

## 文件清单

```
funding-arb-bot/
├── config.yaml       # 配置（填 API Key 和资金）
├── preflight.py      # 预检脚本 ← 第一个运行的
├── main.py           # 主程序（--monitor / 实盘）
├── capital.py        # 资金计算（比例 → 金额）
├── screener.py       # 动态币种筛选
├── monitor.py        # 费率监控 (ccxt 只读)
├── executor.py       # 下单引擎 (Binance SDK)
├── position.py       # 仓位记录 (SQLite)
├── notifier.py       # Telegram 通知
├── requirements.txt  # Python 依赖
└── README.md         # 收益测算
```

## 命令速查

```bash
# 预检
python preflight.py --monitor   # 检查监控模式
python preflight.py             # 检查完整模式

# 监控（不下单）
python main.py --monitor
python main.py --monitor --capital 5000
python main.py --monitor --capital 10000 --t3

# 实盘
python main.py --capital 10000
python main.py --capital 20000 --t3

# 资金分配预览
python capital.py
```
