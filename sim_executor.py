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
from decimal import Decimal, ROUND_DOWN
from loguru import logger


class SimulatedExecutor:
    """
    模拟下单器 — 接口与 BinanceExecutor 完全一致
    可直接替换，main.py 无需改动
    """

    def __init__(self, config: dict):
        self.fees = config["fees"]
        self.max_slippage = config["strategy"]["risk"]["max_slippage"]

    def open_arbitrage(
        self,
        symbol: str,
        usdt_amount: float,
        current_price: float,
        direction: str = "positive",
    ) -> dict:
        """模拟开仓：用实时价格 + 随机滑点"""

        # 模拟滑点：0.01% ~ 0.05%（主流币的真实范围）
        slippage = random.uniform(0.0001, 0.0005)
        if direction == "positive":
            spot_price = current_price * (1 + slippage / 2)   # 买入略高
            futures_price = current_price * (1 - slippage / 2) # 做空略低
        else:
            spot_price = current_price * (1 - slippage / 2)
            futures_price = current_price * (1 + slippage / 2)

        quantity = usdt_amount / current_price

        # 模拟手续费
        spot_fee = usdt_amount * self.fees["spot_taker"]
        futures_fee = usdt_amount * self.fees["futures_taker"]

        logger.info(
            f"[模拟开仓] {symbol} | "
            f"{'现货买+合约空' if direction == 'positive' else '现货卖+合约多'} | "
            f"${usdt_amount:.0f} | "
            f"价格 {current_price:.2f} | "
            f"滑点 {slippage:.4%} | "
            f"手续费 ${spot_fee + futures_fee:.2f}"
        )

        return {
            "success": True,
            "spot_avg_price": spot_price,
            "futures_avg_price": futures_price,
            "quantity": quantity,
            "slippage": slippage,
            "simulated": True,
        }

    def close_arbitrage(
        self,
        symbol: str,
        quantity: float,
        direction: str = "positive",
    ) -> dict:
        """模拟平仓"""
        slippage = random.uniform(0.0001, 0.0005)

        logger.info(f"[模拟平仓] {symbol} | 数量 {quantity:.6f}")

        return {
            "success": True,
            "simulated": True,
            "slippage": slippage,
        }

    def set_leverage(self, symbol: str, leverage: int = 1):
        """模拟设置杠杆"""
        logger.debug(f"[模拟] 设置 {symbol} 杠杆 {leverage}x")

    def get_spot_balance(self, asset: str = "USDT") -> float:
        return 99999.0  # 模拟无限余额

    def get_futures_balance(self, asset: str = "USDT") -> float:
        return 99999.0

    def get_futures_positions(self) -> list:
        return []
