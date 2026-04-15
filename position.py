"""
仓位管理模块 — SQLite 存储所有交易记录，追踪盈亏
"""
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

    # ------------------------------------------------------------------
    # 开仓记录
    # ------------------------------------------------------------------
    def record_open(
        self, symbol, direction, quantity, spot_price,
        futures_price, usdt_amount, slippage, fees_paid
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """INSERT INTO positions
               (symbol, direction, quantity, spot_price, futures_price,
                usdt_amount, slippage, fees_paid, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, direction, quantity, spot_price,
             futures_price, usdt_amount, slippage, fees_paid, now),
        )
        self.conn.commit()
        pos_id = cursor.lastrowid
        logger.info(f"记录开仓: #{pos_id} {symbol} {direction} ${usdt_amount:.2f}")
        return pos_id

    # ------------------------------------------------------------------
    # 费率收入记录
    # ------------------------------------------------------------------
    def record_funding(self, position_id: int, rate: float, payment: float):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO funding_logs (position_id, rate, payment, settled_at) VALUES (?, ?, ?, ?)",
            (position_id, rate, payment, now),
        )
        self.conn.execute(
            "UPDATE positions SET funding_earned = funding_earned + ? WHERE id = ?",
            (payment, position_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # 平仓记录
    # ------------------------------------------------------------------
    def record_close(self, position_id: int, close_pnl: float, fees: float, rebate: float):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """UPDATE positions SET
               status = 'closed', closed_at = ?,
               close_pnl = ?, fees_paid = fees_paid + ?,
               fees_rebated = fees_rebated + ?,
               net_pnl = funding_earned + ? - fees_paid - ? + fees_rebated + ?
               WHERE id = ?""",
            (now, close_pnl, fees, rebate, close_pnl, fees, rebate, position_id),
        )
        self.conn.commit()
        logger.info(f"记录平仓: #{position_id}")

    # ------------------------------------------------------------------
    # 费率反转计数（用于判断是否平仓）
    # ------------------------------------------------------------------
    def increment_reverse_count(self, position_id: int):
        self.conn.execute(
            "UPDATE positions SET rate_reverse_count = rate_reverse_count + 1 WHERE id = ?",
            (position_id,),
        )
        self.conn.commit()

    def reset_reverse_count(self, position_id: int):
        self.conn.execute(
            "UPDATE positions SET rate_reverse_count = 0 WHERE id = ?",
            (position_id,),
        )
        self.conn.commit()

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
                COUNT(*)                                           AS total_trades,
                SUM(CASE WHEN status='open' THEN 1 ELSE 0 END)    AS open_trades,
                SUM(funding_earned)                                AS total_funding,
                SUM(fees_paid)                                     AS total_fees,
                SUM(fees_rebated)                                  AS total_rebate,
                SUM(net_pnl)                                       AS total_net_pnl
            FROM positions
        """).fetchone()
        return dict(row) if row else {}

    def get_daily_pnl(self, date_str: str = None) -> float:
        """查询当日盈亏"""
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self.conn.execute(
            """SELECT COALESCE(SUM(payment), 0) AS daily_pnl
               FROM funding_logs
               WHERE settled_at LIKE ?""",
            (f"{date_str}%",),
        ).fetchone()
        return float(row["daily_pnl"]) if row else 0.0

    def close_db(self):
        self.conn.close()
