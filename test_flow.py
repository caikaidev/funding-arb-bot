"""
生命周期集成测试 (B 组：注入假仓位，验证 task 完整流程)

解决的测试盲区：当前市场持续负费率 → screener 筛不出候选 →
simulate 模式下无法自然走通 open→funding→close 流程。

策略：直接向临时 SQLite 注入仓位 + mock monitor/screener，
      用真实 PositionManager + SimulatedExecutor 驱动 bot 任务，
      对比执行前后 DB 状态做断言。

覆盖场景:
  S1  费率结算正确写入 DB
  S2  min_holding_hours 保护期：新仓即使费率转负也不平仓
  S3  保护期后费率转负：触发平仓，position.status → closed
  S4  日亏损上限：亏损超限后 task_scan_and_open 不调用 executor
  S5  对账器孤儿仓位：reconciler.is_clean=False → 开仓被拦截

用法:
  python test_flow.py
  python -m pytest test_flow.py -v
"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from capital import resolve
from position import PositionManager

# ---------------------------------------------------------------------------
# 共用测试配置（与 test_p0.py 保持一致）
# ---------------------------------------------------------------------------

TEST_CONFIG = {
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
        "exit": {
            "min_profitable_rate": 0.0005, "min_holding_hours": 24,
            "max_holding_days": 30, "rate_reverse_count": 3,
        },
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

# 一个满足回本门槛的高费率候选（0.2%/8h）
_MOCK_COIN = {
    "symbol": "BTC/USDT:USDT",
    "binance_symbol": "BTCUSDT",
    "tier": 1,
    "tier_name": "T1 核心 (BTC/ETH)",
    "rate": 0.002,
    "abs_rate": 0.002,
    "annualized": 0.219,
    "mid_price": 60000.0,
    "direction": "positive",
    "max_position": 3000.0,
    "score": 75.0,
}

# ---------------------------------------------------------------------------
# 辅助：构造 bot 实例（绕过 __init__，手动装配依赖）
# ---------------------------------------------------------------------------

def make_bot(db_path: str) -> "FundingArbitrageBot":  # noqa: F821
    """
    用 object.__new__ 绕过构造器，按需注入 mock 依赖。
    使用真实 PositionManager（写真实 SQLite）和 SimulatedExecutor（无网络依赖）。
    """
    from main import FundingArbitrageBot
    from sim_executor import SimulatedExecutor

    bot = object.__new__(FundingArbitrageBot)
    bot.config = TEST_CONFIG
    bot.plan = resolve(TEST_CONFIG)
    bot.mode = "simulate"
    bot.fees_cfg = TEST_CONFIG["fees"]
    bot.risk = TEST_CONFIG["strategy"]["risk"]
    bot.running = True
    bot.notifier = None
    bot.reconciler = None
    bot.positions = PositionManager(db_path)
    bot.executor = SimulatedExecutor(TEST_CONFIG)

    # Mock monitor — 默认费率正常，按需在各 test 中覆盖
    monitor = MagicMock()
    monitor.fetch_funding_rate = AsyncMock(return_value={
        "symbol": "BTC/USDT:USDT", "rate": 0.001,
    })
    monitor.should_exit = AsyncMock(return_value=(False, ""))
    monitor.fetch_basis = AsyncMock(return_value={
        "basis_pct": 0.0001, "spot_price": 60000.0, "perp_price": 60006.0,
    })
    monitor.get_ticker = AsyncMock(return_value={"quoteVolume": 1e9, "last": 60000.0})
    bot.monitor = monitor

    # Mock screener — 返回一个满足条件的高费率标的
    screener = MagicMock()
    screener.screen = AsyncMock(return_value=[_MOCK_COIN])
    screener.check_allocation = MagicMock(return_value=(True, 3000.0, "通过"))
    bot.screener = screener

    return bot


def seed_position(pm: PositionManager, hours_ago: float = 0.5) -> int:
    """注入一条测试仓位，opened_at 调整为 hours_ago 小时前"""
    pos_id = pm.record_open(
        symbol="BTCUSDT",
        direction="positive",
        quantity=0.05,
        spot_price=60000.0,
        futures_price=60010.0,
        usdt_amount=3000.0,
        slippage=0.000167,
        fees_paid=4.2,
        open_basis=0.000167,
    )
    opened_at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    pm.conn.execute("UPDATE positions SET opened_at = ? WHERE id = ?", (opened_at, pos_id))
    pm.conn.commit()
    return pos_id


# ---------------------------------------------------------------------------
# 基类：每个测试用临时 DB + 结算窗口 patch
# ---------------------------------------------------------------------------

class BotTestCase(unittest.TestCase):
    """提供临时 DB 和结算窗口绕过的基类"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        # 结算窗口守卫必须 patch，否则测试可能偶发在 HH:59:30–HH:00:30 失败
        from main import FundingArbitrageBot
        self._win_patcher = patch.object(
            FundingArbitrageBot, "_in_settlement_window", return_value=False
        )
        self._win_patcher.start()

    def tearDown(self):
        self._win_patcher.stop()
        os.unlink(self.db_path)


# ===========================================================================
# 场景 1 — 费率结算写入 DB
# ===========================================================================

class TestFundingSettlement(BotTestCase):

    def test_positive_rate_credited(self):
        """正费率 0.1%/8h → funding_earned += usdt_amount × rate"""
        bot = make_bot(self.db_path)
        seed_position(bot.positions)
        bot.monitor.fetch_funding_rate = AsyncMock(return_value={
            "symbol": "BTC/USDT:USDT", "rate": 0.001,
        })

        asyncio.run(bot.task_record_funding())

        pos = bot.positions.get_open_positions()[0]
        self.assertAlmostEqual(pos["funding_earned"], 3000.0 * 0.001, places=4)

    def test_negative_rate_deducted(self):
        """负费率（持仓 positive）→ payment 为负，funding_earned 减少"""
        bot = make_bot(self.db_path)
        seed_position(bot.positions)
        bot.monitor.fetch_funding_rate = AsyncMock(return_value={
            "symbol": "BTC/USDT:USDT", "rate": -0.001,
        })

        asyncio.run(bot.task_record_funding())

        pos = bot.positions.get_open_positions()[0]
        self.assertAlmostEqual(pos["funding_earned"], 3000.0 * (-0.001), places=4)

    def test_funding_log_record_created(self):
        """结算后 funding_logs 表有对应记录"""
        bot = make_bot(self.db_path)
        seed_position(bot.positions)

        asyncio.run(bot.task_record_funding())

        row = bot.positions.conn.execute(
            "SELECT COUNT(*) FROM funding_logs"
        ).fetchone()[0]
        self.assertEqual(row, 1)


# ===========================================================================
# 场景 2 — min_holding_hours 保护期：新仓不因费率波动平仓
# ===========================================================================

class TestMinHoldingHoursProtection(BotTestCase):

    def test_new_position_not_closed_on_rate_reversal(self):
        """仓位仅开 2h（< 24h 保护期），费率转负 → 不平仓"""
        bot = make_bot(self.db_path)
        seed_position(bot.positions, hours_ago=2)

        # monitor.should_exit 真实行为通过 opened_at 判断
        # 此处 mock 返回保护期内的"不平仓"结果
        bot.monitor.should_exit = AsyncMock(return_value=(False, ""))

        asyncio.run(bot.task_check_and_close())

        self.assertEqual(len(bot.positions.get_open_positions()), 1)

    def test_position_open_status_unchanged(self):
        """保护期内，position.status 保持 open"""
        bot = make_bot(self.db_path)
        pos_id = seed_position(bot.positions, hours_ago=1)
        bot.monitor.should_exit = AsyncMock(return_value=(False, ""))

        asyncio.run(bot.task_check_and_close())

        pos = bot.positions.get_position(pos_id)
        self.assertEqual(pos["status"], "open")


# ===========================================================================
# 场景 3 — 保护期后费率转负：触发平仓
# ===========================================================================

class TestCloseAfterProtection(BotTestCase):

    def test_old_position_closed_on_rate_reversal(self):
        """仓位已开 25h（> 24h 保护期），费率转负 → position.status = closed"""
        bot = make_bot(self.db_path)
        seed_position(bot.positions, hours_ago=25)
        bot.monitor.should_exit = AsyncMock(return_value=(True, "费率转负 (-0.1000%)"))

        asyncio.run(bot.task_check_and_close())

        self.assertEqual(len(bot.positions.get_open_positions()), 0)

    def test_closed_position_has_close_pnl(self):
        """平仓后 close_pnl 字段不为 NULL"""
        bot = make_bot(self.db_path)
        pos_id = seed_position(bot.positions, hours_ago=25)
        bot.monitor.should_exit = AsyncMock(return_value=(True, "费率转负"))

        asyncio.run(bot.task_check_and_close())

        pos = bot.positions.get_position(pos_id)
        self.assertEqual(pos["status"], "closed")
        self.assertIsNotNone(pos["close_pnl"])


# ===========================================================================
# 场景 4 — 日亏损上限：亏损超限后不开新仓
# ===========================================================================

class TestDailyLossLimit(BotTestCase):

    def test_loss_over_limit_blocks_open(self):
        """今日亏损 -100 USDT > 日限 50 USDT → executor.open_arbitrage 不被调用"""
        bot = make_bot(self.db_path)
        # 注入今日亏损记录（直接通过 record_funding 写 funding_logs）
        pos_id = seed_position(bot.positions)
        bot.positions.record_funding(pos_id, -0.03, -100.0)  # payment = -100 USDT

        # 用 spy 代替原 SimulatedExecutor.open_arbitrage
        bot.executor.open_arbitrage = MagicMock(
            return_value={"success": True, "spot_avg_price": 60000, "futures_avg_price": 60010,
                          "quantity": 0.05, "slippage": 0.0002}
        )

        asyncio.run(bot.task_scan_and_open())

        bot.executor.open_arbitrage.assert_not_called()

    def test_within_limit_allows_open(self):
        """今日无亏损 → 正常扫描，open_arbitrage 被调用"""
        bot = make_bot(self.db_path)
        bot.executor.open_arbitrage = MagicMock(
            return_value={"success": True, "spot_avg_price": 60000, "futures_avg_price": 60010,
                          "quantity": 0.05, "slippage": 0.0002}
        )

        asyncio.run(bot.task_scan_and_open())

        bot.executor.open_arbitrage.assert_called_once()


# ===========================================================================
# 场景 5 — 对账器孤儿仓位 → 开仓被拦截
# ===========================================================================

class TestReconcilerBlocksOpen(BotTestCase):

    def test_orphan_position_prevents_open(self):
        """交易所有孤儿合约仓位 → reconciler.is_clean=False → 不开新仓"""
        from reconciler import Reconciler

        bot = make_bot(self.db_path)
        mock_exc = MagicMock()
        mock_exc.get_futures_positions.return_value = [
            {"symbol": "SOLUSDT", "positionAmt": "-10.0"}  # DB 中无此仓位
        ]
        bot.reconciler = Reconciler(mock_exc, bot.positions, notifier=None)

        # 先运行对账
        asyncio.run(bot.reconciler.check())
        self.assertFalse(bot.reconciler.is_clean)

        # 再运行开仓扫描
        bot.executor.open_arbitrage = MagicMock(
            return_value={"success": True, "spot_avg_price": 60000, "futures_avg_price": 60010,
                          "quantity": 0.05, "slippage": 0.0002}
        )
        asyncio.run(bot.task_scan_and_open())

        bot.executor.open_arbitrage.assert_not_called()

    def test_reconciler_clean_allows_open(self):
        """账实相符 → is_clean=True → 正常开仓"""
        from reconciler import Reconciler

        bot = make_bot(self.db_path)
        mock_exc = MagicMock()
        mock_exc.get_futures_positions.return_value = []  # 空，与 DB 一致
        bot.reconciler = Reconciler(mock_exc, bot.positions, notifier=None)

        asyncio.run(bot.reconciler.check())
        self.assertTrue(bot.reconciler.is_clean)

        bot.executor.open_arbitrage = MagicMock(
            return_value={"success": True, "spot_avg_price": 60000, "futures_avg_price": 60010,
                          "quantity": 0.05, "slippage": 0.0002}
        )
        asyncio.run(bot.task_scan_and_open())

        bot.executor.open_arbitrage.assert_called_once()


# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
