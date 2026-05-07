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
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception

from binance.spot import Spot
from binance.um_futures import UMFutures
from binance.error import ClientError, ServerError

from base_executor import BaseExecutor


def _is_transient_error(exc: Exception) -> bool:
    """只对 429 限频和 5xx 服务器错误重试"""
    if isinstance(exc, ServerError):
        return True
    if isinstance(exc, ClientError) and getattr(exc, "status_code", 0) == 429:
        return True
    return False


_query_retry = retry(
    retry=retry_if_exception(_is_transient_error),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)


class BinanceExecutor(BaseExecutor):
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
        self.max_single_order_usdt = config["strategy"]["risk"].get("max_single_order_usdt") or 0
        self.split_thresholds = config.get("split_thresholds", {})

        # 缓存交易精度
        self._precision_cache = {}
        # 严重告警队列（线程安全 append/clear），由 main 异步推送 TG
        self._critical_errors: list[str] = []

    # ------------------------------------------------------------------
    # 精度 & 成交校验
    # ------------------------------------------------------------------
    def _check_fill(self, result: dict, requested_qty: str) -> None:
        """验证市价单实际成交量，不足请求量 99% 视为失败（部分成交保护）"""
        executed = float(result.get("executedQty") or result.get("cumQty", 0))
        requested = float(requested_qty)
        if requested > 0 and executed < requested * 0.99:
            raise ValueError(
                f"部分成交: 请求 {requested}, 实际 {executed:.6f} ({executed/requested:.1%})"
            )

    # ------------------------------------------------------------------
    # 精度处理
    # ------------------------------------------------------------------
    @_query_retry
    def _preload_precision(self) -> None:
        """启动时一次性加载全量交易对精度，避免每次 cache miss 都拉取 ~2MB 的 exchange_info"""
        if self._precision_cache:
            return
        info = self.futures.exchange_info()
        for s in info["symbols"]:
            min_qty = None
            step_size = None
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    min_qty = float(f["minQty"])
                    step_size = float(f["stepSize"])
            self._precision_cache[s["symbol"]] = {
                "qty_precision": s["quantityPrecision"],
                "price_precision": s["pricePrecision"],
                "min_qty": min_qty,
                "step_size": step_size,
            }
        logger.info(f"预加载交易精度: {len(self._precision_cache)} 个交易对")

    def _get_precision(self, symbol: str) -> dict:
        """获取交易对的精度信息（首次调用时触发全量预加载）"""
        if not self._precision_cache:
            self._preload_precision()
        if symbol in self._precision_cache:
            return self._precision_cache[symbol]
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
    # 核心：原子化开仓（含分批 + 下单顺序支持）
    # ------------------------------------------------------------------
    def open_arbitrage(
        self,
        symbol: str,
        usdt_amount: float,
        current_price: float,
        direction: str = "positive",
        order_priority: str = "concurrent",
    ) -> dict:
        """
        原子化开仓，大额自动分批

        Args:
            order_priority: 'concurrent'（并发）| 'futures_first'（合约先行，推荐）
        """
        # 胖手指保护：单笔金额不得超过硬上限
        if self.max_single_order_usdt and usdt_amount > self.max_single_order_usdt:
            logger.error(
                f"单笔金额 ${usdt_amount:.0f} 超过硬上限 ${self.max_single_order_usdt:.0f}，拒绝下单"
            )
            return {"success": False, "error": f"fat_finger: ${usdt_amount:.0f} > ${self.max_single_order_usdt:.0f}"}

        chunks = self._split_order(symbol, usdt_amount)

        if len(chunks) == 1:
            return self._open_single(symbol, chunks[0], current_price, direction, order_priority)

        logger.info(f"大额开仓分 {len(chunks)} 批执行: {symbol} ${usdt_amount:.0f}")
        successful = []
        total_qty = 0.0
        weighted_spot = 0.0
        weighted_futures = 0.0

        for i, chunk_usdt in enumerate(chunks):
            if i > 0:
                time.sleep(0.5)
            r = self._open_single(symbol, chunk_usdt, current_price, direction, order_priority)
            if r["success"]:
                q = r["quantity"]
                successful.append(r)
                total_qty += q
                weighted_spot += r["spot_avg_price"] * q
                weighted_futures += r["futures_avg_price"] * q
            else:
                logger.warning(f"第 {i + 1}/{len(chunks)} 批开仓失败: {r.get('error')}")
                if not successful:
                    return r  # 首批即失败，直接返回
                break  # 已有成功批次，保留为较小仓位

        if not successful:
            return {"success": False, "error": "all_chunks_failed"}

        partial = len(successful) < len(chunks)
        if partial:
            logger.warning(f"部分开仓: {symbol} 完成 {len(successful)}/{len(chunks)} 批")

        return {
            "success": True,
            "spot_avg_price": weighted_spot / total_qty,
            "futures_avg_price": weighted_futures / total_qty,
            "quantity": total_qty,
            "slippage": sum(r["slippage"] for r in successful) / len(successful),
            "chunks": len(successful),
            "partial": partial,
        }

    def _open_single(
        self,
        symbol: str,
        usdt_amount: float,
        current_price: float,
        direction: str,
        order_priority: str,
    ) -> dict:
        """执行单批开仓（内部方法，不含分批逻辑）"""
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
            f"准备开仓: {symbol} | 价格: {current_price} | "
            f"数量: {quantity} | 金额: ${usdt_amount:.2f} | "
            f"方向: {direction} | 模式: {order_priority}"
        )

        def exec_spot():
            return self.spot.new_order(
                symbol=symbol,
                side="BUY" if direction == "positive" else "SELL",
                type="MARKET",
                quantity=quantity,
            )

        def exec_futures():
            return self.futures.new_order(
                symbol=symbol,
                side="SELL" if direction == "positive" else "BUY",
                type="MARKET",
                quantity=quantity,
            )

        spot_result = None
        futures_result = None
        spot_error = None
        futures_error = None

        if order_priority == "futures_first":
            # 合约先行：顺序执行，降低持仓不对称风险
            try:
                futures_result = exec_futures()
            except Exception as e:
                futures_error = e

            if futures_error:
                logger.error(f"合约先行下单失败: {futures_error}")
                return {"success": False, "error": f"futures_failed: {futures_error}"}

            # 校验合约成交量
            try:
                self._check_fill(futures_result, quantity)
            except ValueError as e:
                logger.error(f"合约部分成交，不开现货: {e}")
                return {"success": False, "error": f"futures_partial_fill: {e}"}

            try:
                spot_result = exec_spot()
            except Exception as e:
                spot_error = e

            if spot_error:
                logger.error(f"合约已成交，现货下单失败: {spot_error}，回滚合约...")
                self._rollback_futures(symbol, quantity, direction)
                return {"success": False, "error": f"spot_failed: {spot_error}", "rolled_back": True}

            # 校验现货成交量
            try:
                self._check_fill(spot_result, quantity)
            except ValueError as e:
                logger.error(f"现货部分成交: {e}，回滚合约...")
                self._rollback_futures(symbol, quantity, direction)
                return {"success": False, "error": f"spot_partial_fill: {e}", "rolled_back": True}

        else:
            # 并发模式（默认）
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                spot_future = pool.submit(exec_spot)
                futures_future = pool.submit(exec_futures)

                try:
                    spot_result = spot_future.result(timeout=10)
                except Exception as e:
                    spot_error = e

                try:
                    futures_result = futures_future.result(timeout=10)
                except Exception as e:
                    futures_error = e

            spot_ok = spot_result is not None and spot_error is None
            futures_ok = futures_result is not None and futures_error is None

            if not spot_ok and not futures_ok:
                logger.error(f"双边失败! 现货: {spot_error} | 合约: {futures_error}")
                return {"success": False, "error": "both_failed"}

            if spot_ok and not futures_ok:
                logger.error(f"合约下单失败: {futures_error}，回滚现货...")
                self._rollback_spot(symbol, quantity, direction)
                return {"success": False, "error": f"futures_failed: {futures_error}", "rolled_back": True}

            if not spot_ok and futures_ok:
                logger.error(f"现货下单失败: {spot_error}，回滚合约...")
                self._rollback_futures(symbol, quantity, direction)
                return {"success": False, "error": f"spot_failed: {spot_error}", "rolled_back": True}

            # 两边都有返回：校验成交量
            try:
                self._check_fill(spot_result, quantity)
                self._check_fill(futures_result, quantity)
            except ValueError as e:
                logger.error(f"成交量不足，回滚两腿: {e}")
                self._rollback_spot(symbol, quantity, direction)
                self._rollback_futures(symbol, quantity, direction)
                return {"success": False, "error": f"partial_fill: {e}", "rolled_back": True}

        # 两边都成功 → 计算滑点
        spot_avg = self._calc_avg_price(spot_result)
        futures_avg = self._calc_avg_price(futures_result)
        slippage = abs(spot_avg - futures_avg) / current_price

        # 滑点超标：立即回滚两腿，不记录仓位
        if slippage > self.max_slippage:
            logger.error(f"滑点 {slippage:.4%} 超过阈值 {self.max_slippage:.2%}，立即回滚两腿")
            self._rollback_spot(symbol, quantity, direction)
            self._rollback_futures(symbol, quantity, direction)
            return {"success": False, "error": f"slippage_exceeded: {slippage:.4%}", "rolled_back": True}

        logger.success(
            f"开仓成功: {symbol} | "
            f"现货成交: {spot_avg:.2f} | "
            f"合约成交: {futures_avg:.2f} | "
            f"滑点: {slippage:.4%}"
        )

        return {
            "success": True,
            "spot": spot_result,
            "futures": futures_result,
            "spot_avg_price": spot_avg,
            "futures_avg_price": futures_avg,
            "quantity": qty_float,
            "slippage": slippage,
        }

    # ------------------------------------------------------------------
    # 平仓（含分批支持）
    # ------------------------------------------------------------------
    def close_arbitrage(
        self,
        symbol: str,
        quantity: float,
        direction: str = "positive",
        current_price: float = None,
        usdt_amount: float = None,
    ) -> dict:
        """
        平仓：方向与开仓相反，大额自动分批
        positive: 现货卖 + 合约买回
        reverse:  现货买回 + 合约卖

        Args:
            current_price: 当前市价（BinanceExecutor 忽略，由实际成交价决定）
            usdt_amount: 开仓时的 USDT 金额，用于判断是否需要分批
        """
        if usdt_amount:
            chunks_usdt = self._split_order(symbol, usdt_amount)
            if len(chunks_usdt) > 1:
                chunks_qty = [quantity * (c / usdt_amount) for c in chunks_usdt]
            else:
                chunks_qty = [quantity]
        else:
            chunks_qty = [quantity]

        if len(chunks_qty) == 1:
            return self._close_single(symbol, chunks_qty[0], direction, current_price)

        logger.info(f"大额平仓分 {len(chunks_qty)} 批执行: {symbol}")
        successful = []
        total_qty = 0.0
        weighted_spot = 0.0
        weighted_futures = 0.0

        for i, chunk_qty in enumerate(chunks_qty):
            if i > 0:
                time.sleep(0.5)
            r = self._close_single(symbol, chunk_qty, direction, current_price)
            if r["success"]:
                q_done = float(self._round_qty(chunk_qty, symbol))
                successful.append(r)
                total_qty += q_done
                weighted_spot += r["spot_avg_price"] * q_done
                weighted_futures += r["futures_avg_price"] * q_done
            else:
                logger.warning(f"第 {i + 1}/{len(chunks_qty)} 批平仓失败: {r.get('error')}")
                if not successful:
                    return r
                break

        if not successful:
            return {"success": False, "error": "all_chunks_failed"}

        partial = len(successful) < len(chunks_qty)
        if partial:
            logger.warning(f"部分平仓: {symbol} 完成 {len(successful)}/{len(chunks_qty)} 批")
            # 分批平仓只完成部分时，仓位已变成“半平”状态，必须触发人工关注
            self._critical_errors.append(
                f"[紧急] 部分平仓: {symbol} 完成 {len(successful)}/{len(chunks_qty)} 批，"
                "请尽快核对剩余仓位，避免账实错配"
            )

        return {
            "success": True,
            "spot_avg_price": weighted_spot / total_qty,
            "futures_avg_price": weighted_futures / total_qty,
            "chunks": len(successful),
            "partial": partial,
            "quantity": total_qty,
        }

    def _close_single(
        self,
        symbol: str,
        quantity: float,
        direction: str,
        current_price: float = None,
    ) -> dict:
        """执行单批平仓（内部方法，current_price 由实际成交价决定，此参数仅保留接口一致性）"""
        qty_str = self._round_qty(quantity, symbol)
        logger.info(f"准备平仓: {symbol} | 数量: {qty_str} | 方向: {direction}")

        def exec_spot_close():
            return self.spot.new_order(
                symbol=symbol,
                side="SELL" if direction == "positive" else "BUY",
                type="MARKET",
                quantity=qty_str,
            )

        def exec_futures_close():
            return self.futures.new_order(
                symbol=symbol,
                side="BUY" if direction == "positive" else "SELL",
                type="MARKET",
                quantity=qty_str,
                reduceOnly=True,
            )

        spot_result = None
        futures_result = None
        spot_error = None
        futures_error = None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            sf = pool.submit(exec_spot_close)
            ff = pool.submit(exec_futures_close)
            try:
                spot_result = sf.result(timeout=10)
            except Exception as e:
                spot_error = e
            try:
                futures_result = ff.result(timeout=10)
            except Exception as e:
                futures_error = e

        if spot_error or futures_error:
            logger.error(f"平仓异常: spot={spot_error} futures={futures_error}")
            # 单腿成功、另一腿失败：仓位已失衡，需立即告警
            if not (spot_error and futures_error):
                closed_leg = "spot" if spot_result is not None else "futures"
                open_leg = "futures" if closed_leg == "spot" else "spot"
                self._critical_errors.append(
                    f"[紧急] 平仓单腿失败: {symbol} — {closed_leg}腿已平，{open_leg}腿失败，仓位失衡！需人工处理"
                )
            return {"success": False, "error": f"spot={spot_error} futures={futures_error}"}

        # 校验成交量 + 补平尾单（平仓部分成交时不做 rollback，而是补单平尾）
        self._close_tail(symbol, qty_str, spot_result, direction, "spot")
        self._close_tail(symbol, qty_str, futures_result, direction, "futures")

        spot_avg = self._calc_avg_price(spot_result)
        futures_avg = self._calc_avg_price(futures_result)
        logger.success(f"平仓成功: {symbol} | 现货: {spot_avg:.4f} | 合约: {futures_avg:.4f}")
        return {
            "success": True,
            "spot": spot_result,
            "futures": futures_result,
            "spot_avg_price": spot_avg,
            "futures_avg_price": futures_avg,
        }

    def _close_tail(
        self, symbol: str, requested_qty: str, result: dict, direction: str, leg: str
    ) -> None:
        """平仓部分成交时补发尾单，确保仓位完整平掉"""
        executed = float(result.get("executedQty") or result.get("cumQty", 0))
        requested = float(requested_qty)
        if requested <= 0 or executed >= requested * 0.99:
            return
        missing = requested - executed
        tail_str = self._round_qty(missing, symbol)
        logger.warning(f"平仓部分成交 ({executed/requested:.1%})，补发尾单: {symbol} {leg} {tail_str}")
        try:
            if leg == "spot":
                self.spot.new_order(
                    symbol=symbol,
                    side="SELL" if direction == "positive" else "BUY",
                    type="MARKET",
                    quantity=tail_str,
                )
            else:
                self.futures.new_order(
                    symbol=symbol,
                    side="BUY" if direction == "positive" else "SELL",
                    type="MARKET",
                    quantity=tail_str,
                    reduceOnly=True,
                )
            logger.info(f"平仓尾单成功: {symbol} {leg}")
        except Exception as e:
            logger.critical(f"平仓尾单失败！需手动处理 {symbol} {leg} {tail_str}: {e}")
            self._critical_errors.append(f"[紧急] 平仓尾单失败 {symbol} {leg}: {e}")

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
            self._critical_errors.append(f"[紧急] 现货回滚失败 {symbol}: {e}")

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
            self._critical_errors.append(f"[紧急] 合约回滚失败 {symbol}: {e}")

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
    @_query_retry
    def check_bnb_balance(self) -> float:
        """查询 BNB 现货余额（用于手续费抵扣检查）"""
        try:
            info = self.spot.account()
            for b in info["balances"]:
                if b["asset"] == "BNB":
                    return float(b["free"])
            return 0.0
        except Exception as e:
            logger.error(f"查询 BNB 余额失败: {e}")
            return 0.0

    @_query_retry
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

    @_query_retry
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

    @_query_retry
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
