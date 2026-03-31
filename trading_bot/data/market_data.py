"""
data/market_data.py
===================
Market data retrieval and indicator calculation.

This module is the single point of contact for historical OHLC data.
It downloads candles from MT5, converts them to a pandas DataFrame,
and calculates all technical indicators required by the strategy.

Public functions
----------------
get_ohlc()            – Download raw OHLC candles.
add_indicators()      – Compute EMA, RSI, ATR on a DataFrame.
get_prepared_data()   – Convenience: fetch + compute in one call.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import MetaTrader5 as mt5

import config
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Candle retrieval
# ---------------------------------------------------------------------------

def get_ohlc(
    symbol: str,
    timeframe: int = config.TIMEFRAME,
    n_candles: int = config.CANDLES_LOOKBACK,
) -> Optional[pd.DataFrame]:
    """
    Download the last *n_candles* OHLC bars for *symbol* from MT5.

    Parameters
    ----------
    symbol     : str  Instrument name.
    timeframe  : int  MT5 timeframe constant (e.g. mt5.TIMEFRAME_H1).
    n_candles  : int  Number of historical bars to fetch.

    Returns
    -------
    pd.DataFrame with columns [time, open, high, low, close, tick_volume]
    indexed by time, or None on failure.
    """
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n_candles)

    if rates is None or len(rates) == 0:
        logger.error(
            "No data returned for %s (TF=%s) – error: %s",
            symbol, timeframe, mt5.last_error(),
        )
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)

    # Keep only the columns we need
    df = df[["open", "high", "low", "close", "volume"]].copy()

    logger.debug("Fetched %d candles for %s", len(df), symbol)
    return df


# ---------------------------------------------------------------------------
# Indicator calculation
# ---------------------------------------------------------------------------

def _ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (uses pandas ewm for speed)."""
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder's RSI.

    Uses an exponentially-weighted moving average of gains/losses,
    matching the industry-standard Wilder smoothing method.
    """
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)   # Neutral fill for initial NaN values


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range.

    True range = max(high-low, |high-prev_close|, |low-prev_close|)
    """
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift(1)).abs()
    lpc = (df["low"]  - df["close"].shift(1)).abs()

    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return atr


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute and append all required indicators to *df*.

    Columns added
    -------------
    ema_fast   – EMA with period config.EMA_FAST  (default 50)
    ema_slow   – EMA with period config.EMA_SLOW  (default 200)
    rsi        – RSI-14
    atr        – ATR-14
    atr_avg    – Rolling mean of ATR (same window) for the ATR filter

    Parameters
    ----------
    df : pd.DataFrame
        Must contain columns: open, high, low, close.

    Returns
    -------
    pd.DataFrame with indicator columns appended (in-place modification
    is avoided – a copy is returned).
    """
    df = df.copy()

    df["ema_fast"] = _ema(df["close"], config.EMA_FAST)
    df["ema_slow"] = _ema(df["close"], config.EMA_SLOW)
    df["rsi"]      = _rsi(df["close"], config.RSI_PERIOD)
    df["atr"]      = _atr(df,          config.ATR_PERIOD)

    # Rolling average of ATR used for the volatility filter
    df["atr_avg"]  = df["atr"].rolling(window=config.ATR_PERIOD).mean()

    return df


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------

def get_prepared_data(
    symbol: str,
    timeframe: int = config.TIMEFRAME,
    n_candles: int = config.CANDLES_LOOKBACK,
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLC data and immediately compute all indicators.

    Returns the last fully-closed candle (excludes the live forming bar)
    so signals are not recalculated on partial data.

    Parameters
    ----------
    symbol    : str  Instrument name.
    timeframe : int  MT5 timeframe constant.
    n_candles : int  Lookback period.

    Returns
    -------
    pd.DataFrame or None
    """
    df = get_ohlc(symbol, timeframe, n_candles)
    if df is None:
        return None

    df = add_indicators(df)

    # Drop the last bar (currently forming / incomplete)
    df = df.iloc[:-1]

    # Drop rows with NaN from indicator warm-up
    df.dropna(inplace=True)

    return df
