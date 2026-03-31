"""
risk/position_manager.py
========================
Automatic stop-loss management for open positions.

Implements two progressive SL adjustment stages:

Stage 1 – Break-even
    When floating profit reaches +1R (one unit of initial risk),
    the SL is moved to the entry price, eliminating risk on the trade.

Stage 2 – Trailing stop
    When floating profit reaches +2R, a trailing stop is activated
    that follows price at a distance of ATR × TRAILING_ATR_MULT.

Both stages use the MT5 ``modify_sl()`` call to update the live order.

Public functions
----------------
manage_positions()   – Iterate open positions and apply SL rules.
"""

from __future__ import annotations

from typing import Optional

import MetaTrader5 as mt5

import config
from execution.broker_connector import get_positions, modify_sl, get_price
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Main entry point called from the bot loop
# ---------------------------------------------------------------------------

def manage_positions(atr_map: dict[str, float]) -> None:
    """
    Review every open position and apply break-even / trailing logic.

    Parameters
    ----------
    atr_map : dict[str, float]
        Dictionary mapping symbol → current ATR value, pre-computed
        by the main loop to avoid redundant data fetches.
        Example: ``{"EURUSD": 0.00082, "XAUUSD": 4.35}``
    """
    positions = get_positions()

    for pos in positions:
        symbol = pos.symbol
        atr    = atr_map.get(symbol, 0.0)

        if atr == 0.0:
            logger.debug(
                "manage_positions: ATR not available for %s – skipping.", symbol
            )
            continue

        _apply_sl_management(pos, atr)


# ---------------------------------------------------------------------------
# Per-position logic
# ---------------------------------------------------------------------------

def _apply_sl_management(pos, atr: float) -> None:
    """
    Evaluate a single position and move SL if conditions are met.

    Parameters
    ----------
    pos : MT5 position object
    atr : float  Current ATR for the symbol.
    """
    is_buy = pos.type == mt5.ORDER_TYPE_BUY

    # ------------------------------------------------------------------
    # Determine the initial risk distance (1R)
    # We infer it from the difference between entry price and original SL.
    # If SL has already been moved to break-even we fall back to ATR-based 1R.
    # ------------------------------------------------------------------
    if pos.sl != 0.0:
        initial_risk = abs(pos.price_open - pos.sl)
    else:
        # No SL set (shouldn't happen, but be defensive)
        initial_risk = atr * config.SL_ATR_MULTIPLIER

    if initial_risk == 0.0:
        return

    # ------------------------------------------------------------------
    # Current floating profit in price units (R-multiple)
    # ------------------------------------------------------------------
    current_price = _get_current_price(pos.symbol, is_buy)
    if current_price is None:
        return

    if is_buy:
        profit_distance = current_price - pos.price_open
    else:
        profit_distance = pos.price_open - current_price

    r_multiple = profit_distance / initial_risk

    # ------------------------------------------------------------------
    # Stage 2: Trailing stop (activates at +2R)
    # ------------------------------------------------------------------
    if r_multiple >= config.TRAILING_START_R:
        trailing_distance = atr * config.TRAILING_ATR_MULT

        if is_buy:
            new_sl = current_price - trailing_distance
            # Only move SL upward (never widen)
            if new_sl > pos.sl:
                logger.info(
                    "TRAILING SL | %s ticket=%s | %.5f → %.5f (R=%.2f)",
                    pos.symbol, pos.ticket, pos.sl, new_sl, r_multiple,
                )
                modify_sl(pos.ticket, new_sl, pos.symbol)
        else:
            new_sl = current_price + trailing_distance
            # Only move SL downward (never widen)
            if new_sl < pos.sl or pos.sl == 0.0:
                logger.info(
                    "TRAILING SL | %s ticket=%s | %.5f → %.5f (R=%.2f)",
                    pos.symbol, pos.ticket, pos.sl, new_sl, r_multiple,
                )
                modify_sl(pos.ticket, new_sl, pos.symbol)

        return   # Trailing takes precedence over break-even

    # ------------------------------------------------------------------
    # Stage 1: Break-even (activates at +1R)
    # ------------------------------------------------------------------
    if r_multiple >= config.BREAKEVEN_R:
        entry = pos.price_open

        if is_buy and pos.sl < entry:
            logger.info(
                "BREAK-EVEN | %s ticket=%s | Moving SL %.5f → %.5f (R=%.2f)",
                pos.symbol, pos.ticket, pos.sl, entry, r_multiple,
            )
            modify_sl(pos.ticket, entry, pos.symbol)

        elif not is_buy and (pos.sl > entry or pos.sl == 0.0):
            logger.info(
                "BREAK-EVEN | %s ticket=%s | Moving SL %.5f → %.5f (R=%.2f)",
                pos.symbol, pos.ticket, pos.sl, entry, r_multiple,
            )
            modify_sl(pos.ticket, entry, pos.symbol)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_current_price(symbol: str, is_buy: bool) -> Optional[float]:
    """
    Return the price used to measure floating P&L.
    BUY positions are valued at Bid; SELL positions at Ask.
    """
    price_info = get_price(symbol)
    if price_info is None:
        return None
    return price_info["bid"] if is_buy else price_info["ask"]
