"""
Tier 常量与分类 — 与 screener 共享，独立于 ccxt 依赖

抽离原因：backtest.py 不需要 ccxt（仅做 CSV 重放），但又要复用 screener 的
分级阈值和 classify 函数。直接 from screener import 会触发 ccxt 模块加载
（~80MiB），在低内存服务器上容易 OOM 杀掉正在跑的监控 bot。
"""

TIER1_BASES = {"BTC", "ETH"}
TIER2_BASES = {
    "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK",
    "DOT", "NEAR", "SUI", "ARB", "OP", "UNI", "APT",
    "TIA", "FIL", "ATOM", "LTC", "ETC", "MATIC",
}

# 流动性门槛（固定，与资金量无关）
# min_rate 单位: 8h 费率。年化 = min_rate × 3 × 365
# T1/T2 = 0.0001 ≈ 10.95% 年化（盘活流动性极好的核心币种）
# T3   = 0.0004 ≈ 43.8% 年化（小币波动大，要更高门槛覆盖风险）
TIER_THRESHOLDS = {
    1: {"min_vol": 500e6, "min_oi": 200e6, "max_spread": 0.0003, "min_depth": 1e6, "min_rate": 0.0001, "min_hours": 0},
    2: {"min_vol": 100e6, "min_oi": 50e6,  "max_spread": 0.0005, "min_depth": 500e3, "min_rate": 0.0001, "min_hours": 0},
    3: {"min_vol": 20e6,  "min_oi": 10e6,  "max_spread": 0.001,  "min_depth": 100e3, "min_rate": 0.0004, "min_hours": 48},
}


def classify(symbol: str) -> int:
    base = symbol.split("/")[0] if "/" in symbol else symbol.replace("USDT", "")
    if base in TIER1_BASES:
        return 1
    if base in TIER2_BASES:
        return 2
    return 3
