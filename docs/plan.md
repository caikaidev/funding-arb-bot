# 资金费率套利机器人 — 优化路线图

> 基于前期讨论的完整总结，作为后续开发的参考文档
> 
> 当前版本：v4（已提交 GitHub）
> 本文档：规划 v5 ~ v7 的优化方向

---

## 目录

- [第一部分：现有加密套利优化](#第一部分现有加密套利优化)
  - [P0 必须做](#p0-必须做上线前)
  - [P1 重要优化](#p1-重要优化稳定运行后)
  - [P2 收益提升](#p2-收益提升持续迭代)
- [第二部分：美股链上套利兼容与拓展](#第二部分美股链上套利兼容与拓展)
  - [架构改造](#一架构改造多资产抽象层)
  - [美股适配](#二美股特有逻辑)
  - [监控先行](#三监控先行方案)
  - [实盘切入条件](#四实盘切入条件)
- [附录](#附录)

---

## 第一部分：现有加密套利优化

### P0 必须做（上线前）

#### 1. BNB 手续费抵扣

**背景：** VIP 0 现货 Taker 费率 0.10%，用 BNB 抵扣后降至 0.075%，合约从 0.04% 降至 0.036%。对于频繁换仓的策略，每年可多赚约 0.7% 年化。

**改动：**
- config.yaml 增加 `use_bnb_discount: true` 配置项
- executor.py 开仓前检查 BNB 余额是否足够抵扣（预估手续费 × 1.2 的 BNB 等值）
- 当 BNB 余额不足时通过 Telegram 提醒补充
- 费用计算模块 (`_calc_fees`) 根据是否有 BNB 使用不同费率

```yaml
# config.yaml 新增
fees:
  use_bnb_discount: true
  spot_taker_bnb: 0.00075       # BNB 抵扣后
  futures_taker_bnb: 0.00036
  bnb_min_balance: 0.05         # 低于此 BNB 余额时告警
```

#### 2. 开仓顺序优化：合约先行

**背景：** 当前 `asyncio.gather` 并发下单，两笔单几乎同时发出。但如果必须有先后，应该合约做空先行。因为如果合约空单成交后价格暴跌，空单在赚钱，你可以用更低的价格买入现货，反而有利；反过来先买现货再遇暴跌则亏损。

**改动：**
- executor.py 中 `open_arbitrage` 方法增加一个可选参数 `order_priority`
- 默认 `concurrent`（当前并发模式），可选 `futures_first`
- `futures_first` 模式：先发合约单，确认成交后 200ms 内发现货单
- 回滚逻辑不变

```python
# executor.py
async def open_arbitrage(self, ..., order_priority="concurrent"):
    if order_priority == "futures_first":
        futures_result = await self._exec_futures(...)
        if futures_result.success:
            spot_result = await self._exec_spot(...)
            if not spot_result.success:
                await self._rollback_futures(...)
    else:
        # 当前并发模式
        ...
```

#### 3. 分批下单（大仓位防滑点）

**背景：** 当单仓金额超过一定阈值时，市价单会吃穿多档订单簿，导致滑点过大。

**各币种的分批阈值建议：**

| 币种 | 单笔上限 | 超过后分批 |
|------|---------|-----------|
| BTC  | $20,000 | 不需要分批 |
| ETH  | $15,000 | 2 批 |
| SOL  | $8,000  | 2-3 批 |
| 其他 T2 | $5,000 | 2-3 批 |
| T3 | $2,000 | 不应该超过此值 |

**改动：**
- executor.py 增加 `_split_order` 方法
- 根据 symbol 查表获取分批阈值
- 超过阈值时自动拆分为多笔，间隔 500ms-1s
- 每批仍然是现货+合约并发

```python
# executor.py
SPLIT_THRESHOLDS = {
    "BTCUSDT": 20000,
    "ETHUSDT": 15000,
    "SOLUSDT": 8000,
    "DEFAULT": 5000,
}

def _split_order(self, symbol, usdt_amount):
    threshold = SPLIT_THRESHOLDS.get(symbol, SPLIT_THRESHOLDS["DEFAULT"])
    if usdt_amount <= threshold:
        return [usdt_amount]
    n = math.ceil(usdt_amount / threshold)
    return [usdt_amount / n] * n
```

#### 4. 基差感知开平仓

**背景：** 现货和合约之间有基差（basis），开仓时基差为正、平仓时基差为负，中间的差值就是额外损失。反之则是额外收益。

**改动：**
- monitor.py 增加 `fetch_basis` 方法，同时获取现货和合约价格计算基差
- 开仓前检查基差：基差过大（如 > 0.1%）时延迟开仓
- 平仓时优先在基差收敛（接近 0）时执行
- position.py 记录开仓和平仓时的基差，用于后续分析

```python
# monitor.py
async def fetch_basis(self, symbol):
    spot_ticker = await self.exchange.fetch_ticker(spot_symbol)
    perp_ticker = await self.exchange.fetch_ticker(perp_symbol)
    basis = (perp_ticker["last"] - spot_ticker["last"]) / spot_ticker["last"]
    return {"basis_pct": basis, "spot": spot_ticker["last"], "perp": perp_ticker["last"]}
```

---

### P1 重要优化（稳定运行后）

#### 5. 费率预测与入场时机优化

**背景：** 当前策略是费率达标就开仓。但 Binance 提供 `nextFundingRate`（预测费率），可以更精准地判断是否值得入场。

**改动：**
- monitor.py 的 `find_opportunities` 增加预测费率权重
- 如果当前费率高但预测费率骤降，降低评分或跳过
- 如果当前费率刚达标但预测费率在上升，优先入场
- screener.py 的评分公式中加入预测费率因子（权重 10%）

#### 6. 费率结算前智能检查

**背景：** 费率每 8 小时结算一次（UTC 0/8/16），只有在结算瞬间持有仓位才收到/支付费率。可以在结算前 5 分钟做最后一次检查。

**改动：**
- main.py 增加 `task_pre_settlement_check` 定时任务
- 在 UTC 23:55, 7:55, 15:55 执行
- 检查所有持仓的下一期预测费率
- 如果预测费率对我们不利（即将转负），在结算前平仓避免付费
- 如果预测费率有利但当前未开仓，考虑在结算前 1-2 分钟快速开仓吃一次费率

```python
# main.py 新增调度
scheduler.add_job(
    self.task_pre_settlement_check,
    "cron",
    hour="23,7,15", minute=55,
    id="pre_settle"
)
```

#### 7. 复利自动再投入

**背景：** 每月将净收入并入本金可提升长期收益。月收益率 1.2% 时，复利 vs 单利年化差约 1%。

**改动：**
- capital.py 增加 `recalculate_from_balance` 方法
- 每月 1 号 UTC 0:00 自动读取 Binance 账户余额
- 按余额重新计算 CapitalPlan（使用同样的比例）
- 更新 screener 和 main 的分配方案
- Telegram 通知本月资金调整情况
- 可选：设置 `auto_compound: true/false` 配置项
- 安全限制：单次调整幅度不超过 ±20%，防止因 API 数据异常导致错误分配

```yaml
# config.yaml 新增
compound:
  enabled: true
  schedule: "monthly"           # monthly / weekly
  day_of_month: 1
  max_adjustment_pct: 0.20      # 单次最大调整幅度
  min_capital: 5000             # 低于此值不调整（可能是提现了）
```

#### 8. 交易记录与报表增强

**改动：**
- report.py 增加以下维度的统计：
  - 按 Tier 分组的收益对比
  - 按持仓时长分组的收益率
  - 手续费 vs 返佣的月度趋势
  - 胜率（盈利轮次 / 总轮次）
  - 最大单次亏损和最大连续亏损天数
  - 基差损益统计（P0-4 实现后）
- 可选：生成 HTML 报表或 CSV 导出

---

### P2 收益提升（持续迭代）

#### 9. 跨所费率差套利（可选扩展）

**背景：** 同一币种在 Binance 和 Bybit 的费率可能不同。如果 Binance BTC 费率 +0.03%、Bybit 费率 +0.01%，可以在 Binance 做空 + Bybit 做多，两边都在收钱。

**前提：** 需要在 Bybit 也部署资金，且需要 Bybit 官方 SDK 下单（同样避免 ccxt 的 brokerId 问题）。

**改动：**
- 新增 `bybit_executor.py`（Bybit 官方 SDK 下单）
- screener.py 增加跨所费率差扫描
- 当同币种跨所费率差 > 0.02% 时触发
- 风险：跨所资金不互通，极端行情下单边爆仓风险更高

**优先级说明：** 这个方向增加了很大的复杂度（两个所的资金管理、转账、回滚），建议在单所策略完全稳定后再考虑。

#### 10. Maker 单优化手续费

**背景：** 当前全部用市价单（Taker），如果能用限价单（Maker）成交，合约端费率从 0.04% 降到 0.02%，现货端根据 VIP 等级也有降低。

**改动：**
- executor.py 增加 `order_type` 参数：`market` / `limit_aggressive`
- `limit_aggressive` 模式：在对手方最优价挂限价单，大概率秒成交但走 Maker 费率
- 设置超时：如果 3 秒未成交则撤单改市价
- 注意：这会增加两条腿不同步的风险，需要更精细的回滚逻辑

#### 11. 动态参数自适应

**背景：** 当前所有阈值（最低费率、最大滑点、换仓频率等）是固定的。市场环境不同时应该自动调整。

**改动：**
- 新增 `adaptive.py` 模块
- 根据过去 7 天的平均费率动态调整开仓门槛：
  - 如果 7 日均费率 > 0.03%（牛市），可以适当降低门槛捕获更多机会
  - 如果 7 日均费率 < 0.01%（低迷期），提高门槛减少无谓换仓
- 根据最近 10 次交易的实际滑点动态调整分批阈值
- 所有自适应调整有上下限硬约束，防止极端值

---

## 第二部分：美股链上套利兼容与拓展

### 一、架构改造（多资产抽象层）

当前代码和 Binance + 加密货币强耦合。要支持美股链上永续，需要抽象出一个通用层。

#### 资产类型抽象

```python
# models.py（新增）

from enum import Enum
from dataclasses import dataclass

class AssetClass(Enum):
    CRYPTO = "crypto"
    US_STOCK = "us_stock"
    INDEX = "index"
    COMMODITY = "commodity"
    FX = "fx"

class MarketStatus(Enum):
    OPEN_24_7 = "24/7"           # 加密：永远开放
    MARKET_HOURS = "market"       # 美股：工作日 9:30-16:00 ET
    EXTENDED_HOURS = "extended"   # 盘前盘后

@dataclass
class TradableAsset:
    symbol: str                   # "BTCUSDT" / "AAPL-PERP"
    asset_class: AssetClass
    base: str                     # "BTC" / "AAPL"
    quote: str                    # "USDT" / "USDC"

    # 交易属性
    perp_platform: str            # "binance" / "kraken" / "hyperliquid"
    spot_platform: str | None     # "binance" / "kraken_xstocks" / None
    has_spot_hedge: bool          # 是否有可用的现货对冲

    # 市场时间
    market_status: MarketStatus
    trading_hours: dict | None    # 美股需要 {"open": "13:30 UTC", "close": "20:00 UTC"}

    # 风险参数
    max_position_usd: float
    min_funding_rate: float
    max_spread_pct: float
    min_volume_24h: float
```

#### 执行器接口抽象

```python
# base_executor.py（新增）

from abc import ABC, abstractmethod

class BaseExecutor(ABC):
    """所有执行器的基类"""

    @abstractmethod
    def open_arbitrage(self, symbol, usdt_amount, price, direction) -> dict:
        ...

    @abstractmethod
    def close_arbitrage(self, symbol, quantity, direction) -> dict:
        ...

    @abstractmethod
    def set_leverage(self, symbol, leverage) -> None:
        ...

    @abstractmethod
    def get_balance(self, asset) -> float:
        ...
```

当前的 `BinanceExecutor` 和 `PaperExecutor` 继承这个基类。未来新增：
- `KrakenExecutor`（xStocks 现货 + 永续）
- `HyperliquidExecutor`（链上永续）
- `OstiumExecutor`（RWA 永续）

#### 筛选器抽象

```python
# base_screener.py（新增）

class BaseScreener(ABC):
    """筛选器基类"""

    @abstractmethod
    async def screen(self) -> list[dict]:
        ...

    @abstractmethod
    def check_allocation(self, coin, positions) -> tuple[bool, float, str]:
        ...
```

加密和美股各自实现，main.py 通过配置决定加载哪些筛选器。

---

### 二、美股特有逻辑

#### 1. 交易时间窗口管理

**核心问题：** 美股有休市时段，链上永续 24/7 交易。休市期间永续价格可能剧烈偏离，但你无法操作现货腿（如果用传统券商的话）。

**解决方案设计：**

```python
# market_hours.py（新增）

import pytz
from datetime import datetime, time

US_EASTERN = pytz.timezone("US/Eastern")

# 美股正常交易时间
MARKET_OPEN  = time(9, 30)
MARKET_CLOSE = time(16, 0)

# 风险窗口（休市期间不开新仓，接近收盘时考虑平仓）
SAFE_OPEN_WINDOW  = (time(10, 0), time(15, 0))   # 开仓安全窗口
CLOSE_WARNING     = time(15, 30)                   # 收盘前 30 分钟告警

def is_us_market_open() -> bool:
    """判断美股是否在交易时段"""
    now_et = datetime.now(US_EASTERN)
    if now_et.weekday() >= 5:  # 周末
        return False
    return MARKET_OPEN <= now_et.time() <= MARKET_CLOSE

def is_safe_to_open() -> bool:
    """是否在安全开仓窗口"""
    now_et = datetime.now(US_EASTERN)
    if now_et.weekday() >= 5:
        return False
    return SAFE_OPEN_WINDOW[0] <= now_et.time() <= SAFE_OPEN_WINDOW[1]

def hours_until_market_open() -> float:
    """距离下次开盘还有多少小时"""
    ...
```

**策略规则：**
- 美股标的只在 `is_safe_to_open()` 为 True 时开仓
- 收盘前 30 分钟：检查所有美股仓位，如果无法在链上平台做完全对冲，则平仓
- 周末：美股仓位全部平仓或设置极宽的止损
- 如果使用 Kraken xStocks（链上代币化现货 + 链上永续），则不受此限制，因为两条腿都是 24/7

#### 2. 股息处理

美股公司会分红。如果你持有代币化股票（现货腿），可能会收到股息；但永续合约端不一定调整。

**设计：**
- 维护一个股息日历（可从公开 API 获取）
- 除息日前一天发 Telegram 告警
- 如果平台不对永续合约做股息调整，除息日前平仓避免风险
- 如果平台会调整（类似 CFD 的股息补偿），则可以正常持仓

```yaml
# config.yaml 美股扩展
us_stocks:
  dividend_calendar_api: "https://api.example.com/dividends"
  auto_close_before_exdiv: true
  exdiv_close_hours_before: 24
```

#### 3. 预言机风险监控

链上美股永续合约的价格来自预言机（Oracle），而非交易所撮合。预言机故障或操纵可能导致价格偏离。

**设计：**
- 新增 `oracle_monitor.py`
- 同时追踪：链上永续价格、预言机报价、Yahoo Finance 实时价格
- 如果三者偏差 > 0.5%，暂停开仓并告警
- 如果偏差 > 1%，触发紧急平仓

#### 4. 对冲缺口风险量化

当使用传统券商持有真股票做对冲时，休市期间永续端的风险敞口是未对冲的。

**设计：**
- 计算每个持仓在休市期间的最大潜在亏损（基于历史波动率）
- 美股单日 gap 的历史统计：平均 0.3-0.5%，极端情况 5%+
- 根据敞口金额和预期 gap 设置最大持仓量

```
最大美股仓位 = 可承受的隔夜亏损 / 预期最大 gap
例：可承受 $50 亏损，预期最大 gap 3% → 最大仓位 $1,667
```

---

### 三、监控先行方案

**在代码上不做大改的前提下，先跑美股费率监控。**

#### Phase 1：纯数据采集（立即可做）

新增一个独立脚本 `stock_monitor.py`，不影响现有加密策略代码：

```python
# stock_monitor.py（独立脚本）
#
# 功能：
#   1. 每 5 分钟获取各平台美股永续的费率
#   2. 记录到 stock_rates.db（SQLite）
#   3. 每天生成日报发到 Telegram
#
# 数据源：
#   - Kraken xStocks 永续费率 (通过 ccxt)
#   - Hyperliquid HIP-3 股票永续费率 (通过 API)
#   - Ostium 费率 (通过公开 API)
#
# 监控标的（初始）：
#   AAPL, TSLA, NVDA, MSFT, AMZN, META, GOOG
#   SPX (S&P 500 指数), NDX (纳斯达克 100)
```

#### Phase 2：回测数据积累（1-3 个月）

- 收集足够多的费率历史数据
- 分析：费率方向分布、均值、波动率、与美股盘前盘后的关系
- 对比：同一标的在不同平台的费率差异
- 输出：每周汇总报告

需要关注的核心指标：
```
- 费率正负比（多少时间为正 vs 负）
- 平均绝对费率（可套利空间有多大）
- 费率持续性（正费率能连续多久）
- 费率与美股交易时段的关系（盘中 vs 盘后 vs 周末）
- 代币化现货 vs 永续的价差（基差行为）
- 订单簿深度变化（流动性够不够做）
```

#### Phase 3：模拟回测（数据足够后）

- 用积累的历史费率数据回测加密套利策略在美股上的表现
- 评估：扣除手续费、滑点、对冲缺口后的净收益率
- 与同期加密套利收益对比
- 决定是否投入实盘

---

### 四、实盘切入条件

以下条件全部满足时，可以将美股链上套利纳入实盘：

```
□ 至少一个平台同时提供美股代币化现货 + 永续合约（同所对冲）
□ 目标标的（如 AAPL）的永续日交易量 > $1 亿
□ Bid-Ask 价差 < 0.1%
□ ±0.5% 深度 > $50 万
□ 费率历史数据积累 > 6 个月
□ 回测显示年化 > 10%（扣除所有成本后）
□ 监管无重大不确定性（该平台未被 SEC 调查等）
□ 代码已完成多资产抽象层改造
□ 模拟盘运行 > 1 个月且无重大 Bug
```

---

## 附录

### A. 收益预期汇总

```
$10,000 本金 | Binance VIP 0 | 30% 返佣 | BNB 抵扣 | 月复利

保守（熊市/横盘，费率 0.010%）:  年化 ~7-8%
中性（常态市场，费率 0.020%）:   年化 ~16-17%
乐观（牛市情绪，费率 0.035%）:   年化 ~33-34%

稳健长期预期（跨完整周期）:      年化 12-18%
```

### B. 滑点影响速查表

```
           $2,500    $10,000    $30,000
BTC        0.001%    0.002%     0.005%
ETH        0.001%    0.003%     0.008%
SOL        0.002%    0.008%     0.025%
DOGE       0.003%    0.012%     0.040%
ARB        0.005%    0.025%     0.080%

* 以上为单次市价单滑点，一轮开平仓总滑点 = 单次 × 4
```

### C. 开发优先级排序

```
Phase 1 (v5) — 上实盘前必须完成:
  P0-1  BNB 手续费抵扣
  P0-2  合约先行下单
  P0-3  分批下单防滑点
  P0-4  基差感知开平仓

Phase 2 (v6) — 实盘稳定后:
  P1-5  费率预测入场优化
  P1-6  结算前智能检查
  P1-7  复利自动再投入
  P1-8  报表增强

Phase 3 (v7) — 持续迭代:
  P2-9   跨所费率差套利
  P2-10  Maker 单优化
  P2-11  动态参数自适应

Phase 4 (v8) — 美股扩展:
  多资产抽象层改造
  美股费率监控脚本
  数据积累与回测
  满足切入条件后接入实盘
```

### D. 风险等级提醒

```
策略层面（可控）:
  - 单腿成交风险     → 回滚机制 + 合约先行
  - 滑点过大         → 分批下单 + 币种阈值
  - 费率反向         → 自动平仓 + 预测检查
  - 基差波动         → 基差感知 + 择时平仓

平台层面（部分可控）:
  - API 故障         → 1x 杠杆兜底 + 告警
  - 费率规则变更     → 持续监控公告

系统层面（不可控）:
  - 交易所暴雷       → 定期提取利润到链上
  - 监管政策变化     → 关注合规动态
  - 市场结构性变化   → 接受年化下降的可能
```