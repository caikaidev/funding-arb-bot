"""
模拟执行器 — 不下单，但完整模拟交易流程

模拟内容:
  - 根据实时行情计算成交价（加上模拟滑点）
  - 记录虚拟仓位到 SQLite
  - 每次费率结算时计算真实应收金额
  - 模拟手续费和返佣
  - 一周后可查看"如果真的下单，赚了多少"
"""
import random
import time
from loguru import logger

from base_executor import BaseExecutor


class SimulatedExecutor(BaseExecutor):
    """
    模拟下单器 — 接口与 BinanceExecutor 完全一致
    可直接替换，main.py 无需改动
    """

    def __init__(self, config: dict):
        self.fees = config["fees"]
        self.max_slippage = config["strategy"]["risk"]["max_slippage"]
        self.split_thresholds = config.get("split_thresholds", {})

    def open_arbitrage(
        self,
        symbol: str,
        usdt_amount: float,
        current_price: float,
        direction: str = "positive",
        order_priority: str = "concurrent",
    ) -> dict:
        """模拟开仓：用实时价格 + 随机滑点，支持分批"""
        chunks = self._split_order(symbol, usdt_amount)

        total_qty = 0.0
        weighted_spot = 0.0
        weighted_futures = 0.0
        total_slippage = 0.0

        for i, chunk_usdt in enumerate(chunks):
            if i > 0:
                time.sleep(0.5)

            slippage = random.uniform(0.0001, 0.0005)
            if direction == "positive":
                spot_price = current_price * (1 + slippage / 2)    # 买入略高
                futures_price = current_price * (1 - slippage / 2)  # 做空略低
            else:
                spot_price = current_price * (1 - slippage / 2)
                futures_price = current_price * (1 + slippage / 2)

            chunk_qty = chunk_usdt / current_price
            total_qty += chunk_qty
            weighted_spot += spot_price * chunk_qty
            weighted_futures += futures_price * chunk_qty
            total_slippage += slippage

        avg_slippage = total_slippage / len(chunks)
        spot_fee = usdt_amount * self.fees["spot_taker"]
        futures_fee = usdt_amount * self.fees["futures_taker"]

        batch_info = f"分 {len(chunks)} 批 | " if len(chunks) > 1 else ""
        logger.info(
            f"[模拟开仓] {symbol} | "
            f"{'现货买+合约空' if direction == 'positive' else '现货卖+合约多'} | "
            f"${usdt_amount:.0f} | {batch_info}"
            f"价格 {current_price:.2f} | "
            f"滑点 {avg_slippage:.4%} | "
            f"手续费 ${spot_fee + futures_fee:.2f}"
        )

        return {
            "success": True,
            "spot_avg_price": weighted_spot / total_qty,
            "futures_avg_price": weighted_futures / total_qty,
            "quantity": total_qty,
            "slippage": avg_slippage,
            "simulated": True,
            "chunks": len(chunks),
        }

    def close_arbitrage(
        self,
        symbol: str,
        quantity: float,
        direction: str = "positive",
        current_price: float = None,
        usdt_amount: float = None,
    ) -> dict:
        """模拟平仓：用实时价格 + 随机滑点，支持分批"""
        chunks_qty: list[float]
        if usdt_amount and current_price:
            chunks_usdt = self._split_order(symbol, usdt_amount)
            chunks_qty = [quantity * (c / usdt_amount) for c in chunks_usdt] if len(chunks_usdt) > 1 else [quantity]
        else:
            chunks_qty = [quantity]

        total_qty = 0.0
        weighted_spot = 0.0
        weighted_futures = 0.0
        total_slippage = 0.0

        for i, chunk_qty in enumerate(chunks_qty):
            if i > 0:
                time.sleep(0.5)

            slippage = random.uniform(0.0001, 0.0005)
            if current_price:
                if direction == "positive":
                    spot_price = current_price * (1 - slippage / 2)
                    futures_price = current_price * (1 + slippage / 2)
                else:
                    spot_price = current_price * (1 + slippage / 2)
                    futures_price = current_price * (1 - slippage / 2)
            else:
                spot_price = 0.0
                futures_price = 0.0

            total_qty += chunk_qty
            weighted_spot += spot_price * chunk_qty
            weighted_futures += futures_price * chunk_qty
            total_slippage += slippage

        avg_slippage = total_slippage / len(chunks_qty)
        batch_info = f"分 {len(chunks_qty)} 批 | " if len(chunks_qty) > 1 else ""
        logger.info(
            f"[模拟平仓] {symbol} | {batch_info}"
            f"数量 {quantity:.6f} | 滑点 {avg_slippage:.4%}"
        )

        return {
            "success": True,
            "simulated": True,
            "spot_avg_price": weighted_spot / total_qty if total_qty else 0.0,
            "futures_avg_price": weighted_futures / total_qty if total_qty else 0.0,
            "slippage": avg_slippage,
            "chunks": len(chunks_qty),
        }

    def check_bnb_balance(self) -> float:
        return 1.0  # 模拟充足 BNB 余额

    def set_leverage(self, symbol: str, leverage: int = 1):
        logger.debug(f"[模拟] 设置 {symbol} 杠杆 {leverage}x")

    def get_spot_balance(self, asset: str = "USDT") -> float:
        return 99999.0

    def get_futures_balance(self, asset: str = "USDT") -> float:
        return 99999.0

    def get_futures_positions(self) -> list:
        return []
