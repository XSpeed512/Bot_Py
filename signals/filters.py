"""
signals/filters.py
==================
Smart Entry Filters

Applied AFTER the scoring engine generates a candidate signal.
Each filter can REJECT or DOWNGRADE a signal.

Filters implemented:
  1. Fake breakout detection   — price broke a level but closed back inside
  2. Liquidity sweep detection — wick pierced key level then reversed
  3. Support/resistance proximity — are we near a key S/R zone?
  4. Candle structure check    — reject dojis, inside bars, indecision
  5. Spread/slippage guard     — reject if market is too illiquid
  6. Session filter            — only trade during liquid sessions
  7. Momentum confirmation     — MACD or slope must agree with direction

No lookahead bias. All filters use iloc[-1] (closed bars only).
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timezone

from signals.scoring import Direction, SignalScore

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    passed: bool
    confidence_adjustment: float   # Added to confidence score (can be negative)
    rejection_reason: Optional[str]
    flags: dict                    # Detailed pass/fail per filter


class EntryFilterEngine:
    """
    Runs all smart entry filters against a candidate signal.
    Returns a FilterResult that the execution engine uses.
    """

    def __init__(self, cfg: dict):
        ef = cfg["entry_filters"]
        self.fakeout_lookback      = ef["fake_breakout_lookback"]
        self.sweep_atr_factor      = ef["liquidity_sweep_atr_factor"]
        self.sr_proximity_factor   = ef["sr_proximity_atr_factor"]
        self.body_ratio_min        = ef["candle_body_ratio_min"]
        self.max_spread_pct        = ef["max_spread_pct"]
        self.session_enabled       = ef["session_filter"]["enabled"]
        self.allowed_sessions      = ef["session_filter"]["allowed_sessions"]

        ind = cfg["indicators"]
        self.atr_period = ind["atr_period"]

    # ─── Public API ────────────────────────────────────────────────

    def apply(
        self,
        df: pd.DataFrame,
        signal: SignalScore,
        current_spread_pct: float = 0.0,
        current_time: Optional[datetime] = None,
    ) -> FilterResult:
        """
        Run all filters against the signal.
        Returns FilterResult with pass/fail and any confidence adjustments.
        """
        if not signal.is_valid:
            return FilterResult(False, 0.0, "invalid_signal", {})

        flags = {}
        adjustment = 0.0
        rejections = []

        # 1. Candle structure
        struct_ok, struct_adj = self._candle_structure(df, signal.direction)
        flags["candle_structure"] = struct_ok
        if not struct_ok:
            rejections.append("weak_candle_structure")
        adjustment += struct_adj

        # 2. Fake breakout check
        fakeout, fakeout_adj = self._fake_breakout(df, signal.direction)
        flags["fake_breakout"] = not fakeout
        if fakeout:
            rejections.append("fake_breakout_detected")
        adjustment += fakeout_adj

        # 3. Liquidity sweep
        sweep, sweep_adj = self._liquidity_sweep(df, signal.direction)
        flags["liquidity_sweep"] = sweep  # sweep in our FAVOUR = good
        adjustment += sweep_adj

        # 4. S/R proximity (bonus if we're near a key level)
        sr_bonus = self._sr_proximity(df, signal.direction)
        flags["sr_proximity"] = sr_bonus > 0
        adjustment += sr_bonus

        # 5. Spread filter
        spread_ok = self._spread_filter(current_spread_pct)
        flags["spread_ok"] = spread_ok
        if not spread_ok:
            rejections.append(f"spread_too_wide_{current_spread_pct:.3f}pct")

        # 6. Session filter
        if self.session_enabled and current_time is not None:
            session_ok = self._session_filter(current_time)
            flags["session_ok"] = session_ok
            if not session_ok:
                rejections.append("outside_session")
        else:
            flags["session_ok"] = True

        # 7. Momentum confirmation (MACD cross)
        macd_ok, macd_adj = self._macd_confirmation(df, signal.direction)
        flags["macd_confirmation"] = macd_ok
        adjustment += macd_adj

        # Final pass/fail:
        # Hard rejections: fake breakout, bad spread, outside session
        hard_rejects = {"fake_breakout_detected", "spread_too_wide", "outside_session"}
        has_hard_reject = any(
            any(r.startswith(h) for h in hard_rejects)
            for r in rejections
        )

        # Weak candle alone is a soft reject (downgrade, not block)
        # unless combined with other issues
        if has_hard_reject:
            passed = False
        elif len(rejections) >= 2:
            # Two soft rejects = block
            passed = False
        else:
            passed = True

        reason = "; ".join(rejections) if rejections else None

        logger.debug(
            "Filter result: passed=%s adj=%.1f flags=%s reason=%s",
            passed, adjustment, flags, reason
        )

        return FilterResult(
            passed=passed,
            confidence_adjustment=adjustment,
            rejection_reason=reason,
            flags=flags,
        )

    # ─── Filter Implementations ────────────────────────────────────

    def _candle_structure(
        self, df: pd.DataFrame, direction: Direction
    ) -> tuple[bool, float]:
        """
        Reject dojis and inside bars.
        Bonus for strong directional candles.
        """
        last  = df.iloc[-1]
        o, h, l, c = last["open"], last["high"], last["low"], last["close"]
        rng = h - l
        if rng == 0:
            return False, -10.0

        body_ratio = abs(c - o) / rng

        if body_ratio < self.body_ratio_min:
            return False, -8.0  # Weak body — indecision candle

        # Inside bar check (current high/low within previous bar)
        prev = df.iloc[-2]
        is_inside_bar = (h <= prev["high"]) and (l >= prev["low"])
        if is_inside_bar:
            return False, -5.0

        # Directional bonus
        if direction == Direction.LONG and c > o:
            return True, +3.0
        if direction == Direction.SHORT and c < o:
            return True, +3.0

        return True, 0.0

    def _fake_breakout(
        self, df: pd.DataFrame, direction: Direction
    ) -> tuple[bool, float]:
        """
        Detect if price broke a recent high/low but then closed back inside.
        Classic trap for retail traders.
        """
        lookback = self.fakeout_lookback
        if len(df) < lookback + 2:
            return False, 0.0

        window   = df.iloc[-(lookback + 1):-1]  # Previous N bars (not current)
        current  = df.iloc[-1]

        recent_high = float(window["high"].max())
        recent_low  = float(window["low"].min())

        if direction == Direction.LONG:
            # Fakeout: current bar briefly broke above recent high but closed below it
            fakeout = (
                float(current["high"]) > recent_high and
                float(current["close"]) < recent_high
            )
        else:
            fakeout = (
                float(current["low"]) < recent_low and
                float(current["close"]) > recent_low
            )

        return fakeout, -15.0 if fakeout else 0.0

    def _liquidity_sweep(
        self, df: pd.DataFrame, direction: Direction
    ) -> tuple[bool, float]:
        """
        Detect a liquidity sweep: price spiked through a key level
        (sweeping stop-losses) and then reversed strongly.
        This is actually BULLISH for our direction — institutions swept
        retail stops then pushed the other way.

        A sweep in our favour = confirmation bonus.
        """
        if len(df) < 3:
            return False, 0.0

        atr = self._atr(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if direction == Direction.LONG:
            # Sweep: prev bar made a new low below recent support,
            # but current bar closes well above that low
            prev_low_was_extreme = float(prev["low"]) < float(df["low"].iloc[-10:-2].min())
            recovery = float(last["close"]) - float(prev["low"]) > atr * self.sweep_atr_factor
            sweep = prev_low_was_extreme and recovery
        else:
            prev_high_was_extreme = float(prev["high"]) > float(df["high"].iloc[-10:-2].max())
            rejection = float(prev["high"]) - float(last["close"]) > atr * self.sweep_atr_factor
            sweep = prev_high_was_extreme and rejection

        return sweep, +8.0 if sweep else 0.0

    def _sr_proximity(
        self, df: pd.DataFrame, direction: Direction
    ) -> float:
        """
        Detect proximity to support/resistance using swing highs/lows.
        Bonus if price is near a key level (mean-reversion opportunity).
        """
        if len(df) < 20:
            return 0.0

        atr   = self._atr(df)
        price = float(df["close"].iloc[-1])
        window = df.iloc[-50:-1] if len(df) >= 51 else df.iloc[:-1]

        # Find swing highs and lows
        swing_highs = self._swing_points(window["high"], is_high=True)
        swing_lows  = self._swing_points(window["low"],  is_high=False)

        levels = swing_highs + swing_lows
        if not levels:
            return 0.0

        nearest = min(abs(price - lvl) for lvl in levels)
        threshold = atr * self.sr_proximity_factor

        if nearest < threshold:
            return +6.0  # Close to a key level = structure bonus
        return 0.0

    def _spread_filter(self, spread_pct: float) -> bool:
        """Reject if spread is too wide (illiquid market)."""
        return spread_pct <= self.max_spread_pct

    def _session_filter(self, current_time: datetime) -> bool:
        """
        Only trade during liquid sessions.
        Times in UTC.
        """
        hour = current_time.hour

        in_london   = 8  <= hour < 17
        in_new_york = 13 <= hour < 22

        if "london" in self.allowed_sessions and in_london:
            return True
        if "new_york" in self.allowed_sessions and in_new_york:
            return True
        if "london" not in self.allowed_sessions and "new_york" not in self.allowed_sessions:
            return True  # No session filter configured

        return False

    def _macd_confirmation(
        self, df: pd.DataFrame, direction: Direction
    ) -> tuple[bool, float]:
        """
        MACD histogram must agree with trade direction.
        Uses standard 12/26/9 parameters.
        """
        close   = df["close"]
        ema12   = close.ewm(span=12, adjust=False).mean()
        ema26   = close.ewm(span=26, adjust=False).mean()
        macd    = ema12 - ema26
        signal  = macd.ewm(span=9, adjust=False).mean()
        hist    = macd - signal

        current_hist = float(hist.iloc[-1])
        prev_hist    = float(hist.iloc[-2]) if len(hist) > 1 else 0.0

        if direction == Direction.LONG:
            agrees  = current_hist > 0
            rising  = current_hist > prev_hist
        else:
            agrees  = current_hist < 0
            rising  = current_hist < prev_hist  # Falling histogram = SHORT momentum

        if agrees and rising:
            return True, +5.0
        if agrees:
            return True, +2.0
        return False, -5.0

    # ─── Helpers ──────────────────────────────────────────────────

    def _atr(self, df: pd.DataFrame) -> float:
        high  = df["high"]
        low   = df["low"]
        close = df["close"]
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs()
        ], axis=1).max(axis=1)
        return float(tr.ewm(span=self.atr_period, adjust=False).mean().iloc[-1])

    @staticmethod
    def _swing_points(series: pd.Series, is_high: bool, window: int = 5) -> list[float]:
        """Find local swing highs or lows using a rolling window."""
        points = []
        vals   = series.values
        for i in range(window, len(vals) - window):
            segment = vals[i - window: i + window + 1]
            pivot   = vals[i]
            if is_high and pivot == max(segment):
                points.append(float(pivot))
            elif not is_high and pivot == min(segment):
                points.append(float(pivot))
        return points
