"""
strategy/entry_strategy.py
===========================
Entry Strategy — Motore Segnali Adattivo

Questo è il cuore del merge. Sostituisce la vecchia logica binaria
EMA/RSI/ATR con il nuovo sistema a scoring multi-fattore.

Pipeline per ogni simbolo:
  1. RegimeDetector  → classifica il mercato (trending/ranging/high_vol/low_vol)
  2. MTFEngine       → verifica l'allineamento HTF/LTF
  3. ScoringEngine   → calcola confidence score (0–100) su 5 componenti
  4. EntryFilterEngine → 7 filtri smart (fakeout, sweep, S/R, spread, sessione...)
  5. RiskManager     → calcola SL, TP, volume in lotti MT5
  6. Ritorna TradeSignal oppure None

Compatibilità: l'interfaccia verso main.py rimane identica —
check_entry_signal(symbol, market_data, risk_manager) → TradeSignal | None
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone

import pandas as pd

import config
from data.market_data import MarketData
from risk.risk_manager import RiskManager, TradeSetup
from strategy.regime import RegimeDetector, Regime
from strategy.mtf import MTFEngine, Bias
from signals.scoring import ScoringEngine, Direction, SignalScore
from signals.filters import EntryFilterEngine
from self_improve.tracker import PerformanceTracker
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TradeSignal:
    """
    Segnale di trading pronto per essere inviato a broker_connector.
    Compatibile con il formato atteso da main.py.
    """
    symbol: str
    direction: str          # "buy" | "sell"
    volume: float
    entry_price: float
    stop_loss: float
    take_profit: float
    atr: float
    confidence: float
    trade_quality: float
    risk_score: float
    regime: str
    r_r_ratio: float
    # Ticket MT5 — impostato da main.py dopo l'apertura dell'ordine
    _ticket: int = 0
    # Scores dettagliati — salvati nel database per self-improvement
    trend_score: float = 0.0
    momentum_score: float = 0.0
    volume_score: float = 0.0
    structure_score: float = 0.0
    volatility_score: float = 0.0


class EntryStrategy:
    """
    Motore di segnali unificato. Una sola istanza in main.py.
    """

    def __init__(self, tracker: PerformanceTracker):
        adv_cfg = config.build_advanced_config()

        self.regime_detector = RegimeDetector(adv_cfg)
        self.mtf_engine      = MTFEngine(adv_cfg)
        self.scoring_engine  = ScoringEngine(adv_cfg)
        self.filter_engine   = EntryFilterEngine(adv_cfg)
        self.tracker         = tracker
        self._adv_cfg        = adv_cfg

    # ─── API pubblica ─────────────────────────────────────────────

    def check_entry_signal(
        self,
        symbol: str,
        market_data: MarketData,
        risk_manager: RiskManager,
        open_positions_count: int = 0,
    ) -> Optional[TradeSignal]:
        """
        Analizza un simbolo e ritorna un TradeSignal se tutte le
        condizioni sono soddisfatte, altrimenti None.

        Parameters
        ----------
        symbol      : Es. "EURUSD"
        market_data : Istanza di MarketData (connessa a MT5)
        risk_manager: Istanza di RiskManager (con balance aggiornato)
        """

        # ── 1. Scarica dati HTF e LTF ─────────────────────────
        df_htf = market_data.get_ohlcv_htf(symbol)
        df_ltf = market_data.get_ohlcv_ltf(symbol)

        if df_htf is None or df_ltf is None:
            logger.debug("%s: dati non disponibili", symbol)
            return None

        if len(df_ltf) < 220 or len(df_htf) < 50:
            logger.debug("%s: barre insufficienti (LTF=%d HTF=%d)",
                         symbol, len(df_ltf), len(df_htf))
            return None

        # ── 2. Spread check preventivo ────────────────────────
        spread = market_data.get_spread_pct(symbol)

        # ── 3. Market regime detection ────────────────────────
        regime = self.regime_detector.detect(df_htf)
        logger.info("%s | Regime: %s | ADX=%.1f ATR_ratio=%.2f conf=%.0f%%",
                    symbol, regime.regime.value,
                    regime.adx, regime.atr_ratio, regime.confidence * 100)

        # High volatility: blocca nuovi ingressi
        if regime.regime == Regime.HIGH_VOL:
            logger.info("%s: HIGH_VOL — nessun ingresso", symbol)
            return None

        # ── 4. Multi-timeframe alignment ──────────────────────
        mtf = self.mtf_engine.analyse(df_htf, df_ltf)

        if not mtf.aligned:
            logger.info("%s: MTF non allineato (HTF=%s LTF=%s) — skip",
                        symbol, mtf.htf_bias.value, mtf.ltf_bias.value)
            return None

        # ── 5. Confidence scoring ─────────────────────────────
        signal: SignalScore = self.scoring_engine.score(df_ltf, regime, mtf)

        if not signal.is_valid:
            logger.info("%s: nessun segnale valido — motivo: %s",
                        symbol, signal.metadata.get("reason", "no_signal"))
            return None

        # Soglia minima confidence adattata per setup specifico
        setup_mult      = self.tracker.setup_confidence_multiplier(
            regime.regime.value, signal.direction.value
        )
        min_conf        = self._adv_cfg["scoring"]["min_confidence"] * setup_mult
        min_quality     = self._adv_cfg["scoring"]["min_trade_quality"]
        max_risk        = self._adv_cfg["scoring"]["max_risk_score"]

        if signal.confidence < min_conf:
            logger.info("%s: confidence %.1f < soglia %.1f — skip",
                        symbol, signal.confidence, min_conf)
            return None

        if signal.trade_quality < min_quality:
            logger.info("%s: qualità %.1f < soglia %.1f — skip",
                        symbol, signal.trade_quality, min_quality)
            return None

        if signal.risk_score > max_risk:
            logger.info("%s: risk score %.1f > soglia %.1f — skip",
                        symbol, signal.risk_score, max_risk)
            return None

        # ── 6. Smart entry filters ────────────────────────────
        filter_result = self.filter_engine.apply(
            df_ltf,
            signal,
            current_spread_pct=spread,
            current_time=datetime.now(tz=timezone.utc),
        )

        if not filter_result.passed:
            logger.info("%s: filtro entry rifiutato — %s",
                        symbol, filter_result.rejection_reason)
            return None

        # Aggiusta confidence con i bonus/malus dei filtri
        adj_confidence = signal.confidence + filter_result.confidence_adjustment
        if adj_confidence < min_conf:
            logger.info("%s: confidence post-filtro %.1f < soglia %.1f — skip",
                        symbol, adj_confidence, min_conf)
            return None

        # ── 7. Risk management ────────────────────────────────
        open_count = len(risk_manager.account.__dict__)  # approssimato
        setup: TradeSetup = risk_manager.compute_trade_setup(
            symbol=symbol,
            direction=signal.direction,
            df_ltf=df_ltf,
            confidence=adj_confidence,
            atr_ratio=regime.atr_ratio,
            open_positions=open_positions_count,  # FIX: valore reale passato da main.py
        )

        if not setup.approved:
            logger.info("%s: risk manager rifiutato — %s",
                        symbol, setup.rejection_reason)
            return None

        # ── 8. Costruisci e ritorna il segnale ────────────────
        trade_signal = TradeSignal(
            symbol=symbol,
            direction=setup.direction,
            volume=setup.volume,
            entry_price=setup.entry_price,
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            atr=setup.atr,
            confidence=round(adj_confidence, 1),
            trade_quality=round(signal.trade_quality, 1),
            risk_score=round(signal.risk_score, 1),
            regime=regime.regime.value,
            r_r_ratio=setup.r_r_ratio,
            trend_score=signal.trend_score,
            momentum_score=signal.momentum_score,
            volume_score=signal.volume_score,
            structure_score=signal.structure_score,
            volatility_score=signal.volatility_score,
        )

        logger.info(
            "✅ SEGNALE %s %s | conf=%.1f quality=%.1f risk=%.1f | "
            "entry=%.5f SL=%.5f TP=%.5f | %.2f lotti | R/R=%.2f",
            symbol, setup.direction.upper(),
            adj_confidence, signal.trade_quality, signal.risk_score,
            setup.entry_price, setup.stop_loss, setup.take_profit,
            setup.volume, setup.r_r_ratio,
        )

        return trade_signal

    def update_config_from_tracker(self) -> None:
        """
        Aggiorna la configurazione dei moduli interni dopo un ciclo
        di auto-adattamento del tracker.
        Chiamato da main.py dopo tracker.adapt_parameters().
        """
        new_adv_cfg = config.build_advanced_config()

        # Aggiorna solo i parametri auto-tunable
        for key in config.SELF_IMPROVE_TUNABLE:
            if key == "SCORE_MIN_CONFIDENCE":
                new_adv_cfg["scoring"]["min_confidence"] = getattr(config, key)
            elif key == "RSI_BUY_LEVEL":
                new_adv_cfg["indicators"]["rsi_oversold"] = getattr(config, key)
            elif key == "RSI_SELL_LEVEL":
                new_adv_cfg["indicators"]["rsi_overbought"] = getattr(config, key)
            elif key == "SL_ATR_MULTIPLIER":
                new_adv_cfg["risk"]["atr_sl_multiplier"] = getattr(config, key)
            elif key == "TP_ATR_MULTIPLIER":
                new_adv_cfg["risk"]["atr_tp_multiplier"] = getattr(config, key)

        self._adv_cfg = new_adv_cfg
        logger.info("EntryStrategy: configurazione aggiornata dopo adattamento")
