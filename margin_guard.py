"""
强平防护 — 监控合约持仓的风险水平，分级响应。

| 状态     | risk 区间        | 行动                                  |
| -------- | ---------------- | ------------------------------------- |
| safe     | < warn           | 不动作                                |
| warn     | [warn, topup)    | TG 告警                               |
| topup    | [topup, crit)    | 自动从现货划 USDT 给合约，目标拉回 target |
| critical | >= crit / 异常   | 强制双腿平仓                          |

风险度量：优先用 marginRatio（Binance 直接返回）；缺失/为 0 时按
liquidationPrice 与 markPrice 的相对距离换算（distance_pct = |liq-mark|/mark；
risk = 1 - distance_pct/safe_distance，safe_distance 默认 0.5 即 50%）。

依赖外部对象，不直接持有 SDK；测试时用 MagicMock 注入。
"""
from __future__ import annotations

import time
from typing import Optional

from loguru import logger


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class MarginGuard:
    """
    Args:
        executor: 提供 `get_futures_positions()` / `close_arbitrage(symbol, qty, direction, ...)` /
                  可选 `get_spot_balance(asset)`
        positions: PositionManager，用于按 symbol 找到 DB 中对应仓位（拿 direction / quantity / usdt_amount）
        transfer_service: TransferService 实例，可为 None
        notifier: TelegramNotifier，可为 None
        config: 完整 config dict
    """

    def __init__(self, executor, positions, transfer_service, notifier, config: dict):
        self.executor = executor
        self.positions = positions
        self.transfer = transfer_service
        self.notifier = notifier

        cfg = (config.get("margin_guard") or {})
        self.enabled = bool(cfg.get("enabled", True))
        self.warn_ratio = float(cfg.get("warn_ratio", 0.50))
        self.topup_ratio = float(cfg.get("topup_ratio", 0.70))
        self.critical_ratio = float(cfg.get("critical_ratio", 0.85))
        self.target_ratio = float(cfg.get("topup_target_ratio", 0.50))
        self.safe_distance_pct = float(cfg.get("safe_distance_pct", 0.50))
        self.cooldown_seconds = int(cfg.get("topup_cooldown_seconds", 300))
        self.spot_buffer_pct = float(cfg.get("spot_buffer_pct", 0.10))

        self._last_topup_at: dict[str, float] = {}

    async def check_and_protect(self) -> None:
        if not self.enabled:
            return
        try:
            positions = self.executor.get_futures_positions() or []
        except Exception as e:
            logger.error(f"[强平防护] 拉取合约持仓失败: {e}")
            return

        # 注入风险分并按危险度倒序：最高风险先处理，避免被前置仓位的慢操作拖延
        ranked = []
        for p in positions:
            risk = self._risk_score(p)
            ranked.append((risk, p))
        ranked.sort(key=lambda x: x[0], reverse=True)

        for risk, p in ranked:
            try:
                await self._handle_one(p, risk)
            except Exception as e:
                logger.exception(f"[强平防护] 处理 {p.get('symbol')} 异常: {e}")

    def _risk_score(self, p: dict) -> float:
        """统一返回 [0, 1+] 的风险分，1.0 = 已到 maintenance 边缘。"""
        ratio = _safe_float(p.get("marginRatio"))
        if ratio > 0:
            return ratio

        # marginRatio 可能为 0（cross 模式或字段缺失），用 liquidation 距离反推
        liq = _safe_float(p.get("liquidationPrice"))
        mark = _safe_float(p.get("markPrice"))
        if liq > 0 and mark > 0 and self.safe_distance_pct > 0:
            distance = abs(mark - liq) / mark
            risk = 1.0 - min(distance / self.safe_distance_pct, 1.0)
            return max(0.0, risk)
        return 0.0

    async def _handle_one(self, p: dict, risk: float) -> None:
        symbol = p.get("symbol")
        amt = _safe_float(p.get("positionAmt"))
        if amt == 0 or not symbol:
            return

        if risk < self.warn_ratio:
            return

        if risk >= self.critical_ratio:
            logger.critical(f"[强平防护] {symbol} risk={risk:.1%} 进入 critical，强制平仓")
            await self._force_close(symbol)
            await self._notify_critical(symbol, risk, "critical 阈值触发")
            return

        if risk >= self.topup_ratio:
            # 冷却：避免连续 cron 重复划资
            now = time.monotonic()
            last = self._last_topup_at.get(symbol, 0.0)
            if now - last < self.cooldown_seconds:
                logger.info(
                    f"[强平防护] {symbol} 仍在补仓冷却中 "
                    f"({int(self.cooldown_seconds - (now - last))}s 剩余)，跳过"
                )
                return

            need = self._calc_topup_amount(p, risk)
            need = self._cap_to_resources(need)
            if need <= 0:
                logger.warning(f"[强平防护] {symbol} risk={risk:.1%} 但可补金额=0，转 critical")
                await self._force_close(symbol)
                await self._notify_critical(symbol, risk, "现货可用不足，无法补保证金")
                return

            logger.warning(f"[强平防护] {symbol} risk={risk:.1%} 触发 topup，需补 ${need:.2f}")
            ok = False
            if self.transfer is not None and self.transfer.enabled:
                try:
                    ok = self.transfer.spot_to_futures(need)
                except Exception as e:
                    logger.error(f"[强平防护] 划转异常: {e}")
                    ok = False
            if ok:
                self._last_topup_at[symbol] = now
                await self._notify_topup(symbol, risk, need)
            else:
                logger.critical(f"[强平防护] {symbol} 补保证金失败，转为 critical")
                await self._force_close(symbol)
                await self._notify_critical(symbol, risk, "topup 失败")
            return

        # warn 区间
        await self._notify_warn(symbol, risk)

    def _calc_topup_amount(self, p: dict, risk: float) -> float:
        """
        估算把当前 risk 拉回 target_ratio 需要补的 USDT。

        优先按 maint / margin_balance 模型：
            risk ≈ maint / margin_balance ⇒ 想拉到 target 需 margin_balance' = maint / target
            need = maint/target − margin_balance
        缺失时退化为按 isolatedWallet × (risk/target − 1)。
        """
        if risk <= 0 or self.target_ratio <= 0:
            return 0.0

        maint = _safe_float(p.get("maintMargin"))
        margin_balance = _safe_float(p.get("marginBalance"))
        if maint > 0 and margin_balance > 0:
            need = maint / self.target_ratio - margin_balance
            return max(0.0, round(need, 2))

        wallet = _safe_float(p.get("isolatedWallet")) or _safe_float(p.get("isolatedMargin"))
        if wallet > 0:
            need = wallet * (risk / self.target_ratio - 1.0)
            return max(0.0, round(need, 2))

        return 0.0

    def _cap_to_resources(self, need: float) -> float:
        """把需要补的金额夹到现货可用余额（留 buffer）和 TransferService 上限以内。"""
        if need <= 0:
            return 0.0
        capped = need
        get_spot = getattr(self.executor, "get_spot_balance", None)
        if callable(get_spot):
            try:
                spot = float(get_spot("USDT"))
                usable = spot * (1.0 - max(0.0, min(self.spot_buffer_pct, 1.0)))
                capped = min(capped, max(0.0, usable))
            except Exception as e:
                logger.warning(f"[强平防护] 查询现货余额失败: {e}")
        if self.transfer is not None and self.transfer.enabled:
            cap_to_remaining = getattr(self.transfer, "cap_to_remaining", None)
            if callable(cap_to_remaining):
                capped = min(capped, cap_to_remaining(capped))
        return round(capped, 2)

    async def _force_close(self, symbol: str) -> None:
        if self.positions is None:
            logger.error(f"[强平防护] 无 PositionManager，无法关 {symbol}")
            return
        pos = None
        for op in self.positions.get_open_positions() or []:
            if op.get("symbol") == symbol:
                pos = op
                break
        if pos is None:
            logger.warning(f"[强平防护] DB 未找到 {symbol} 的开仓记录，跳过强平")
            return
        try:
            self.executor.close_arbitrage(
                symbol,
                pos["quantity"],
                pos["direction"],
                current_price=None,
                usdt_amount=pos.get("usdt_amount"),
            )
        except Exception as e:
            logger.exception(f"[强平防护] 强制平仓异常 {symbol}: {e}")

    # -- notifier helpers ------------------------------------------------
    async def _safe_notify(self, fn, *args, **kwargs):
        if not self.notifier:
            return
        try:
            await fn(*args, **kwargs)
        except Exception:
            pass

    async def _notify_warn(self, symbol, ratio):
        if hasattr(self.notifier, "on_margin_warn"):
            await self._safe_notify(self.notifier.on_margin_warn, symbol, ratio)
        else:
            await self._safe_notify(
                self.notifier.on_error, f"[强平预警] {symbol} marginRatio={ratio:.1%}"
            )

    async def _notify_topup(self, symbol, ratio, amount):
        if hasattr(self.notifier, "on_margin_topup"):
            await self._safe_notify(self.notifier.on_margin_topup, symbol, ratio, amount)
        else:
            await self._safe_notify(
                self.notifier.on_error,
                f"[强平防护] {symbol} marginRatio={ratio:.1%}，已划入 ${amount:.2f}",
            )

    async def _notify_critical(self, symbol, ratio, reason):
        if hasattr(self.notifier, "on_margin_critical"):
            await self._safe_notify(self.notifier.on_margin_critical, symbol, ratio, reason)
        else:
            await self._safe_notify(
                self.notifier.on_error,
                f"[紧急] {symbol} marginRatio={ratio:.1%}，{reason}，触发强制平仓",
            )
