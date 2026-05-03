"""
模拟结果查看器 — 一周后运行此脚本查看完整报告

使用方式:
  python results.py                # 查看模拟结果
  python results.py --db arbitrage.db  # 查看实盘结果
  python results.py --export       # 导出 CSV
"""
import argparse
import sqlite3
from datetime import datetime, timezone
from collections import defaultdict


def _initial_capital_hint() -> str:
    """用于空库提示；优先读 config.yaml，失败则给通用说明。"""
    try:
        import yaml

        with open("config.yaml") as f:
            cap = yaml.safe_load(f).get("initial_capital")
        if cap is not None:
            return f"python main.py --simulate --capital {cap}"
    except Exception:
        pass
    return "python main.py --simulate（资金可用 config.yaml 的 initial_capital，勿须与示例一致）"


def main():
    parser = argparse.ArgumentParser(description="查看套利结果")
    parser.add_argument("--db", default="simulate.db", help="数据库文件")
    parser.add_argument("--export", action="store_true", help="导出 CSV")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # 总览
    # ------------------------------------------------------------------
    summary = conn.execute("""
        SELECT
            COUNT(*)                                        AS total_trades,
            SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_trades,
            SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_trades,
            COALESCE(SUM(funding_earned), 0)                AS total_funding,
            COALESCE(SUM(fees_paid), 0)                     AS total_fees,
            COALESCE(SUM(fees_rebated), 0)                  AS total_rebate,
            COALESCE(SUM(net_pnl), 0)                       AS total_net_pnl,
            MIN(opened_at)                                  AS first_trade,
            MAX(COALESCE(closed_at, opened_at))             AS last_activity
        FROM positions
    """).fetchone()

    positions = conn.execute(
        "SELECT * FROM positions ORDER BY opened_at"
    ).fetchall()

    funding_logs = conn.execute(
        "SELECT * FROM funding_logs ORDER BY settled_at"
    ).fetchall()

    # 运行天数
    if summary["first_trade"]:
        first = datetime.fromisoformat(summary["first_trade"])
        now = datetime.now(timezone.utc)
        days = max((now - first).total_seconds() / 86400, 1)
    else:
        days = 0

    db_name = "模拟" if "simulate" in args.db else "实盘"

    print()
    print(f"{'='*60}")
    print(f"  {db_name}交易报告")
    print(f"{'='*60}")

    if not positions:
        print(f"\n  数据库为空，还没有交易记录。")
        print(f"  确认机器人已运行: {_initial_capital_hint()}")
        print(f"{'='*60}")
        return

    print(f"  运行时间:   {days:.1f} 天 ({summary['first_trade'][:16]} 至今)")
    print(f"  总交易次数: {summary['total_trades']}")
    print(f"  当前持仓:   {summary['open_trades']}")
    print(f"  已平仓:     {summary['closed_trades']}")

    print(f"\n{'─'*60}")
    print(f"  收益明细")
    print(f"{'─'*60}")
    print(f"  费率收入:     ${summary['total_funding']:>+10.4f}")
    print(f"  手续费支出:   ${summary['total_fees']:>10.4f}")
    print(f"  返佣收入:     ${summary['total_rebate']:>+10.4f}")
    print(f"  ────────────────────────────")
    print(f"  净盈亏:       ${summary['total_net_pnl']:>+10.4f}")

    if days > 0:
        daily_avg = summary["total_net_pnl"] / days
        monthly_est = daily_avg * 30
        # 从第一笔交易的资金推算本金
        first_pos = positions[0]
        est_capital = first_pos["usdt_amount"] / 0.375  # T1 占 37.5%
        annual_pct = (daily_avg * 365 / est_capital * 100) if est_capital > 0 else 0

        print(f"\n{'─'*60}")
        print(f"  收益预估")
        print(f"{'─'*60}")
        print(f"  日均净收入:   ${daily_avg:>+10.4f}")
        print(f"  月度预估:     ${monthly_est:>+10.2f}")
        print(f"  年化预估:     ${daily_avg * 365:>+10.2f}")
        print(f"  年化利率:     {annual_pct:>+10.1f}%")

    # ------------------------------------------------------------------
    # 按币种分组
    # ------------------------------------------------------------------
    by_symbol = defaultdict(lambda: {"count": 0, "funding": 0, "fees": 0, "rebate": 0})
    for p in positions:
        s = p["symbol"]
        by_symbol[s]["count"] += 1
        by_symbol[s]["funding"] += p["funding_earned"] or 0
        by_symbol[s]["fees"] += p["fees_paid"] or 0
        by_symbol[s]["rebate"] += p["fees_rebated"] or 0

    print(f"\n{'─'*60}")
    print(f"  按币种统计")
    print(f"{'─'*60}")
    print(f"  {'币种':<12} {'次数':>4} {'费率收入':>10} {'手续费':>10} {'净收入':>10}")
    print(f"  {'─'*48}")
    for sym, data in sorted(by_symbol.items(), key=lambda x: x[1]["funding"], reverse=True):
        net = data["funding"] - data["fees"] + data["rebate"]
        print(f"  {sym:<12} {data['count']:>4} ${data['funding']:>+9.4f} ${data['fees']:>9.4f} ${net:>+9.4f}")

    # ------------------------------------------------------------------
    # 按天统计费率收入
    # ------------------------------------------------------------------
    if funding_logs:
        by_day = defaultdict(float)
        for f in funding_logs:
            day = f["settled_at"][:10]
            by_day[day] += f["payment"]

        print(f"\n{'─'*60}")
        print(f"  每日费率收入")
        print(f"{'─'*60}")
        for day in sorted(by_day.keys()):
            bar_len = int(abs(by_day[day]) * 20)  # 简单柱状图
            bar = "█" * min(bar_len, 40)
            sign = "+" if by_day[day] >= 0 else ""
            print(f"  {day}  ${sign}{by_day[day]:.4f}  {bar}")

    # ------------------------------------------------------------------
    # 当前持仓
    # ------------------------------------------------------------------
    open_pos = [p for p in positions if p["status"] == "open"]
    if open_pos:
        print(f"\n{'─'*60}")
        print(f"  当前持仓")
        print(f"{'─'*60}")
        for p in open_pos:
            opened = p["opened_at"][:16]
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(p["opened_at"])).days
            print(
                f"  #{p['id']} {p['symbol']:<12} "
                f"{'做空' if p['direction']=='positive' else '做多'} "
                f"${p['usdt_amount']:>7,.0f} | "
                f"已收 ${p['funding_earned']:>+.4f} | "
                f"{age}天 | {opened}"
            )

    print(f"\n{'='*60}")

    # ------------------------------------------------------------------
    # 导出 CSV
    # ------------------------------------------------------------------
    if args.export:
        import csv
        filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(filename, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "symbol", "direction", "usdt_amount",
                "funding_earned", "fees_paid", "fees_rebated",
                "net_pnl", "status", "opened_at", "closed_at"
            ])
            for p in positions:
                writer.writerow([
                    p["id"], p["symbol"], p["direction"], p["usdt_amount"],
                    p["funding_earned"], p["fees_paid"], p["fees_rebated"],
                    p["net_pnl"], p["status"], p["opened_at"], p["closed_at"],
                ])
        print(f"  已导出: {filename}")

    conn.close()


if __name__ == "__main__":
    main()
