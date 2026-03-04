"""
Momentum Scalper Bot — Persistence (SQLite)
Сохранение состояния для восстановления после перезапуска.
"""

import sqlite3
import json
import time
import logging
from pathlib import Path

logger = logging.getLogger("persistence")

DB_FILE = Path(__file__).parent / "bot_state.db"


class BotPersistence:
    """SQLite хранилище для состояния бота."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_FILE)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        """Создать таблицы если не существуют."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                qty TEXT NOT NULL,
                sl TEXT,
                tp TEXT,
                open_time REAL NOT NULL,
                close_time REAL,
                pnl REAL DEFAULT 0.0,
                status TEXT DEFAULT 'OPEN',
                vol_regime TEXT,
                entry_quality REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bot_stats (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS trailing_state (
                symbol TEXT PRIMARY KEY,
                best_price REAL NOT NULL,
                activated INTEGER DEFAULT 0,
                entry_atr REAL NOT NULL,
                open_time REAL NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
        """)
        self.conn.commit()

    # ─────────────────────────────────────────────────────────
    # Trades
    # ─────────────────────────────────────────────────────────
    def save_trade(self, symbol: str, direction: str, side: str,
                   entry_price: float, qty: str, sl: str, tp: str,
                   open_time: float, vol_regime: str = "",
                   entry_quality: float = 0.0) -> int:
        """Сохранить открытую сделку. Возвращает trade_id."""
        cur = self.conn.execute(
            """INSERT INTO trades (symbol, direction, side, entry_price,
               qty, sl, tp, open_time, status, vol_regime, entry_quality)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)""",
            (symbol, direction, side, entry_price, qty, sl, tp,
             open_time, vol_regime, entry_quality)
        )
        self.conn.commit()
        return cur.lastrowid

    def close_trade(self, symbol: str, pnl: float, status: str):
        """Закрыть сделку по символу."""
        self.conn.execute(
            """UPDATE trades SET pnl = ?, status = ?, close_time = ?
               WHERE symbol = ? AND status = 'OPEN'""",
            (pnl, status, time.time(), symbol)
        )
        self.conn.commit()

    def get_open_trades(self) -> list[dict]:
        """Получить все открытые сделки."""
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN'"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_trade_history(self, limit: int = 100) -> list[dict]:
        """Получить историю сделок."""
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_session_stats(self) -> dict:
        """Получить статистику всей сессии из БД."""
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl <= 0 AND status != 'OPEN' THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(CASE WHEN status != 'OPEN' THEN pnl ELSE 0 END), 0) as total_pnl
            FROM trades
        """).fetchone()
        return dict(row) if row else {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}

    # ─────────────────────────────────────────────────────────
    # Trailing Stop State
    # ─────────────────────────────────────────────────────────
    def save_trailing_state(self, symbol: str, best_price: float,
                            activated: bool, entry_atr: float,
                            open_time: float):
        """Сохранить состояние trailing stop для символа."""
        self.conn.execute(
            """INSERT OR REPLACE INTO trailing_state
               (symbol, best_price, activated, entry_atr, open_time, updated_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (symbol, best_price, int(activated), entry_atr, open_time)
        )
        self.conn.commit()

    def get_trailing_states(self) -> dict[str, dict]:
        """Получить все trailing stop состояния."""
        rows = self.conn.execute("SELECT * FROM trailing_state").fetchall()
        result = {}
        for r in rows:
            result[r["symbol"]] = {
                "best_price": r["best_price"],
                "activated": bool(r["activated"]),
                "entry_atr": r["entry_atr"],
                "open_time": r["open_time"],
            }
        return result

    def remove_trailing_state(self, symbol: str):
        """Удалить trailing state для закрытой позиции."""
        self.conn.execute(
            "DELETE FROM trailing_state WHERE symbol = ?", (symbol,)
        )
        self.conn.commit()

    # ─────────────────────────────────────────────────────────
    # Bot Stats (key-value store)
    # ─────────────────────────────────────────────────────────
    def save_stat(self, key: str, value):
        """Сохранить произвольное значение."""
        self.conn.execute(
            """INSERT OR REPLACE INTO bot_stats (key, value, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)""",
            (key, json.dumps(value))
        )
        self.conn.commit()

    def get_stat(self, key: str, default=None):
        """Получить значение по ключу."""
        row = self.conn.execute(
            "SELECT value FROM bot_stats WHERE key = ?", (key,)
        ).fetchone()
        if row:
            return json.loads(row["value"])
        return default

    # ─────────────────────────────────────────────────────────
    # Cleanup
    # ─────────────────────────────────────────────────────────
    def cleanup_stale_opens(self, active_symbols: set[str]):
        """Пометить 'OPEN' записи как LOST если нет на бирже."""
        opens = self.get_open_trades()
        for t in opens:
            if t["symbol"] not in active_symbols:
                self.conn.execute(
                    """UPDATE trades SET status = 'LOST', close_time = ?
                       WHERE id = ?""",
                    (time.time(), t["id"])
                )
        self.conn.commit()

    def close(self):
        """Закрыть соединение."""
        self.conn.close()
