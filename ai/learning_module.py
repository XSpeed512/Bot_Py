"""
ai/learning_module.py
======================
Trade journaling e performance analytics via SQLite.

Versione unificata: mantiene tutte le funzioni standalone originali
e aggiunge una classe LearningModule con i metodi usati da main.py.

Il database è invariato — stesso schema, stessa posizione (ai/trades.db).
I nuovi campi (confidence, regime, scores) vengono aggiunti come colonne
opzionali per non rompere la compatibilità con trade già registrati.
"""

from __future__ import annotations

import sqlite3
import os
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
    Crea la tabella trades se non esiste.
    Aggiunge le colonne extra del nuovo sistema se mancano (migrazione safe).
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
                notes       TEXT,
                -- Nuovi campi adattivi (nullable per compatibilità)
                confidence      REAL,
                trade_quality   REAL,
                risk_score      REAL,
                regime          TEXT,
                trend_score     REAL,
                momentum_score  REAL,
                volume_score    REAL,
                structure_score REAL,
                volatility_score REAL,
                exit_reason     TEXT,
                exit_price      REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON trades(symbol)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_result ON trades(result)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket ON trades(ticket)")
        conn.commit()

        # Migrazione safe: aggiungi colonne nuove se db già esiste
        _migrate_add_columns(conn)

        logger.info("Database inizializzato: %s", config.DB_PATH)
    except sqlite3.Error as exc:
        logger.error("Errore inizializzazione database: %s", exc)
    finally:
        conn.close()


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    """Aggiunge le nuove colonne se non esistono già (per db preesistenti)."""
    new_cols = [
        ("confidence",       "REAL"),
        ("trade_quality",    "REAL"),
        ("risk_score",       "REAL"),
        ("regime",           "TEXT"),
        ("trend_score",      "REAL"),
        ("momentum_score",   "REAL"),
        ("volume_score",     "REAL"),
        ("structure_score",  "REAL"),
        ("volatility_score", "REAL"),
        ("exit_reason",      "TEXT"),
        ("exit_price",       "REAL"),
    ]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    for col_name, col_type in new_cols:
        if col_name not in existing:
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
                conn.commit()
                logger.debug("Colonna aggiunta al DB: %s", col_name)
            except sqlite3.Error:
                pass  # Già esiste in un'altra migrazione concorrente


# ---------------------------------------------------------------------------
# Funzioni standalone (API originale invariata)
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
        logger.info("Trade salvato nel DB | id=%s symbol=%s dir=%s entry=%.5f",
                    row_id, symbol, direction, entry)
        return row_id
    except sqlite3.Error as exc:
        logger.error("save_trade DB error: %s", exc)
        return None
    finally:
        conn.close()


def update_trade_result(
    ticket: int,
    result: str,
    pnl: float,
    risk_reward: float,
    notes: str = "",
    exit_reason: str = "",
) -> bool:
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE trades
            SET result      = ?,
                pnl         = ?,
                risk_reward = ?,
                close_time  = ?,
                exit_reason = ?,
                notes       = CASE WHEN ? != '' THEN ? ELSE notes END
            WHERE ticket = ? AND result = 'OPEN'
            """,
            (
                result, pnl, risk_reward,
                datetime.utcnow().isoformat(timespec="seconds"),
                exit_reason,
                notes, notes,
                ticket,
            ),
        )
        conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info("Trade aggiornato | ticket=%s result=%s pnl=%.2f R=%.2f",
                        ticket, result, pnl, risk_reward)
        return updated
    except sqlite3.Error as exc:
        logger.error("update_trade_result DB error: %s", exc)
        return False
    finally:
        conn.close()


def load_trades(
    symbol: Optional[str] = None,
    result: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    query  = "SELECT * FROM trades WHERE 1=1"
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
    conn = _get_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT
                COUNT(*)                                          AS total_trades,
                SUM(CASE WHEN result = 'WIN'       THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN result = 'LOSS'      THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN result = 'BREAKEVEN' THEN 1 ELSE 0 END) AS breakevens,
                AVG(CASE WHEN result != 'OPEN' THEN risk_reward END)   AS avg_rr,
                SUM(COALESCE(pnl, 0))                                  AS total_pnl
            FROM trades
            WHERE result != 'OPEN'
        """).fetchone()

        total = row["total_trades"] or 0
        wins  = row["wins"]         or 0
        return {
            "total_trades": total,
            "wins":         wins,
            "losses":       row["losses"]     or 0,
            "breakevens":   row["breakevens"] or 0,
            "win_rate_pct": (wins / total * 100) if total > 0 else 0.0,
            "avg_rr":       round(row["avg_rr"]    or 0.0, 2),
            "total_pnl":    round(row["total_pnl"] or 0.0, 2),
        }
    except sqlite3.Error as exc:
        logger.error("get_performance_stats DB error: %s", exc)
        return {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Classe LearningModule — usata da main.py
# ---------------------------------------------------------------------------

class LearningModule:
    """
    Wrapper a oggetto per le funzioni standalone.
    Aggiunge record_trade_open() e get_last_trade() usati dal nuovo main.py.
    """

    def __init__(self):
        initialize_database()

    # ── Metodi originali wrappati ──────────────────────────────────

    def save_trade(self, *args, **kwargs):
        return save_trade(*args, **kwargs)

    def update_trade_result(self, *args, **kwargs):
        return update_trade_result(*args, **kwargs)

    def load_trades(self, *args, **kwargs):
        return load_trades(*args, **kwargs)

    def get_performance_stats(self) -> dict:
        return get_performance_stats()

    # ── Nuovi metodi per il sistema adattivo ──────────────────────

    def record_trade_open(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        sl: float,
        tp: float,
        volume: float,
        confidence: float = 0.0,
        trade_quality: float = 0.0,
        risk_score: float = 0.0,
        regime: str = "",
        ticket: Optional[int] = None,
        trend_score: float = 0.0,
        momentum_score: float = 0.0,
        volume_score: float = 0.0,
        structure_score: float = 0.0,
        volatility_score: float = 0.0,
    ) -> Optional[int]:
        """
        Registra l'apertura di un trade con tutti i dati del sistema di scoring.
        Salva confidence, trade_quality, risk_score e i 5 score componenti.
        """
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO trades
                    (symbol, direction, entry, stop_loss, take_profit, lot_size,
                     result, open_time, ticket, confidence, trade_quality,
                     risk_score, regime, trend_score, momentum_score,
                     volume_score, structure_score, volatility_score)
                VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol, direction.upper(), entry_price, sl, tp, volume,
                    datetime.utcnow().isoformat(timespec="seconds"),
                    ticket, confidence, trade_quality, risk_score, regime,
                    trend_score, momentum_score, volume_score,
                    structure_score, volatility_score,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        except sqlite3.Error as exc:
            logger.error("record_trade_open DB error: %s", exc)
            return None
        finally:
            conn.close()

    def get_last_trade(self, symbol: str) -> Optional[dict]:
        """
        Recupera l'ultimo trade chiuso per un simbolo.
        Usato da main.py per recuperare exit_price e exit_reason
        dopo che MT5 ha chiuso la posizione.
        """
        conn = _get_connection()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM trades
                WHERE symbol = ? AND result != 'OPEN'
                ORDER BY id DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            # Normalizza i campi attesi da main.py
            # exit_price viene ora letta dalla colonna dedicata del DB
            if not d.get("exit_price"):
                d["exit_price"] = d.get("entry", 0.0)
            d.setdefault("exit_reason", d.get("exit_reason", "unknown"))
            d.setdefault("duration_bars", 0)
            return d
        except sqlite3.Error as exc:
            logger.error("get_last_trade DB error: %s", exc)
            return None
        finally:
            conn.close()


    def get_open_trade_by_symbol(self, symbol: str) -> Optional[dict]:
        """
        Recupera l'ultimo trade OPEN per un simbolo specifico.
        Usato da _record_closed_trades in main.py per trovare il ticket.
        """
        conn = _get_connection()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM trades
                WHERE symbol = ? AND result = 'OPEN'
                ORDER BY id DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as exc:
            logger.error("get_open_trade_by_symbol DB error: %s", exc)
            return None
        finally:
            conn.close()

    def get_open_trades(self) -> list[dict]:
        """
        Recupera tutti i trade ancora segnati come OPEN nel database.
        Usato per la riconciliazione all'avvio.

        Returns
        -------
        list of dict
            Liste di trade con chiavi: id, symbol, ticket, direction, entry,
            stop_loss, take_profit, lot_size, open_time, confidence, regime
        """
        conn = _get_connection()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM trades
                WHERE result = 'OPEN'
                ORDER BY open_time ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            logger.error("get_open_trades DB error: %s", exc)
            return []
        finally:
            conn.close()

    def update_closed_trade(
        self,
        ticket: int,
        exit_price: float,
        close_time: str,
        exit_reason: str = "closed_at_market",
        pnl: float = 0.0,
    ) -> bool:
        """
        Aggiorna un trade nel database dopo che è stato chiuso su MT5.
        Calcola il result (WIN/LOSS) basato sul PnL.

        Parameters
        ----------
        ticket : int
            Position ticket di MT5
        exit_price : float
            Prezzo di chiusura
        close_time : str
            Timestamp della chiusura (ISO format)
        exit_reason : str
            Motivo della chiusura (e.g. 'take_profit', 'stop_loss', 'trailing_stop')
        pnl : float
            Profitto/perdita in valuta

        Returns
        -------
        bool
            True se l'update è andato a buon fine
        """
        conn = _get_connection()
        try:
            # Determina il result basato sul PnL
            result = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
            
            # Recupera il trade per calcolare R/R
            row = conn.execute(
                """
                SELECT entry, stop_loss, direction, lot_size FROM trades
                WHERE ticket = ? AND result = 'OPEN'
                """,
                (ticket,),
            ).fetchone()
            
            if row is None:
                logger.warning("update_closed_trade: ticket %s non trovato nel DB", ticket)
                return False
            
            entry, sl, direction, lot_size = row
            
            # Calcola il risk/reward
            sl_dist = abs(entry - sl)
            if sl_dist > 0:
                if direction.upper() == "BUY":
                    profit_dist = exit_price - entry
                else:
                    profit_dist = entry - exit_price
                risk_reward = profit_dist / sl_dist if sl_dist > 0 else 0.0
            else:
                risk_reward = 0.0
            
            # Aggiorna il trade
            cursor = conn.execute(
                """
                UPDATE trades
                SET result      = ?,
                    pnl         = ?,
                    risk_reward = ?,
                    close_time  = ?,
                    exit_reason = ?,
                    exit_price  = ?
                WHERE ticket = ? AND result = 'OPEN'
                """,
                (result, pnl, risk_reward, close_time, exit_reason, exit_price, ticket),
            )
            conn.commit()
            
            if cursor.rowcount > 0:
                logger.info(
                    "Trade riconciliato | ticket=%s result=%s pnl=%.2f R/R=%.2f",
                    ticket, result, pnl, risk_reward
                )
                return True
            else:
                logger.warning("update_closed_trade: nessun trade aggiornato per ticket %s", ticket)
                return False
                
        except sqlite3.Error as exc:
            logger.error("update_closed_trade DB error: %s", exc)
            return False
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Helper privato
# ---------------------------------------------------------------------------

def _get_connection() -> sqlite3.Connection:
    db_dir = os.path.dirname(config.DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    return sqlite3.connect(config.DB_PATH)
