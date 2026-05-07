# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A funding rate arbitrage bot for Binance. It earns yield by simultaneously holding a spot position and an opposite perpetual futures position, collecting the funding rate difference every 8 hours. Written in Chinese (comments, logs, UI).

## Commands

```bash
# Preflight checks
python preflight.py --monitor   # Monitor-mode checks only
python preflight.py             # Full checks (including trade permissions)

# Run modes
python main.py --monitor --capital 10000    # Read-only: scan and print, no DB
python main.py --simulate --capital 10000   # Paper trading: virtual positions in simulate.db
python main.py --capital 10000              # Live trading: real orders, arbitrage.db

# Flags
--t3 / --no-t3       # Enable/disable T3 (small-cap) tier
--config path.yaml    # Custom config file

# View results
python results.py                     # Simulation report (simulate.db)
python results.py --db arbitrage.db   # Live report
python results.py --export            # Export CSV

# Capital allocation preview
python capital.py

# Deploy to Linux server (systemd)
bash deploy.sh
```

## Architecture

**Three operating modes** controlled by `main.py` CLI flags:
- **monitor**: Read-only scanning via ccxt, no database, no orders
- **simulate**: Full pipeline with `SimulatedExecutor` writing to `simulate.db`
- **live**: Real orders via Binance SDK writing to `arbitrage.db`

**Dual SDK design** (critical for rebate preservation):
- `ccxt` is used **only for reading** market data (funding rates, order books, tickers) in `monitor.py` and `screener.py`. ccxt injects a brokerId that would affect rebate attribution.
- `binance-connector` / `binance-futures-connector` (official Binance SDKs) are used **only for order execution** in `executor.py`. This preserves the user's 30% fee rebate.

**Data flow per scan cycle:**
1. `DynamicScreener.screen()` — loads markets via ccxt, fetches all funding rates in batch, filters by tier thresholds (volume, OI, spread, depth), scores and ranks candidates
2. `FundingArbitrageBot.task_scan_and_open()` — checks allocation limits per tier, calls executor
3. `BinanceExecutor.open_arbitrage()` — concurrent spot + futures market orders via ThreadPoolExecutor, with atomic rollback if one leg fails
4. `PositionManager` — records to SQLite

**Tier system** (coin classification in `screener.py`):
- T1 (core): BTC, ETH — highest allocation, lowest rate threshold
- T2 (major): SOL, BNB, XRP, etc. (20 coins) — medium allocation
- T3 (opportunity): everything else — smallest allocation, requires `--t3` flag, has listing-age and total-cap constraints

**Capital allocation** (`capital.py`): All amounts are derived from `initial_capital` and percentage config in `config.yaml`. The `CapitalPlan` dataclass flows through the entire system. Percentages: 80% tradable, 15% on-chain reserve (Aave), 5% emergency.

**Scheduling** (`APScheduler`):
- Scan + open: every N minutes (default 5)
- Position check + close: every 2N minutes
- Funding settlement recording: cron at 00:05, 08:05, 16:05 UTC (matching Binance 8h cycles)
- Daily report: cron at 00:30 UTC

**Exit conditions** (checked in `task_check_and_close`):
- Funding rate reverses direction
- Rate drops below `min_profitable_rate`
- Position held longer than `max_holding_days`
- T2/T3 liquidity drops below threshold

## Key Design Decisions

- `executor.py` uses `concurrent.futures.ThreadPoolExecutor` to fire spot and futures legs simultaneously, with rollback logic if one side fails. Both `open_arbitrage` and `close_arbitrage` follow this pattern.
- `SimulatedExecutor` has an identical interface to `BinanceExecutor` — swap is done by import in `main.py.__init__` based on mode.
- Symbol format conversion: Binance native format is `BTCUSDT`, ccxt format is `BTC/USDT:USDT`. The helper `_to_ccxt()` in `screener.py` converts between them.
- SQLite databases are mode-separated: `simulate.db` vs `arbitrage.db`.
- Config uses proportion-driven design: only `initial_capital` needs to be set, everything else derives from percentages in `config.yaml`.

## Memory & Long-Running Constraints

This bot runs 24/7 on small VPS (typically 912 MiB RAM, 20-40 GB disk). All code must be written with long-running stability in mind.

### Data Loading Rules

- **Never load entire datasets into memory.** Backtest, results, and any data analysis must use streaming/iterators (e.g., `backtest.py`'s `stream_snapshots()` yields one scan round at a time, peak < 5 MiB).
- **Never `SELECT *` without `WHERE` in hot paths.** Queries called every scan cycle (e.g., `get_open_positions`, `get_daily_pnl`) must have indexed columns in their WHERE clauses.
- **Avoid `LIKE` for date filtering** — use `BETWEEN` with an index on the timestamp column. `LIKE 'YYYY-MM-DD%'` forces full table scan.
- **Large API responses (exchangeInfo, fetch_funding_rates) must be consumed and released promptly.** Extract only needed fields; do not store raw responses in long-lived caches.

### Cache & Growth Rules

- **All in-memory caches must have a TTL or size cap.** Never let a dict/set/list grow unboundedly. Use overwrite-on-refresh pattern (see `_market_cache` with 5-min TTL).
- **Deduplication sets that grow over time** (e.g., `_funding_done` in `backtest.py`) should be periodically pruned or use a sliding window. For unbounded data, prefer `CREATE UNIQUE INDEX ... INSERT OR IGNORE` in SQLite over in-memory sets.
- **Thread pools and HTTP sessions should be created once and reused**, not created per-call. `aiohttp.ClientSession` especially must be shared — creating one per request leaks TCP connections.

### SQLite Rules

- **Always use WAL mode** (`PRAGMA journal_mode=WAL`) and set `busy_timeout` (≥ 5000 ms). Default journal mode causes `database is locked` under concurrent APScheduler jobs.
- **Add indexes on columns used in WHERE/ORDER BY** for any table that grows over time. At minimum: `positions(status)`, `positions(closed_at)`, `funding_logs(settled_at)`.
- **For columns that store JSON blobs** (e.g., `trade_logs.raw_response`), consider making storage optional and only persisting on error paths.

### File Output Rules

- **Any code that appends to files (CSV, logs) must have a cleanup mechanism.** Loguru handles `.log` files via `retention`, but CSV snapshots and other output files need explicit cleanup (e.g., delete files older than N days).
- **Monitor-mode CSV snapshots grow ~13 MB/day.** Always pair `_append_raw_csv()` with a cleanup call.

### ccxt / SDK Rules

- **Only one ccxt exchange instance per process.** The `load_markets()` call caches ~80-120 MB of metadata. Screener and Monitor must share the same instance (already done via `exchange=self.screener.exchange`).
- **When caching market metadata**, only keep fields actually used (`swap`, `quote`, `active`, `info.onboardDate`). Do not cache the full market dict.
- **`_get_precision()` should pre-build the full cache on startup** from a single `exchange_info()` call, not fetch the full payload on each cache miss.
