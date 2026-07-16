"""
core/regime.py
==============
Market Regime Detector

Classifies current market state into one of four regimes:
  TRENDING_BULL  — strong upward momentum, ADX > threshold
  TRENDING_BEAR  — strong downward momentum, ADX > threshold
  RANGING        — low-directional, mean-reverting price action
  HIGH_VOL       — explosive volatility, tighten filters
  LOW_VOL        — compressed volatility, widen SL, wait for expansion

Strategy behaviour adapts per regime:
  - Trending  → use trend-following entry logic, wider TP
  - Ranging   → use mean-reversion entries, tighter TP
  - High vol  → reduce position size, require higher confidence
  - Low vol   → wait for breakout, reduce entries
"""

from __future__ import annotations
from enum import Enum
from dataclasses import dataclass
import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    TRENDING_BULL = "trending_bull"
    TRENDING_BEAR = "trending_bear"
    RANGING       = "ranging"
    HIGH_VOL      = "high_vol"
    LOW_VOL       = "low_vol"
    UNKNOWN       = "unknown"


@dataclass
class RegimeResult:
    regime: Regime
    adx: float
    bbw: float               # Bollinger Band Width (normalised)
    atr_ratio: float         # Current ATR / Average ATR
    trend_direction: int     # +1 bull, -1 bear, 0 neutral
    confidence: float        # 0–1 how confident we are in regime label
    description: str


class RegimeDetector:
    """
    Uses ADX, Bollinger Band Width, and ATR ratio to classify regime.
    Does NOT use any lookahead — all indicators are computed on closed bars.
    """

    def __init__(self, cfg: dict):
        r = cfg["regime"]
        self.adx_trending  = r["adx_trending_threshold"]
        self.adx_strong    = r["adx_strong_threshold"]
        self.bbw_ranging   = r["bbw_ranging_threshold"]
        self.atr_hi_mult   = r["atr_high_vol_multiplier"]
        self.atr_lo_mult   = r["atr_low_vol_multiplier"]
        self.lookback      = r["regime_lookback"]

        ind = cfg["indicators"]
        self.ema_fast  = ind["ema_fast"]
        self.ema_slow  = ind["ema_slow"]
        self.adx_p     = ind["adx_period"]
        self.bb_p      = ind["bb_period"]
        self.bb_std    = ind["bb_std"]
        self.atr_p     = ind["atr_period"]
        self.atr_avg_p = ind["atr_avg_period"]

    # ─── Public API ────────────────────────────────────────────────

    def detect(self, df: pd.DataFrame) -> RegimeResult:
        """
        Parameters
        ----------
        df : OHLCV DataFrame with columns [open, high, low, close, volume]
             Must contain at least `lookback` bars. Sorted oldest→newest.

        Returns
        -------
        RegimeResult with regime label and supporting metrics.
        """
        if len(df) < self.lookback:
            logger.warning("RegimeDetector: insufficient bars (%d < %d)", len(df), self.lookback)
            return RegimeResult(Regime.UNKNOWN, 0, 0, 1.0, 0, 0.0, "insufficient data")

        # Compute indicators on all data, read only the last value
        adx_val       = self._adx(df)
        bbw_val       = self._bbw(df)
        atr_ratio_val = self._atr_ratio(df)
        trend_dir     = self._trend_direction(df)

        return self._classify(adx_val, bbw_val, atr_ratio_val, trend_dir)

    # ─── Classification Logic ──────────────────────────────────────

    def _classify(
        self,
        adx: float,
        bbw: float,
        atr_ratio: float,
        trend_dir: int,
    ) -> RegimeResult:
        """
        Classificazione regime con priorità esplicita e confidence sempre in [0, 1].

        Ordine di priorità (dal più al meno urgente):
          1. High volatility  — sovrascrive tutto, protegge il capitale
          2. Low volatility   — mercato compresso, evita fakeout
          3. Trending         — ADX forte E BBW ampia: trend confermato
          4. Ranging          — tutto il resto (ADX basso O BBW stretta)
        """
        # ── 1. High volatility ────────────────────────────────────
        if atr_ratio > self.atr_hi_mult:
            # Confidence sale linearmente oltre la soglia, max 1.0
            excess = (atr_ratio - self.atr_hi_mult) / max(self.atr_hi_mult, 0.1)
            conf   = min(1.0, 0.6 + excess * 0.4)
            return RegimeResult(
                Regime.HIGH_VOL, adx, bbw, atr_ratio, trend_dir, round(conf, 3),
                f"Alta volatilità (ATR ratio {atr_ratio:.2f})"
            )

        # ── 2. Low volatility ─────────────────────────────────────
        if atr_ratio < self.atr_lo_mult:
            deficit = (self.atr_lo_mult - atr_ratio) / max(self.atr_lo_mult, 0.1)
            conf    = min(1.0, 0.6 + deficit * 0.4)
            return RegimeResult(
                Regime.LOW_VOL, adx, bbw, atr_ratio, trend_dir, round(conf, 3),
                f"Bassa volatilità / compressione (ATR ratio {atr_ratio:.2f})"
            )

        # ── 3. Trending — ADX forte E BBW ampia ──────────────────
        if adx >= self.adx_trending and bbw > self.bbw_ranging:
            regime    = Regime.TRENDING_BULL if trend_dir >= 0 else Regime.TRENDING_BEAR
            direction = "bullish" if trend_dir >= 0 else "bearish"
            # Scala linearmente da 0.5 (ADX=soglia) a 1.0 (ADX=forte)
            span = max(self.adx_strong - self.adx_trending, 1.0)
            conf = 0.5 + 0.5 * min(1.0, (adx - self.adx_trending) / span)
            return RegimeResult(
                regime, adx, bbw, atr_ratio, trend_dir, round(conf, 3),
                f"Trend {direction} (ADX {adx:.1f})"
            )

        # ── 4. Ranging — tutto il resto ───────────────────────────
        # Confidence calibrata sull'ADX:
        #   - ADX < soglia trending: ranging netto → conf alta
        #   - ADX >= soglia (BBW stretta, fine trend): ambiguo → conf bassa ma > 0
        if adx < self.adx_trending:
            conf = 0.5 + 0.5 * (1.0 - adx / max(self.adx_trending, 1.0))
        else:
            # ADX alto ma BBW stretta: mercato ambiguo, confidenza ridotta
            conf = max(0.10, 0.40 - (adx - self.adx_trending) / max(self.adx_trending, 1.0) * 0.30)
        # Bonus se BBW molto stretta (consolidamento forte)
        if bbw <= self.bbw_ranging * 0.5:
            conf = min(1.0, conf + 0.10)
        conf = round(min(1.0, max(0.10, conf)), 3)
        return RegimeResult(
            Regime.RANGING, adx, bbw, atr_ratio, trend_dir, conf,
            f"Ranging / laterale (ADX {adx:.1f}, BBW {bbw:.4f})"
        )

    # ─── Indicator Helpers ─────────────────────────────────────────

    def _adx(self, df: pd.DataFrame) -> float:
        """
        Average Directional Index (ADX) — misura la FORZA del trend, range 0–100.

        FIX: la versione precedente usava Wilder smooth senza normalizzare,
        producendo valori cumulativi >100 (es. ADX=280, 574).
        Ora dividiamo i valori smoothed per n per ottenere medie corrette,
        mantenendo il range 0–100 come da specifica originale Wilder.
        """
        high  = df["high"].values
        low   = df["low"].values
        close = df["close"].values
        n     = self.adx_p

        if len(high) < n * 2 + 1:
            return 0.0

        # True Range
        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1]))
        )

        # Directional movement
        up   = high[1:] - high[:-1]
        down = low[:-1] - low[1:]
        dm_plus  = np.where((up > down) & (up > 0), up, 0.0)
        dm_minus = np.where((down > up) & (down > 0), down, 0.0)

        def wilder_smooth(x, p):
            """Wilder smoothing — restituisce valori cumulativi grezzi."""
            s = np.zeros(len(x))
            s[p - 1] = np.sum(x[:p])
            for i in range(p, len(x)):
                s[i] = s[i - 1] - s[i - 1] / p + x[i]
            return s

        # FIX: normalizza per n per ottenere medie (non somme cumulative)
        atr_s = wilder_smooth(tr, n) / n
        dmp_s = wilder_smooth(dm_plus, n) / n
        dmm_s = wilder_smooth(dm_minus, n) / n

        with np.errstate(divide="ignore", invalid="ignore"):
            di_plus  = np.where(atr_s != 0, 100 * dmp_s / atr_s, 0.0)
            di_minus = np.where(atr_s != 0, 100 * dmm_s / atr_s, 0.0)
            dx       = np.where((di_plus + di_minus) != 0,
                                100 * np.abs(di_plus - di_minus) / (di_plus + di_minus),
                                0.0)

        # FIX: smooth ADX e normalizza
        adx = wilder_smooth(dx, n) / n
        # Clamp a [0, 100] come garanzia finale
        return float(np.clip(adx[-1], 0.0, 100.0))

    def _bbw(self, df: pd.DataFrame) -> float:
        """Bollinger Band Width — normalised spread of the bands."""
        close = df["close"]
        mid   = close.rolling(self.bb_p).mean()
        std   = close.rolling(self.bb_p).std(ddof=0)
        upper = mid + self.bb_std * std
        lower = mid - self.bb_std * std
        # Normalise by midline to get percentage width
        bbw   = (upper - lower) / mid
        return float(bbw.iloc[-1])

    def _atr_ratio(self, df: pd.DataFrame) -> float:
        """Current ATR / rolling average ATR."""
        atr     = self._compute_atr(df, self.atr_p)
        atr_avg = atr.rolling(self.atr_avg_p).mean()
        current = float(atr.iloc[-1])
        avg     = float(atr_avg.iloc[-1])
        return current / avg if avg > 0 else 1.0

    def _trend_direction(self, df: pd.DataFrame) -> int:
        """
        EMA cross direction.
        +1 = EMA fast above slow (bullish), -1 = bearish, 0 = flat/crossing.
        """
        close     = df["close"]
        ema_fast  = close.ewm(span=self.ema_fast, adjust=False).mean()
        ema_slow  = close.ewm(span=self.ema_slow, adjust=False).mean()
        diff      = float(ema_fast.iloc[-1]) - float(ema_slow.iloc[-1])
        threshold = float(close.iloc[-1]) * 0.0005  # 0.05% tolerance band

        if diff > threshold:
            return 1
        if diff < -threshold:
            return -1
        return 0

    @staticmethod
    def _compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
        high  = df["high"]
        low   = df["low"]
        close = df["close"]
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs()
        ], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()
