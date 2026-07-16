"""
core/mtf.py
===========
Multi-Timeframe Confirmation Engine

Principle:
  - Higher timeframe (1H) sets the BIAS — never trade against it
  - Lower timeframe (5M) provides ENTRY PRECISION
  - Both must agree before a signal is generated

This prevents the classic retail mistake of entering on noise
while the higher-order trend is pointing the opposite direction.

No lookahead bias: all decisions use iloc[-1] (last CLOSED bar).
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)


class Bias(str, Enum):
    LONG  = "long"
    SHORT = "short"
    FLAT  = "flat"


@dataclass
class MTFResult:
    htf_bias: Bias
    ltf_bias: Bias
    aligned: bool        # True when HTF and LTF agree
    htf_ema_fast: float
    htf_ema_slow: float
    ltf_ema_fast: float
    ltf_ema_slow: float
    htf_rsi: float
    ltf_rsi: float
    alignment_score: float   # 0–1: strength of alignment


class MTFEngine:
    """
    Compares trend bias across two timeframes.
    Called with pre-fetched OHLCV DataFrames for each timeframe.
    """

    def __init__(self, cfg: dict):
        ind = cfg["indicators"]
        self.ema_fast   = ind["ema_fast"]
        self.ema_slow   = ind["ema_slow"]
        self.ema_signal = ind["ema_signal"]
        self.rsi_period = ind["rsi_period"]

    # ─── Public API ────────────────────────────────────────────────

    def analyse(
        self,
        df_htf: pd.DataFrame,
        df_ltf: pd.DataFrame,
    ) -> MTFResult:
        """
        Parameters
        ----------
        df_htf : OHLCV on higher timeframe (e.g. 1H)
        df_ltf : OHLCV on lower timeframe  (e.g. 5M)
        """
        htf_bias, htf_ema_f, htf_ema_s, htf_rsi = self._analyse_single(df_htf)
        ltf_bias, ltf_ema_f, ltf_ema_s, ltf_rsi = self._analyse_single(df_ltf)

        aligned = htf_bias == ltf_bias and htf_bias != Bias.FLAT

        # Alignment score: how strongly do both TFs agree?
        alignment_score = self._alignment_score(
            df_htf, df_ltf, htf_bias, ltf_bias
        )

        return MTFResult(
            htf_bias=htf_bias,
            ltf_bias=ltf_bias,
            aligned=aligned,
            htf_ema_fast=htf_ema_f,
            htf_ema_slow=htf_ema_s,
            ltf_ema_fast=ltf_ema_f,
            ltf_ema_slow=ltf_ema_s,
            htf_rsi=htf_rsi,
            ltf_rsi=ltf_rsi,
            alignment_score=alignment_score,
        )

    # ─── Per-Timeframe Analysis ────────────────────────────────────

    def _analyse_single(
        self,
        df: pd.DataFrame,
    ) -> tuple[Bias, float, float, float]:
        close    = df["close"]
        ema_fast = float(close.ewm(span=self.ema_fast, adjust=False).mean().iloc[-1])
        ema_slow = float(close.ewm(span=self.ema_slow, adjust=False).mean().iloc[-1])
        rsi_val  = self._rsi(close)

        # Tolerance: 0.05% of price to ignore micro-crossings
        tolerance = float(close.iloc[-1]) * 0.0005

        if ema_fast > ema_slow + tolerance:
            bias = Bias.LONG
        elif ema_fast < ema_slow - tolerance:
            bias = Bias.SHORT
        else:
            bias = Bias.FLAT

        return bias, ema_fast, ema_slow, rsi_val

    # ─── Alignment Score ──────────────────────────────────────────

    def _alignment_score(
        self,
        df_htf: pd.DataFrame,
        df_ltf: pd.DataFrame,
        htf_bias: Bias,
        ltf_bias: Bias,
    ) -> float:
        """
        Score = combination of:
          1. Whether biases match (0 or 1)
          2. Magnitude of EMA separation on each TF
          3. RSI agreement (not extreme against signal)
        """
        if htf_bias == Bias.FLAT or ltf_bias == Bias.FLAT:
            return 0.3
        if htf_bias != ltf_bias:
            return 0.1

        # EMA separation as % of price — larger = stronger trend
        htf_close   = float(df_htf["close"].iloc[-1])
        ltf_close   = float(df_ltf["close"].iloc[-1])

        htf_ema_f   = float(df_htf["close"].ewm(span=self.ema_fast, adjust=False).mean().iloc[-1])
        htf_ema_s   = float(df_htf["close"].ewm(span=self.ema_slow, adjust=False).mean().iloc[-1])
        ltf_ema_f   = float(df_ltf["close"].ewm(span=self.ema_fast, adjust=False).mean().iloc[-1])
        ltf_ema_s   = float(df_ltf["close"].ewm(span=self.ema_slow, adjust=False).mean().iloc[-1])

        htf_sep     = abs(htf_ema_f - htf_ema_s) / htf_close
        ltf_sep     = abs(ltf_ema_f - ltf_ema_s) / ltf_close

        # Normalise: 0.5% separation = very strong, cap at 1.0
        htf_score   = min(1.0, htf_sep / 0.005)
        ltf_score   = min(1.0, ltf_sep / 0.005)

        # Weighted: HTF is more important
        score = 0.60 * htf_score + 0.40 * ltf_score
        return round(score, 3)

    # ─── RSI ──────────────────────────────────────────────────────

    def _rsi(self, close: pd.Series) -> float:
        """Wilder RSI — no lookahead."""
        delta  = close.diff()
        gain   = delta.clip(lower=0)
        loss   = (-delta).clip(lower=0)
        avg_g  = gain.ewm(alpha=1 / self.rsi_period, min_periods=self.rsi_period, adjust=False).mean()
        avg_l  = loss.ewm(alpha=1 / self.rsi_period, min_periods=self.rsi_period, adjust=False).mean()
        rs     = avg_g / avg_l.replace(0, np.nan)
        rsi    = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])
