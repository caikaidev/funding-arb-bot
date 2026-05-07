"""
统一资金划转封装 — Spot ↔ USDⓈ-M Futures。

包装 Binance `user_universal_transfer`，附加单次/单日金额上限，
防 API key 被盗后的大额搬运。

只在 live 模式注入真实 spot SDK；simulate 模式注入 mock 即可。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

from loguru import logger


SPOT_TO_FUTURES = "MAIN_UMFUTURE"
FUTURES_TO_SPOT = "UMFUTURE_MAIN"


class TransferLimitExceeded(Exception):
    """超出单次或单日划转上限"""


class TransferService:
    """
    Args:
        spot_sdk: 任意提供 `user_universal_transfer(type, asset, amount)` 方法的对象
        capital_plan: capital.CapitalPlan，用于解算上限基数
        config: dict，要求含 `transfer.max_pct_per_transfer`、`transfer.max_pct_per_day`
        clock: 可注入的时间函数（默认 utcnow），用于测试日切重置
    """

    def __init__(self, spot_sdk, capital_plan, config: dict, clock=None):
        self.sdk = spot_sdk
        self.config = config.get("transfer", {}) or {}
        self.enabled = bool(self.config.get("enabled", False))
        self.asset = self.config.get("asset", "USDT")

        cap_init = float(getattr(capital_plan, "initial", 0) or 0)
        self.max_per_transfer = cap_init * float(self.config.get("max_pct_per_transfer", 0.5))
        self.daily_cap = cap_init * float(self.config.get("max_pct_per_day", 1.0))
        self.retry_attempts = int(self.config.get("retry_attempts", 2))
        self.retry_backoff = float(self.config.get("retry_backoff", 1.0))

        self._lock = Lock()
        self._daily_total = 0.0
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._daily_reset_at = self._today_str()

    def _today_str(self) -> str:
        return self._clock().strftime("%Y-%m-%d")

    def _roll_daily_locked(self) -> None:
        today = self._today_str()
        if today != self._daily_reset_at:
            self._daily_reset_at = today
            self._daily_total = 0.0

    def _book_amount(self, amount: float) -> None:
        """原子预占额度：检查 + 累加在同一把锁内完成，失败抛异常不动账。"""
        if amount <= 0:
            raise ValueError(f"非法划转金额: {amount}")
        if self.max_per_transfer > 0 and amount > self.max_per_transfer:
            raise TransferLimitExceeded(
                f"单次划转 ${amount:.2f} > 上限 ${self.max_per_transfer:.2f}"
            )
        with self._lock:
            self._roll_daily_locked()
            if self.daily_cap > 0 and self._daily_total + amount > self.daily_cap:
                raise TransferLimitExceeded(
                    f"当日累计划转 ${self._daily_total + amount:.2f} > 上限 ${self.daily_cap:.2f}"
                )
            self._daily_total += amount

    def _refund_amount(self, amount: float) -> None:
        with self._lock:
            self._daily_total = max(0.0, self._daily_total - amount)

    def transfer(self, type_: str, amount: float, asset: Optional[str] = None) -> bool:
        """
        Returns True 表示提交成功，False 表示功能未启用 / SDK 重试后仍失败（已 log）。
        超限会抛 TransferLimitExceeded。
        """
        if not self.enabled:
            logger.debug("划转功能未启用，跳过")
            return False
        if self.sdk is None:
            logger.debug("无 SDK 注入，跳过划转")
            return False

        asset = asset or self.asset
        self._book_amount(amount)

        amount_str = f"{amount:.8f}".rstrip("0").rstrip(".") or "0"
        last_err: Optional[Exception] = None
        for attempt in range(1, self.retry_attempts + 2):  # 首次 + retry_attempts 次
            try:
                self.sdk.user_universal_transfer(type=type_, asset=asset, amount=amount_str)
                logger.info(f"划转完成: {type_} {asset} {amount:.4f}")
                return True
            except Exception as e:
                last_err = e
                if attempt <= self.retry_attempts:
                    sleep_s = self.retry_backoff * attempt
                    logger.warning(
                        f"划转失败 attempt={attempt} type={type_} amount={amount}: {e}，"
                        f"{sleep_s:.1f}s 后重试"
                    )
                    time.sleep(sleep_s)
        # 全部重试失败：归还额度，避免冻结 daily 限额
        self._refund_amount(amount)
        logger.error(f"划转最终失败 type={type_} amount={amount}: {last_err}")
        return False

    def spot_to_futures(self, amount: float, asset: Optional[str] = None) -> bool:
        return self.transfer(SPOT_TO_FUTURES, amount, asset)

    def futures_to_spot(self, amount: float, asset: Optional[str] = None) -> bool:
        return self.transfer(FUTURES_TO_SPOT, amount, asset)

    @property
    def remaining_daily(self) -> float:
        with self._lock:
            self._roll_daily_locked()
            if self.daily_cap <= 0:
                return float("inf")
            return max(0.0, self.daily_cap - self._daily_total)

    def cap_to_remaining(self, amount: float) -> float:
        """把请求金额夹到当前剩余的单次 / 单日上限以内，便于调用方做最佳努力划转。"""
        if amount <= 0:
            return 0.0
        capped = amount
        if self.max_per_transfer > 0:
            capped = min(capped, self.max_per_transfer)
        capped = min(capped, self.remaining_daily)
        return max(0.0, capped)
