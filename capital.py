"""
资金计算模块 — 将配置中的比例转换为实际金额

只需传入 initial_capital，其余全部自动计算。
"""
from dataclasses import dataclass
from loguru import logger


@dataclass
class CapitalPlan:
    """计算后的资金分配方案"""
    initial: float          # 初始资金
    tradable: float         # 可套利资金
    reserve: float          # 链上底仓
    emergency: float        # 应急备用
    max_positions: int      # 最大仓位数
    t3_enabled: bool
    tiers: dict             # {1: TierAlloc, 2: TierAlloc, ...}
    daily_loss_limit: float


@dataclass
class TierAlloc:
    """单个等级的资金分配"""
    tier: int
    name: str
    max_position: float     # 单仓上限 (USDT)
    max_count: int          # 最多几仓
    total_cap: float        # 该等级总上限（主要约束 T3）


def resolve(config: dict, capital_override: float = None) -> CapitalPlan:
    """
    根据配置和初始资金，计算所有金额

    Args:
        config: 完整配置 dict
        capital_override: 命令行传入的资金，优先于配置文件

    Returns:
        CapitalPlan
    """
    initial = capital_override or config["initial_capital"]
    alloc = config["allocation"]

    tradable = initial * alloc["tradable_pct"]
    reserve = initial * alloc["reserve_pct"]
    emergency = initial * alloc["emergency_pct"]

    t3_on = config["tiers"]["t3_enabled"]
    profile_key = "with_t3" if t3_on else "without_t3"
    profile = config["tiers"][profile_key]

    tiers = {}

    # T1
    t1_cfg = profile["t1"]
    tiers[1] = TierAlloc(
        tier=1,
        name="T1 核心 (BTC/ETH)",
        max_position=round(tradable * t1_cfg["position_pct"], 2),
        max_count=t1_cfg["max_count"],
        total_cap=round(tradable * t1_cfg["position_pct"] * t1_cfg["max_count"], 2),
    )

    # T2
    t2_cfg = profile["t2"]
    tiers[2] = TierAlloc(
        tier=2,
        name="T2 主流",
        max_position=round(tradable * t2_cfg["position_pct"], 2),
        max_count=t2_cfg["max_count"],
        total_cap=round(tradable * t2_cfg["position_pct"] * t2_cfg["max_count"], 2),
    )

    # T3
    if t3_on:
        t3_cfg = profile["t3"]
        tiers[3] = TierAlloc(
            tier=3,
            name="T3 机会",
            max_position=round(tradable * t3_cfg["position_pct"], 2),
            max_count=t3_cfg["max_count"],
            total_cap=round(tradable * t3_cfg["total_pct"], 2),
        )

    daily_limit = initial * config["strategy"]["risk"].get("daily_loss_limit_pct", 0.005)

    plan = CapitalPlan(
        initial=initial,
        tradable=round(tradable, 2),
        reserve=round(reserve, 2),
        emergency=round(emergency, 2),
        max_positions=profile["max_positions"],
        t3_enabled=t3_on,
        tiers=tiers,
        daily_loss_limit=round(daily_limit, 2),
    )

    return plan


def print_plan(plan: CapitalPlan):
    """打印资金分配表"""
    mode = "T1+T2+T3" if plan.t3_enabled else "T1+T2"

    logger.info("=" * 55)
    logger.info(f"  资金分配方案 | 模式: {mode}")
    logger.info(f"  初始资金:     ${plan.initial:>10,.2f}")
    logger.info("-" * 55)
    logger.info(f"  可套利资金:   ${plan.tradable:>10,.2f}  ({plan.tradable/plan.initial:.0%})")
    logger.info(f"  链上底仓:     ${plan.reserve:>10,.2f}  ({plan.reserve/plan.initial:.0%})")
    logger.info(f"  应急备用:     ${plan.emergency:>10,.2f}  ({plan.emergency/plan.initial:.0%})")
    logger.info("-" * 55)

    for t in sorted(plan.tiers.values(), key=lambda x: x.tier):
        logger.info(
            f"  {t.name:<20} "
            f"${t.max_position:>8,.2f}/仓 × {t.max_count}仓  "
            f"(上限 ${t.total_cap:,.2f})"
        )

    logger.info("-" * 55)
    logger.info(f"  最大仓位数:   {plan.max_positions}")
    logger.info(f"  单日亏损上限: ${plan.daily_loss_limit:,.2f}")
    logger.info("=" * 55)


# 独立测试
if __name__ == "__main__":
    import yaml
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    # 测试不同资金量
    for amount in [5000, 10000, 20000, 50000]:
        plan = resolve(cfg, capital_override=amount)
        print_plan(plan)
        print()
