"""
费率监控模块 — 使用 ccxt（只读，不下单，不影响返佣）
"""
import asyncio
import ccxt.async_support as ccxt
from loguru import logger
from datetime import datetime, timezone


class FundingRateMonitor:
    """
    使用 ccxt 统一接口监控多交易所费率。
    仅用于读取数据，不涉及下单，因此 ccxt 内置的 brokerId 不影响返佣。
    """

    def __init__(self, config: dict, exchange=None):
        """
        Args:
            config: 完整配置 dict
            exchange: 可选的外部 ccxt 客户端。传入则共用（节省 markets metadata 内存
                ~80MB，并让 rate limiter 全局生效）；不传则自建（旧行为）。
        """
        if exchange is not None:
            self.exchange = exchange
            self._owns_exchange = False
        else:
            binance_cfg = config["exchanges"]["binance"]
            self.exchange = ccxt.binance(
                {
                    "apiKey": binance_cfg["api_key"],
                    "secret": binance_cfg["api_secret"],
                    "options": {"defaultType": "swap"},
                    "enableRateLimit": True,
                }
            )
            self._owns_exchange = True
        self.strategy = config["strategy"]

    # ------------------------------------------------------------------
    # 核心：获取费率
    # ------------------------------------------------------------------
    async def fetch_funding_rate(self, symbol: str) -> dict | None:
        """获取单个币种的当前费率和预测费率"""
        try:
            data = await self.exchange.fetch_funding_rate(symbol)
            rate = data.get("fundingRate", 0)
            next_time = data.get("fundingDatetime")
            predicted = data.get("nextFundingRate")  # 部分交易所支持

            return {
                "symbol": symbol,
                "rate": rate,
                "annualized": abs(rate) * 3 * 365,  # 8h → 年化
                "predicted_rate": predicted,
                "next_settlement": next_time,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.warning(f"获取 {symbol} 费率失败: {e}")
            return None

    async def fetch_all_rates(self) -> list[dict]:
        """并发获取白名单所有币种的费率"""
        symbols = self.strategy["whitelist"]

        # ccxt 格式转换：BTCUSDT → BTC/USDT:USDT
        ccxt_symbols = []
        for s in symbols:
            if s.endswith("USDT"):
                base = s.replace("USDT", "")
                ccxt_symbols.append(f"{base}/USDT:USDT")

        tasks = [self.fetch_funding_rate(s) for s in ccxt_symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        rates = []
        for r in results:
            if isinstance(r, dict) and r is not None:
                rates.append(r)

        return rates

    # ------------------------------------------------------------------
    # 机会筛选
    # ------------------------------------------------------------------
    async def find_opportunities(self) -> list[dict]:
        """筛选满足开仓条件的套利机会"""
        rates = await self.fetch_all_rates()
        min_rate = self.strategy["entry"]["min_funding_rate"]
        min_annual = self.strategy["entry"]["min_annualized"]

        opportunities = []
        for r in rates:
            if abs(r["rate"]) >= min_rate and r["annualized"] >= min_annual:
                r["direction"] = "positive" if r["rate"] > 0 else "reverse"
                opportunities.append(r)
                logger.info(
                    f"发现机会: {r['symbol']} | "
                    f"费率: {r['rate']:.4%} | "
                    f"年化: {r['annualized']:.1%} | "
                    f"方向: {r['direction']}"
                )

        return sorted(opportunities, key=lambda x: x["annualized"], reverse=True)

    async def should_exit(
        self, symbol: str, direction: str, opened_at: str = None
    ) -> tuple[bool, str]:
        """检查是否应该平仓"""
        data = await self.fetch_funding_rate(symbol)
        if data is None:
            return False, "无法获取费率"

        rate = data["rate"]
        min_rate = self.strategy["exit"]["min_profitable_rate"]

        # 最短持仓时间保护：在保护期内不因费率轻微变动而触发平仓
        if opened_at:
            min_hold = self.strategy["exit"].get("min_holding_hours", 24)
            age_hours = (
                datetime.now(timezone.utc) - datetime.fromisoformat(opened_at)
            ).total_seconds() / 3600
            if age_hours < min_hold:
                return False, ""

        if direction == "positive" and rate < 0:
            return True, f"费率转负 ({rate:.4%})"

        if direction == "reverse" and rate > 0:
            return True, f"费率转正 ({rate:.4%})"

        if abs(rate) < min_rate:
            return True, f"费率过低 ({rate:.4%})"

        return False, ""

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    async def get_ticker(self, symbol: str) -> dict | None:
        """获取当前价格（用于计算下单数量）"""
        try:
            return await self.exchange.fetch_ticker(symbol)
        except Exception as e:
            logger.error(f"获取 {symbol} 行情失败: {e}")
            return None

    async def fetch_basis(self, symbol: str) -> dict | None:
        """
        获取现货-永续价差（基差）

        Args:
            symbol: ccxt 永续合约格式，如 'BTC/USDT:USDT'

        Returns:
            {'basis_pct': float, 'spot_price': float, 'perp_price': float}
            基差为正表示永续溢价（合约 > 现货）
        """
        try:
            # 永续: 'BTC/USDT:USDT' → 现货: 'BTC/USDT'
            spot_symbol = symbol.split(":")[0]

            # 并发获取两个 ticker，减少延迟
            perp_ticker, spot_ticker = await asyncio.gather(
                self.exchange.fetch_ticker(symbol),
                self.exchange.fetch_ticker(spot_symbol),
            )

            perp_last = float(perp_ticker.get("last") or 0)
            spot_last = float(spot_ticker.get("last") or 0)

            if not spot_last:
                return None

            basis_pct = (perp_last - spot_last) / spot_last
            return {
                "basis_pct": basis_pct,
                "spot_price": spot_last,
                "perp_price": perp_last,
            }
        except Exception as e:
            logger.warning(f"获取 {symbol} 基差失败: {e}")
            return None

    async def close(self):
        # 仅关闭自建的 exchange；共用的由所有者（screener）负责关
        if self._owns_exchange:
            await self.exchange.close()
