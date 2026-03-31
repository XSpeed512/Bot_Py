"""
strategy/entry_strategy.py
===========================
Entry signal generator.

Implements a trend-following / mean-reversion hybrid strategy:

* Trend filter  : EMA50 vs EMA200 establishes direction.
* Pullback      : RSI identifies short-term exhaustion within the trend.
* Volatility    : ATR filter ensures adequate market movement before entry.

Signal values
-------------
``"BUY"``  – Long entry conditions met.
``"SELL"`` – Short entry conditions met.
``"NONE"`` – No tradeable setup at this time.
"""

from __future__ import annotations

import pandas as pd

import config
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------

def generate_signal(df: pd.DataFrame, symbol: str) -> str:
    """
    Analyse the most recent closed candle and return a trade signal.

    The DataFrame must already contain indicator columns produced by
    ``data.market_data.add_indicators()``.

    Entry Rules
    -----------
    BUY:
        1. EMA50 > EMA200         (uptrend)
        2. RSI < RSI_BUY_LEVEL    (oversold / pullback)
        3. ATR > ATR_avg * ATR_FILTER_MULTIPLIER  (volatility confirmation)

    SELL:
        1. EMA50 < EMA200         (downtrend)
        2. RSI > RSI_SELL_LEVEL   (overbought / pullback)
        3. ATR > ATR_avg * ATR_FILTER_MULTIPLIER  (volatility confirmation)

    Parameters
    ----------
    df     : pd.DataFrame  Prepared OHLC + indicator data.
    symbol : str           Used only for logging.

    Returns
    -------
    str  ``"BUY"``, ``"SELL"``, or ``"NONE"``
    """
    if df is None or len(df) < 2:
        logger.warning("%s: Insufficient data for signal generation.", symbol)
        return "NONE"

    # Reference the last fully-closed bar
    last = df.iloc[-1]

    ema_fast = last["ema_fast"]
    ema_slow = last["ema_slow"]
    rsi      = last["rsi"]
    atr      = last["atr"]
    atr_avg  = last["atr_avg"]

    # ------------------------------------------------------------------
    # Guard: ensure indicator values are valid numbers
    # ------------------------------------------------------------------
    for name, value in [("ema_fast", ema_fast), ("ema_slow", ema_slow),
                        ("rsi", rsi), ("atr", atr), ("atr_avg", atr_avg)]:
        if pd.isna(value):
            logger.debug("%s: %s is NaN – skipping signal.", symbol, name)
            return "NONE"

    # ------------------------------------------------------------------
    # Volatility filter (shared by both directions)
    # ------------------------------------------------------------------
    volatility_ok = atr >= (atr_avg * config.ATR_FILTER_MULTIPLIER)

    # ------------------------------------------------------------------
    # BUY conditions
    # ------------------------------------------------------------------
    trend_up    = ema_fast > ema_slow
    rsi_pullback_buy = rsi < config.RSI_BUY_LEVEL

    if trend_up and rsi_pullback_buy and volatility_ok:
        logger.info(
            "SIGNAL BUY | %s | EMA50=%.5f EMA200=%.5f RSI=%.1f ATR=%.5f",
            symbol, ema_fast, ema_slow, rsi, atr,
        )
        return "BUY"

    # ------------------------------------------------------------------
    # SELL conditions
    # ------------------------------------------------------------------
    trend_down       = ema_fast < ema_slow
    rsi_pullback_sell = rsi > config.RSI_SELL_LEVEL

    if trend_down and rsi_pullback_sell and volatility_ok:
        logger.info(
            "SIGNAL SELL | %s | EMA50=%.5f EMA200=%.5f RSI=%.1f ATR=%.5f",
            symbol, ema_fast, ema_slow, rsi, atr,
        )
        return "SELL"

    # ------------------------------------------------------------------
    # No setup
    # ------------------------------------------------------------------
    logger.debug(
        "SIGNAL NONE | %s | trend_up=%s trend_dn=%s rsi=%.1f vol_ok=%s",
        symbol, trend_up, trend_down, rsi, volatility_ok,
    )
    return "NONE"


# ---------------------------------------------------------------------------
# Helpers used by risk module
# ---------------------------------------------------------------------------

def get_atr(df: pd.DataFrame) -> float:
    """Return the ATR value of the last closed bar, or 0.0."""
    if df is None or len(df) == 0:
        return 0.0
    return float(df.iloc[-1]["atr"])


def get_last_close(df: pd.DataFrame) -> float:
    """Return the closing price of the last closed bar, or 0.0."""
    if df is None or len(df) == 0:
        return 0.0
    return float(df.iloc[-1]["close"])
