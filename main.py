"""
资金费率套利机器人 v5

三种模式:
  python main.py --monitor  --capital 10000    # 纯看：扫描打印，不记录
  python main.py --simulate --capital 10000    # 模拟：走完整流程，虚拟建仓，记录到 DB
  python main.py --capital 10000               # 实盘：真金白银

模拟模式特点:
  ✅ 每 5 分钟扫描真实费率
  ✅ 按策略规则决定开仓/平仓
  ✅ 用实时价格+模拟滑点计算成交价
  ✅ 虚拟仓位写入 SQLite
  ✅ 每 8 小时按真实费率结算收入
  ✅ 一周后用 results.py 查看完整损益报告
  ❌ 不发送任何真实订单
"""
import argparse
import asyncio
import yaml
import signal
from datetime import datetime, timezone
from loguru import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from capital import resolve, print_plan, CapitalPlan
from screener import DynamicScreener, classify, _to_ccxt
from monitor import FundingRateMonitor
from position import PositionManager
from notifier import TelegramNotifier

logger.add(
    "logs/bot_{time:YYYY-MM-DD}.log",
    rotation="1 day", retention="30 days", level="INFO",
    format="{time:HH:mm:ss} | {level:<7} | {message}",
)


def parse_args():
    p = argparse.ArgumentParser(description="资金费率套利机器人")
    p.add_argument("--capital", type=float, default=None, help="初始资金 (USDT)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--monitor", action="store_true", help="纯监控（只看不记录）")
    g.add_argument("--simulate", action="store_true", help="模拟交易（虚拟建仓，记录到DB）")
    p.add_argument("--t3", action="store_true", help="启用 T3")
    p.add_argument("--no-t3", action="store_true", help="关闭 T3")
    p.add_argument("--config", type=str, default="config.yaml")
    return p.parse_args()


class FundingArbitrageBot:

    def __init__(self, config: dict, plan: CapitalPlan, mode: str = "live"):
        """mode: 'monitor' | 'simulate' | 'live'"""
        self.config = config
        self.plan = plan
        self.mode = mode

        self.screener = DynamicScreener(config, plan)
        self.monitor = FundingRateMonitor(config)
        self.fees_cfg = config["fees"]
        self.risk = config["strategy"]["risk"]
        self.running = True

        # 模拟和实盘都写 DB，但用不同的数据库文件
        if mode == "simulate":
            self.positions = PositionManager(db_path="simulate.db")
            from sim_executor import SimulatedExecutor
            self.executor = SimulatedExecutor(config)
        elif mode == "live":
            self.positions = PositionManager(db_path="arbitrage.db")
            from executor import BinanceExecutor
            self.executor = BinanceExecutor(config)
        else:
            self.positions = None
            self.executor = None

        # Telegram: 实盘开，模拟可选，监控关
        tg_enabled = config.get("telegram", {}).get("enabled", False)
        if mode == "live" and tg_enabled:
            self.notifier = TelegramNotifier(config)
        elif mode == "simulate" and tg_enabled:
            self.notifier = TelegramNotifier(config)
        else:
            self.notifier = None

    # ==================================================================
    # 监控模式
    # ==================================================================
    async def task_monitor_scan(self):
        try:
            qualified = await self.screener.screen()
            if not qualified:
                logger.info("未发现合格标的")
                return

            mock_pos = []
            used = 0

            logger.info(f"\n{'='*70}")
            logger.info(f" 模拟分配 (可用 ${self.plan.tradable:,.0f})")
            logger.info(f"{'='*70}")

            for coin in qualified:
                ok, alloc, _ = self.screener.check_allocation(coin, mock_pos)
                if not ok:
                    continue
                alloc = min(alloc, self.plan.tradable - used)
                if alloc < 200:
                    continue
                daily = alloc * abs(coin["rate"]) * 3
                logger.info(
                    f" [{coin['tier_name'][:5]}] {coin['binance_symbol']:>10} | "
                    f"费率 {coin['rate']:+.4%} | 年化 {coin['annualized']:>5.1%} | "
                    f"仓 ${alloc:>6,.0f} | 日收 ${daily:.2f}"
                )
                mock_pos.append({"symbol": coin["binance_symbol"], "usdt_amount": alloc})
                used += alloc

            logger.info(f"{'='*70}")

        except Exception as e:
            logger.exception(f"监控异常: {e}")

    # ==================================================================
    # 模拟 / 实盘：扫描→开仓
    # ==================================================================
    async def task_scan_and_open(self):
        try:
            qualified = await self.screener.screen()
            if not qualified:
                return

            open_pos = self.positions.get_open_positions()
            used = sum(p["usdt_amount"] for p in open_pos)
            available = self.plan.tradable - used

            if available < 500:
                logger.info(f"可用不足 ${available:.0f}")
                return

            for coin in qualified:
                ok, alloc, reason = self.screener.check_allocation(coin, open_pos)
                if not ok:
                    continue
                alloc = min(alloc, available)
                if alloc < 200:
                    continue

                self.executor.set_leverage(coin["binance_symbol"], self.risk["max_leverage"])

                result = self.executor.open_arbitrage(
                    symbol=coin["binance_symbol"],
                    usdt_amount=alloc,
                    current_price=coin["mid_price"],
                    direction=coin["direction"],
                )

                if result["success"]:
                    fees = self._calc_fees(alloc)
                    self.positions.record_open(
                        symbol=coin["binance_symbol"],
                        direction=coin["direction"],
                        quantity=result["quantity"],
                        spot_price=result["spot_avg_price"],
                        futures_price=result["futures_avg_price"],
                        usdt_amount=alloc,
                        slippage=result["slippage"],
                        fees_paid=fees,
                    )
                    if self.notifier:
                        await self.notifier.on_open(
                            coin["binance_symbol"], coin["direction"],
                            alloc, result["slippage"], coin["rate"],
                        )
                    open_pos = self.positions.get_open_positions()
                    available -= alloc

                    tag = "[模拟]" if self.mode == "simulate" else "[实盘]"
                    logger.info(
                        f"{tag} 开仓 [{coin['tier_name']}] "
                        f"{coin['binance_symbol']} ${alloc:.0f}"
                    )

        except Exception as e:
            logger.exception(f"扫描异常: {e}")

    # ==================================================================
    # 模拟 / 实盘：持仓检查→平仓
    # ==================================================================
    async def task_check_and_close(self):
        try:
            for pos in self.positions.get_open_positions():
                symbol = pos["symbol"]
                ccxt_sym = _to_ccxt(symbol)
                tier = classify(ccxt_sym)

                should_close, reason = await self.monitor.should_exit(
                    ccxt_sym, pos["direction"]
                )

                if not should_close:
                    age = (datetime.now(timezone.utc) -
                           datetime.fromisoformat(pos["opened_at"])).days
                    if age >= self.config["strategy"]["exit"]["max_holding_days"]:
                        should_close, reason = True, f"持仓 {age} 天"

                if not should_close and tier == 3:
                    ticker = await self.monitor.get_ticker(ccxt_sym)
                    if ticker and float(ticker.get("quoteVolume", 0) or 0) < 10e6:
                        should_close, reason = True, "T3 流动性不足"

                if not should_close and tier == 2:
                    ticker = await self.monitor.get_ticker(ccxt_sym)
                    if ticker and float(ticker.get("quoteVolume", 0) or 0) < 50e6:
                        should_close, reason = True, "T2 流动性下降"

                if should_close:
                    result = self.executor.close_arbitrage(
                        symbol, pos["quantity"], pos["direction"]
                    )
                    if result["success"]:
                        fees = self._calc_fees(pos["usdt_amount"])
                        rebate = (pos["fees_paid"] + fees) * self.fees_cfg["rebate_rate"]
                        self.positions.record_close(pos["id"], 0, fees, rebate)
                        tag = "[模拟]" if self.mode == "simulate" else "[实盘]"
                        logger.info(f"{tag} 平仓 {symbol}: {reason}")

        except Exception as e:
            logger.exception(f"持仓检查异常: {e}")

    # ==================================================================
    # 费率结算（模拟和实盘共用）
    # ==================================================================
    async def task_record_funding(self):
        try:
            for pos in self.positions.get_open_positions():
                data = await self.monitor.fetch_funding_rate(_to_ccxt(pos["symbol"]))
                if not data:
                    continue
                rate = data["rate"]
                payment = pos["usdt_amount"] * (rate if pos["direction"] == "positive" else -rate)
                self.positions.record_funding(pos["id"], rate, payment)

                tag = "[模拟]" if self.mode == "simulate" else "[实盘]"
                logger.info(
                    f"{tag} 费率到账 {pos['symbol']} | "
                    f"{rate:+.4%} | ${payment:+.4f}"
                )
        except Exception as e:
            logger.exception(f"费率记录异常: {e}")

    # ==================================================================
    async def task_daily_report(self):
        try:
            summary = self.positions.get_summary()
            tag = "模拟" if self.mode == "simulate" else "实盘"
            logger.info(
                f"[{tag}日报] "
                f"仓位:{summary.get('open_trades',0)} | "
                f"费率收入:${summary.get('total_funding',0):+.2f} | "
                f"手续费:${summary.get('total_fees',0):.2f} | "
                f"返佣:${summary.get('total_rebate',0):.2f} | "
                f"净盈亏:${summary.get('total_net_pnl',0):+.2f}"
            )
            if self.notifier:
                await self.notifier.on_daily_report(summary)
        except Exception as e:
            logger.exception(f"日报异常: {e}")

    def _calc_fees(self, amount):
        return amount * (self.fees_cfg["spot_taker"] + self.fees_cfg["futures_taker"])

    # ==================================================================
    async def run(self):
        sched = AsyncIOScheduler()
        m = self.config["schedule"]["check_interval_minutes"]

        if self.mode == "monitor":
            sched.add_job(self.task_monitor_scan, "interval", minutes=m, id="monitor")
        else:
            sched.add_job(self.task_scan_and_open, "interval", minutes=m, id="scan")
            sched.add_job(self.task_check_and_close, "interval", minutes=m * 2, id="check")
            sched.add_job(self.task_record_funding, "cron", hour="0,8,16", minute=5, id="funding")
            sched.add_job(self.task_daily_report, "cron", hour=0, minute=30, id="report")

        sched.start()

        mode_labels = {
            "monitor": "📡 纯监控",
            "simulate": "🧪 模拟交易（不下真单，记录到 simulate.db）",
            "live": "💰 实盘交易",
        }
        tier_str = "T1+T2+T3" if self.plan.t3_enabled else "T1+T2"

        print_plan(self.plan)
        logger.info(f"  运行模式:   {mode_labels[self.mode]}")
        logger.info(f"  等级:       {tier_str}")
        logger.info(f"  扫描间隔:   {m} 分钟")
        if self.mode == "simulate":
            logger.info(f"  数据文件:   simulate.db")
            logger.info(f"  查看结果:   python results.py")
        logger.info("=" * 55)

        if self.notifier:
            await self.notifier.on_start(self.plan.initial, [f"{self.mode} | {tier_str}"])

        # 启动后立即执行一次
        if self.mode == "monitor":
            await self.task_monitor_scan()
        else:
            await self.task_scan_and_open()

        try:
            while self.running:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            sched.shutdown()
            await self.screener.close()
            await self.monitor.close()
            if self.positions:
                self.positions.close_db()
            if self.notifier:
                await self.notifier.on_stop()
            logger.info("已安全退出")


def main():
    import os
    os.makedirs("logs", exist_ok=True)

    args = parse_args()
    with open(args.config) as f:
        config = yaml.safe_load(f)

    if args.t3:
        config["tiers"]["t3_enabled"] = True
    elif args.no_t3:
        config["tiers"]["t3_enabled"] = False

    plan = resolve(config, capital_override=args.capital)

    if args.monitor:
        mode = "monitor"
    elif args.simulate:
        mode = "simulate"
    else:
        mode = "live"

    if mode == "live":
        print_plan(plan)
        confirm = input("\n  ⚠️  实盘模式，确认? [Y/n] ").strip().lower()
        if confirm and confirm != "y":
            print("  已取消")
            return

    bot = FundingArbitrageBot(config, plan, mode=mode)
    loop = asyncio.new_event_loop()

    def shutdown(sig, frame):
        bot.running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    loop.run_until_complete(bot.run())


if __name__ == "__main__":
    main()
