"""
signals/scoring.py
==================
Modular Confidence Scoring Engine

Replaces the old binary BUY/SELL logic with a weighted multi-factor
scoring model. Every signal gets:
  - confidence_score  (0–100) — how good the setup looks
  - trade_quality     (0–100) — alignment, structure, confluence
  - risk_score        (0–100) — how risky the entry is right now

A trade is only queued when:
  confidence_score >= cfg.scoring.min_confidence
  trade_quality    >= cfg.scoring.min_trade_quality
  risk_score       <= cfg.scoring.max_risk_score

Individual component scores are logged for the self-improvement
module to track which factors correlate with winning trades.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import pandas as pd
import numpy as np
import logging

from strategy.regime import Regime, RegimeResult
from strategy.mtf import MTFResult, Bias

logger = logging.getLogger(__name__)


class Direction(str, Enum):
    LONG  = "long"
    SHORT = "short"
    NONE  = "none"


@dataclass
class SignalScore:
    direction: Direction

    # Component scores (0–100)
    trend_score:      float = 0.0
    momentum_score:   float = 0.0
    volume_score:     float = 0.0
    structure_score:  float = 0.0
    volatility_score: float = 0.0

    # Composite
    confidence:    float = 0.0   # Weighted sum of above
    trade_quality: float = 0.0   # MTF alignment + regime fit
    risk_score:    float = 50.0  # Higher = riskier

    # Supporting data for logging/self-improvement
    metadata: dict = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return self.direction != Direction.NONE


class ScoringEngine:
    """
    Computes a multi-factor score for a potential trade entry.
    All scores are bounded [0, 100].
    """

    def __init__(self, cfg: dict):
        self.cfg    = cfg
        w           = cfg["scoring"]["weights"]
        self.w_trend    = w["trend"]
        self.w_mom      = w["momentum"]
        self.w_volume   = w["volume"]
        self.w_struct   = w["structure"]
        self.w_vol      = w["volatility"]

        ind = cfg["indicators"]
        self.rsi_oversold    = ind["rsi_oversold"]
        self.rsi_overbought  = ind["rsi_overbought"]
        self.rsi_period      = ind["rsi_period"]
        self.atr_period      = ind["atr_period"]
        self.atr_avg_period  = ind["atr_avg_period"]
        self.volume_ma_p     = ind["volume_ma_period"]
        self.vol_spike_mult  = ind["volume_spike_multiplier"]
        self.bb_period       = ind["bb_period"]
        self.bb_std          = ind["bb_std"]
        self.ema_fast        = ind["ema_fast"]
        self.ema_slow        = ind["ema_slow"]
        self.ema_signal      = ind["ema_signal"]

    # ─── Public API ────────────────────────────────────────────────

    def score(
        self,
        df_ltf: pd.DataFrame,
        regime: RegimeResult,
        mtf: MTFResult,
    ) -> SignalScore:
        """
        Parameters
        ----------
        df_ltf  : LTF OHLCV DataFrame (last bar = current)
        regime  : Output from RegimeDetector
        mtf     : Output from MTFEngine
        """

        # Determine candidate direction from HTF bias
        if mtf.htf_bias == Bias.LONG:
            direction = Direction.LONG
        elif mtf.htf_bias == Bias.SHORT:
            direction = Direction.SHORT
        else:
            return SignalScore(direction=Direction.NONE, metadata={"reason": "htf_flat"})

        # For RANGING regime, allow counter-trend mean-reversion entries
        # but only when RSI is extreme and structure supports it
        if regime.regime == Regime.RANGING:
            direction = self._ranging_direction(df_ltf, direction)
            if direction == Direction.NONE:
                return SignalScore(direction=Direction.NONE, metadata={"reason": "ranging_no_entry"})

        # Block entries in high volatility
        if regime.regime == Regime.HIGH_VOL:
            logger.debug("Scoring blocked: HIGH_VOL regime")
            return SignalScore(direction=Direction.NONE, metadata={"reason": "high_vol"})

        # Compute individual scores
        t_score  = self._trend_score(df_ltf, direction)
        m_score  = self._momentum_score(df_ltf, direction)
        v_score  = self._volume_score(df_ltf)
        s_score  = self._structure_score(df_ltf, direction)
        vt_score = self._volatility_score(df_ltf, regime)

        # Weighted confidence score
        confidence = (
            self.w_trend   * t_score +
            self.w_mom     * m_score +
            self.w_volume  * v_score +
            self.w_struct  * s_score +
            self.w_vol     * vt_score
        ) * 100

        # Regime-based confidence adjustment
        confidence += self._regime_confidence_boost(regime, direction)
        confidence  = min(100.0, max(0.0, confidence))

        # Trade quality: MTF alignment + regime fit
        quality = self._trade_quality(mtf, regime, direction, confidence)

        # Risk score: market danger level
        risk = self._risk_score(df_ltf, regime)

        sig = SignalScore(
            direction=direction,
            trend_score=round(t_score * 100, 1),
            momentum_score=round(m_score * 100, 1),
            volume_score=round(v_score * 100, 1),
            structure_score=round(s_score * 100, 1),
            volatility_score=round(vt_score * 100, 1),
            confidence=round(confidence, 1),
            trade_quality=round(quality, 1),
            risk_score=round(risk, 1),
            metadata={
                "regime": regime.regime.value,
                "htf_bias": mtf.htf_bias.value,
                "ltf_bias": mtf.ltf_bias.value,
                "mtf_alignment": mtf.aligned,
                "alignment_score": mtf.alignment_score,
            }
        )
        logger.info(
            "Signal [%s] conf=%.1f quality=%.1f risk=%.1f | "
            "trend=%.0f mom=%.0f vol=%.0f struct=%.0f vt=%.0f",
            direction.value, confidence, quality, risk,
            t_score*100, m_score*100, v_score*100, s_score*100, vt_score*100
        )
        return sig

    # ─── Component Scorers ────────────────────────────────────────

    def _trend_score(self, df: pd.DataFrame, direction: Direction) -> float:
        """
        Score based on:
          - EMA alignment (fast/signal/slow all stacked)
          - EMA separation (distance = strength)
          - Price position relative to EMAs
        Returns 0–1.
        """
        close   = df["close"]
        ema_f   = close.ewm(span=self.ema_fast, adjust=False).mean().iloc[-1]
        ema_sig = close.ewm(span=self.ema_signal, adjust=False).mean().iloc[-1]
        ema_s   = close.ewm(span=self.ema_slow, adjust=False).mean().iloc[-1]
        price   = float(close.iloc[-1])

        if direction == Direction.LONG:
            # Perfect: price > ema_fast > ema_signal > ema_slow
            aligned  = int(price > ema_f > ema_sig > ema_s)
            half_ok  = int(ema_f > ema_s)  # at minimum, fast > slow
        else:
            aligned  = int(price < ema_f < ema_sig < ema_s)
            half_ok  = int(ema_f < ema_s)

        # EMA separation as strength indicator
        sep_score = min(1.0, abs(ema_f - ema_s) / (price * 0.005))

        score = 0.4 * aligned + 0.3 * half_ok + 0.3 * sep_score
        return float(score)

    def _momentum_score(self, df: pd.DataFrame, direction: Direction) -> float:
        """
        RSI-based pullback quality:
          - LONG: RSI recovering from oversold (was < oversold, now climbing)
          - SHORT: RSI retreating from overbought
        Also uses RSI slope (momentum of momentum).
        Returns 0–1.
        """
        close = df["close"]
        rsi   = self._rsi(close)
        current_rsi = float(rsi.iloc[-1])
        prev_rsi    = float(rsi.iloc[-2]) if len(rsi) > 1 else current_rsi

        if direction == Direction.LONG:
            # Best entry: RSI was below oversold and is now rising
            was_oversold = float(rsi.iloc[-3:].min()) < self.rsi_oversold if len(rsi) >= 3 else False
            rising       = current_rsi > prev_rsi
            not_overbought = current_rsi < 60  # Avoid chasing

            score = 0.0
            if was_oversold: score += 0.5
            elif current_rsi < self.rsi_oversold + 10: score += 0.3
            if rising: score += 0.3
            if not_overbought: score += 0.2
        else:
            was_overbought = float(rsi.iloc[-3:].max()) > self.rsi_overbought if len(rsi) >= 3 else False
            falling        = current_rsi < prev_rsi
            not_oversold   = current_rsi > 40

            score = 0.0
            if was_overbought: score += 0.5
            elif current_rsi > self.rsi_overbought - 10: score += 0.3
            if falling: score += 0.3
            if not_oversold: score += 0.2

        return float(score)

    def _volume_score(self, df: pd.DataFrame) -> float:
        """
        Volume confirmation:
          - Is current bar's volume above the rolling average?
          - Is volume increasing (expanding into the move)?
        Returns 0–1.
        """
        vol    = df["volume"]
        avg    = float(vol.rolling(self.volume_ma_p).mean().iloc[-1])
        curr   = float(vol.iloc[-1])
        prev   = float(vol.iloc[-2]) if len(vol) > 1 else curr

        if avg == 0:
            return 0.5  # Neutral if no data

        ratio  = curr / avg
        rising = curr > prev

        # Score: capped at 1.5× average (beyond that = unusual spike, not better)
        magnitude_score = min(1.0, (ratio - 1.0) / (self.vol_spike_mult - 1.0))
        magnitude_score = max(0.0, magnitude_score)

        score = 0.6 * magnitude_score + 0.4 * float(rising)
        return float(score)

    def _structure_score(self, df: pd.DataFrame, direction: Direction) -> float:
        """
        Candle structure quality:
          - Body ratio (body vs total range — avoids dojis)
          - Wick ratio (direction-appropriate wicks)
          - Price position relative to Bollinger Bands
        Returns 0–1.
        """
        last   = df.iloc[-1]
        o, h, l, c = last["open"], last["high"], last["low"], last["close"]
        total_range = h - l
        if total_range == 0:
            return 0.3

        body       = abs(c - o)
        body_ratio = body / total_range

        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l

        if direction == Direction.LONG:
            # Want: large body, small upper wick, larger lower wick (rejection of lows)
            wick_score = lower_wick / total_range
            is_bullish = float(c > o)
        else:
            wick_score = upper_wick / total_range
            is_bullish = float(c < o)

        body_score = min(1.0, body_ratio / 0.5)  # 50% body = full score

        # Bollinger Band position
        close = df["close"]
        mid   = float(close.rolling(self.bb_period).mean().iloc[-1])
        std   = float(close.rolling(self.bb_period).std(ddof=0).iloc[-1])
        upper = mid + self.bb_std * std
        lower = mid - self.bb_std * std
        price = float(c)

        if direction == Direction.LONG and std > 0:
            # Good entry: near lower band (not chasing)
            bb_score = max(0.0, 1.0 - (price - lower) / (upper - lower))
        elif direction == Direction.SHORT and std > 0:
            bb_score = max(0.0, (price - lower) / (upper - lower))
        else:
            bb_score = 0.5

        score = 0.35 * body_score + 0.25 * wick_score + 0.20 * is_bullish + 0.20 * bb_score
        return float(score)

    def _volatility_score(self, df: pd.DataFrame, regime: RegimeResult) -> float:
        """
        We want moderate volatility:
          - Too low  → no movement, spreads eat profit
          - Too high → unpredictable, stops get hit
        Returns 0–1 with peak at ATR ratio ≈ 1.0 (normal volatility).
        """
        atr_ratio = regime.atr_ratio
        # Bell-curve-like: peak at 1.0, drops off on both sides
        score = max(0.0, 1.0 - abs(atr_ratio - 1.0) / 0.8)
        return float(score)

    # ─── Trade Quality & Risk ──────────────────────────────────────

    def _trade_quality(
        self,
        mtf: MTFResult,
        regime: RegimeResult,
        direction: Direction,
        confidence: float,
    ) -> float:
        """
        Quality = how much the setup "makes sense" given the context.
        Key factors:
          - MTF alignment
          - Regime fitness (trend entry in trending market, etc.)
          - Overall confidence level
        """
        quality = 0.0

        # MTF alignment is the most important quality gate
        if mtf.aligned:
            quality += 35.0 + (mtf.alignment_score * 15.0)
        else:
            quality += 10.0  # Harsh penalty for disagreement

        # Regime fitness
        if regime.regime in (Regime.TRENDING_BULL, Regime.TRENDING_BEAR):
            # Trend trade in trending market = high quality
            if (regime.regime == Regime.TRENDING_BULL and direction == Direction.LONG) or \
               (regime.regime == Regime.TRENDING_BEAR and direction == Direction.SHORT):
                quality += 25.0
            else:
                quality += 5.0  # Counter-trend = low quality
        elif regime.regime == Regime.RANGING:
            # Mean-reversion in ranging = good quality
            quality += 20.0
        else:
            quality += 10.0

        # Confidence contribution
        quality += confidence * 0.25

        return min(100.0, quality)

    def _risk_score(self, df: pd.DataFrame, regime: RegimeResult) -> float:
        """
        Risk score (higher = more dangerous):
          - High ATR ratio → higher risk
          - Recent large adverse moves → higher risk
        """
        risk = 50.0  # Base

        # ATR risk
        if regime.atr_ratio > 1.5:
            risk += 20.0
        elif regime.atr_ratio < 0.7:
            risk += 10.0  # Low vol also risky (fake moves)
        else:
            risk -= 10.0  # Normal vol → lower risk

        # Recent candle range (last 3 bars)
        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        recent_swings = float((high.iloc[-3:] - low.iloc[-3:]).max()) / float(close.iloc[-1])
        if recent_swings > 0.02:  # 2% swings in recent bars
            risk += 15.0

        return min(100.0, max(0.0, risk))

    # ─── Regime Boost ─────────────────────────────────────────────

    def _regime_confidence_boost(self, regime: RegimeResult, direction: Direction) -> float:
        s = self.cfg["scoring"]
        if regime.regime == Regime.RANGING:
            return s.get("ranging_confidence_boost", 5)
        if regime.regime in (Regime.TRENDING_BULL, Regime.TRENDING_BEAR):
            return s.get("trending_confidence_boost", 8)
        return 0.0

    # ─── Ranging Direction ────────────────────────────────────────

    def _ranging_direction(self, df: pd.DataFrame, bias: Direction) -> Direction:
        """
        In a ranging market, use RSI extremes for mean-reversion.
        Can enter against HTF bias if RSI is at an extreme.
        """
        close   = df["close"]
        rsi_val = float(self._rsi(close).iloc[-1])
        price   = float(close.iloc[-1])
        low_20  = float(df["low"].rolling(20).min().iloc[-1])
        high_20 = float(df["high"].rolling(20).max().iloc[-1])
        rng     = high_20 - low_20

        if rsi_val < self.rsi_oversold and rng > 0:
            return Direction.LONG
        if rsi_val > self.rsi_overbought and rng > 0:
            return Direction.SHORT
        return Direction.NONE

    # ─── Helpers ──────────────────────────────────────────────────

    def _rsi(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain  = delta.clip(lower=0)
        loss  = (-delta).clip(lower=0)
        avg_g = gain.ewm(alpha=1 / self.rsi_period, min_periods=self.rsi_period, adjust=False).mean()
        avg_l = loss.ewm(alpha=1 / self.rsi_period, min_periods=self.rsi_period, adjust=False).mean()
        rs    = avg_g / avg_l.replace(0, np.nan)
        return 100 - (100 / (1 + rs))
