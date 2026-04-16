"""
执行器抽象基类 — 定义开平仓接口规范

BinanceExecutor 和 SimulatedExecutor 均继承此类，保证接口一致性。
未来新增交易所执行器（如 KrakenExecutor）也需继承此类。
"""
import math
from abc import ABC, abstractmethod


class BaseExecutor(ABC):
    """所有执行器的抽象基类"""

    @abstractmethod
    def open_arbitrage(
        self,
        symbol: str,
        usdt_amount: float,
        current_price: float,
        direction: str = "positive",
        order_priority: str = "concurrent",
    ) -> dict:
        """
        原子化开仓：同时执行现货和合约两条腿，支持自动分批

        Args:
            order_priority: 'concurrent'（并发，默认）| 'futures_first'（合约先行）

        Returns:
            成功: {'success': True, 'spot_avg_price': float, 'futures_avg_price': float,
                   'quantity': float, 'slippage': float, ...}
            失败: {'success': False, 'error': str, 'rolled_back': bool}
        """
        ...

    @abstractmethod
    def close_arbitrage(
        self,
        symbol: str,
        quantity: float,
        direction: str = "positive",
        current_price: float = None,
        usdt_amount: float = None,
    ) -> dict:
        """
        原子化平仓：同时平掉现货和合约两条腿，支持自动分批

        Args:
            current_price: 当前市价（SimulatedExecutor 用于模拟成交价，BinanceExecutor 忽略）
            usdt_amount: 开仓 USDT 金额，用于分批阈值判断

        Returns:
            成功: {'success': True, 'spot_avg_price': float, 'futures_avg_price': float, ...}
            失败: {'success': False, 'error': str}
        """
        ...

    @abstractmethod
    def check_bnb_balance(self) -> float:
        """查询 BNB 余额（用于手续费抵扣检查）"""
        ...

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: int = 1) -> None:
        """设置合约杠杆"""
        ...

    @abstractmethod
    def get_spot_balance(self, asset: str = "USDT") -> float:
        """查询现货账户余额"""
        ...

    @abstractmethod
    def get_futures_balance(self, asset: str = "USDT") -> float:
        """查询合约账户余额"""
        ...

    @abstractmethod
    def get_futures_positions(self) -> list:
        """查询当前合约持仓"""
        ...

    # ------------------------------------------------------------------
    # 具体方法（子类无需覆盖）
    # ------------------------------------------------------------------
    def _split_order(self, symbol: str, usdt_amount: float) -> list[float]:
        """
        按配置阈值将大额订单拆分为多批

        子类须在 __init__ 中设置 self.split_thresholds（dict）。
        Returns: USDT 金额列表，单批时长度为 1
        """
        thresholds = getattr(self, "split_thresholds", {})
        threshold = thresholds.get(symbol, thresholds.get("default", 5000))
        if usdt_amount <= threshold:
            return [usdt_amount]
        n = math.ceil(usdt_amount / threshold)
        return [usdt_amount / n] * n
