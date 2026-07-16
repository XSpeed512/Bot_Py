"""
self_improve/tracker.py
=======================
Performance Tracker + Self-Improvement Engine

Two responsibilities:
  1. TRACK all performance metrics in real time
     - win rate, expectancy, profit factor, Sharpe, drawdown
     - per-setup-type breakdowns (which regime + direction works best)

  2. ADAPT parameters over time without overfitting
     - Uses Exponential Moving Average scoring per setup type
     - Adjusts configurable thresholds toward observed optimal values
     - Adaptation rate is small (max 5% shift per cycle) for stability
     - Requires minimum trade count before any adaptation fires

Design philosophy:
  - No ML models, no external APIs
  - Pure statistical feedback loop
  - Transparent, explainable, auditable

Stored state persists across bot restarts via JSON.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import json
import math
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    direction: str
    regime: str
    entry_price: float
    exit_price: float
    position_size: float
    stop_loss: float
    take_profit: float
    pnl: float
    pnl_pct: float
    r_multiple: float          # PnL expressed as multiples of initial risk
    duration_bars: int
    entry_time: str
    exit_time: str
    confidence: float
    trade_quality: float
    risk_score: float
    # Component scores at entry
    trend_score: float = 0.0
    momentum_score: float = 0.0
    volume_score: float = 0.0
    structure_score: float = 0.0
    volatility_score: float = 0.0
    # Outcome
    was_winner: bool = False
    exit_reason: str = "unknown"   # sl_hit | tp_hit | trailing | manual


@dataclass
class SetupStats:
    """Statistics for a specific setup category (regime + direction)."""
    setup_key: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_r: float = 0.0         # Sum of R-multiples
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    ema_score: float = 50.0      # EMA-weighted quality score (0–100)
    last_updated: str = ""

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def expectancy(self) -> float:
        """Expected R per trade."""
        return self.total_r / self.total_trades if self.total_trades > 0 else 0.0


@dataclass
class PerformanceSnapshot:
    timestamp: str
    total_trades: int
    win_rate: float
    expectancy: float
    profit_factor: float
    max_drawdown: float
    current_drawdown: float
    sharpe_ratio: float
    balance: float
    peak_balance: float
    best_setup: str
    worst_setup: str


class PerformanceTracker:

    def __init__(self, cfg: dict):
        si = cfg["self_improve"]
        pf = cfg["performance"]

        self.enabled              = si["enabled"]
        self.min_trades           = si["min_trades_before_adapt"]
        self.adaptation_rate      = si["adaptation_rate"]
        self.ema_decay            = si["ema_decay"]
        self.retune_interval_h    = si["retune_interval_hours"]
        self.tunable              = si["tunable"]
        self.bounds               = si["bounds"]

        self.track_file           = pf["track_file"]
        self.min_sharpe_warn      = pf["min_sharpe_warn"]
        self.min_wr_warn          = pf["min_win_rate_warn"]
        self.max_dd_warn          = pf["max_drawdown_warn"]

        # In-memory state
        self.trades: list[TradeRecord] = []
        self.setup_stats: dict[str, SetupStats] = {}
        self.balance_curve: list[float] = []
        self.peak_balance: float = 0.0
        self.current_balance: float = 0.0
        self.last_retune_time: Optional[datetime] = None

        self._load_state()

    # ─── Public API ────────────────────────────────────────────────

    def record_trade(self, trade: TradeRecord, current_balance: float) -> None:
        """Add a completed trade and update all statistics."""
        self.trades.append(trade)
        self.balance_curve.append(current_balance)
        self.current_balance = current_balance
        self.peak_balance    = max(self.peak_balance, current_balance)

        # Update setup-level stats
        self._update_setup_stats(trade)

        # Log warnings
        self._check_performance_warnings()

        # Save state to disk
        self._save_state()

        logger.info(
            "Trade recorded: %s %s PnL=%.2f R=%.2f | WR=%.1f%% expectancy=%.3f",
            trade.direction, trade.regime,
            trade.pnl, trade.r_multiple,
            self.win_rate * 100, self.expectancy
        )

    def should_adapt(self) -> bool:
        """Return True if it's time to run the adaptation cycle."""
        if not self.enabled:
            return False
        if len(self.trades) < self.min_trades:
            return False
        if self.last_retune_time is None:
            return True
        hours_since = (datetime.now(tz=timezone.utc) - self.last_retune_time).total_seconds() / 3600
        return hours_since >= self.retune_interval_h

    def adapt_parameters(self, current_cfg: dict) -> dict:
        """
        Adjust configurable parameters based on recent performance.
        Returns the updated config dict.

        Adaptation logic:
          - For each tunable parameter, compute the "optimal" direction
            based on what correlates with better win rate / expectancy
          - Nudge the parameter by at most `adaptation_rate` per cycle
          - Clamp to configured bounds

        This is NOT gradient descent. It is simple directional nudging
        based on clear statistical signals. Transparent and auditable.
        """
        if not self.should_adapt():
            return current_cfg

        logger.info("Running parameter adaptation cycle (trades: %d)", len(self.trades))
        self.last_retune_time = datetime.now(tz=timezone.utc)

        updated = current_cfg.copy()

        # Win rate over last 30 trades
        recent = self.trades[-30:]
        recent_wr = sum(1 for t in recent if t.was_winner) / len(recent) if recent else 0.5

        # --- Confidence threshold ---
        # If win rate is high, we can be slightly more selective (raise threshold)
        # If win rate is low, loosen slightly to not starve the system
        if "scoring.min_confidence" in self.tunable:
            key     = "scoring.min_confidence"
            current = updated["scoring"]["min_confidence"]
            target  = current + (5.0 if recent_wr > 0.55 else -3.0)
            updated["scoring"]["min_confidence"] = self._clamp_adapt(current, target, key)

        # --- RSI thresholds ---
        # If momentum_score correlates poorly with wins, adjust RSI bands
        if "indicators.rsi_oversold" in self.tunable:
            key     = "indicators.rsi_oversold"
            current = updated["indicators"]["rsi_oversold"]
            # Check if long trades with low RSI won more often
            long_rsi_wins = self._score_correlation("momentum_score", long_only=True)
            target = current + (2.0 if long_rsi_wins < 0.45 else -1.0)
            updated["indicators"]["rsi_oversold"] = self._clamp_adapt(current, target, key)

        if "indicators.rsi_overbought" in self.tunable:
            key     = "indicators.rsi_overbought"
            current = updated["indicators"]["rsi_overbought"]
            short_rsi_wins = self._score_correlation("momentum_score", long_only=False)
            target  = current + (-2.0 if short_rsi_wins < 0.45 else +1.0)
            updated["indicators"]["rsi_overbought"] = self._clamp_adapt(current, target, key)

        # --- ATR SL multiplier ---
        # If most losses are from SL being too tight, widen slightly
        if "risk.atr_sl_multiplier" in self.tunable:
            key       = "risk.atr_sl_multiplier"
            current   = updated["risk"]["atr_sl_multiplier"]
            sl_hit_pct = sum(1 for t in recent if t.exit_reason == "sl_hit") / max(len(recent), 1)
            target    = current + (0.1 if sl_hit_pct > 0.65 else -0.05)
            updated["risk"]["atr_sl_multiplier"] = self._clamp_adapt(current, target, key)

        # --- ATR TP multiplier ---
        if "risk.atr_tp_multiplier" in self.tunable:
            key     = "risk.atr_tp_multiplier"
            current = updated["risk"]["atr_tp_multiplier"]
            tp_hit_pct = sum(1 for t in recent if t.exit_reason == "tp_hit" and t.was_winner) / max(len(recent), 1)
            # If TP is being hit easily, push it further; if rarely hit, pull closer
            target  = current + (0.1 if tp_hit_pct < 0.3 else -0.05)
            updated["risk"]["atr_tp_multiplier"] = self._clamp_adapt(current, target, key)

        logger.info("Adaptation complete. Changes applied.")
        return updated

    def get_snapshot(self) -> PerformanceSnapshot:
        """Return a full performance snapshot."""
        best, worst = self._best_worst_setups()
        return PerformanceSnapshot(
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            total_trades=len(self.trades),
            win_rate=self.win_rate,
            expectancy=self.expectancy,
            profit_factor=self.profit_factor,
            max_drawdown=self.max_drawdown,
            current_drawdown=self.current_drawdown,
            sharpe_ratio=self.sharpe_ratio,
            balance=self.current_balance,
            peak_balance=self.peak_balance,
            best_setup=best,
            worst_setup=worst,
        )

    def setup_confidence_multiplier(self, regime: str, direction: str) -> float:
        """
        Returns a multiplier (0.8–1.2) for the confidence threshold
        based on how well this specific setup has performed historically.
        Better-performing setups get a slightly lower bar.
        """
        key = f"{regime}_{direction}"
        stats = self.setup_stats.get(key)
        if stats is None or stats.total_trades < 5:
            return 1.0  # Not enough data → neutral

        ema_score = stats.ema_score  # 0–100

        # Map ema_score to multiplier: 80+ → 0.9 (easier bar), 30- → 1.15
        if ema_score >= 70:
            return 0.90
        if ema_score >= 55:
            return 0.95
        if ema_score <= 35:
            return 1.15
        if ema_score <= 45:
            return 1.08
        return 1.0

    # ─── Computed Properties ──────────────────────────────────────

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.was_winner) / len(self.trades)

    @property
    def expectancy(self) -> float:
        """Mean R-multiple per trade."""
        if not self.trades:
            return 0.0
        return sum(t.r_multiple for t in self.trades) / len(self.trades)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss   = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    @property
    def max_drawdown(self) -> float:
        """Maximum peak-to-trough drawdown as fraction."""
        if len(self.balance_curve) < 2:
            return 0.0
        peak = self.balance_curve[0]
        mdd  = 0.0
        for bal in self.balance_curve:
            peak = max(peak, bal)
            dd   = (peak - bal) / peak if peak > 0 else 0.0
            mdd  = max(mdd, dd)
        return mdd

    @property
    def current_drawdown(self) -> float:
        if self.peak_balance <= 0:
            return 0.0
        return (self.peak_balance - self.current_balance) / self.peak_balance

    @property
    def sharpe_ratio(self) -> float:
        """Annualised Sharpe ratio from R-multiples (proxy for returns)."""
        if len(self.trades) < 10:
            return 0.0
        rs = [t.r_multiple for t in self.trades]
        mean_r = sum(rs) / len(rs)
        std_r  = math.sqrt(sum((r - mean_r) ** 2 for r in rs) / len(rs))
        if std_r == 0:
            return 0.0
        # Assume ~252 trades/year as rough annualisation
        return (mean_r / std_r) * math.sqrt(252)

    # ─── Internal ─────────────────────────────────────────────────

    def _update_setup_stats(self, trade: TradeRecord) -> None:
        key = f"{trade.regime}_{trade.direction}"
        if key not in self.setup_stats:
            self.setup_stats[key] = SetupStats(setup_key=key)

        stats = self.setup_stats[key]
        stats.total_trades += 1

        if trade.was_winner:
            stats.wins += 1
            stats.avg_win_r = (
                (stats.avg_win_r * (stats.wins - 1) + trade.r_multiple) / stats.wins
            )
        else:
            stats.losses += 1
            if stats.losses > 0:
                stats.avg_loss_r = (
                    (stats.avg_loss_r * (stats.losses - 1) + abs(trade.r_multiple)) / stats.losses
                )

        stats.total_pnl += trade.pnl
        stats.total_r   += trade.r_multiple

        # EMA score: blend of win_rate and expectancy, normalised to 0–100
        trade_score = (
            60.0 * float(trade.was_winner) +    # Win/loss binary: 60 pts
            40.0 * min(1.0, max(0.0, (trade.r_multiple + 1.0) / 3.0))  # R-scaled: up to 40 pts
        )
        decay = self.ema_decay
        stats.ema_score = decay * stats.ema_score + (1 - decay) * trade_score
        stats.last_updated = datetime.now(tz=timezone.utc).isoformat()

    def _score_correlation(self, score_field: str, long_only: bool = True) -> float:
        """Win rate for trades where a specific score was above median."""
        direction = "long" if long_only else "short"
        relevant  = [t for t in self.trades if t.direction == direction]
        if len(relevant) < 10:
            return 0.5

        scores = [getattr(t, score_field, 50.0) for t in relevant]
        median = sorted(scores)[len(scores) // 2]
        high_score_trades = [t for t, s in zip(relevant, scores) if s >= median]

        if not high_score_trades:
            return 0.5
        return sum(1 for t in high_score_trades if t.was_winner) / len(high_score_trades)

    def _clamp_adapt(self, current: float, target: float, key: str) -> float:
        """Nudge current toward target by at most adaptation_rate, then clamp to bounds."""
        bounds = self.bounds.get(key)
        if bounds is None:
            return current

        # Max change per cycle
        max_delta = abs(current) * self.adaptation_rate
        change    = max(-max_delta, min(max_delta, target - current))
        new_val   = current + change
        return max(bounds[0], min(bounds[1], new_val))

    def _check_performance_warnings(self) -> None:
        if len(self.trades) < 20:
            return
        if self.win_rate < self.min_wr_warn:
            logger.warning("Win rate %.1f%% below threshold %.1f%%",
                           self.win_rate * 100, self.min_wr_warn * 100)
        if self.sharpe_ratio < self.min_sharpe_warn:
            logger.warning("Sharpe %.2f below threshold %.2f",
                           self.sharpe_ratio, self.min_sharpe_warn)
        if self.current_drawdown > self.max_dd_warn:
            logger.warning("Current drawdown %.2f%% above warning level %.2f%%",
                           self.current_drawdown * 100, self.max_dd_warn * 100)

    def _best_worst_setups(self) -> tuple[str, str]:
        if not self.setup_stats:
            return ("none", "none")
        items = [(k, v) for k, v in self.setup_stats.items() if v.total_trades >= 5]
        if not items:
            return ("insufficient_data", "insufficient_data")
        best  = max(items, key=lambda x: x[1].ema_score)
        worst = min(items, key=lambda x: x[1].ema_score)
        return best[0], worst[0]

    # ─── Persistence ──────────────────────────────────────────────

    def _save_state(self) -> None:
        try:
            Path(self.track_file).parent.mkdir(parents=True, exist_ok=True)
            state = {
                "trades":          [asdict(t) for t in self.trades[-500:]],  # Keep last 500
                "setup_stats":     {k: asdict(v) for k, v in self.setup_stats.items()},
                "balance_curve":   self.balance_curve[-1000:],
                "peak_balance":    self.peak_balance,
                "current_balance": self.current_balance,
                "last_retune":     self.last_retune_time.isoformat() if self.last_retune_time else None,
            }
            with open(self.track_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error("Failed to save performance state: %s", e)

    def _load_state(self) -> None:
        if not os.path.exists(self.track_file):
            return
        try:
            with open(self.track_file) as f:
                state = json.load(f)
            self.trades          = [TradeRecord(**t) for t in state.get("trades", [])]
            self.balance_curve   = state.get("balance_curve", [])
            self.peak_balance    = state.get("peak_balance", 0.0)
            self.current_balance = state.get("current_balance", 0.0)
            raw_setup = state.get("setup_stats", {})
            self.setup_stats = {k: SetupStats(**v) for k, v in raw_setup.items()}
            lt = state.get("last_retune")
            self.last_retune_time = datetime.fromisoformat(lt) if lt else None
            logger.info("Loaded performance state: %d trades", len(self.trades))
        except Exception as e:
            logger.error("Failed to load performance state: %s", e)
