"""
backtest/engine.py
==================
Backtesting Framework

Implements:
  1. Walk-Forward Validation  — split data into N folds, train on IS, test on OOS
  2. Monte Carlo Simulation   — randomise trade sequence to estimate robustness
  3. Out-of-sample protection — always holds back a final OOS block untouched

Anti-overfitting principles:
  - Parameters are ONLY optimised on in-sample data
  - OOS results are the true performance estimate
  - If IS >> OOS performance, flag as potentially overfit
  - Use conservative commission and slippage estimates

Usage:
  engine = BacktestEngine(cfg, strategy_fn)
  results = engine.walk_forward(df_htf, df_ltf)
  mc_stats = engine.monte_carlo(results.trades)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Optional
import pandas as pd
import numpy as np
import logging
import random
import math

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    entry_bar: int
    exit_bar: int
    direction: str
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    pnl: float
    pnl_pct: float
    r_multiple: float
    was_winner: bool
    exit_reason: str
    commission: float
    slippage: float


@dataclass
class FoldResult:
    fold_id: int
    is_start: int
    is_end: int
    oos_start: int
    oos_end: int
    is_trades: list[BacktestTrade]
    oos_trades: list[BacktestTrade]
    is_win_rate: float
    oos_win_rate: float
    is_expectancy: float
    oos_expectancy: float
    is_profit_factor: float
    oos_profit_factor: float
    overfitting_score: float   # 0 = no overfit, 1 = severe overfit


@dataclass
class BacktestResult:
    fold_results: list[FoldResult]
    all_oos_trades: list[BacktestTrade]
    total_win_rate: float
    total_expectancy: float
    total_profit_factor: float
    max_drawdown: float
    sharpe_ratio: float
    calmar_ratio: float
    total_return_pct: float
    overfitting_risk: str      # low | medium | high
    summary: str


@dataclass
class MonteCarloStats:
    median_return: float
    percentile_5: float
    percentile_95: float
    probability_of_ruin: float   # % of simulations ending in drawdown > 20%
    expected_max_drawdown: float
    median_final_balance: float


class BacktestEngine:

    def __init__(self, cfg: dict, strategy_fn: Callable):
        """
        Parameters
        ----------
        cfg         : Full config dict
        strategy_fn : Callable(df_htf, df_ltf, cfg) → list[BacktestTrade]
                      Your strategy function that returns trades for a given data slice.
        """
        bt = cfg["backtest"]
        self.n_folds        = bt["walk_forward_folds"]
        self.oos_ratio      = bt["oos_ratio"]
        self.mc_runs        = bt["monte_carlo_runs"]
        self.commission_pct = bt["commission_pct"] / 100.0
        self.slippage_pct   = bt["slippage_pct"] / 100.0
        self.strategy_fn    = strategy_fn
        self.cfg            = cfg

    # ─── Walk-Forward Validation ───────────────────────────────────

    def walk_forward(
        self,
        df_htf: pd.DataFrame,
        df_ltf: pd.DataFrame,
    ) -> BacktestResult:
        """
        Split data into n_folds windows. For each fold:
          - In-sample (IS):  first (1 - oos_ratio) fraction
          - Out-of-sample (OOS): last oos_ratio fraction (NEVER used for optimisation)

        The final oos_ratio of total data is always held out as
        a terminal OOS block — never touched during any fold.
        """
        n           = len(df_ltf)
        # Reserve final 20% as terminal OOS (never seen during fold construction)
        terminal_oos_start = int(n * 0.80)
        working_n   = terminal_oos_start

        fold_size   = working_n // self.n_folds
        oos_size    = int(fold_size * self.oos_ratio)
        is_size     = fold_size - oos_size

        fold_results = []
        all_oos_trades: list[BacktestTrade] = []

        for fold_id in range(self.n_folds):
            fold_start = fold_id * fold_size
            fold_end   = fold_start + fold_size

            is_start   = fold_start
            is_end     = fold_start + is_size
            oos_start  = is_end
            oos_end    = fold_end

            # Align HTF slice
            ratio_htf = len(df_htf) / n
            htf_is    = df_htf.iloc[int(is_start * ratio_htf): int(is_end * ratio_htf)].reset_index(drop=True)
            htf_oos   = df_htf.iloc[int(oos_start * ratio_htf): int(oos_end * ratio_htf)].reset_index(drop=True)
            ltf_is    = df_ltf.iloc[is_start:is_end].reset_index(drop=True)
            ltf_oos   = df_ltf.iloc[oos_start:oos_end].reset_index(drop=True)

            if len(ltf_is) < 100 or len(ltf_oos) < 20:
                logger.warning("Fold %d: insufficient data, skipping", fold_id)
                continue

            logger.info("Fold %d: IS=%d-%d OOS=%d-%d", fold_id, is_start, is_end, oos_start, oos_end)

            # Run strategy on IS (for metrics only — no parameter optimisation here)
            is_trades  = self._run_strategy(htf_is, ltf_is)
            oos_trades = self._run_strategy(htf_oos, ltf_oos)

            # Apply realistic costs to all trades
            is_trades  = [self._apply_costs(t) for t in is_trades]
            oos_trades = [self._apply_costs(t) for t in oos_trades]

            all_oos_trades.extend(oos_trades)

            is_metrics  = self._compute_metrics(is_trades)
            oos_metrics = self._compute_metrics(oos_trades)

            # Overfitting score: how much worse is OOS vs IS?
            # If IS expectancy is 3× OOS expectancy → likely overfit
            overfit = self._overfitting_score(is_metrics, oos_metrics)

            fold_results.append(FoldResult(
                fold_id=fold_id,
                is_start=is_start, is_end=is_end,
                oos_start=oos_start, oos_end=oos_end,
                is_trades=is_trades, oos_trades=oos_trades,
                is_win_rate=is_metrics["win_rate"],
                oos_win_rate=oos_metrics["win_rate"],
                is_expectancy=is_metrics["expectancy"],
                oos_expectancy=oos_metrics["expectancy"],
                is_profit_factor=is_metrics["profit_factor"],
                oos_profit_factor=oos_metrics["profit_factor"],
                overfitting_score=overfit,
            ))

        # Terminal OOS (never seen in any fold)
        htf_terminal = df_htf.iloc[int(terminal_oos_start * ratio_htf):].reset_index(drop=True)
        ltf_terminal = df_ltf.iloc[terminal_oos_start:].reset_index(drop=True)
        terminal_trades = [self._apply_costs(t) for t in self._run_strategy(htf_terminal, ltf_terminal)]
        all_oos_trades.extend(terminal_trades)

        # Aggregate OOS metrics
        all_metrics   = self._compute_metrics(all_oos_trades)
        overfit_risk  = self._aggregate_overfit_risk(fold_results)

        result = BacktestResult(
            fold_results=fold_results,
            all_oos_trades=all_oos_trades,
            total_win_rate=all_metrics["win_rate"],
            total_expectancy=all_metrics["expectancy"],
            total_profit_factor=all_metrics["profit_factor"],
            max_drawdown=all_metrics["max_drawdown"],
            sharpe_ratio=all_metrics["sharpe"],
            calmar_ratio=all_metrics["calmar"],
            total_return_pct=all_metrics["total_return_pct"],
            overfitting_risk=overfit_risk,
            summary=self._build_summary(all_metrics, fold_results, overfit_risk),
        )

        logger.info(
            "Walk-forward complete: WR=%.1f%% Exp=%.3f PF=%.2f DD=%.1f%% Sharpe=%.2f Overfit=%s",
            result.total_win_rate * 100, result.total_expectancy,
            result.total_profit_factor, result.max_drawdown * 100,
            result.sharpe_ratio, result.overfitting_risk
        )
        return result

    # ─── Monte Carlo Simulation ────────────────────────────────────

    def monte_carlo(
        self,
        trades: list[BacktestTrade],
        starting_balance: float = 5000.0,
    ) -> MonteCarloStats:
        """
        Randomly resample the trade sequence self.mc_runs times.
        Estimates the distribution of outcomes and probability of ruin.
        """
        if len(trades) < 10:
            logger.warning("Monte Carlo: insufficient trades (%d)", len(trades))
            return MonteCarloStats(0, 0, 0, 1.0, 1.0, starting_balance)

        r_multiples = [t.r_multiple for t in trades]
        risk_per_trade = starting_balance * 0.01  # 1% risk assumption

        final_balances = []
        max_drawdowns  = []
        ruin_count     = 0
        ruin_threshold = 0.20  # 20% drawdown = "ruin" for this simulation

        for _ in range(self.mc_runs):
            sequence    = random.choices(r_multiples, k=len(r_multiples))
            balance     = starting_balance
            peak        = starting_balance
            max_dd      = 0.0

            for r in sequence:
                pnl      = r * risk_per_trade
                balance += pnl
                peak     = max(peak, balance)
                dd       = (peak - balance) / peak if peak > 0 else 0.0
                max_dd   = max(max_dd, dd)

                if balance <= 0:
                    balance = 0.0
                    break

            final_balances.append(balance)
            max_drawdowns.append(max_dd)
            if max_dd >= ruin_threshold:
                ruin_count += 1

        final_balances.sort()
        n = len(final_balances)

        stats = MonteCarloStats(
            median_return=(final_balances[n // 2] - starting_balance) / starting_balance,
            percentile_5=(final_balances[int(n * 0.05)] - starting_balance) / starting_balance,
            percentile_95=(final_balances[int(n * 0.95)] - starting_balance) / starting_balance,
            probability_of_ruin=ruin_count / self.mc_runs,
            expected_max_drawdown=sum(max_drawdowns) / len(max_drawdowns),
            median_final_balance=final_balances[n // 2],
        )

        logger.info(
            "Monte Carlo (%d runs): median=%.1f%% 5th=%.1f%% POR=%.1f%% E[MDD]=%.1f%%",
            self.mc_runs,
            stats.median_return * 100, stats.percentile_5 * 100,
            stats.probability_of_ruin * 100, stats.expected_max_drawdown * 100
        )
        return stats

    # ─── Helpers ──────────────────────────────────────────────────

    def _run_strategy(
        self,
        df_htf: pd.DataFrame,
        df_ltf: pd.DataFrame,
    ) -> list[BacktestTrade]:
        """Delegate to the injected strategy function."""
        try:
            return self.strategy_fn(df_htf, df_ltf, self.cfg)
        except Exception as e:
            logger.error("Strategy function raised during backtest: %s", e)
            return []

    def _apply_costs(self, trade: BacktestTrade) -> BacktestTrade:
        """Deduct commission and slippage from trade PnL."""
        trade_value = trade.entry_price * trade.position_size
        cost        = trade_value * (self.commission_pct * 2 + self.slippage_pct)
        trade.commission = cost
        trade.pnl       -= cost
        trade.was_winner = trade.pnl > 0
        return trade

    def _compute_metrics(self, trades: list[BacktestTrade]) -> dict:
        if not trades:
            return {
                "win_rate": 0.0, "expectancy": 0.0, "profit_factor": 0.0,
                "max_drawdown": 0.0, "sharpe": 0.0, "calmar": 0.0,
                "total_return_pct": 0.0,
            }

        winners = [t for t in trades if t.was_winner]
        losers  = [t for t in trades if not t.was_winner]

        win_rate      = len(winners) / len(trades)
        expectancy    = sum(t.r_multiple for t in trades) / len(trades)
        gross_profit  = sum(t.pnl for t in winners)
        gross_loss    = abs(sum(t.pnl for t in losers))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Drawdown on cumulative PnL curve
        cumulative = 0.0
        peak       = 0.0
        max_dd     = 0.0
        for t in trades:
            cumulative += t.pnl
            peak        = max(peak, cumulative)
            dd          = (peak - cumulative) / peak if peak > 0 else 0.0
            max_dd      = max(max_dd, dd)

        # Sharpe (R-multiple based)
        rs = [t.r_multiple for t in trades]
        mean_r = sum(rs) / len(rs)
        std_r  = math.sqrt(sum((r - mean_r) ** 2 for r in rs) / len(rs)) if len(rs) > 1 else 0.0
        sharpe = (mean_r / std_r) * math.sqrt(252) if std_r > 0 else 0.0

        total_pnl        = sum(t.pnl for t in trades)
        total_return_pct = total_pnl / 5000.0 * 100  # Relative to starting capital

        calmar = (total_return_pct / 100) / max_dd if max_dd > 0 else 0.0

        return {
            "win_rate": win_rate, "expectancy": expectancy,
            "profit_factor": profit_factor, "max_drawdown": max_dd,
            "sharpe": sharpe, "calmar": calmar,
            "total_return_pct": total_return_pct,
        }

    def _overfitting_score(self, is_m: dict, oos_m: dict) -> float:
        """
        Score 0–1 where 0 = no overfit, 1 = severe overfit.
        Based on degradation of expectancy and win rate IS→OOS.
        """
        if is_m["expectancy"] <= 0:
            return 0.5  # Neutral if IS isn't even profitable

        exp_degradation = max(0.0, 1.0 - (oos_m["expectancy"] / (is_m["expectancy"] + 1e-9)))
        wr_degradation  = max(0.0, is_m["win_rate"] - oos_m["win_rate"])

        return min(1.0, 0.6 * exp_degradation + 0.4 * wr_degradation * 2)

    def _aggregate_overfit_risk(self, folds: list[FoldResult]) -> str:
        if not folds:
            return "unknown"
        avg_overfit = sum(f.overfitting_score for f in folds) / len(folds)
        if avg_overfit < 0.25:
            return "low"
        if avg_overfit < 0.50:
            return "medium"
        return "high"

    def _build_summary(self, m: dict, folds: list[FoldResult], overfit: str) -> str:
        return (
            f"Walk-Forward ({len(folds)} folds) | "
            f"OOS WR: {m['win_rate']:.1%} | "
            f"Expectancy: {m['expectancy']:.3f}R | "
            f"PF: {m['profit_factor']:.2f} | "
            f"MDD: {m['max_drawdown']:.1%} | "
            f"Sharpe: {m['sharpe']:.2f} | "
            f"Return: {m['total_return_pct']:.1f}% | "
            f"Overfit risk: {overfit}"
        )
