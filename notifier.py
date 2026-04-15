"""
Telegram 通知模块 — 手机实时接收开仓/平仓/异常告警
"""
import aiohttp
from loguru import logger


class TelegramNotifier:

    def __init__(self, config: dict):
        tg = config.get("telegram", {})
        self.enabled = tg.get("enabled", False)
        self.bot_token = tg.get("bot_token", "")
        self.chat_id = tg.get("chat_id", "")
        self.url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    async def send(self, text: str, urgent: bool = False):
        if not self.enabled:
            return
        prefix = "🚨 " if urgent else "📊 "
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    self.url,
                    json={
                        "chat_id": self.chat_id,
                        "text": prefix + text,
                        "parse_mode": "Markdown",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                )
        except Exception as e:
            logger.warning(f"Telegram 发送失败: {e}")

    # ---- 预定义消息模板 ----

    async def on_start(self, capital: float, symbols: list):
        await self.send(
            f"*机器人已启动*\n"
            f"总资金: `${capital:,.0f}`\n"
            f"监控币种: `{', '.join(symbols)}`\n"
            f"模式: 稳健 (1x 杠杆)"
        )

    async def on_opportunity(self, symbol, rate, annualized):
        await self.send(
            f"*发现套利机会*\n"
            f"币种: `{symbol}`\n"
            f"费率: `{rate:.4%}` / 8h\n"
            f"年化: `{annualized:.1%}`"
        )

    async def on_open(self, symbol, direction, amount, slippage, rate):
        d = "现货买+合约空" if direction == "positive" else "现货卖+合约多"
        await self.send(
            f"*开仓成功* ✅\n"
            f"币种: `{symbol}`\n"
            f"方向: {d}\n"
            f"金额: `${amount:,.2f}`\n"
            f"滑点: `{slippage:.4%}`\n"
            f"当前费率: `{rate:.4%}`"
        )

    async def on_close(self, symbol, funding_earned, fees, rebate, net_pnl, reason):
        await self.send(
            f"*平仓完成* 🔒\n"
            f"币种: `{symbol}`\n"
            f"费率收入: `${funding_earned:+.4f}`\n"
            f"手续费: `${fees:.4f}`\n"
            f"返佣: `+${rebate:.4f}`\n"
            f"净盈亏: `${net_pnl:+.4f}`\n"
            f"原因: {reason}"
        )

    async def on_funding(self, symbol, rate, payment, total_earned):
        await self.send(
            f"*费率到账* 💰\n"
            f"币种: `{symbol}`\n"
            f"费率: `{rate:.4%}`\n"
            f"本次: `${payment:+.4f}`\n"
            f"累计: `${total_earned:+.4f}`"
        )

    async def on_error(self, message):
        await self.send(f"*异常告警*\n{message}", urgent=True)

    async def on_daily_report(self, summary: dict):
        await self.send(
            f"*每日报告* 📋\n"
            f"活跃仓位: `{summary.get('open_trades', 0)}`\n"
            f"累计费率收入: `${summary.get('total_funding', 0):+.2f}`\n"
            f"累计手续费: `${summary.get('total_fees', 0):.2f}`\n"
            f"累计返佣: `${summary.get('total_rebate', 0):+.2f}`\n"
            f"净盈亏: `${summary.get('total_net_pnl', 0):+.2f}`"
        )

    async def on_stop(self):
        await self.send("*机器人已停止* 🛑")
