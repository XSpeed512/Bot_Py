"""
ai/learning_module.py
======================
Trade journaling and performance analytics via SQLite.

Every trade executed by the bot is recorded here.  The schema is
deliberately ML-friendly so that future models can ingest the data
directly without transformation.

Public functions
----------------
initialize_database()   – Create tables if they do not exist.
save_trade()            – Persist a completed or newly opened trade.
update_trade_result()   – Mark a trade as closed with its outcome.
load_trades()           – Retrieve trades (all or filtered).
get_performance_stats() – Return basic win-rate / R-multiple metrics.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

import config
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------

def initialize_database() -> None:
    """
    Create the SQLite database and ``trades`` table if they do not exist.

    Schema
    ------
    id          INTEGER PRIMARY KEY
    symbol      TEXT     Instrument name (e.g. "EURUSD")
    direction   TEXT     "BUY" or "SELL"
    entry       REAL     Entry price
    stop_loss   REAL     Original SL price
    take_profit REAL     TP price
    lot_size    REAL     Volume in lots
    result      TEXT     "WIN" | "LOSS" | "BREAKEVEN" | "OPEN"
    pnl         REAL     Monetary P&L in account currency
    risk_reward REAL     Actual achieved R-multiple
    open_time   TEXT     ISO timestamp of trade open
    close_time  TEXT     ISO timestamp of trade close (NULL if open)
    ticket      INTEGER  MT5 position ticket
    notes       TEXT     Free-form notes for manual annotation
    """
    conn = _get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL,
                direction   TEXT    NOT NULL,
                entry       REAL    NOT NULL,
                stop_loss   REAL    NOT NULL,
                take_profit REAL    NOT NULL,
                lot_size    REAL    NOT NULL,
                result      TEXT    NOT NULL DEFAULT 'OPEN',
                pnl         REAL,
                risk_reward REAL,
                open_time   TEXT    NOT NULL,
                close_time  TEXT,
                ticket      INTEGER,
                notes       TEXT
            )
        """)
        # Index frequently queried columns
        conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON trades(symbol)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_result ON trades(result)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket ON trades(ticket)")
        conn.commit()
        logger.info("Database initialised at %s", config.DB_PATH)
    except sqlite3.Error as exc:
        logger.error("Database initialisation error: %s", exc)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def save_trade(
    symbol: str,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    lot_size: float,
    ticket: Optional[int] = None,
    notes: str = "",
) -> Optional[int]:
    """
    Insert a new trade record with status ``OPEN``.

    Parameters
    ----------
    symbol      : str
    direction   : str   ``"BUY"`` or ``"SELL"``
    entry       : float Entry price
    stop_loss   : float Original SL
    take_profit : float TP
    lot_size    : float Volume in lots
    ticket      : int   MT5 position ticket (optional at insertion time)
    notes       : str   Optional annotation

    Returns
    -------
    int   Row id of the inserted record, or None on failure.
    """
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO trades
                (symbol, direction, entry, stop_loss, take_profit,
                 lot_size, result, open_time, ticket, notes)
            VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)
            """,
            (
                symbol, direction, entry, stop_loss, take_profit,
                lot_size,
                datetime.utcnow().isoformat(timespec="seconds"),
                ticket, notes,
            ),
        )
        conn.commit()
        row_id = cursor.lastrowid
        logger.info(
            "Trade saved to DB | id=%s symbol=%s dir=%s entry=%.5f",
            row_id, symbol, direction, entry,
        )
        return row_id
    except sqlite3.Error as exc:
        logger.error("save_trade DB error: %s", exc)
        return None
    finally:
        conn.close()


def update_trade_result(
    ticket: int,
    result: str,       # "WIN" | "LOSS" | "BREAKEVEN"
    pnl: float,
    risk_reward: float,
    notes: str = "",
) -> bool:
    """
    Mark an existing trade as closed with its outcome.

    Parameters
    ----------
    ticket      : int   MT5 position ticket (used to find the record)
    result      : str   ``"WIN"``, ``"LOSS"``, or ``"BREAKEVEN"``
    pnl         : float Monetary profit/loss
    risk_reward : float Achieved R-multiple (profit / initial_risk)
    notes       : str   Optional annotation

    Returns
    -------
    bool  True if a row was updated.
    """
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE trades
            SET result     = ?,
                pnl        = ?,
                risk_reward = ?,
                close_time  = ?,
                notes       = CASE WHEN ? != '' THEN ? ELSE notes END
            WHERE ticket = ? AND result = 'OPEN'
            """,
            (
                result, pnl, risk_reward,
                datetime.utcnow().isoformat(timespec="seconds"),
                notes, notes,
                ticket,
            ),
        )
        conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(
                "Trade result updated | ticket=%s result=%s pnl=%.2f R=%.2f",
                ticket, result, pnl, risk_reward,
            )
        return updated
    except sqlite3.Error as exc:
        logger.error("update_trade_result DB error: %s", exc)
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def load_trades(
    symbol: Optional[str] = None,
    result: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    """
    Retrieve trade records from the database.

    Parameters
    ----------
    symbol : str, optional   Filter by symbol.
    result : str, optional   Filter by result (``"WIN"``, ``"LOSS"``, etc.)
    limit  : int             Maximum rows to return.

    Returns
    -------
    list of dict  Each dict maps column name → value.
    """
    query = "SELECT * FROM trades WHERE 1=1"
    params: list = []

    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    if result:
        query += " AND result = ?"
        params.append(result)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        logger.error("load_trades DB error: %s", exc)
        return []
    finally:
        conn.close()


def get_performance_stats() -> dict:
    """
    Calculate basic performance metrics over all closed trades.

    Returns
    -------
    dict with keys:
        total_trades, wins, losses, breakevens,
        win_rate_pct, avg_rr, total_pnl
    """
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                        AS total_trades,
                SUM(CASE WHEN result = 'WIN'  THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN result = 'BREAKEVEN' THEN 1 ELSE 0 END) AS breakevens,
                AVG(CASE WHEN result != 'OPEN' THEN risk_reward END) AS avg_rr,
                SUM(COALESCE(pnl, 0))                          AS total_pnl
            FROM trades
            WHERE result != 'OPEN'
            """
        ).fetchone()

        total  = row["total_trades"] or 0
        wins   = row["wins"]         or 0

        return {
            "total_trades": total,
            "wins":         wins,
            "losses":       row["losses"]     or 0,
            "breakevens":   row["breakevens"] or 0,
            "win_rate_pct": (wins / total * 100) if total > 0 else 0.0,
            "avg_rr":       round(row["avg_rr"] or 0.0, 2),
            "total_pnl":    round(row["total_pnl"] or 0.0, 2),
        }
    except sqlite3.Error as exc:
        logger.error("get_performance_stats DB error: %s", exc)
        return {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_connection() -> sqlite3.Connection:
    """Open and return a connection to the configured SQLite file."""
    import os
    db_dir = os.path.dirname(config.DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    return sqlite3.connect(config.DB_PATH)
