"""
execution/reconciliation.py
============================
Riconciliazione dei trade all'avvio del bot.

Controlla quali trade aperti dal bot sono stati chiusi su MT5
mentre il bot era offline e aggiorna il database locale.

FIX rispetto alla versione precedente:
  - Accetta tracker come parametro per registrare i trade riconciliati
    nelle statistiche di self-improvement (era assente)
  - Tolleranza _determine_exit_reason aumentata (0.00001 → symbol-aware)
    per gestire correttamente XAUUSD (~0.01) e indici (~0.1)
  - Aggiunto riconoscimento "trailing_stop" quando SL è stato modificato
  - market_data passato per recuperare contract_size e calcolare R corretto
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

import MetaTrader5 as mt5

from utils.logger import get_logger
from execution.broker_connector import get_closed_deal_by_ticket, get_positions
from ai.learning_module import LearningModule

logger = get_logger(__name__)


def reconcile_trades(
    learning_mod: LearningModule,
    tracker=None,        # PerformanceTracker — opzionale per compatibilità
    market_data=None,    # MarketData — opzionale, usato per contract_size
) -> dict:
    """
    Riconcilia i trade aperti nel DB al riavvio del bot.

    Logica:
    1. Recupera dal DB tutti i trade segnati come OPEN
    2. Per ogni trade OPEN:
       - Controlla se è ancora aperto su MT5
       - Se NO, recupera il deal chiuso dalla history
       - Aggiorna il DB con i dati di chiusura
       - Aggiorna il PerformanceTracker se disponibile
    3. Restituisce un report con i trade riconciliati
    """
    logger.info("=" * 60)
    logger.info("INIZIO RICONCILIAZIONE TRADE")
    logger.info("=" * 60)

    open_trades = learning_mod.get_open_trades()
    logger.info("Trade aperti nel DB: %d", len(open_trades))

    if not open_trades:
        logger.info("Nessun trade aperto — riconciliazione completata")
        return {"reconciled": 0, "still_open": 0, "failed": 0, "trades": []}

    current_positions = get_positions()
    open_tickets = {p.ticket for p in current_positions}
    logger.info("Posizioni aperte su MT5: %d", len(open_tickets))

    reconciled       = []
    still_open_count = 0
    failed_count     = 0

    for trade in open_trades:
        ticket      = trade.get("ticket")
        symbol      = trade.get("symbol", "UNKNOWN")
        direction   = trade.get("direction", "BUY")
        entry_price = trade.get("entry", 0.0)

        if ticket in open_tickets:
            logger.debug("Trade %d (%s) ancora aperto su MT5", ticket, symbol)
            still_open_count += 1
            continue

        closed_deal = get_closed_deal_by_ticket(ticket)

        if closed_deal is None:
            logger.warning(
                "Trade %d (%s) non trovato nella history MT5 — impossibile riconciliare",
                ticket, symbol,
            )
            failed_count += 1
            continue

        exit_price = closed_deal["close_price"]
        close_time = closed_deal["close_time"]
        profit     = closed_deal["profit"]

        # FIX: determina exit_reason con tolleranza symbol-aware
        exit_reason = _determine_exit_reason(trade, exit_price, symbol)

        # Converti close_time a ISO
        if isinstance(close_time, (int, float)):
            close_dt       = datetime.fromtimestamp(close_time, tz=timezone.utc)
            close_time_iso = close_dt.isoformat()
        else:
            close_time_iso = str(close_time) if close_time else datetime.now(tz=timezone.utc).isoformat()

        updated = learning_mod.update_closed_trade(
            ticket=ticket,
            exit_price=exit_price,
            close_time=close_time_iso,
            exit_reason=exit_reason,
            pnl=profit,
        )

        if updated:
            reconciled.append({
                "ticket":     ticket,
                "symbol":     symbol,
                "direction":  direction,
                "entry":      entry_price,
                "exit":       exit_price,
                "pnl":        profit,
                "reason":     exit_reason,
                "close_time": close_time_iso,
            })
            logger.info(
                "✅ Riconciliato | %s ticket=%d dir=%s entry=%.5f exit=%.5f pnl=%.2f reason=%s",
                symbol, ticket, direction, entry_price, exit_price, profit, exit_reason,
            )

            # FIX: registra anche nel PerformanceTracker se disponibile
            if tracker is not None:
                _register_in_tracker(
                    tracker, trade, exit_price, profit, close_time_iso, exit_reason, market_data
                )
        else:
            logger.error("❌ Errore update DB | %s ticket=%d", symbol, ticket)
            failed_count += 1

    logger.info("=" * 60)
    logger.info("RISULTATI RICONCILIAZIONE")
    logger.info("  Riconciliati:   %d", len(reconciled))
    logger.info("  Ancora aperti:  %d", still_open_count)
    logger.info("  Falliti:        %d", failed_count)
    logger.info("=" * 60)

    return {
        "reconciled": len(reconciled),
        "still_open": still_open_count,
        "failed":     failed_count,
        "trades":     reconciled,
    }


def _register_in_tracker(
    tracker,
    trade: dict,
    exit_price: float,
    profit: float,
    close_time_iso: str,
    exit_reason: str,
    market_data,
) -> None:
    """
    Registra un trade riconciliato nel PerformanceTracker.
    Importa TradeRecord qui per evitare dipendenze circolari.
    """
    try:
        from self_improve.tracker import TradeRecord

        symbol    = trade.get("symbol", "UNKNOWN")
        entry     = trade.get("entry", 0.0)
        sl        = trade.get("stop_loss", 0.0)
        volume    = trade.get("lot_size", 0.0)
        direction = trade.get("direction", "BUY").lower()
        regime    = trade.get("regime") or "unknown"

        sl_dist = abs(entry - sl)

        # Contract size da MT5
        contract_size = 100000  # default forex
        if market_data is not None:
            try:
                info = mt5.symbol_info(symbol)
                if info:
                    contract_size = info.trade_contract_size
            except Exception:
                pass

        r_mult = (
            profit / (sl_dist * volume * contract_size)
            if sl_dist > 0 and volume > 0 and contract_size > 0
            else 0.0
        )

        # Balance corrente dal tracker (approssimato)
        current_balance = tracker.current_balance if tracker.current_balance > 0 else 0.0

        record = TradeRecord(
            trade_id=str(uuid.uuid4())[:8],
            symbol=symbol,
            direction=direction,
            regime=regime,
            entry_price=entry,
            exit_price=exit_price,
            position_size=volume,
            stop_loss=sl,
            take_profit=trade.get("take_profit", 0.0),
            pnl=round(profit, 2),
            pnl_pct=profit / (entry * volume) if entry > 0 and volume > 0 else 0.0,
            r_multiple=round(r_mult, 3),
            duration_bars=0,
            entry_time=trade.get("open_time", ""),
            exit_time=close_time_iso,
            confidence=trade.get("confidence") or 0.0,
            trade_quality=0.0,
            risk_score=0.0,
            was_winner=profit > 0,
            exit_reason=exit_reason,
        )
        tracker.record_trade(record, current_balance)

    except Exception as e:
        logger.error("_register_in_tracker errore: %s", e)


def _determine_exit_reason(trade: dict, exit_price: float, symbol: str = "") -> str:
    """
    Determina il motivo della chiusura basandosi su entry, SL, TP e exit_price.

    FIX: tolleranza symbol-aware invece di 0.00001 fisso.
    - Forex (EURUSD, GBPUSD): tolleranza = 0.0001  (1 pip)
    - XAUUSD: tolleranza = 0.10  (10 cents)
    - Indici (US500, USTEC, US30): tolleranza = 0.50

    Aggiunto: riconoscimento trailing_stop quando SL sembra spostato
    (exit tra entry e SL originale per LONG, o viceversa per SHORT).
    """
    entry     = trade.get("entry", 0.0)
    sl        = trade.get("stop_loss", 0.0)
    tp        = trade.get("take_profit", 0.0)
    direction = trade.get("direction", "BUY").upper()

    # Tolleranza symbol-aware
    sym_upper = symbol.upper()
    if "XAU" in sym_upper or "GOLD" in sym_upper:
        tolerance = 0.10
    elif any(idx in sym_upper for idx in ["US500", "USTEC", "US30", "XUS", "NAS", "SPX", "DOW"]):
        tolerance = 0.50
    elif "JPY" in sym_upper:
        tolerance = 0.010   # JPY ha meno decimali
    else:
        tolerance = 0.0002  # Standard forex: ~2 pip

    if direction == "BUY":
        if tp > 0 and abs(exit_price - tp) < tolerance:
            return "take_profit"
        if sl > 0 and abs(exit_price - sl) < tolerance:
            return "stop_loss"
        # Trailing stop: chiuso in perdita ma sopra lo SL originale (SL era stato spostato)
        if sl > 0 and exit_price < entry and exit_price > sl:
            return "trailing_stop"
        if exit_price < entry:
            return "stop_loss"
        return "manual"
    else:  # SELL
        if tp > 0 and abs(exit_price - tp) < tolerance:
            return "take_profit"
        if sl > 0 and abs(exit_price - sl) < tolerance:
            return "stop_loss"
        # Trailing stop per SHORT
        if sl > 0 and exit_price > entry and exit_price < sl:
            return "trailing_stop"
        if exit_price > entry:
            return "stop_loss"
        return "manual"
