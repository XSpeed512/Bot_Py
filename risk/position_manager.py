"""
risk/position_manager.py
========================
Gestione automatica dello stop-loss sulle posizioni aperte.

Invariato rispetto all'originale — la logica break-even e trailing
rimane su MT5. Il nuovo RiskEngine adattivo viene usato solo per
il calcolo iniziale del trade; una volta aperta la posizione,
questo modulo si occupa della sua gestione su MT5.

Due stadi progressivi:
  Stage 1 – Break-even (+1R): SL portato a entry price
  Stage 2 – Trailing stop  (+2R): trail a distanza ATR × TRAILING_ATR_MULT
"""

from __future__ import annotations
from typing import Optional

import MetaTrader5 as mt5

import config
from execution.broker_connector import get_positions, modify_sl, get_price
from utils.logger import get_logger

logger = get_logger(__name__)


def manage_positions(atr_map: dict[str, float]) -> None:
    """
    Esamina ogni posizione aperta e applica la logica break-even / trailing.

    Parameters
    ----------
    atr_map : dict[str, float]
        Dizionario symbol → ATR corrente, calcolato dal loop principale
        per evitare fetch ridondanti.
        Esempio: {"EURUSD": 0.00082, "XAUUSD": 4.35}
    """
    positions = get_positions()

    for pos in positions:
        symbol = pos.symbol
        atr    = atr_map.get(symbol, 0.0)

        if atr == 0.0:
            logger.debug("manage_positions: ATR non disponibile per %s — skip", symbol)
            continue

        _apply_sl_management(pos, atr)


def _apply_sl_management(pos, atr: float) -> None:
    """Valuta una singola posizione e sposta lo SL se le condizioni sono soddisfatte."""
    is_buy = pos.type == mt5.ORDER_TYPE_BUY

    # Calcola il rischio iniziale (1R) dall'SL originale
    if pos.sl != 0.0:
        initial_risk = abs(pos.price_open - pos.sl)
    else:
        initial_risk = atr * config.SL_ATR_MULTIPLIER

    if initial_risk == 0.0:
        return

    current_price = _get_current_price(pos.symbol, is_buy)
    if current_price is None:
        return

    profit_distance = (current_price - pos.price_open) if is_buy else (pos.price_open - current_price)
    r_multiple      = profit_distance / initial_risk

    # ── Stage 2: Trailing stop (+2R) ──────────────────────────────
    if r_multiple >= config.TRAILING_START_R:
        trailing_distance = atr * config.TRAILING_ATR_MULT

        if is_buy:
            new_sl = current_price - trailing_distance
            if new_sl > pos.sl:
                logger.info("TRAILING SL | %s ticket=%s | %.5f → %.5f (R=%.2f)",
                            pos.symbol, pos.ticket, pos.sl, new_sl, r_multiple)
                modify_sl(pos.ticket, new_sl, pos.symbol)
        else:
            new_sl = current_price + trailing_distance
            if new_sl < pos.sl or pos.sl == 0.0:
                logger.info("TRAILING SL | %s ticket=%s | %.5f → %.5f (R=%.2f)",
                            pos.symbol, pos.ticket, pos.sl, new_sl, r_multiple)
                modify_sl(pos.ticket, new_sl, pos.symbol)
        return   # Trailing ha precedenza sul break-even

    # ── Stage 1: Break-even (+1R) ──────────────────────────────────
    if r_multiple >= config.BREAKEVEN_R:
        entry = pos.price_open

        if is_buy and pos.sl < entry:
            logger.info("BREAK-EVEN | %s ticket=%s | SL %.5f → %.5f (R=%.2f)",
                        pos.symbol, pos.ticket, pos.sl, entry, r_multiple)
            modify_sl(pos.ticket, entry, pos.symbol)

        elif not is_buy and (pos.sl > entry or pos.sl == 0.0):
            logger.info("BREAK-EVEN | %s ticket=%s | SL %.5f → %.5f (R=%.2f)",
                        pos.symbol, pos.ticket, pos.sl, entry, r_multiple)
            modify_sl(pos.ticket, entry, pos.symbol)


def _get_current_price(symbol: str, is_buy: bool) -> Optional[float]:
    price_info = get_price(symbol)
    if price_info is None:
        return None
    return price_info["bid"] if is_buy else price_info["ask"]
