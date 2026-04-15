"""
下单执行模块 — 使用 Binance 官方 SDK（不注入 brokerId，保护返佣）

核心设计：
1. 使用 ThreadPoolExecutor 并发执行现货+合约两笔订单
2. 任一笔失败立即回滚另一笔
3. 官方 SDK 不会附带 ccxt 的 brokerId，返佣完整保留
"""
import concurrent.futures
import time
from decimal import Decimal, ROUND_DOWN
from loguru import logger

from binance.spot import Spot
from binance.um_futures import UMFutures
from binance.error import ClientError, ServerError


class BinanceExecutor:
    """
    Binance 官方 SDK 下单器
    - 现货: binance-connector (Spot)
    - 合约: binance-futures-connector (UMFutures)
    - 不注入任何 brokerId → 返佣 30% 完整到账
    """

    def __init__(self, config: dict):
        creds = config["exchanges"]["binance"]

        spot_kwargs = {
            "api_key": creds["api_key"],
            "api_secret": creds["api_secret"],
        }
        futures_kwargs = {
            "key": creds["api_key"],
            "secret": creds["api_secret"],
        }

        # 如果有测试网配置
        if "spot_base_url" in creds:
            spot_kwargs["base_url"] = creds["spot_base_url"]
        if "futures_base_url" in creds:
            futures_kwargs["base_url"] = creds["futures_base_url"]

        self.spot = Spot(**spot_kwargs)
        self.futures = UMFutures(**futures_kwargs)
        self.fees = config["fees"]
        self.max_slippage = config["strategy"]["risk"]["max_slippage"]

        # 缓存交易精度
        self._precision_cache = {}

    # ------------------------------------------------------------------
    # 精度处理
    # ------------------------------------------------------------------
    def _get_precision(self, symbol: str) -> dict:
        """获取交易对的精度信息"""
        if symbol in self._precision_cache:
            return self._precision_cache[symbol]

        info = self.futures.exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                qty_precision = s["quantityPrecision"]
                price_precision = s["pricePrecision"]
                # 获取最小下单量
                min_qty = None
                step_size = None
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        min_qty = float(f["minQty"])
                        step_size = float(f["stepSize"])

                result = {
                    "qty_precision": qty_precision,
                    "price_precision": price_precision,
                    "min_qty": min_qty,
                    "step_size": step_size,
                }
                self._precision_cache[symbol] = result
                return result

        raise ValueError(f"未找到交易对 {symbol}")

    def _round_qty(self, qty: float, symbol: str) -> str:
        """按交易所精度截断数量（向下取整，避免超出余额）"""
        prec = self._get_precision(symbol)
        step = prec["step_size"]

        if step and step > 0:
            # 按 step_size 对齐
            d_qty = Decimal(str(qty))
            d_step = Decimal(str(step))
            rounded = (d_qty // d_step) * d_step
            return str(rounded)
        else:
            p = prec["qty_precision"]
            d = Decimal(str(qty))
            return str(d.quantize(Decimal(10) ** -p, rounding=ROUND_DOWN))

    # ------------------------------------------------------------------
    # 核心：原子化开仓
    # ------------------------------------------------------------------
    def open_arbitrage(
        self,
        symbol: str,
        usdt_amount: float,
        current_price: float,
        direction: str = "positive",
    ) -> dict:
        """
        原子化开仓：并发执行现货+合约

        Args:
            symbol: 'BTCUSDT'
            usdt_amount: 投入金额 (USDT)
            current_price: 当前价格（由 monitor 提供）
            direction: 'positive' = 现货买+合约空 | 'reverse' = 现货卖+合约多

        Returns:
            {'success': bool, 'spot': {}, 'futures': {}, 'slippage': float}
        """
        # 1. 计算下单数量
        raw_qty = usdt_amount / current_price
        quantity = self._round_qty(raw_qty, symbol)
        qty_float = float(quantity)

        prec = self._get_precision(symbol)
        if prec["min_qty"] and qty_float < prec["min_qty"]:
            return {
                "success": False,
                "error": f"数量 {quantity} 低于最小下单量 {prec['min_qty']}",
            }

        logger.info(
            f"准备开仓: {symbol} | "
            f"价格: {current_price} | "
            f"数量: {quantity} | "
            f"金额: ${usdt_amount:.2f} | "
            f"方向: {direction}"
        )

        # 2. 定义两条腿的下单函数
        def exec_spot():
            if direction == "positive":
                return self.spot.new_order(
                    symbol=symbol,
                    side="BUY",
                    type="MARKET",
                    quantity=quantity,
                )
            else:
                return self.spot.new_order(
                    symbol=symbol,
                    side="SELL",
                    type="MARKET",
                    quantity=quantity,
                )

        def exec_futures():
            if direction == "positive":
                return self.futures.new_order(
                    symbol=symbol,
                    side="SELL",
                    type="MARKET",
                    quantity=quantity,
                )
            else:
                return self.futures.new_order(
                    symbol=symbol,
                    side="BUY",
                    type="MARKET",
                    quantity=quantity,
                )

        # 3. 并发执行（ThreadPoolExecutor）
        spot_result = None
        futures_result = None
        spot_error = None
        futures_error = None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            spot_future = pool.submit(exec_spot)
            futures_future = pool.submit(exec_futures)

            # 等待结果
            try:
                spot_result = spot_future.result(timeout=10)
            except Exception as e:
                spot_error = e

            try:
                futures_result = futures_future.result(timeout=10)
            except Exception as e:
                futures_error = e

        # 4. 处理结果
        spot_ok = spot_result is not None and spot_error is None
        futures_ok = futures_result is not None and futures_error is None

        if spot_ok and futures_ok:
            # 两边都成功 → 计算滑点
            spot_avg = self._calc_avg_price(spot_result)
            futures_avg = self._calc_avg_price(futures_result)
            slippage = abs(spot_avg - futures_avg) / current_price

            logger.success(
                f"开仓成功: {symbol} | "
                f"现货成交: {spot_avg:.2f} | "
                f"合约成交: {futures_avg:.2f} | "
                f"滑点: {slippage:.4%}"
            )

            if slippage > self.max_slippage:
                logger.warning(f"滑点 {slippage:.4%} 超过阈值 {self.max_slippage:.2%}")

            return {
                "success": True,
                "spot": spot_result,
                "futures": futures_result,
                "spot_avg_price": spot_avg,
                "futures_avg_price": futures_avg,
                "quantity": qty_float,
                "slippage": slippage,
            }

        # 单边失败 → 回滚
        if spot_ok and not futures_ok:
            logger.error(f"合约下单失败: {futures_error}，回滚现货...")
            self._rollback_spot(symbol, quantity, direction)
            return {"success": False, "error": f"futures_failed: {futures_error}", "rolled_back": True}

        if not spot_ok and futures_ok:
            logger.error(f"现货下单失败: {spot_error}，回滚合约...")
            self._rollback_futures(symbol, quantity, direction)
            return {"success": False, "error": f"spot_failed: {spot_error}", "rolled_back": True}

        # 双边失败
        logger.error(f"双边失败! 现货: {spot_error} | 合约: {futures_error}")
        return {"success": False, "error": "both_failed"}

    # ------------------------------------------------------------------
    # 平仓
    # ------------------------------------------------------------------
    def close_arbitrage(
        self,
        symbol: str,
        quantity: float,
        direction: str = "positive",
    ) -> dict:
        """
        平仓：方向与开仓相反
        positive 开仓 = 现货买+合约空 → 平仓 = 现货卖+合约买
        """
        qty_str = self._round_qty(quantity, symbol)

        logger.info(f"准备平仓: {symbol} | 数量: {qty_str} | 方向: {direction}")

        def exec_spot_close():
            side = "SELL" if direction == "positive" else "BUY"
            return self.spot.new_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=qty_str,
            )

        def exec_futures_close():
            side = "BUY" if direction == "positive" else "SELL"
            return self.futures.new_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=qty_str,
                reduceOnly=True,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            sf = pool.submit(exec_spot_close)
            ff = pool.submit(exec_futures_close)

            try:
                spot_result = sf.result(timeout=10)
                futures_result = ff.result(timeout=10)
                logger.success(f"平仓成功: {symbol}")
                return {
                    "success": True,
                    "spot": spot_result,
                    "futures": futures_result,
                }
            except Exception as e:
                logger.error(f"平仓异常: {e}")
                return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 回滚 & 工具
    # ------------------------------------------------------------------
    def _rollback_spot(self, symbol, quantity, direction):
        """回滚现货单"""
        try:
            side = "SELL" if direction == "positive" else "BUY"
            self.spot.new_order(symbol=symbol, side=side, type="MARKET", quantity=quantity)
            logger.info("现货回滚成功")
        except Exception as e:
            logger.critical(f"现货回滚失败！需手动处理: {e}")

    def _rollback_futures(self, symbol, quantity, direction):
        """回滚合约单"""
        try:
            side = "BUY" if direction == "positive" else "SELL"
            self.futures.new_order(
                symbol=symbol, side=side, type="MARKET",
                quantity=quantity, reduceOnly=True,
            )
            logger.info("合约回滚成功")
        except Exception as e:
            logger.critical(f"合约回滚失败！需手动处理: {e}")

    def _calc_avg_price(self, order_result: dict) -> float:
        """从订单结果计算平均成交价"""
        fills = order_result.get("fills", [])
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            total_val = sum(float(f["qty"]) * float(f["price"]) for f in fills)
            return total_val / total_qty if total_qty > 0 else 0

        # 合约返回格式不同
        avg = order_result.get("avgPrice")
        if avg:
            return float(avg)

        return float(order_result.get("price", 0))

    # ------------------------------------------------------------------
    # 账户查询
    # ------------------------------------------------------------------
    def get_spot_balance(self, asset: str = "USDT") -> float:
        """查询现货余额"""
        try:
            info = self.spot.account()
            for b in info["balances"]:
                if b["asset"] == asset:
                    return float(b["free"])
            return 0.0
        except Exception as e:
            logger.error(f"查询现货余额失败: {e}")
            return 0.0

    def get_futures_balance(self, asset: str = "USDT") -> float:
        """查询合约余额"""
        try:
            balances = self.futures.balance()
            for b in balances:
                if b["asset"] == asset:
                    return float(b["availableBalance"])
            return 0.0
        except Exception as e:
            logger.error(f"查询合约余额失败: {e}")
            return 0.0

    def get_futures_positions(self) -> list:
        """查询合约持仓"""
        try:
            positions = self.futures.get_position_risk()
            return [
                p for p in positions
                if float(p.get("positionAmt", 0)) != 0
            ]
        except Exception as e:
            logger.error(f"查询合约持仓失败: {e}")
            return []

    def set_leverage(self, symbol: str, leverage: int = 1):
        """设置杠杆（建议 1x）"""
        try:
            self.futures.change_leverage(symbol=symbol, leverage=leverage)
            logger.info(f"设置 {symbol} 杠杆为 {leverage}x")
        except Exception as e:
            # 已经是该杠杆会报错，可忽略
            logger.debug(f"设置杠杆: {e}")
