"""
P0 安全改动单元测试 (A 组：纯逻辑，全程 mock，不依赖网络/真实行情)

覆盖:
  #1  _check_fill            — 部分成交检测
  #2  _open_single 滑点中止  — slippage_exceeded → rollback 两腿
  #1  _open_single 成交量    — partial_fill → rollback 对应腿
  #4  should_exit 保护期     — min_holding_hours 内不平仓
  #5  Reconciler             — 账实不符标脏 / 恢复后解除
  #7  _in_settlement_window  — 结算窗口时间边界
  #8  _passes_break_even     — 费率回本门槛计算
  #9  部分平仓 partial → 不写 DB 平仓
  #10 开仓 record_trade 手续费按 actual_usdt（非 alloc）
  #11 close_arbitrage 分批部分成功 → 返回 quantity + 紧急告警

用法:
  python test_p0.py
  python -m pytest test_p0.py -v
"""
import asyncio
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# 无本地 ccxt 包时仍允许 import monitor/main（CI/精简环境）
if "ccxt" not in sys.modules:
    sys.modules["ccxt"] = MagicMock()
    _ccxt_async = MagicMock()
    sys.modules["ccxt.async_support"] = _ccxt_async
    _ccxt_async.binance = MagicMock(return_value=MagicMock())

# ---------------------------------------------------------------------------
# 共用测试配置
# ---------------------------------------------------------------------------

EXECUTOR_CONFIG = {
    "exchanges": {"binance": {"api_key": "k", "api_secret": "s"}},
    "fees": {
        "spot_taker": 0.001, "futures_taker": 0.0004,
        "use_bnb_discount": False,
    },
    "strategy": {"risk": {"max_slippage": 0.002}},
    "split_thresholds": {"default": 5000},
}

MONITOR_CONFIG = {
    "exchanges": {"binance": {"api_key": "k", "api_secret": "s"}},
    "strategy": {
        "exit": {"min_profitable_rate": 0.0005, "min_holding_hours": 24}
    },
}

BOT_CONFIG = {
    "exchanges": {"binance": {"api_key": "k", "api_secret": "s"}},
    "fees": {
        "spot_taker": 0.001, "spot_maker": 0.001,
        "futures_taker": 0.0004, "futures_maker": 0.0002,
        "rebate_rate": 0.30, "use_bnb_discount": False,
    },
    "strategy": {
        "whitelist": ["BTCUSDT"],
        "order_priority": "futures_first",
        "entry": {"min_funding_rate": 0.0002, "min_annualized": 0.15, "max_basis_pct": 0.001},
        "exit": {"min_profitable_rate": 0.0005, "min_holding_hours": 24, "max_holding_days": 30},
        "risk": {"max_leverage": 1, "max_slippage": 0.002, "daily_loss_limit_pct": 0.005},
    },
    "allocation": {"tradable_pct": 0.80, "reserve_pct": 0.15, "emergency_pct": 0.05},
    "initial_capital": 10000,
    "tiers": {
        "t3_enabled": False,
        "without_t3": {
            "max_positions": 3,
            "t1": {"position_pct": 0.375, "max_count": 2},
            "t2": {"position_pct": 0.3125, "max_count": 2},
        },
    },
    "schedule": {"check_interval_minutes": 5},
    "telegram": {"enabled": False},
    "split_thresholds": {"default": 5000},
}

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def make_executor():
    """创建 BinanceExecutor，SDK 构造器全部 mock（不发网络请求）"""
    with patch("executor.Spot"), patch("executor.UMFutures"):
        from executor import BinanceExecutor
        exc = BinanceExecutor(EXECUTOR_CONFIG)
    exc._get_precision = MagicMock(return_value={
        "qty_precision": 3, "price_precision": 2,
        "min_qty": 0.001, "step_size": 0.001,
    })
    return exc


def order_full(qty: str, price: float) -> dict:
    return {"executedQty": qty, "fills": [{"qty": qty, "price": str(price)}], "avgPrice": str(price)}


def order_partial(qty: str, ratio: float, price: float) -> dict:
    filled = f"{float(qty) * ratio:.3f}"
    return {"executedQty": filled, "fills": [{"qty": filled, "price": str(price)}], "avgPrice": str(price)}


# ===========================================================================
# 测试组 1 — P0 #1: _check_fill
# ===========================================================================

class TestCheckFill(unittest.TestCase):

    def setUp(self):
        self.exc = make_executor()

    def test_full_fill_passes(self):
        self.exc._check_fill({"executedQty": "1.000"}, "1.000")

    def test_partial_50pct_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.exc._check_fill({"executedQty": "0.500"}, "1.000")
        self.assertIn("部分成交", str(ctx.exception))

    def test_99pct_passes_98pct_fails(self):
        self.exc._check_fill({"executedQty": "0.990"}, "1.000")   # 99% ✓
        with self.assertRaises(ValueError):
            self.exc._check_fill({"executedQty": "0.980"}, "1.000")  # 98% ✗

    def test_zero_qty_is_safe(self):
        self.exc._check_fill({"executedQty": "0.000"}, "0.000")

    def test_cumqty_fallback(self):
        """部分交易所返回 cumQty 而非 executedQty"""
        with self.assertRaises(ValueError):
            self.exc._check_fill({"cumQty": "0.200"}, "1.000")


# ===========================================================================
# 测试组 2 — P0 #1+#2: _open_single futures_first 模式下的成交量 & 滑点守卫
# ===========================================================================

class TestOpenSingleGuards(unittest.TestCase):

    def setUp(self):
        self.exc = make_executor()

    def _open(self, futures_result, spot_result=None):
        self.exc.futures.new_order = MagicMock(return_value=futures_result)
        self.exc.spot.new_order = MagicMock(return_value=spot_result or order_full("0.050", 60000))
        return self.exc._open_single("BTCUSDT", 3000, 60000, "positive", "futures_first")

    # ---- 合约部分成交 ----

    def test_futures_partial_fill_no_spot_placed(self):
        """合约部分成交 → 不下现货单，直接返回失败"""
        result = self._open(futures_result=order_partial("0.050", 0.5, 60000))
        self.assertFalse(result["success"])
        self.assertIn("partial_fill", result["error"])
        self.exc.spot.new_order.assert_not_called()

    # ---- 现货部分成交 ----

    def test_spot_partial_fill_rolls_back_futures(self):
        """现货部分成交 → 回滚已成交的合约单"""
        self.exc._rollback_futures = MagicMock()
        result = self._open(
            futures_result=order_full("0.050", 60000),
            spot_result=order_partial("0.050", 0.5, 60000),
        )
        self.assertFalse(result["success"])
        self.assertIn("partial_fill", result["error"])
        self.exc._rollback_futures.assert_called_once()

    # ---- 滑点超标 ----

    def test_slippage_abort_rolls_back_both(self):
        """滑点 ≈ 0.3% > max_slippage 0.2% → 回滚两腿，返回 slippage_exceeded"""
        self.exc._rollback_spot = MagicMock()
        self.exc._rollback_futures = MagicMock()
        # 现货 60000，合约 60180 → |diff|/ref ≈ 0.30%
        result = self._open(
            futures_result=order_full("0.050", 60180),
            spot_result=order_full("0.050", 60000),
        )
        self.assertFalse(result["success"])
        self.assertIn("slippage_exceeded", result["error"])
        self.exc._rollback_spot.assert_called_once()
        self.exc._rollback_futures.assert_called_once()

    def test_acceptable_slippage_succeeds(self):
        """滑点 ≈ 0.017% < 0.2% → 正常开仓"""
        result = self._open(
            futures_result=order_full("0.050", 60010),
            spot_result=order_full("0.050", 60000),
        )
        self.assertTrue(result["success"])
        self.assertAlmostEqual(result["spot_avg_price"], 60000.0)
        self.assertAlmostEqual(result["futures_avg_price"], 60010.0)


# ===========================================================================
# 测试组 3 — P0 #7: _in_settlement_window
# ===========================================================================

class TestSettlementWindow(unittest.TestCase):
    """验证结算窗口 HH:59:30–HH:00:30 UTC 的边界判断"""

    def _check(self, minute: int, second: int) -> bool:
        fake_now = datetime(2026, 1, 1, 8, minute, second, tzinfo=timezone.utc)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            from main import FundingArbitrageBot
            return FundingArbitrageBot._in_settlement_window()

    def test_59m30s_in_window(self):
        self.assertTrue(self._check(59, 30))

    def test_59m59s_in_window(self):
        self.assertTrue(self._check(59, 59))

    def test_59m29s_not_in_window(self):
        self.assertFalse(self._check(59, 29))

    def test_00m00s_in_window(self):
        self.assertTrue(self._check(0, 0))

    def test_00m30s_in_window(self):
        self.assertTrue(self._check(0, 30))

    def test_00m31s_not_in_window(self):
        self.assertFalse(self._check(0, 31))

    def test_midperiod_not_in_window(self):
        self.assertFalse(self._check(30, 0))


# ===========================================================================
# 测试组 4 — P0 #8: _passes_break_even
# ===========================================================================

class TestPassesBreakEven(unittest.TestCase):
    """
    BNB 折扣关：双程手续费 = amount × (0.001 + 0.0004) × 2 = amount × 0.0028
    回本门槛（×1.5） = amount × 0.0042
    min_holding_hours=24 → 3 次结算（24/8）
    所需费率 ≥ 0.0042 / 3 = 0.0014 / 8h
    """

    def setUp(self):
        from main import FundingArbitrageBot
        self.bot = object.__new__(FundingArbitrageBot)
        self.bot.config = BOT_CONFIG
        self.bot.fees_cfg = BOT_CONFIG["fees"]

    def test_high_rate_passes(self):
        # 0.2%/8h → 3 次 = 0.006 × 3000 = 18 > 0.0042 × 3000 = 12.6 ✓
        self.assertTrue(self.bot._passes_break_even({"rate": 0.002}, 3000))

    def test_low_rate_fails(self):
        # 0.1%/8h → 3 次 = 0.003 × 3000 = 9 < 12.6 ✗
        self.assertFalse(self.bot._passes_break_even({"rate": 0.001}, 3000))

    def test_exact_threshold_passes(self):
        # 0.0014 → 3 次 = 0.0042 × 3000 = 12.6 = 12.6，>= 成立 ✓
        self.assertTrue(self.bot._passes_break_even({"rate": 0.0014}, 3000))

    def test_just_below_threshold_fails(self):
        self.assertFalse(self.bot._passes_break_even({"rate": 0.00139}, 3000))

    def test_negative_rate_uses_abs(self):
        """方向无关，取绝对值后计算（正常不会进来，但防御）"""
        self.assertTrue(self.bot._passes_break_even({"rate": -0.002}, 3000))


# ===========================================================================
# 测试组 5 — P0 #4: should_exit min_holding_hours 保护
# ===========================================================================

class TestShouldExitMinHoldingHours(unittest.TestCase):

    def setUp(self):
        with patch("monitor.ccxt.binance"):
            from monitor import FundingRateMonitor
            self.monitor = FundingRateMonitor(MONITOR_CONFIG)

    def _run(self, rate: float, hours_ago: float) -> tuple[bool, str]:
        opened_at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
        self.monitor.fetch_funding_rate = AsyncMock(return_value={
            "symbol": "BTC/USDT:USDT", "rate": rate,
        })
        return asyncio.run(
            self.monitor.should_exit("BTC/USDT:USDT", "positive", opened_at=opened_at)
        )

    def test_negative_rate_within_24h_stays_open(self):
        """费率转负，但仅开仓 2h（< 24h 保护期）→ 不平仓"""
        should_close, _ = self._run(rate=-0.001, hours_ago=2)
        self.assertFalse(should_close)

    def test_negative_rate_after_24h_closes(self):
        """费率转负，已开仓 25h（> 24h 保护期）→ 平仓"""
        should_close, reason = self._run(rate=-0.001, hours_ago=25)
        self.assertTrue(should_close)
        self.assertIn("转负", reason)

    def test_positive_rate_within_24h_stays_open(self):
        """费率仍正，在保护期内 → 不平仓"""
        should_close, _ = self._run(rate=0.001, hours_ago=2)
        self.assertFalse(should_close)

    def test_no_opened_at_ignores_protection(self):
        """不传 opened_at → 跳过保护期逻辑，费率转负立即平仓"""
        self.monitor.fetch_funding_rate = AsyncMock(
            return_value={"symbol": "BTC/USDT:USDT", "rate": -0.001}
        )
        should_close, _ = asyncio.run(
            self.monitor.should_exit("BTC/USDT:USDT", "positive")
        )
        self.assertTrue(should_close)

    def test_rate_below_min_after_24h_closes(self):
        """费率低于 min_profitable_rate，且过了保护期 → 平仓"""
        should_close, reason = self._run(rate=0.0001, hours_ago=25)  # 低于 0.0005
        self.assertTrue(should_close)
        self.assertIn("过低", reason)


# ===========================================================================
# 测试组 6 — P0 #5: Reconciler
# ===========================================================================

class TestReconciler(unittest.TestCase):

    def _make(self, db_positions, exchange_positions):
        from reconciler import Reconciler
        mock_exc = MagicMock()
        mock_exc.get_futures_positions.return_value = exchange_positions
        mock_pm = MagicMock()
        mock_pm.get_open_positions.return_value = db_positions
        return Reconciler(mock_exc, mock_pm, notifier=None)

    def test_matching_positions_is_clean(self):
        db = [{"symbol": "BTCUSDT", "quantity": 0.05, "direction": "positive"}]
        ex = [{"symbol": "BTCUSDT", "positionAmt": "-0.05"}]
        rec = self._make(db, ex)
        self.assertTrue(asyncio.run(rec.check()))
        self.assertTrue(rec.is_clean)

    def test_quantity_discrepancy_flags_dirty(self):
        db = [{"symbol": "BTCUSDT", "quantity": 0.05, "direction": "positive"}]
        ex = [{"symbol": "BTCUSDT", "positionAmt": "-0.02"}]  # 60% 偏差
        rec = self._make(db, ex)
        self.assertFalse(asyncio.run(rec.check()))
        self.assertFalse(rec.is_clean)

    def test_orphan_futures_flags_dirty(self):
        """交易所有持仓，DB 无记录 → 孤儿仓位"""
        rec = self._make(db_positions=[], exchange_positions=[
            {"symbol": "SOLUSDT", "positionAmt": "-10.0"}
        ])
        self.assertFalse(asyncio.run(rec.check()))
        self.assertFalse(rec.is_clean)

    def test_clean_after_dirty_restores(self):
        """先脏后净 → is_clean 恢复为 True"""
        db = [{"symbol": "BTCUSDT", "quantity": 0.05, "direction": "positive"}]
        rec = self._make(db, [{"symbol": "BTCUSDT", "positionAmt": "-0.01"}])
        asyncio.run(rec.check())
        self.assertFalse(rec.is_clean)

        rec.executor.get_futures_positions.return_value = [
            {"symbol": "BTCUSDT", "positionAmt": "-0.05"}
        ]
        asyncio.run(rec.check())
        self.assertTrue(rec.is_clean)

    def test_exchange_query_failure_does_not_block(self):
        """对账查询失败时不阻塞交易，保持 is_clean=True"""
        rec = self._make(db_positions=[], exchange_positions=[])
        rec.executor.get_futures_positions.side_effect = Exception("网络超时")
        self.assertTrue(asyncio.run(rec.check()))
        self.assertTrue(rec.is_clean)


# ===========================================================================
# 测试组 7 — A2: 胖手指单笔硬上限
# ===========================================================================

class TestFatFinger(unittest.TestCase):

    def test_over_limit_returns_failure(self):
        """金额超过 max_single_order_usdt → 返回失败，不下单"""
        exc = make_executor()
        exc.max_single_order_usdt = 1000
        result = exc.open_arbitrage("BTCUSDT", 2000, 60000)
        self.assertFalse(result["success"])
        self.assertIn("fat_finger", result["error"])
        exc.spot.new_order.assert_not_called()
        exc.futures.new_order.assert_not_called()

    def test_zero_limit_disables_check(self):
        """max_single_order_usdt=0 表示禁用，不拦截"""
        exc = make_executor()
        exc.max_single_order_usdt = 0
        exc.spot.new_order = MagicMock(return_value=order_full("0.050", 60000.0))
        exc.futures.new_order = MagicMock(return_value=order_full("0.050", 60010.0))
        result = exc.open_arbitrage("BTCUSDT", 3000, 60000)
        self.assertTrue(result["success"])

    def test_at_exact_limit_passes(self):
        """金额等于上限，允许通过"""
        exc = make_executor()
        exc.max_single_order_usdt = 3000
        exc.spot.new_order = MagicMock(return_value=order_full("0.050", 60000.0))
        exc.futures.new_order = MagicMock(return_value=order_full("0.050", 60010.0))
        result = exc.open_arbitrage("BTCUSDT", 3000, 60000)
        self.assertTrue(result["success"])


# ===========================================================================
# 测试组 8 — A3: _close_tail 平仓尾单
# ===========================================================================

class TestCloseTail(unittest.TestCase):

    def setUp(self):
        self.exc = make_executor()

    def test_full_fill_no_tail_order(self):
        """成交量 ≥ 99% 不补单"""
        self.exc.spot.new_order = MagicMock()
        self.exc._close_tail("BTCUSDT", "0.050", {"executedQty": "0.050"}, "positive", "spot")
        self.exc.spot.new_order.assert_not_called()

    def test_near_full_fill_no_tail_order(self):
        """成交量 = 99.0% 临界值，不补单"""
        self.exc.spot.new_order = MagicMock()
        self.exc._close_tail("BTCUSDT", "0.100", {"executedQty": "0.099"}, "positive", "spot")
        self.exc.spot.new_order.assert_not_called()

    def test_partial_fill_spot_tail_sent(self):
        """现货部分成交 → 补发卖单尾单"""
        self.exc.spot.new_order = MagicMock(return_value={"executedQty": "0.025"})
        self.exc._close_tail("BTCUSDT", "0.050", {"executedQty": "0.025"}, "positive", "spot")
        self.exc.spot.new_order.assert_called_once()
        call_kwargs = self.exc.spot.new_order.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "SELL")
        self.assertEqual(call_kwargs["type"], "MARKET")

    def test_partial_fill_futures_tail_sent(self):
        """合约部分成交 → 补发 reduceOnly 买单尾单"""
        self.exc.futures.new_order = MagicMock(return_value={"executedQty": "0.025"})
        self.exc._close_tail("BTCUSDT", "0.050", {"executedQty": "0.025"}, "positive", "futures")
        self.exc.futures.new_order.assert_called_once()
        call_kwargs = self.exc.futures.new_order.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "BUY")
        self.assertTrue(call_kwargs.get("reduceOnly"))

    def test_tail_failure_appended_to_critical_errors(self):
        """尾单补发失败 → 追加到 _critical_errors"""
        self.exc.spot.new_order = MagicMock(side_effect=Exception("网络超时"))
        self.exc._close_tail("BTCUSDT", "0.050", {"executedQty": "0.025"}, "positive", "spot")
        self.assertGreater(len(self.exc._critical_errors), 0)
        self.assertIn("尾单失败", self.exc._critical_errors[0])


# ===========================================================================
# 测试组 9 — Review P1: partial 平仓不记账 & actual_usdt 手续费
# ===========================================================================

class TestPartialCloseSkipsDb(unittest.IsolatedAsyncioTestCase):
    """partial=True 时不得 record_close，避免账实错配"""

    async def test_close_position_skips_db_when_partial(self):
        from capital import resolve
        from main import FundingArbitrageBot

        bot = FundingArbitrageBot(BOT_CONFIG, resolve(BOT_CONFIG, capital_override=10000), mode="simulate")
        bot.executor.close_arbitrage = MagicMock(return_value={
            "success": True,
            "partial": True,
            "spot_avg_price": 100.0,
            "futures_avg_price": 100.0,
        })
        bot.monitor.fetch_basis = AsyncMock(return_value={
            "basis_pct": 0.0001,
            "perp_price": 100.0,
        })
        bot.positions.record_close = MagicMock()
        bot.positions.record_trade = MagicMock()
        pos = {
            "id": 1,
            "symbol": "BTCUSDT",
            "quantity": 0.1,
            "direction": "positive",
            "spot_price": 99.0,
            "futures_price": 101.0,
            "usdt_amount": 10.0,
            "fees_paid": 0.02,
            "funding_earned": 0.5,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        ok = await bot._close_position(pos, "test")
        self.assertFalse(ok)
        bot.positions.record_close.assert_not_called()


class TestOpenUsesActualUsdtFees(unittest.IsolatedAsyncioTestCase):
    """开仓成交明细手续费应与 actual_usdt 一致，而非预算 alloc"""

    async def test_record_trade_fees_follow_actual_usdt(self):
        from capital import resolve
        from main import FundingArbitrageBot

        bot = FundingArbitrageBot(BOT_CONFIG, resolve(BOT_CONFIG, capital_override=10000), mode="simulate")
        bot._in_settlement_window = MagicMock(return_value=False)
        bot.positions.get_open_positions = MagicMock(return_value=[])
        bot.positions.get_daily_pnl = MagicMock(return_value=0)
        bot.screener.screen = AsyncMock(return_value=[{
            "binance_symbol": "BTCUSDT",
            "tier": 1,
            "tier_name": "T1 核心",
            "direction": "positive",
            "rate": 0.001,
            "annualized": 0.1,
            "mid_price": 60000.0,
            "score": 90.0,
        }])
        bot.screener.check_allocation = MagicMock(return_value=(True, 3000.0, "通过"))
        bot.monitor.fetch_basis = AsyncMock(return_value={
            "basis_pct": 0.0001,
            "perp_price": 60000.0,
        })
        qty = 0.04838709677419355
        spot_px = 60000.0
        bot.executor.open_arbitrage = MagicMock(return_value={
            "success": True,
            "quantity": qty,
            "spot_avg_price": spot_px,
            "futures_avg_price": 59990.0,
            "slippage": 0.0001,
            "spot": {"executedQty": "0.048"},
            "futures": {"executedQty": "0.048"},
        })
        bot.executor.set_leverage = MagicMock()
        bot.positions.record_open = MagicMock(return_value=1)
        bot.positions.record_trade = MagicMock()

        await bot.task_scan_and_open()

        self.assertTrue(bot.positions.record_trade.called)
        calls = bot.positions.record_trade.call_args_list
        self.assertEqual(len(calls), 2)
        actual_usdt = qty * spot_px
        expected_spot_fee = actual_usdt * BOT_CONFIG["fees"]["spot_taker"]
        expected_futures_fee = actual_usdt * BOT_CONFIG["fees"]["futures_taker"]
        fee_spot = calls[0][0][7]
        fee_futures = calls[1][0][7]
        self.assertAlmostEqual(fee_spot, expected_spot_fee, places=6)
        self.assertAlmostEqual(fee_futures, expected_futures_fee, places=6)


class TestExecutorPartialChunkClose(unittest.TestCase):
    """分批平仓仅完成部分批次时返回 quantity 并写入 critical_errors"""

    def test_partial_close_returns_quantity_and_alerts(self):
        exc = make_executor()
        exc.split_thresholds = {"default": 1000.0}
        n_calls = [0]

        def close_single(sym, q, direction, current_price):
            n_calls[0] += 1
            if n_calls[0] == 1:
                return {
                    "success": True,
                    "spot_avg_price": 100.0,
                    "futures_avg_price": 100.0,
                    "spot": {},
                    "futures": {},
                }
            return {"success": False, "error": "simulated_batch_fail"}

        exc._close_single = MagicMock(side_effect=close_single)
        exc._round_qty = MagicMock(side_effect=lambda q, s: f"{float(q):.5f}")

        result = exc.close_arbitrage(
            "BTCUSDT", 0.05, "positive",
            current_price=None,
            usdt_amount=3000.0,
        )
        self.assertTrue(result["success"])
        self.assertTrue(result.get("partial"))
        self.assertIn("quantity", result)
        self.assertGreater(result["quantity"], 0)
        self.assertTrue(any("部分平仓" in e for e in exc._critical_errors))


# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
