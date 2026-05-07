"""
强平防护 + 划转 + 钱包再平衡 单元测试（全程 mock，不打真实网络）。

用法:
  python -m pytest test_margin_transfer.py -v
"""
import asyncio
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock

# 与 test_p0 一致：无 ccxt 时 stub 掉，避免 import 失败
if "ccxt" not in sys.modules:
    sys.modules["ccxt"] = MagicMock()
    _ccxt_async = MagicMock()
    sys.modules["ccxt.async_support"] = _ccxt_async
    _ccxt_async.binance = MagicMock(return_value=MagicMock())

from capital import CapitalPlan, TierAlloc
from transfer_service import (
    TransferService,
    TransferLimitExceeded,
    SPOT_TO_FUTURES,
    FUTURES_TO_SPOT,
)
from margin_guard import MarginGuard


def make_plan(initial=10000.0):
    return CapitalPlan(
        initial=initial,
        tradable=initial * 0.8,
        reserve=initial * 0.15,
        emergency=initial * 0.05,
        max_positions=3,
        t3_enabled=False,
        tiers={
            1: TierAlloc(tier=1, name="T1", max_position=3000, max_count=2, total_cap=6000),
            2: TierAlloc(tier=2, name="T2", max_position=2500, max_count=2, total_cap=5000),
        },
        daily_loss_limit=initial * 0.005,
    )


def run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("loop closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ---------------------------------------------------------------------------
# TransferService
# ---------------------------------------------------------------------------

class TestTransferServiceLimits(unittest.TestCase):

    def _make(self, max_pct_xfer=0.5, max_pct_day=1.0, enabled=True):
        sdk = MagicMock()
        sdk.user_universal_transfer = MagicMock(return_value={"tranId": 1})
        cfg = {"transfer": {
            "enabled": enabled,
            "asset": "USDT",
            "max_pct_per_transfer": max_pct_xfer,
            "max_pct_per_day": max_pct_day,
        }}
        ts = TransferService(sdk, make_plan(10000), cfg)
        return ts, sdk

    def test_normal_transfer_succeeds(self):
        ts, sdk = self._make()
        ok = ts.spot_to_futures(1000)
        self.assertTrue(ok)
        sdk.user_universal_transfer.assert_called_once()
        args, kwargs = sdk.user_universal_transfer.call_args
        self.assertEqual(kwargs.get("type"), SPOT_TO_FUTURES)
        self.assertEqual(kwargs.get("asset"), "USDT")

    def test_per_transfer_cap_blocks(self):
        # initial=10000, pct=0.5 → 5000 上限
        ts, sdk = self._make(max_pct_xfer=0.5)
        with self.assertRaises(TransferLimitExceeded):
            ts.spot_to_futures(6000)
        sdk.user_universal_transfer.assert_not_called()

    def test_daily_cap_blocks_after_accumulation(self):
        # 单次 5000，单日 6000 → 第二次 2000 越界
        sdk = MagicMock()
        sdk.user_universal_transfer = MagicMock(return_value={})
        cfg = {"transfer": {
            "enabled": True,
            "max_pct_per_transfer": 0.5,
            "max_pct_per_day": 0.6,
        }}
        ts = TransferService(sdk, make_plan(10000), cfg)
        self.assertTrue(ts.spot_to_futures(5000))
        with self.assertRaises(TransferLimitExceeded):
            ts.spot_to_futures(2000)
        self.assertEqual(sdk.user_universal_transfer.call_count, 1)

    def test_daily_reset_with_clock(self):
        sdk = MagicMock()
        sdk.user_universal_transfer = MagicMock(return_value={})
        now = [datetime(2026, 5, 7, 10, 0, tzinfo=timezone.utc)]
        cfg = {"transfer": {
            "enabled": True,
            "max_pct_per_transfer": 0.5,
            "max_pct_per_day": 0.5,
        }}
        ts = TransferService(sdk, make_plan(10000), cfg, clock=lambda: now[0])
        self.assertTrue(ts.spot_to_futures(5000))
        with self.assertRaises(TransferLimitExceeded):
            ts.spot_to_futures(100)
        # 跨日
        now[0] = now[0] + timedelta(days=1)
        self.assertTrue(ts.spot_to_futures(100))
        self.assertEqual(sdk.user_universal_transfer.call_count, 2)

    def test_disabled_returns_false_without_call(self):
        ts, sdk = self._make(enabled=False)
        self.assertFalse(ts.spot_to_futures(100))
        sdk.user_universal_transfer.assert_not_called()

    def test_sdk_failure_returns_false_no_daily_commit(self):
        sdk = MagicMock()
        sdk.user_universal_transfer = MagicMock(side_effect=RuntimeError("api down"))
        cfg = {"transfer": {
            "enabled": True,
            "max_pct_per_transfer": 0.5,
            "max_pct_per_day": 1.0,
            "retry_attempts": 0,
        }}
        ts = TransferService(sdk, make_plan(10000), cfg)
        self.assertFalse(ts.futures_to_spot(1000))
        # 失败不应消耗每日额度（已 refund）
        self.assertEqual(ts.remaining_daily, 10000.0)

    def test_sdk_retries_then_succeeds(self):
        sdk = MagicMock()
        # 前两次失败，第三次成功
        sdk.user_universal_transfer = MagicMock(
            side_effect=[RuntimeError("rate"), RuntimeError("rate"), {"tranId": 1}]
        )
        cfg = {"transfer": {
            "enabled": True,
            "max_pct_per_transfer": 0.5,
            "max_pct_per_day": 1.0,
            "retry_attempts": 2,
            "retry_backoff": 0.0,
        }}
        ts = TransferService(sdk, make_plan(10000), cfg)
        self.assertTrue(ts.spot_to_futures(500))
        self.assertEqual(sdk.user_universal_transfer.call_count, 3)
        self.assertEqual(ts.remaining_daily, 9500.0)

    def test_cap_to_remaining(self):
        sdk = MagicMock()
        sdk.user_universal_transfer = MagicMock(return_value={})
        cfg = {"transfer": {
            "enabled": True,
            "max_pct_per_transfer": 0.5,
            "max_pct_per_day": 0.6,
        }}
        ts = TransferService(sdk, make_plan(10000), cfg)
        # 单次上限 5000，但日上限 6000 → 想要 5500 时夹到 5000
        self.assertEqual(ts.cap_to_remaining(5500), 5000.0)
        ts.spot_to_futures(5000)
        # 之后日剩余 1000，请求 4000 → 1000
        self.assertEqual(ts.cap_to_remaining(4000), 1000.0)

    def test_zero_amount_raises(self):
        ts, _ = self._make()
        with self.assertRaises(ValueError):
            ts.spot_to_futures(0)
        with self.assertRaises(ValueError):
            ts.spot_to_futures(-1)

    def test_correct_type_for_futures_to_spot(self):
        ts, sdk = self._make()
        ts.futures_to_spot(500)
        kwargs = sdk.user_universal_transfer.call_args.kwargs
        self.assertEqual(kwargs.get("type"), FUTURES_TO_SPOT)


# ---------------------------------------------------------------------------
# MarginGuard
# ---------------------------------------------------------------------------

class TestMarginGuard(unittest.TestCase):

    def _make(self, transfer_ok=True, spot_balance=99999.0, cooldown=0):
        executor = MagicMock()
        executor.close_arbitrage = MagicMock(return_value={"success": True})
        executor.get_spot_balance = MagicMock(return_value=spot_balance)
        positions = MagicMock()
        positions.get_open_positions = MagicMock(return_value=[
            {"symbol": "BTCUSDT", "direction": "positive", "quantity": 0.05, "usdt_amount": 3000},
        ])
        notifier = MagicMock()
        notifier.on_margin_warn = AsyncMock()
        notifier.on_margin_topup = AsyncMock()
        notifier.on_margin_critical = AsyncMock()
        notifier.on_error = AsyncMock()

        transfer = MagicMock()
        transfer.enabled = True
        transfer.spot_to_futures = MagicMock(return_value=transfer_ok)
        transfer.cap_to_remaining = MagicMock(side_effect=lambda x: x)

        cfg = {"margin_guard": {
            "enabled": True,
            "warn_ratio": 0.50,
            "topup_ratio": 0.70,
            "critical_ratio": 0.85,
            "topup_target_ratio": 0.50,
            "topup_cooldown_seconds": cooldown,
            "spot_buffer_pct": 0.10,
        }}
        return MarginGuard(executor, positions, transfer, notifier, cfg), executor, transfer, notifier

    def _pos(self, ratio, amt=0.05, maint=200, balance=400):
        return {
            "symbol": "BTCUSDT",
            "positionAmt": str(amt),
            "marginRatio": str(ratio),
            "maintMargin": str(maint),
            "marginBalance": str(balance),
            "markPrice": "60000",
        }

    def test_safe_no_action(self):
        guard, ex, tr, nf = self._make()
        ex.get_futures_positions = MagicMock(return_value=[self._pos(0.30)])
        run(guard.check_and_protect())
        tr.spot_to_futures.assert_not_called()
        ex.close_arbitrage.assert_not_called()
        nf.on_margin_warn.assert_not_awaited()

    def test_warn_only(self):
        guard, ex, tr, nf = self._make()
        ex.get_futures_positions = MagicMock(return_value=[self._pos(0.55)])
        run(guard.check_and_protect())
        tr.spot_to_futures.assert_not_called()
        ex.close_arbitrage.assert_not_called()
        nf.on_margin_warn.assert_awaited_once()

    def test_topup_calls_transfer_with_target_amount(self):
        guard, ex, tr, nf = self._make()
        # maintMargin=200, marginBalance=300, target=0.50 → need = 200/0.5 - 300 = 100
        ex.get_futures_positions = MagicMock(return_value=[
            self._pos(0.75, maint=200, balance=300)
        ])
        run(guard.check_and_protect())
        tr.spot_to_futures.assert_called_once()
        amount = tr.spot_to_futures.call_args.args[0]
        self.assertAlmostEqual(amount, 100.0, delta=1.0)
        ex.close_arbitrage.assert_not_called()
        nf.on_margin_topup.assert_awaited_once()

    def test_topup_failure_falls_back_to_force_close(self):
        guard, ex, tr, nf = self._make(transfer_ok=False)
        ex.get_futures_positions = MagicMock(return_value=[
            self._pos(0.75, maint=200, balance=300)
        ])
        run(guard.check_and_protect())
        tr.spot_to_futures.assert_called_once()
        ex.close_arbitrage.assert_called_once()
        nf.on_margin_critical.assert_awaited_once()

    def test_critical_force_close_without_transfer(self):
        guard, ex, tr, nf = self._make()
        ex.get_futures_positions = MagicMock(return_value=[self._pos(0.90)])
        run(guard.check_and_protect())
        tr.spot_to_futures.assert_not_called()
        ex.close_arbitrage.assert_called_once()
        args, kwargs = ex.close_arbitrage.call_args
        # 按 DB 中的方向/数量平仓
        self.assertEqual(args[0], "BTCUSDT")
        self.assertEqual(args[1], 0.05)
        self.assertEqual(args[2], "positive")
        nf.on_margin_critical.assert_awaited_once()

    def test_zero_position_skipped(self):
        guard, ex, tr, nf = self._make()
        ex.get_futures_positions = MagicMock(return_value=[
            {"symbol": "BTCUSDT", "positionAmt": "0", "marginRatio": "0.99"}
        ])
        run(guard.check_and_protect())
        ex.close_arbitrage.assert_not_called()
        tr.spot_to_futures.assert_not_called()

    def test_disabled_short_circuits(self):
        guard, ex, tr, nf = self._make()
        guard.enabled = False
        ex.get_futures_positions = MagicMock(return_value=[self._pos(0.99)])
        run(guard.check_and_protect())
        ex.get_futures_positions.assert_not_called()

    def test_cooldown_blocks_consecutive_topup(self):
        guard, ex, tr, nf = self._make(cooldown=300)
        ex.get_futures_positions = MagicMock(return_value=[
            self._pos(0.75, maint=200, balance=300)
        ])
        run(guard.check_and_protect())
        run(guard.check_and_protect())
        # 第一次成功补，第二次冷却中
        self.assertEqual(tr.spot_to_futures.call_count, 1)

    def test_topup_capped_by_spot_balance(self):
        # 计算 need=1000，但现货只有 200，buffer 10% → 可用 180
        guard, ex, tr, nf = self._make(spot_balance=200)
        ex.get_futures_positions = MagicMock(return_value=[
            self._pos(0.75, maint=600, balance=400)  # 600/0.5 - 400 = 800
        ])
        run(guard.check_and_protect())
        tr.spot_to_futures.assert_called_once()
        amt = tr.spot_to_futures.call_args.args[0]
        self.assertAlmostEqual(amt, 180.0, delta=1.0)

    def test_no_resources_falls_back_to_force_close(self):
        guard, ex, tr, nf = self._make(spot_balance=0)
        ex.get_futures_positions = MagicMock(return_value=[
            self._pos(0.75, maint=600, balance=400)
        ])
        run(guard.check_and_protect())
        tr.spot_to_futures.assert_not_called()
        ex.close_arbitrage.assert_called_once()
        nf.on_margin_critical.assert_awaited_once()

    def test_critical_force_close_before_notify(self):
        """先平仓后通知：notifier 阻塞不应拖延强平动作。"""
        call_order = []
        guard, ex, tr, nf = self._make()
        ex.close_arbitrage = MagicMock(side_effect=lambda *a, **k: call_order.append("close"))

        async def slow_notify(*a, **k):
            call_order.append("notify")

        nf.on_margin_critical = AsyncMock(side_effect=slow_notify)
        ex.get_futures_positions = MagicMock(return_value=[self._pos(0.95)])
        run(guard.check_and_protect())
        self.assertEqual(call_order, ["close", "notify"])

    def test_risk_score_falls_back_to_liquidation_distance(self):
        """marginRatio=0 时按 liq 与 mark 的距离推断 risk。"""
        guard, ex, tr, nf = self._make()
        # mark=60000, liq=66000, distance=10%, safe_distance=50% → risk = 1 - 0.10/0.50 = 0.80
        pos = {
            "symbol": "BTCUSDT",
            "positionAmt": "0.05",
            "marginRatio": "0",
            "liquidationPrice": "66000",
            "markPrice": "60000",
            "maintMargin": "200",
            "marginBalance": "300",
        }
        ex.get_futures_positions = MagicMock(return_value=[pos])
        run(guard.check_and_protect())
        # risk 0.80 在 topup 区间 → 应该触发划转
        tr.spot_to_futures.assert_called_once()

    def test_positions_sorted_by_risk_desc(self):
        """高风险仓位先处理（给一个 critical 一个 warn，断言 critical 先关）。"""
        guard, ex, tr, nf = self._make()
        order = []
        ex.close_arbitrage = MagicMock(side_effect=lambda *a, **k: order.append(a[0]))
        positions = MagicMock()
        positions.get_open_positions = MagicMock(return_value=[
            {"symbol": "ETHUSDT", "direction": "positive", "quantity": 1.0, "usdt_amount": 2000},
            {"symbol": "BTCUSDT", "direction": "positive", "quantity": 0.05, "usdt_amount": 3000},
        ])
        guard.positions = positions
        ex.get_futures_positions = MagicMock(return_value=[
            {"symbol": "ETHUSDT", "positionAmt": "1.0", "marginRatio": "0.55"},
            {"symbol": "BTCUSDT", "positionAmt": "0.05", "marginRatio": "0.95"},
        ])
        run(guard.check_and_protect())
        # critical 平仓应只 close BTCUSDT，且首先处理（warn 的 ETH 不平仓）
        self.assertEqual(order, ["BTCUSDT"])


# ---------------------------------------------------------------------------
# Bot._rebalance_after_trade
# ---------------------------------------------------------------------------

class TestRebalanceAfterTrade(unittest.TestCase):
    """单独测 bot 的钱包再平衡，避免拉起完整 main.py 链路。"""

    def _make_bot(self, spot, fut, enabled=True):
        # 延迟 import 以拿到带 transfer_service 字段的最新 main
        from main import FundingArbitrageBot

        bot = FundingArbitrageBot.__new__(FundingArbitrageBot)
        bot.config = {"transfer": {"enabled": enabled}}
        bot.plan = make_plan(10000)
        bot.notifier = MagicMock()
        bot.notifier.on_rebalance = AsyncMock()

        executor = MagicMock()
        executor.get_spot_balance = MagicMock(return_value=spot)
        executor.get_futures_balance = MagicMock(return_value=fut)
        bot.executor = executor

        ts = MagicMock()
        ts.enabled = enabled
        ts.spot_to_futures = MagicMock(return_value=True)
        ts.futures_to_spot = MagicMock(return_value=True)
        bot.transfer_service = ts
        return bot, ts

    def test_spot_to_futures_when_spot_high(self):
        bot, ts = self._make_bot(spot=8000, fut=2000)
        run(bot._rebalance_after_trade())
        ts.spot_to_futures.assert_called_once()
        ts.futures_to_spot.assert_not_called()
        amt = ts.spot_to_futures.call_args.args[0]
        self.assertAlmostEqual(amt, 3000.0, delta=1.0)
        bot.notifier.on_rebalance.assert_awaited_once()

    def test_futures_to_spot_when_futures_high(self):
        bot, ts = self._make_bot(spot=1000, fut=9000)
        run(bot._rebalance_after_trade())
        ts.futures_to_spot.assert_called_once()
        ts.spot_to_futures.assert_not_called()
        amt = ts.futures_to_spot.call_args.args[0]
        self.assertAlmostEqual(amt, 4000.0, delta=1.0)

    def test_skip_when_within_threshold(self):
        # min_pos = 2500, 25% = 625 阈值；差额 200 < 625 应跳过
        bot, ts = self._make_bot(spot=5100, fut=4900)
        run(bot._rebalance_after_trade())
        ts.spot_to_futures.assert_not_called()
        ts.futures_to_spot.assert_not_called()

    def test_skip_when_within_floor_threshold(self):
        # 即使 min_pos×0.25 很小，floor=$100 起步：差额 80 应该跳过
        bot, ts = self._make_bot(spot=5040, fut=4960)
        # 差额 = 40，threshold = max(100, 625) = 625 → 跳过
        run(bot._rebalance_after_trade())
        ts.spot_to_futures.assert_not_called()

    def test_disabled_short_circuits(self):
        bot, ts = self._make_bot(spot=8000, fut=2000, enabled=False)
        run(bot._rebalance_after_trade())
        ts.spot_to_futures.assert_not_called()
        ts.futures_to_spot.assert_not_called()
        bot.executor.get_spot_balance.assert_not_called()

    def test_balance_query_failure_does_not_raise(self):
        bot, ts = self._make_bot(spot=8000, fut=2000)
        bot.executor.get_spot_balance = MagicMock(side_effect=RuntimeError("net down"))
        run(bot._rebalance_after_trade())
        ts.spot_to_futures.assert_not_called()

    def test_transfer_limit_swallowed(self):
        bot, ts = self._make_bot(spot=9000, fut=1000)
        ts.spot_to_futures = MagicMock(side_effect=TransferLimitExceeded("over"))
        run(bot._rebalance_after_trade())
        bot.notifier.on_rebalance.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
