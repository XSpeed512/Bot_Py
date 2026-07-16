"""
risk/risk_manager.py
====================
Risk Manager Unificato

Unisce il risk manager originale (sizing su MT5, break-even, trailing)
con il nuovo RiskEngine adattivo (ATR dinamico, Kelly, ruin protection).

Flusso:
  1. Il nuovo RiskEngine calcola SL, TP, size e approva/rifiuta il trade
  2. Questo modulo traduce i risultati in valori concreti MT5
     (volume in lotti, prezzi con i digits corretti)
  3. Gestisce break-even e trailing stop sulle posizioni aperte

Il vecchio risk_manager è sostituito, ma l'interfaccia verso
broker_connector e position_manager rimane identica.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from datetime import date, datetime, timezone

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

import config
from utils.logger import get_logger
# Nuovo modulo adattivo
from risk.risk_engine import RiskEngine, AccountState, RiskParameters
from signals.scoring import Direction

logger = get_logger(__name__)


@dataclass
class TradeSetup:
    """Output finale pronto per broker_connector.place_order()."""
    symbol: str
    direction: str          # "buy" | "sell"
    volume: float           # Lotti MT5
    entry_price: float
    stop_loss: float
    take_profit: float
    atr: float
    risk_amount: float
    r_r_ratio: float
    approved: bool
    rejection_reason: Optional[str] = None


class RiskManager:
    """
    Punto di accesso unico per tutto il risk management.
    Instanziato una volta in main.py e passato a tutti i moduli.
    """

    def __init__(self):
        adv_cfg           = config.build_advanced_config()
        self._engine      = RiskEngine(adv_cfg)
        self._adv_cfg     = adv_cfg

        # AccountState viene inizializzato con il balance corrente
        # e aggiornato ad ogni ciclo
        self.account = AccountState(
            balance=0.0,
            peak_balance=0.0,
            daily_start_balance=0.0,
            daily_date=datetime.now(tz=timezone.utc).date(),
        )

    # ─── Aggiornamento stato account ──────────────────────────────

    def update_account(self, balance: float) -> None:
        """
        Chiamato all'inizio di ogni ciclo per sincronizzare il balance.
        Aggiorna anche adv_cfg['risk']['account_balance'] usato dal RiskEngine.
        """
        self.account.balance      = balance
        self.account.peak_balance = max(self.account.peak_balance, balance)
        self._adv_cfg["risk"]["account_balance"] = balance

        today = datetime.now(tz=timezone.utc).date()
        if today != self.account.daily_date:
            self.account.daily_start_balance = balance
            self.account.daily_date          = today
            logger.info("Nuovo giorno di trading. Balance giornaliero reset: %.2f", balance)

        # FIX: inizializza daily_start_balance al primo update se è ancora 0
        if self.account.daily_start_balance <= 0 and balance > 0:
            self.account.daily_start_balance = balance

    def update_after_trade(self, was_winner: bool) -> None:
        """Aggiorna contatori win/loss consecutivi."""
        if was_winner:
            self.account.consecutive_wins  += 1
            self.account.consecutive_losses = 0
        else:
            self.account.consecutive_losses += 1
            self.account.consecutive_wins    = 0
        self.account.total_trades += 1

    # ─── Calcolo setup trade ──────────────────────────────────────

    def compute_trade_setup(
        self,
        symbol: str,
        direction: Direction,
        df_ltf: pd.DataFrame,
        confidence: float,
        atr_ratio: float,
        open_positions: int = 0,
    ) -> TradeSetup:
        """
        Calcola tutti i parametri del trade usando il RiskEngine adattivo.
        Restituisce un TradeSetup con volume in lotti MT5 e prezzi arrotondati.

        Parameters
        ----------
        symbol         : Es. "EURUSD"
        direction      : Direction.LONG | Direction.SHORT
        df_ltf         : DataFrame OHLCV LTF
        confidence     : Score di confidenza (0–100)
        atr_ratio      : Rapporto ATR corrente / media (dal RegimeDetector)
        open_positions : Posizioni attualmente aperte
        """
        # Chiamata al nuovo RiskEngine
        risk: RiskParameters = self._engine.compute(
            df=df_ltf,
            direction=direction,
            confidence=confidence,
            account=self.account,
            current_atr_ratio=atr_ratio,
            open_positions=open_positions,
        )

        if not risk.approved:
            return TradeSetup(
                symbol=symbol,
                direction="buy" if direction == Direction.LONG else "sell",
                volume=0.0,
                entry_price=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                atr=0.0,
                risk_amount=0.0,
                r_r_ratio=0.0,
                approved=False,
                rejection_reason=risk.rejection_reason,
            )

        # Ottieni info simbolo per arrotondamento e conversione lotti
        sym_info = mt5.symbol_info(symbol)
        if sym_info is None:
            return TradeSetup(
                symbol=symbol, direction="buy", volume=0.0,
                entry_price=0.0, stop_loss=0.0, take_profit=0.0,
                atr=0.0, risk_amount=0.0, r_r_ratio=0.0,
                approved=False, rejection_reason="symbol_info_unavailable",
            )

        digits = sym_info.digits

        # Converti position_size (unità base) in lotti MT5
        # Per forex: 1 lotto standard = 100,000 unità della valuta base
        contract_size = sym_info.trade_contract_size  # tipicamente 100000
        volume_lots   = risk.position_size / contract_size if contract_size > 0 else 0.0

        # Normalizza al volume step
        step     = sym_info.volume_step
        vol_min  = sym_info.volume_min
        vol_max  = sym_info.volume_max
        if step > 0:
            volume_lots = round(round(volume_lots / step) * step, 8)
        volume_lots = max(vol_min, min(vol_max, volume_lots))

        # Arrotonda prezzi ai digits del simbolo
        entry = round(risk.entry_price, digits)
        sl    = round(risk.stop_loss,   digits)
        tp    = round(risk.take_profit, digits)

        logger.info(
            "%s %s | entry=%.5f SL=%.5f TP=%.5f | vol=%.2f lotti | "
            "rischio=$%.2f | R/R=%.2f | ATR=%.5f",
            symbol, direction.value,
            entry, sl, tp, volume_lots,
            risk.risk_amount, risk.r_r_ratio, risk.atr,
        )

        return TradeSetup(
            symbol=symbol,
            direction="buy" if direction == Direction.LONG else "sell",
            volume=volume_lots,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            atr=risk.atr,
            risk_amount=risk.risk_amount,
            r_r_ratio=risk.r_r_ratio,
            approved=True,
        )

    # ─── Trailing stop / break-even ───────────────────────────────

    def get_updated_sl(
        self,
        direction: str,          # "buy" | "sell"
        entry_price: float,
        current_price: float,
        current_sl: float,
        atr: float,
    ) -> float:
        """
        Calcola il nuovo SL per trailing stop e break-even.
        Delega al RiskEngine adattivo; non sposta mai lo SL in senso sfavorevole.
        """
        dir_enum = Direction.LONG if direction == "buy" else Direction.SHORT
        return self._engine.update_trailing_stop(
            direction=dir_enum,
            entry_price=entry_price,
            current_price=current_price,
            current_sl=current_sl,
            atr=atr,
        )

    # ─── Controllo limiti giornalieri ─────────────────────────────

    def is_daily_limit_reached(self) -> bool:
        """Ritorna True se il limite di perdita giornaliera è stato raggiunto."""
        if self.account.daily_start_balance <= 0:
            return False
        daily_loss_pct = (
            (self.account.daily_start_balance - self.account.balance)
            / self.account.daily_start_balance * 100
        )
        if daily_loss_pct >= config.RUIN_MAX_DAILY_LOSS_PCT:
            logger.warning(
                "Limite perdita giornaliera raggiunto: %.2f%% (limite: %.2f%%)",
                daily_loss_pct, config.RUIN_MAX_DAILY_LOSS_PCT
            )
            return True
        return False

    def is_weekly_limit_reached(self) -> bool:
        """Controllo semplificato perdita settimanale (basato su drawdown da peak)."""
        if self.account.peak_balance <= 0:
            return False
        dd = (self.account.peak_balance - self.account.balance) / self.account.peak_balance * 100
        if dd >= config.WEEKLY_LOSS_LIMIT_PCT:
            logger.warning(
                "Limite perdita settimanale raggiunto: drawdown %.2f%% (limite: %.2f%%)",
                dd, config.WEEKLY_LOSS_LIMIT_PCT
            )
            return True
        return False
