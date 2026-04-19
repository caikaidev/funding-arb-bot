"""
账实对账模块 — 对比 DB 持仓与合约真实仓位，发现偏差则暂停开仓并 TG 告警

启动时运行一次，之后每小时运行一次（由 APScheduler 调度）。
仅在 live 模式下使用，simulate 模式跳过。
"""
from loguru import logger


class Reconciler:
    """
    账实核对器。
    - 偏差 > DEVIATION_THRESHOLD (5%) 触发告警
    - 合约有但 DB 无的孤儿仓位触发告警
    - 告警期间 is_clean=False，task_scan_and_open 会拒绝开新仓
    - 对账恢复后自动解除 is_clean 限制
    """

    DEVIATION_THRESHOLD = 0.05

    def __init__(self, executor, positions, notifier=None):
        self.executor = executor
        self.positions = positions
        self.notifier = notifier
        self.is_clean = True

    async def check(self) -> bool:
        """
        执行对账检查。
        返回 True = 账实相符，可以开新仓；False = 发现偏差，已暂停开仓。
        """
        db_positions = self.positions.get_open_positions()

        try:
            exchange_positions = self.executor.get_futures_positions()
        except Exception as e:
            logger.error(f"[对账] 无法获取合约持仓: {e}")
            return True  # 查询失败不阻塞交易，仅记录

        exchange_map = {
            p["symbol"]: float(p.get("positionAmt", 0))
            for p in exchange_positions
        }

        issues = []

        for pos in db_positions:
            symbol = pos["symbol"]
            expected_qty = pos["quantity"]
            actual_amt = exchange_map.get(symbol, 0)

            # positive: perp 是空仓，positionAmt 为负；reverse: perp 是多仓，positionAmt 为正
            actual_qty = abs(actual_amt) if pos["direction"] == "positive" else float(actual_amt)

            deviation = abs(actual_qty - expected_qty) / max(expected_qty, 1e-8)
            if deviation > self.DEVIATION_THRESHOLD:
                issues.append(
                    f"{symbol}: DB={expected_qty:.6f}, "
                    f"交易所={actual_qty:.6f} (偏差 {deviation:.1%})"
                )

        # 合约有但 DB 无的孤儿仓位
        db_symbols = {pos["symbol"] for pos in db_positions}
        for symbol, amt in exchange_map.items():
            if symbol not in db_symbols and abs(amt) > 1e-8:
                issues.append(f"孤儿合约仓位: {symbol} positionAmt={amt} (DB 中无记录)")

        if issues:
            msg = "账实不符，已暂停开新仓\n" + "\n".join(f"• {i}" for i in issues)
            logger.error(f"[对账] {msg}")
            if self.notifier:
                try:
                    await self.notifier.on_error(msg)
                except Exception:
                    pass
            self.is_clean = False
            return False

        if not self.is_clean:
            logger.info("[对账] 账实已恢复一致，允许开新仓")
            self.is_clean = True

        return True
