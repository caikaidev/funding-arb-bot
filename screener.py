"""
动态币种筛选器 v4 — 资金分配由 CapitalPlan 驱动
"""
import asyncio
import ccxt.async_support as ccxt
from datetime import datetime, timezone
from loguru import logger

from capital import CapitalPlan
from tiers import TIER1_BASES, TIER2_BASES, TIER_THRESHOLDS, classify


BLACKLIST_BASES = set()


class DynamicScreener:

    def __init__(self, config: dict, plan: CapitalPlan):
        creds = config["exchanges"]["binance"]
        self.exchange = ccxt.binance({
            "apiKey": creds["api_key"],
            "secret": creds["api_secret"],
            "options": {"defaultType": "swap"},
            "enableRateLimit": True,
        })
        self.plan = plan
        self._market_cache = {}
        self._cache_time = None

    def update_plan(self, plan: CapitalPlan):
        """运行时更新资金方案（比如加减资金后）"""
        self.plan = plan

    # ------------------------------------------------------------------
    async def raw_snapshot(self) -> list[dict]:
        """
        阶段 1 数据采集用：全量永续费率快照, 不做任何过滤（tier / rate / 黑名单都忽略）。

        复用 _load_markets + 批量 fetch_funding_rates, 额外带上 mark / index /
        预测费率 等字段, 供 monitor 模式落 CSV 以事后分析费率分布、调优阈值。

        注意: 与 screen() 解耦, 哪怕 screen 流程报错也不影响这份快照。
        """
        markets = await self._load_markets()

        try:
            all_r = await self.exchange.fetch_funding_rates()
        except Exception as e:
            logger.warning(f"raw_snapshot 批量费率失败: {e}")
            return []

        snapshot = []
        ts = datetime.now(timezone.utc).isoformat()
        for sym, fr in all_r.items():
            if sym not in markets:
                continue
            rate = fr.get("fundingRate") or 0
            info = fr.get("info") or {}
            snapshot.append({
                "timestamp": ts,
                "symbol": sym.replace("/", "").replace(":USDT", ""),
                "tier": classify(sym),
                "funding_rate": rate,
                "annualized": abs(float(rate)) * 3 * 365 if rate else 0,
                "mark_price": fr.get("markPrice") or info.get("markPrice"),
                "index_price": fr.get("indexPrice") or info.get("indexPrice"),
                "predicted_rate": fr.get("nextFundingRate"),
                "next_funding_time": fr.get("fundingDatetime"),
            })
        return snapshot

    # ------------------------------------------------------------------
    async def screen(self) -> list[dict]:
        markets = await self._load_markets()

        # T3 关则只扫 T1+T2
        active_tiers = set(self.plan.tiers.keys())
        symbols = [s for s in markets if classify(s) in active_tiers]
        logger.info(f"扫描 {len(symbols)} 个币种 (活跃等级: {sorted(active_tiers)})")

        rates = await self._batch_rates(symbols)

        # 费率初筛（只做正费率：现货账户不支持裸空，负费率无法实现）
        candidates = []
        for sym, fr in rates.items():
            base = sym.split("/")[0]
            if base in BLACKLIST_BASES:
                continue
            tier = classify(sym)
            if tier not in active_tiers:
                continue
            thresh = TIER_THRESHOLDS[tier]
            rate = fr.get("fundingRate", 0)
            if rate <= 0:
                continue
            if rate >= thresh["min_rate"]:
                candidates.append({
                    "symbol": sym, "tier": tier, "rate": rate, "abs_rate": rate,
                    "next_funding_time": fr.get("fundingTimestamp"),
                    "predicted_rate": fr.get("nextFundingRate"),
                })

        logger.info(f"费率初筛: {len(candidates)} 个")

        # 精筛
        qualified = []
        for c in candidates:
            detail = await self._evaluate(c["symbol"])
            if not detail:
                continue

            thresh = TIER_THRESHOLDS[c["tier"]]
            ok = (
                detail["volume_24h"] >= thresh["min_vol"]
                and detail["open_interest"] >= thresh["min_oi"]
                and detail["spread_pct"] <= thresh["max_spread"]
                and detail["depth_usdt"] >= thresh["min_depth"]
            )
            if c["tier"] == 3 and detail.get("listing_hours", 9999) < thresh["min_hours"]:
                ok = False

            if not ok:
                continue

            tier_alloc = self.plan.tiers[c["tier"]]
            c.update(detail)
            c["annualized"] = c["abs_rate"] * 3 * 365
            c["max_position"] = tier_alloc.max_position
            c["tier_name"] = tier_alloc.name
            c["direction"] = "positive" if c["rate"] > 0 else "reverse"
            c["binance_symbol"] = c["symbol"].replace("/", "").replace(":USDT", "")
            c["score"] = self._score(c)
            qualified.append(c)

        qualified.sort(key=lambda x: x["score"], reverse=True)

        for q in qualified[:8]:
            logger.info(
                f"  [{q['tier_name'][:5]}] {q['binance_symbol']:>10} | "
                f"费率 {q['rate']:+.4%} | 年化 {q['annualized']:.1%} | "
                f"仓限 ${q['max_position']:,.0f} | 分 {q['score']:.1f}"
            )
        return qualified

    # ------------------------------------------------------------------
    def check_allocation(self, coin: dict, open_positions: list[dict]) -> tuple[bool, float, str]:
        """检查能否开仓，返回 (允许, 建议金额, 原因)"""
        tier = coin["tier"]
        alloc = self.plan.tiers.get(tier)
        if not alloc:
            return False, 0, "该等级未启用"

        sym = coin["binance_symbol"]
        if sym in {p["symbol"] for p in open_positions}:
            return False, 0, "已有仓位"

        # 该等级仓位数
        tier_count = sum(1 for p in open_positions if classify(_to_ccxt(p["symbol"])) == tier)
        if tier_count >= alloc.max_count:
            return False, 0, f"{alloc.name} 已满 ({tier_count}/{alloc.max_count})"

        # 总仓位数
        if len(open_positions) >= self.plan.max_positions:
            return False, 0, f"总仓位已满 ({len(open_positions)}/{self.plan.max_positions})"

        amount = alloc.max_position

        # T3 总额度
        if tier == 3:
            t3_used = sum(p["usdt_amount"] for p in open_positions if classify(_to_ccxt(p["symbol"])) == 3)
            remaining = alloc.total_cap - t3_used
            if remaining < 200:
                return False, 0, f"T3 额度用完 (${t3_used:.0f}/{alloc.total_cap:.0f})"
            amount = min(amount, remaining)

        return True, amount, "通过"

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    async def _load_markets(self):
        now = datetime.now(timezone.utc)
        if self._cache_time and (now - self._cache_time).total_seconds() < 300:
            return self._market_cache
        markets = await self.exchange.load_markets(True)
        self._market_cache = {
            k: v for k, v in markets.items()
            if v.get("swap") and v.get("quote") == "USDT" and v.get("active")
        }
        self._cache_time = now
        return self._market_cache

    async def _batch_rates(self, symbols):
        result = {}
        try:
            all_r = await self.exchange.fetch_funding_rates()
            for s in symbols:
                if s in all_r:
                    result[s] = all_r[s]
        except Exception as e:
            logger.warning(f"批量费率失败: {e}")
        return result

    async def _evaluate(self, symbol):
        try:
            ticker, ob = await asyncio.gather(
                self.exchange.fetch_ticker(symbol),
                self.exchange.fetch_order_book(symbol, limit=20),
                return_exceptions=True,
            )
            if isinstance(ticker, Exception) or isinstance(ob, Exception):
                return None

            vol = float(ticker.get("quoteVolume", 0) or 0)
            oi = 0
            try:
                oi_data = await self.exchange.fetch_open_interest(symbol)
                if oi_data:
                    v = oi_data.get("openInterestValue") or oi_data.get("openInterestAmount", 0)
                    oi = float(v)
                    if oi < 1000:
                        oi *= float(ticker.get("last", 1))
            except Exception:
                oi = vol * 0.3

            bid = ob["bids"][0][0] if ob["bids"] else 0
            ask = ob["asks"][0][0] if ob["asks"] else 0
            mid = (bid + ask) / 2 if bid and ask else 1
            spread = (ask - bid) / mid if mid > 0 else 1

            depth = 0
            for p, q in ob.get("bids", []):
                if p >= mid * 0.995:
                    depth += p * q
            for p, q in ob.get("asks", []):
                if p <= mid * 1.005:
                    depth += p * q

            listing_hours = 9999
            info = self._market_cache.get(symbol, {})
            created = info.get("info", {}).get("onboardDate")
            if created:
                try:
                    ts = int(created) / 1000
                    age = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
                    listing_hours = age.total_seconds() / 3600
                except Exception:
                    pass

            return {
                "volume_24h": vol, "open_interest": oi,
                "spread_pct": spread, "depth_usdt": depth,
                "mid_price": mid, "listing_hours": listing_hours,
            }
        except Exception:
            return None

    def _score(self, c):
        thresh = TIER_THRESHOLDS[c["tier"]]
        r = min(100, c["annualized"] / 0.5 * 100)
        v = min(100, 60 + (c["volume_24h"] / thresh["min_vol"] - 1) * 40 / 9)
        o = min(100, 60 + (c["open_interest"] / max(thresh["min_oi"], 1) - 1) * 40 / 9)
        s = max(0, 100 - c["spread_pct"] / thresh["max_spread"] * 100)
        a = min(100, c.get("listing_hours", 9999) / 720 * 100)
        base = r * 0.4 + v * 0.25 + o * 0.15 + s * 0.15 + a * 0.05

        # predicted_rate 风险因子: 预测值显著低于当前值时降权（提前规避费率衰减）
        predicted = c.get("predicted_rate")
        if predicted is not None and c.get("abs_rate", 0) > 0:
            try:
                ratio = float(predicted) / c["abs_rate"]
                if ratio < 0.3:
                    base *= 0.7
                elif ratio < 0.5:
                    base *= 0.85
                elif ratio > 1.2:
                    base *= 1.1
            except (TypeError, ValueError):
                pass

        # 临近结算加分: 鼓励快速回本（30min 内 +8 分，60min 内 +4 分）
        settlement_bonus = 0
        nft = c.get("next_funding_time")
        if nft:
            try:
                now_ms = datetime.now(timezone.utc).timestamp() * 1000
                mins_to_settle = (float(nft) - now_ms) / 60000
                if 0 < mins_to_settle <= 30:
                    settlement_bonus = 8
                elif 30 < mins_to_settle <= 60:
                    settlement_bonus = 4
            except (TypeError, ValueError):
                pass

        return round(base + settlement_bonus, 1)

    async def close(self):
        await self.exchange.close()


def _to_ccxt(s):
    return f"{s.replace('USDT', '')}/USDT:USDT"
