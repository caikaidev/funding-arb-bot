"""
回测框架 — 基于 logs/monitor-candidates-*.csv 重放策略决策

用法:
    python backtest.py                          # 用 config.yaml 默认参数
    python backtest.py --capital 10000          # 覆盖初始资金
    python backtest.py --enable-rotation        # 启用动态换仓对比
    python backtest.py --csv-dir logs           # 自定义 CSV 目录

内存友好设计（服务器只有 912MiB 总内存）:
- 流式读 CSV，每次只持有 1 轮扫描（~561 行 ~110KB），峰值 < 5MiB
- 从 tiers 模块导入常量，避免触发 screener 的 ccxt import（节省 ~80MiB）
- 整个进程峰值预期 < 80MiB

简化点（与实盘 screener 的差异）:
- CSV 不含 volume/OI/spread/depth → 只用 min_rate 过滤，不做精筛
- score 用纯年化做近似（screener 中年化权重 40%，主导排序）
- 不模拟滑点 / 基差 / 拆单（首版只验证「机会捕获 + 持仓时长 + 费率累积」）
"""
import argparse
import csv
import glob
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Optional

import yaml

from capital import resolve
from tiers import TIER_THRESHOLDS, classify


def _to_ccxt(s):
    return f"{s.replace('USDT', '')}/USDT:USDT"


@dataclass
class Position:
    symbol: str
    tier: int
    amount: float
    rate_at_open: float
    opened_at: datetime
    funding_earned: float = 0.0
    settlements: int = 0
    last_rate: float = 0.0  # 最近一次结算时的费率
    reverse_count: int = 0  # 连续反向计数


@dataclass
class ClosedTrade:
    symbol: str
    tier: int
    amount: float
    opened_at: datetime
    closed_at: datetime
    funding_earned: float
    fees: float
    net: float
    settlements: int
    reason: str


@dataclass
class Stats:
    closed: list[ClosedTrade] = field(default_factory=list)
    missed: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # symbol → count
    rotations: int = 0


class Backtest:
    def __init__(self, config_path="config.yaml", csv_dir="logs",
                 capital_override: Optional[float] = None,
                 enable_rotation: bool = False, t3_enabled: bool = True):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)
        self.config["tiers"]["t3_enabled"] = t3_enabled
        self.plan = resolve(self.config, capital_override=capital_override)
        self.csv_dir = csv_dir

        fees = self.config["fees"]
        if fees.get("use_bnb_discount"):
            self.fee_rate = fees["spot_taker_bnb"] + fees["futures_taker_bnb"]
        else:
            self.fee_rate = fees["spot_taker"] + fees["futures_taker"]
        self.rebate = fees.get("rebate_rate", 0)

        self.exit_cfg = self.config["strategy"]["exit"]
        self.min_profitable = self.exit_cfg["min_profitable_rate"]
        self.max_hold_days = self.exit_cfg["max_holding_days"]
        self.reverse_count_limit = self.exit_cfg.get("rate_reverse_count", 3)

        rot_cfg = self.config["strategy"].get("rotation", {})
        self.rotation_enabled = enable_rotation and rot_cfg.get("enabled", True)
        self.rot_multiplier = rot_cfg.get("score_multiplier", 1.5)
        self.rot_min_age_h = rot_cfg.get("min_age_hours", 24)

        self.positions: list[Position] = []
        self.stats = Stats()
        self._funding_done: set[str] = set()  # 已结算的 (timestamp_hour, symbol) 防重

    # ------------------------------------------------------------------
    def stream_snapshots(self) -> Iterator[tuple[datetime, dict]]:
        """
        流式 yield 每一轮扫描 (timestamp, {symbol: row})，永远只持有 1 轮在内存中。

        CSV 内行天然按 timestamp 顺序（一轮扫描一次性写入），所以同 timestamp 的
        行连续出现，无需全文件加载排序。
        """
        files = sorted(glob.glob(os.path.join(self.csv_dir, "monitor-candidates-*.csv")))
        if not files:
            raise FileNotFoundError(f"未找到 {self.csv_dir}/monitor-candidates-*.csv")

        cur_ts: Optional[str] = None
        cur_dt: Optional[datetime] = None
        cur_map: dict = {}

        for path in files:
            with open(path) as f:
                for row in csv.DictReader(f):
                    ts_str = row["timestamp"]
                    if ts_str != cur_ts:
                        if cur_ts is not None and cur_dt is not None:
                            yield cur_dt, cur_map
                        try:
                            cur_dt = datetime.fromisoformat(ts_str)
                        except ValueError:
                            cur_ts = ts_str
                            cur_dt = None
                            cur_map = {}
                            continue
                        cur_ts = ts_str
                        cur_map = {}
                    cur_map[row["symbol"]] = row

        if cur_ts is not None and cur_dt is not None and cur_map:
            yield cur_dt, cur_map

    # ------------------------------------------------------------------
    def _two_way_fee(self, amount: float) -> float:
        """单仓双程手续费（已扣 rebate）"""
        return amount * self.fee_rate * 2 * (1 - self.rebate)

    def _score(self, rate: float, tier: int) -> float:
        """简化评分: 仅按年化（screener 中年化占 40%，主导排序）"""
        annualized = abs(rate) * 3 * 365
        return min(100, annualized / 0.5 * 100)

    def _check_close(self, now: datetime, snapshot: dict):
        """检查所有持仓的离场条件"""
        for pos in self.positions[:]:
            row = snapshot.get(pos.symbol)
            age_days = (now - pos.opened_at).total_seconds() / 86400

            # 最大持仓天数
            if age_days >= self.max_hold_days:
                self._close(pos, now, f"持仓 {age_days:.1f} 天")
                continue

            if not row:
                continue
            try:
                cur_rate = float(row["funding_rate"])
            except (TypeError, ValueError):
                continue

            # 费率反转计数
            if cur_rate < 0:
                pos.reverse_count += 1
            else:
                pos.reverse_count = 0

            if pos.reverse_count >= self.reverse_count_limit:
                self._close(pos, now, f"费率反转 ×{pos.reverse_count}")
                continue

            # 持仓 < min_holding_hours 跳过低费率检查（与 monitor.should_exit 对齐）
            min_hold_h = self.exit_cfg.get("min_holding_hours", 24)
            if age_days * 24 < min_hold_h:
                continue

            if cur_rate < self.min_profitable:
                self._close(pos, now, f"费率过低 {cur_rate*100:.4f}%")

    def _try_open(self, now: datetime, snapshot: dict):
        """筛选候选并尝试开仓（含动态换仓）"""
        active_tiers = set(self.plan.tiers.keys())
        candidates = []
        for sym, row in snapshot.items():
            tier = int(row.get("tier", 0) or classify(_to_ccxt(sym)))
            if tier not in active_tiers:
                continue
            try:
                rate = float(row["funding_rate"])
            except (TypeError, ValueError):
                continue
            if rate <= 0:
                continue
            thresh = TIER_THRESHOLDS.get(tier)
            if not thresh or rate < thresh["min_rate"]:
                continue
            candidates.append({
                "symbol": sym, "tier": tier, "rate": rate,
                "score": self._score(rate, tier),
            })
        candidates.sort(key=lambda x: -x["score"])

        held_symbols = {p.symbol for p in self.positions}
        for c in candidates:
            if c["symbol"] in held_symbols:
                continue

            tier = c["tier"]
            tier_alloc = self.plan.tiers.get(tier)
            if not tier_alloc:
                continue

            same_tier_positions = [p for p in self.positions if p.tier == tier]
            same_tier_count = len(same_tier_positions)
            slot_full = same_tier_count >= tier_alloc.max_count

            # T3 总额度
            if tier == 3:
                t3_used = sum(p.amount for p in self.positions if p.tier == 3)
                if t3_used + tier_alloc.max_position > tier_alloc.total_cap + 1:
                    slot_full = True

            used = sum(p.amount for p in self.positions)
            available = self.plan.tradable - used
            alloc = min(tier_alloc.max_position, available)

            if slot_full:
                # 尝试动态换仓
                if not self.rotation_enabled:
                    self.stats.missed[c["symbol"]] += 1
                    continue
                target = self._find_rotation_target(c, same_tier_positions, snapshot, now)
                if not target:
                    self.stats.missed[c["symbol"]] += 1
                    continue
                self._close(target, now, f"主动换仓 → {c['symbol']}")
                self.stats.rotations += 1
                # 重算可用额度
                used = sum(p.amount for p in self.positions)
                available = self.plan.tradable - used
                alloc = min(tier_alloc.max_position, available)

            if alloc < 200:
                self.stats.missed[c["symbol"]] += 1
                continue

            # break-even 检查（与 main._passes_break_even 对齐）
            min_hold_h = self.exit_cfg.get("min_holding_hours", 24)
            expected_funding = alloc * c["rate"] * (min_hold_h / 8)
            if expected_funding < self._two_way_fee(alloc) * 1.5:
                self.stats.missed[c["symbol"]] += 1
                continue

            self.positions.append(Position(
                symbol=c["symbol"], tier=tier, amount=alloc,
                rate_at_open=c["rate"], opened_at=now,
                last_rate=c["rate"],
            ))
            held_symbols.add(c["symbol"])

    def _find_rotation_target(self, candidate: dict, same_tier: list[Position],
                              snapshot: dict, now: datetime) -> Optional[Position]:
        worst: Optional[Position] = None
        worst_score = float("inf")
        for pos in same_tier:
            age_h = (now - pos.opened_at).total_seconds() / 3600
            if age_h < self.rot_min_age_h:
                continue
            row = snapshot.get(pos.symbol)
            if not row:
                continue
            try:
                cur_rate = float(row["funding_rate"])
            except (TypeError, ValueError):
                continue
            if cur_rate <= 0:
                continue
            cur_score = self._score(cur_rate, pos.tier)
            if cur_score < worst_score:
                worst_score = cur_score
                worst = pos
        if worst is None:
            return None
        if candidate["score"] >= worst_score * self.rot_multiplier:
            return worst
        return None

    def _settle_funding(self, now: datetime, snapshot: dict):
        """每 8h 结算一次（00/08/16:00 UTC 附近的扫描点触发）"""
        # 每个结算窗口对每个持仓只结算一次
        window_key = f"{now.strftime('%Y%m%d')}-{now.hour // 8}"

        for pos in self.positions:
            key = f"{window_key}-{pos.symbol}"
            if key in self._funding_done:
                continue
            row = snapshot.get(pos.symbol)
            if not row:
                continue
            try:
                rate = float(row["funding_rate"])
            except (TypeError, ValueError):
                continue
            payment = pos.amount * rate
            pos.funding_earned += payment
            pos.settlements += 1
            pos.last_rate = rate
            self._funding_done.add(key)

    def _close(self, pos: Position, now: datetime, reason: str):
        fee = self._two_way_fee(pos.amount)
        net = pos.funding_earned - fee
        self.stats.closed.append(ClosedTrade(
            symbol=pos.symbol, tier=pos.tier, amount=pos.amount,
            opened_at=pos.opened_at, closed_at=now,
            funding_earned=pos.funding_earned, fees=fee, net=net,
            settlements=pos.settlements, reason=reason,
        ))
        self.positions.remove(pos)

    # ------------------------------------------------------------------
    def run(self):
        first_ts: Optional[datetime] = None
        last_ts: Optional[datetime] = None
        last_settle_hour = -1
        scan_count = 0

        for ts, snap in self.stream_snapshots():
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            scan_count += 1

            self._check_close(ts, snap)
            # 结算窗口（每 8h 一次, 取 00/08/16 点的第一个扫描）
            if ts.hour in (0, 8, 16) and ts.hour != last_settle_hour:
                self._settle_funding(ts, snap)
                last_settle_hour = ts.hour
            elif ts.hour not in (0, 8, 16):
                last_settle_hour = -1
            self._try_open(ts, snap)

        if first_ts is None or last_ts is None:
            print("无快照数据")
            return

        # 强平所有未平仓
        for pos in self.positions[:]:
            self._close(pos, last_ts, "回测结束")

        print(f"  扫描轮次: {scan_count}")
        self.report(first_ts, last_ts)

    def report(self, start: datetime, end: datetime):
        c = self.stats.closed
        total_funding = sum(t.funding_earned for t in c)
        total_fees = sum(t.fees for t in c)
        total_net = sum(t.net for t in c)
        days = (end - start).total_seconds() / 86400

        print("=" * 70)
        print(f"  回测报告 | 时间窗 {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} ({days:.1f}天)")
        print(f"  本金: ${self.plan.initial:,.0f} | 可套利: ${self.plan.tradable:,.0f}")
        print(f"  动态换仓: {'启用' if self.rotation_enabled else '关闭'}")
        print("=" * 70)
        print(f"  总交易: {len(c)} 笔 | 换仓次数: {self.stats.rotations}")
        print(f"  资金费收入: ${total_funding:>8.2f}")
        print(f"  手续费支出: ${total_fees:>8.2f}")
        print(f"  净盈亏:     ${total_net:>+8.2f}  ({total_net/self.plan.initial*100:+.3f}% 本金)")
        print(f"  日均收益:   ${total_net/max(days,1):>+8.2f}")
        print("-" * 70)

        # 按币聚合
        by_sym = defaultdict(lambda: {"count": 0, "funding": 0, "fees": 0, "net": 0})
        for t in c:
            s = by_sym[t.symbol]
            s["count"] += 1
            s["funding"] += t.funding_earned
            s["fees"] += t.fees
            s["net"] += t.net

        print(f"  {'币种':<14} {'笔数':>4} {'收入':>10} {'费用':>8} {'净':>10}")
        for sym, s in sorted(by_sym.items(), key=lambda x: -x[1]["net"]):
            print(f"  {sym:<14} {s['count']:>4} {s['funding']:>+10.2f} {s['fees']:>8.2f} {s['net']:>+10.2f}")

        if self.stats.missed:
            print("-" * 70)
            print(f"  错过的机会 (Top 10, 槽位满或被 break-even 过滤):")
            top = sorted(self.stats.missed.items(), key=lambda x: -x[1])[:10]
            for sym, count in top:
                print(f"    {sym:<16} 触发 {count} 次未开仓")
        print("=" * 70)


def main():
    p = argparse.ArgumentParser(description="资金费率套利回测")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--csv-dir", default="logs")
    p.add_argument("--capital", type=float, default=None)
    p.add_argument("--enable-rotation", action="store_true",
                   help="启用动态换仓（对照试验）")
    p.add_argument("--no-t3", action="store_true")
    args = p.parse_args()

    bt = Backtest(
        config_path=args.config,
        csv_dir=args.csv_dir,
        capital_override=args.capital,
        enable_rotation=args.enable_rotation,
        t3_enabled=not args.no_t3,
    )
    bt.run()


if __name__ == "__main__":
    main()
