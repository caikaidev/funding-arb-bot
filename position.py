"""
仓位管理模块 — SQLite 存储所有交易记录，追踪盈亏
"""
import json
import sqlite3
from datetime import datetime, timezone
from loguru import logger


class PositionManager:

    def __init__(self, db_path: str = "arbitrage.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row  # 返回 dict-like
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                direction       TEXT NOT NULL,
                quantity        REAL NOT NULL,
                spot_price      REAL NOT NULL,
                futures_price   REAL NOT NULL,
                usdt_amount     REAL NOT NULL,
                slippage        REAL,
                funding_earned  REAL DEFAULT 0,
                fees_paid       REAL DEFAULT 0,
                fees_rebated    REAL DEFAULT 0,
                status          TEXT DEFAULT 'open',
                opened_at       TEXT NOT NULL,
                closed_at       TEXT,
                close_pnl       REAL DEFAULT 0,
                net_pnl         REAL DEFAULT 0,
                rate_reverse_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS funding_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id     INTEGER NOT NULL,
                rate            REAL NOT NULL,
                payment         REAL NOT NULL,
                settled_at      TEXT NOT NULL,
                settlement_key  TEXT,
                FOREIGN KEY (position_id) REFERENCES positions(id)
            );

            CREATE TABLE IF NOT EXISTS trade_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id     INTEGER,
                action          TEXT NOT NULL,
                side            TEXT NOT NULL,
                market          TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                quantity        REAL,
                price           REAL,
                fee             REAL,
                raw_response    TEXT,
                created_at      TEXT NOT NULL
            );
        """)
        self.conn.commit()

        # 迁移：为旧数据库添加基差列（已存在时静默忽略）
        for col in ("open_basis", "close_basis"):
            try:
                self.conn.execute(f"ALTER TABLE positions ADD COLUMN {col} REAL DEFAULT NULL")
                self.conn.commit()
            except Exception:
                pass  # 列已存在

        # 迁移：为 funding_logs 添加幂等 key（已存在时静默忽略）
        try:
            self.conn.execute("ALTER TABLE funding_logs ADD COLUMN settlement_key TEXT")
            self.conn.commit()
        except Exception:
            pass
        # 同一持仓同一结算周期只允许一条资金费记录（NULL 不参与唯一约束）
        self.conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_funding_logs_pos_settlement
               ON funding_logs(position_id, settlement_key)"""
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # 开仓记录
    # ------------------------------------------------------------------
    def record_open(
        self, symbol, direction, quantity, spot_price,
        futures_price, usdt_amount, slippage, fees_paid,
        open_basis: float = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """INSERT INTO positions
               (symbol, direction, quantity, spot_price, futures_price,
                usdt_amount, slippage, fees_paid, opened_at, open_basis)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, direction, quantity, spot_price,
             futures_price, usdt_amount, slippage, fees_paid, now, open_basis),
        )
        self.conn.commit()
        pos_id = cursor.lastrowid
        basis_str = f" | 基差 {open_basis:+.4%}" if open_basis is not None else ""
        logger.info(f"记录开仓: #{pos_id} {symbol} {direction} ${usdt_amount:.2f}{basis_str}")
        return pos_id

    # ------------------------------------------------------------------
    # 单腿成交记录（开仓/平仓各写两条：现货腿 + 合约腿）
    # ------------------------------------------------------------------
    def record_trade(
        self,
        position_id: int,
        action: str,
        side: str,
        market: str,
        symbol: str,
        quantity: float,
        price: float,
        fee: float = None,
        raw_response: dict = None,
    ):
        """
        写入 trade_logs

        Args:
            action: 'open' | 'close'
            side:   'BUY' | 'SELL'
            market: 'spot' | 'futures'
        """
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO trade_logs
               (position_id, action, side, market, symbol, quantity, price, fee, raw_response, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position_id, action, side, market, symbol,
                quantity, price, fee,
                json.dumps(raw_response) if raw_response else None,
                now,
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # 费率收入记录
    # ------------------------------------------------------------------
    def record_funding(
        self,
        position_id: int,
        rate: float,
        payment: float,
        settlement_key: str | None = None,
    ) -> bool:
        """
        记录资金费收入。
        返回 True 表示本次已成功入账；False 表示命中幂等去重（重复周期）。
        """
        now = datetime.now(timezone.utc).isoformat()
        if settlement_key:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO funding_logs
                   (position_id, rate, payment, settled_at, settlement_key)
                   VALUES (?, ?, ?, ?, ?)""",
                (position_id, rate, payment, now, settlement_key),
            )
            if cur.rowcount == 0:
                self.conn.commit()
                return False
        else:
            self.conn.execute(
                "INSERT INTO funding_logs (position_id, rate, payment, settled_at) VALUES (?, ?, ?, ?)",
                (position_id, rate, payment, now),
            )
        self.conn.execute(
            "UPDATE positions SET funding_earned = funding_earned + ? WHERE id = ?",
            (payment, position_id),
        )
        self.conn.commit()
        return True

    # ------------------------------------------------------------------
    # 平仓记录
    # ------------------------------------------------------------------
    def record_close(
        self, position_id: int, close_pnl: float, fees: float, rebate: float,
        close_basis: float = None,
    ):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """UPDATE positions SET
               status = 'closed', closed_at = ?,
               close_pnl = ?, fees_paid = fees_paid + ?,
               fees_rebated = fees_rebated + ?,
               net_pnl = funding_earned + ? - fees_paid - ? + fees_rebated + ?,
               close_basis = ?
               WHERE id = ?""",
            (now, close_pnl, fees, rebate, close_pnl, fees, rebate, close_basis, position_id),
        )
        self.conn.commit()
        logger.info(f"记录平仓: #{position_id}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_open_positions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status = 'open'"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_position(self, position_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_summary(self) -> dict:
        """汇总统计"""
        row = self.conn.execute("""
            SELECT
                COUNT(*)                                                    AS total_trades,
                COALESCE(SUM(CASE WHEN status='open' THEN 1 ELSE 0 END), 0) AS open_trades,
                COALESCE(SUM(funding_earned), 0)                            AS total_funding,
                COALESCE(SUM(fees_paid), 0)                                 AS total_fees,
                COALESCE(SUM(fees_rebated), 0)                              AS total_rebate,
                COALESCE(SUM(net_pnl), 0)                                   AS total_net_pnl
            FROM positions
        """).fetchone()
        return dict(row) if row else {}

    def get_daily_pnl(self, date_str: str = None) -> float:
        """查询当日盈亏：今日结算的资金费 + 今日平仓的价格盈亏"""
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        funding_row = self.conn.execute(
            "SELECT COALESCE(SUM(payment), 0) FROM funding_logs WHERE settled_at LIKE ?",
            (f"{date_str}%",),
        ).fetchone()
        close_row = self.conn.execute(
            "SELECT COALESCE(SUM(close_pnl), 0) FROM positions WHERE closed_at LIKE ? AND status='closed'",
            (f"{date_str}%",),
        ).fetchone()
        return float(funding_row[0]) + float(close_row[0])

    def close_db(self):
        self.conn.close()
