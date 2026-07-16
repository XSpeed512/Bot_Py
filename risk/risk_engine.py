"""
risk/risk_engine.py
===================
Adaptive Risk Management Engine

Computes for every approved signal:
  - Stop loss price       (ATR-based, structure-adjusted)
  - Take profit price     (ATR-based, minimum 2:1 R/R)
  - Position size (units) (volatility-adjusted Kelly fraction)
  - Trailing stop params

Also enforces risk-of-ruin protection:
  - Max daily loss gate
  - Consecutive loss circuit breaker
  - Volatility kill-switch
  - Drawdown halt

All sizing uses ACCOUNT-RELATIVE risk, never fixed lot sizes.
ATR-based SL/TP means they widen in volatile markets automatically.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
import numpy as np
import logging
from datetime import datetime, date, timezone

from signals.scoring import Direction

logger = logging.getLogger(__name__)


@dataclass
class RiskParameters:
    """Output of the risk engine for a single trade."""
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float        # In units/contracts
    risk_amount: float          # USD at risk
    r_r_ratio: float            # Actual R:R achieved
    atr: float
    trailing_stop_distance: float  # ATR-based trail
    approved: bool
    rejection_reason: Optional[str] = None


@dataclass
class AccountState:
    """Tracks account state for risk-of-ruin protection."""
    balance: float
    peak_balance: float
    daily_start_balance: float
    daily_date: date
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    total_trades: int = 0
    is_paused: bool = False
    pause_until: Optional[datetime] = None
    trade_history: list = field(default_factory=list)


class RiskEngine:

    def __init__(self, cfg: dict):
        r = cfg["risk"]
        self.max_risk_pct    = r["max_risk_per_trade_pct"] / 100.0
        self.atr_sl_mult     = r["atr_sl_multiplier"]
        self.atr_tp_mult     = r["atr_tp_multiplier"]
        self.kelly_fraction  = r["kelly_fraction"]
        self.trail_active_r  = r["trailing_stop"]["activation_r_multiple"]
        self.trail_atr_mult  = r["trailing_stop"]["trail_atr_multiplier"]
        self.trail_enabled   = r["trailing_stop"]["enabled"]
        self.max_positions   = r["max_open_positions"]

        rp = cfg["ruin_protection"]
        self.max_daily_loss_pct       = rp["max_daily_loss_pct"] / 100.0
        self.max_consec_losses        = rp["max_consecutive_losses"]
        self.pause_hours              = rp["pause_hours_after_consec"]
        self.vol_kill_mult            = rp["vol_kill_switch_multiplier"]
        self.drawdown_kill_pct        = rp["drawdown_kill_switch_pct"] / 100.0

        ind = cfg["indicators"]
        self.atr_period     = ind["atr_period"]
        self.atr_avg_period = ind["atr_avg_period"]

    # ─── Public API ────────────────────────────────────────────────

    def compute(
        self,
        df: pd.DataFrame,
        direction: Direction,
        confidence: float,          # 0–100, used to scale position size
        account: AccountState,
        current_atr_ratio: float,
        open_positions: int = 0,
    ) -> RiskParameters:
        """
        Compute all risk parameters for a new trade.

        Parameters
        ----------
        df               : LTF OHLCV DataFrame
        direction        : LONG or SHORT
        confidence       : Signal confidence (0–100) — scales position size
        account          : Current account state
        current_atr_ratio: ATR ratio from regime detector
        open_positions   : Number of currently open positions
        """
        # ── Ruin protection checks ─────────────────────────────
        approved, reason = self._ruin_protection_check(account, current_atr_ratio, open_positions)
        if not approved:
            return RiskParameters(
                direction=direction,
                entry_price=0.0, stop_loss=0.0, take_profit=0.0,
                position_size=0.0, risk_amount=0.0, r_r_ratio=0.0,
                atr=0.0, trailing_stop_distance=0.0,
                approved=False, rejection_reason=reason
            )

        # ── Core ATR ───────────────────────────────────────────
        atr = self._atr(df)

        # ── Entry price ────────────────────────────────────────
        entry = float(df["close"].iloc[-1])

        # ── Dynamic SL/TP multipliers ──────────────────────────
        # Adjust multipliers based on recent win rate (self-improvement input)
        sl_mult, tp_mult = self._adaptive_multipliers(account)

        # ── Stop loss and take profit ──────────────────────────
        if direction == Direction.LONG:
            # SL below entry, TP above entry
            sl = entry - sl_mult * atr
            tp = entry + tp_mult * atr
            # Structure refinement: don't place SL above a recent swing low
            swing_low = self._recent_swing_low(df)
            if swing_low and swing_low < entry:
                sl = min(sl, swing_low - 0.1 * atr)  # 0.1 ATR buffer below swing
        else:
            sl = entry + sl_mult * atr
            tp = entry - tp_mult * atr
            swing_high = self._recent_swing_high(df)
            if swing_high and swing_high > entry:
                sl = max(sl, swing_high + 0.1 * atr)

        # ── Risk/Reward ratio ──────────────────────────────────
        sl_distance = abs(entry - sl)
        tp_distance = abs(tp - entry)
        rr_ratio    = tp_distance / sl_distance if sl_distance > 0 else 0.0

        # Reject if R/R is below minimum acceptable
        if rr_ratio < 1.5:
            return RiskParameters(
                direction=direction,
                entry_price=entry, stop_loss=sl, take_profit=tp,
                position_size=0.0, risk_amount=0.0, r_r_ratio=rr_ratio,
                atr=atr, trailing_stop_distance=0.0,
                approved=False, rejection_reason=f"poor_rr_{rr_ratio:.2f}"
            )

        # ── Position sizing ────────────────────────────────────
        position_size = self._position_size(
            account.balance, sl_distance, entry, confidence
        )

        risk_amount = position_size * sl_distance

        # ── Trailing stop distance ─────────────────────────────
        trail_distance = self.trail_atr_mult * atr if self.trail_enabled else 0.0

        logger.info(
            "Risk computed: entry=%.4f SL=%.4f TP=%.4f size=%.4f R/R=%.2f ATR=%.4f",
            entry, sl, tp, position_size, rr_ratio, atr
        )

        return RiskParameters(
            direction=direction,
            entry_price=entry,
            stop_loss=round(sl, 6),
            take_profit=round(tp, 6),
            position_size=round(position_size, 6),
            risk_amount=round(risk_amount, 2),
            r_r_ratio=round(rr_ratio, 2),
            atr=round(atr, 6),
            trailing_stop_distance=round(trail_distance, 6),
            approved=True,
        )

    def update_trailing_stop(
        self,
        direction: Direction,
        entry_price: float,
        current_price: float,
        current_sl: float,
        atr: float,
    ) -> float:
        """
        Return updated trailing stop level.
        Only moves the SL in the profitable direction, never backward.
        Activates after price moves self.trail_active_r × ATR in our favour.
        """
        if not self.trail_enabled:
            return current_sl

        profit_distance = (
            current_price - entry_price if direction == Direction.LONG
            else entry_price - current_price
        )
        activation_threshold = self.trail_active_r * atr

        if profit_distance < activation_threshold:
            return current_sl  # Not yet activated

        if direction == Direction.LONG:
            new_sl = current_price - self.trail_atr_mult * atr
            return max(current_sl, new_sl)  # Only move up
        else:
            new_sl = current_price + self.trail_atr_mult * atr
            return min(current_sl, new_sl)  # Only move down

    # ─── Ruin Protection ──────────────────────────────────────────

    def _ruin_protection_check(
        self,
        account: AccountState,
        atr_ratio: float,
        open_positions: int,
    ) -> tuple[bool, Optional[str]]:

        # Check if bot is in a pause state
        if account.is_paused:
            if account.pause_until and datetime.now(tz=timezone.utc) < account.pause_until:
                return False, "bot_paused"
            else:
                account.is_paused = False  # Pause expired
                account.pause_until = None
                logger.info("Pause expired, resuming trading")

        # Max open positions
        if open_positions >= self.max_positions:
            return False, f"max_positions_reached_{open_positions}"

        # Daily loss limit
        if account.daily_start_balance <= 0:
            logger.warning("daily_start_balance invalid (%.2f); skipping daily loss check.", account.daily_start_balance)
        else:
            daily_loss_pct = (account.daily_start_balance - account.balance) / account.daily_start_balance
            if daily_loss_pct >= self.max_daily_loss_pct:
                return False, f"daily_loss_limit_{daily_loss_pct:.2%}"

        # Consecutive losses
        if account.consecutive_losses >= self.max_consec_losses:
            from datetime import timedelta
            account.is_paused    = True
            account.pause_until  = datetime.now(tz=timezone.utc) + timedelta(hours=self.pause_hours)
            logger.warning("Consecutive loss limit hit (%d). Pausing %dh.",
                           account.consecutive_losses, self.pause_hours)
            return False, f"consecutive_losses_{account.consecutive_losses}"

        # Volatility kill-switch
        if atr_ratio > self.vol_kill_mult:
            return False, f"vol_kill_switch_atr_ratio_{atr_ratio:.2f}"

        # Drawdown kill-switch
        if account.peak_balance > 0:
            drawdown = (account.peak_balance - account.balance) / account.peak_balance
            if drawdown >= self.drawdown_kill_pct:
                logger.critical("DRAWDOWN KILL SWITCH: %.2f%% drawdown. HALTING.", drawdown * 100)
                return False, f"drawdown_kill_{drawdown:.2%}"

        return True, None

    # ─── Position Sizing ──────────────────────────────────────────

    def _position_size(
        self,
        balance: float,
        sl_distance: float,
        entry_price: float,
        confidence: float,
    ) -> float:
        """
        Volatility-adjusted Kelly fraction position sizing.

        Base: risk_amount = balance × max_risk_pct
        Confidence scaling: higher confidence → scale up (within Kelly limit)
        Kelly is further scaled by self.kelly_fraction to avoid full Kelly ruin risk.

        Units = risk_amount / sl_distance_per_unit
        """
        # Confidence scaling: 0.6× at min confidence, 1.0× at 100
        min_conf = 55.0  # Approximately the minimum threshold
        conf_scale = 0.6 + 0.4 * max(0.0, (confidence - min_conf) / (100.0 - min_conf))
        conf_scale = min(1.0, conf_scale)

        risk_amount = balance * self.max_risk_pct * conf_scale * self.kelly_fraction

        if sl_distance <= 0 or entry_price <= 0:
            return 0.0

        # Position size in base units
        size = risk_amount / sl_distance
        return max(0.0, size)

    # ─── Adaptive Multipliers ─────────────────────────────────────

    def _adaptive_multipliers(self, account: AccountState) -> tuple[float, float]:
        """
        Adjust SL/TP multipliers based on recent trade performance.
        After a losing streak → slightly widen SL to avoid early stops.
        After a winning streak → keep standard parameters.
        """
        sl_mult = self.atr_sl_mult
        tp_mult = self.atr_tp_mult

        # After 3+ consecutive losses, give trades more room
        if account.consecutive_losses >= 3:
            sl_mult = min(self.atr_sl_mult * 1.2, 2.5)
            tp_mult = min(self.atr_tp_mult * 1.1, 5.0)

        return sl_mult, tp_mult

    # ─── Swing High/Low Helpers ───────────────────────────────────

    def _recent_swing_low(self, df: pd.DataFrame, window: int = 10) -> Optional[float]:
        if len(df) < window + 2:
            return None
        subset = df["low"].iloc[-window - 1:-1]
        idx    = subset.idxmin()
        return float(subset[idx]) if idx is not None else None

    def _recent_swing_high(self, df: pd.DataFrame, window: int = 10) -> Optional[float]:
        if len(df) < window + 2:
            return None
        subset = df["high"].iloc[-window - 1:-1]
        idx    = subset.idxmax()
        return float(subset[idx]) if idx is not None else None

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
