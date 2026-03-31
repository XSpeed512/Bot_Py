"""
risk/risk_manager.py
=====================
Capital protection and position sizing.

Responsibilities
----------------
* Calculate the correct lot size for a given risk percentage.
* Enforce the maximum simultaneous open-trade limit.
* Enforce daily and weekly loss limits (stop-trading circuit breakers).

The daily/weekly drawdown tracking is held in memory (reset at bot
startup).  For production use, persist these values to the SQLite
database so they survive restarts.
"""

from __future__ import annotations

from datetime import datetime, date, timedelta

import MetaTrader5 as mt5

import config
from execution.broker_connector import (
    get_account_balance,
    get_account_equity,
    get_positions,
)
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# In-memory state (reset on bot start)
# ---------------------------------------------------------------------------

class _DrawdownTracker:
    """
    Simple in-memory tracker for daily and weekly drawdown.

    At initialisation it records the starting balance (or equity)
    so it can measure loss as a percentage of that baseline.
    """

    def __init__(self) -> None:
        self._start_balance: float = 0.0
        self._week_start_balance: float = 0.0
        self._last_reset_day: date = date.min
        self._last_reset_week: date = date.min

    def initialise(self, balance: float) -> None:
        """Set the reference balances at bot startup."""
        self._start_balance      = balance
        self._week_start_balance = balance
        self._last_reset_day     = date.today()
        self._last_reset_week    = date.today()
        logger.info(
            "DrawdownTracker initialised | Balance: %.2f", balance
        )

    def _maybe_reset(self) -> None:
        """Roll-over reference balance at the start of a new day / week."""
        today = date.today()

        if today > self._last_reset_day:
            # New trading day – reset daily reference
            balance = get_account_balance()
            logger.info(
                "New trading day – resetting daily drawdown tracker. Balance: %.2f", balance
            )
            self._start_balance  = balance
            self._last_reset_day = today

        # Monday = weekday 0
        if today.weekday() == 0 and today > self._last_reset_week:
            balance = get_account_balance()
            logger.info(
                "New trading week – resetting weekly drawdown tracker. Balance: %.2f", balance
            )
            self._week_start_balance = balance
            self._last_reset_week    = today

    @property
    def daily_loss_pct(self) -> float:
        """Return today's loss as a positive percentage (0 if in profit)."""
        self._maybe_reset()
        equity = get_account_equity()
        loss   = self._start_balance - equity
        return max(0.0, (loss / self._start_balance) * 100) if self._start_balance else 0.0

    @property
    def weekly_loss_pct(self) -> float:
        """Return this week's loss as a positive percentage."""
        self._maybe_reset()
        equity = get_account_equity()
        loss   = self._week_start_balance - equity
        return max(0.0, (loss / self._week_start_balance) * 100) if self._week_start_balance else 0.0


# Module-level singleton
_tracker = _DrawdownTracker()


def initialise_tracker() -> None:
    """Call once at bot startup to set the baseline balance."""
    _tracker.initialise(get_account_balance())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_trading_allowed() -> bool:
    """
    Check all circuit-breaker conditions.

    Returns
    -------
    bool
        False if trading should be paused (daily/weekly limit breached
        or too many open trades).
    """
    # Daily loss limit
    daily_loss = _tracker.daily_loss_pct
    if daily_loss >= config.DAILY_LOSS_LIMIT_PCT:
        logger.warning(
            "DAILY LOSS LIMIT REACHED (%.2f%% >= %.2f%%) – trading halted for today.",
            daily_loss, config.DAILY_LOSS_LIMIT_PCT,
        )
        return False

    # Weekly loss limit
    weekly_loss = _tracker.weekly_loss_pct
    if weekly_loss >= config.WEEKLY_LOSS_LIMIT_PCT:
        logger.warning(
            "WEEKLY LOSS LIMIT REACHED (%.2f%% >= %.2f%%) – trading halted for the week.",
            weekly_loss, config.WEEKLY_LOSS_LIMIT_PCT,
        )
        return False

    # Max open trades
    open_trades = len(get_positions())
    if open_trades >= config.MAX_OPEN_TRADES:
        logger.info(
            "MAX OPEN TRADES REACHED (%d) – skipping new entries.",
            open_trades,
        )
        return False

    return True


def calculate_lot_size(
    symbol: str,
    stop_loss_distance: float,
) -> float:
    """
    Calculate the position size (in lots) that risks exactly
    ``RISK_PER_TRADE_PCT`` percent of the current account balance.

    Formula
    -------
    ::
        monetary_risk = balance * (risk_pct / 100)
        pip_value     = contract_size * tick_value / tick_size
        lots          = monetary_risk / (stop_distance_in_ticks * pip_value)

    Parameters
    ----------
    symbol             : str    Instrument name.
    stop_loss_distance : float  Absolute distance between entry and SL.

    Returns
    -------
    float  Rounded lot size, clipped to broker min/max. Returns 0.0 on error.
    """
    balance = get_account_balance()
    if balance <= 0 or stop_loss_distance <= 0:
        logger.error(
            "calculate_lot_size: invalid inputs – balance=%.2f, sl_dist=%.5f",
            balance, stop_loss_distance,
        )
        return 0.0

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        logger.error("Symbol info not available for %s", symbol)
        return 0.0

    monetary_risk   = balance * (config.RISK_PER_TRADE_PCT / 100.0)
    tick_size       = symbol_info.trade_tick_size
    tick_value      = symbol_info.trade_tick_value
    contract_size   = symbol_info.trade_contract_size

    if tick_size == 0 or tick_value == 0:
        logger.error("tick_size or tick_value is 0 for %s – cannot size position", symbol)
        return 0.0

    # Convert SL distance to ticks
    sl_in_ticks = stop_loss_distance / tick_size

    # Value per lot for the SL distance
    sl_value_per_lot = sl_in_ticks * tick_value

    if sl_value_per_lot == 0:
        return 0.0

    lots = monetary_risk / sl_value_per_lot

    # Clip to broker limits
    lots = max(symbol_info.volume_min, min(lots, symbol_info.volume_max))

    # Round to broker step
    step = symbol_info.volume_step
    lots = round(round(lots / step) * step, 2)

    logger.debug(
        "Lot size calc | %s | balance=%.2f risk=%.2f sl_dist=%.5f → %.2f lots",
        symbol, balance, monetary_risk, stop_loss_distance, lots,
    )
    return lots


def calculate_sl_tp(
    symbol: str,
    signal: str,
    entry_price: float,
    atr: float,
) -> tuple[float, float]:
    """
    Compute absolute SL and TP prices from the entry price and ATR.

    Parameters
    ----------
    symbol       : str    Instrument name (for digit rounding).
    signal       : str    ``"BUY"`` or ``"SELL"``.
    entry_price  : float  Expected fill price.
    atr          : float  Current ATR value.

    Returns
    -------
    tuple (stop_loss, take_profit)
    """
    symbol_info = mt5.symbol_info(symbol)
    digits = symbol_info.digits if symbol_info else 5

    sl_distance = atr * config.SL_ATR_MULTIPLIER
    tp_distance = sl_distance * config.RISK_REWARD_RATIO

    if signal == "BUY":
        stop_loss   = round(entry_price - sl_distance, digits)
        take_profit = round(entry_price + tp_distance, digits)
    else:  # SELL
        stop_loss   = round(entry_price + sl_distance, digits)
        take_profit = round(entry_price - tp_distance, digits)

    return stop_loss, take_profit
