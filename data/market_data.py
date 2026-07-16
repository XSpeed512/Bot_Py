"""
data/market_data.py
===================
Market Data Feed — MetaTrader 5

Sostituisce il vecchio market_data.py con un adattatore che:
  1. Scarica dati OHLCV da MT5 (invariato rispetto all'originale)
  2. Restituisce DataFrame con colonne standard [open, high, low, close, volume]
     compatibili con tutti i moduli avanzati (regime, mtf, scoring, filters)
  3. Calcola gli indicatori base (EMA, RSI, ATR) necessari all'entry_strategy
  4. Espone get_spread_pct() per il filtro spread

Nessuna logica di segnale qui — solo dati puliti.
"""

from __future__ import annotations
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional

import MetaTrader5 as mt5

import config
from utils.logger import get_logger

logger = get_logger(__name__)


class MarketData:
    """
    Interfaccia unica per tutti i dati di mercato MT5.
    Usata sia dalla entry_strategy originale sia dai nuovi moduli avanzati.
    """

    def __init__(self):
        self._connected = False

    # ─── Connessione ──────────────────────────────────────────────

    def connect(self) -> bool:
        """Inizializza la connessione MT5."""
        if not mt5.initialize():
            logger.error("MT5 initialize() fallito: %s", mt5.last_error())
            return False

        if config.MT5_LOGIN:
            ok = mt5.login(
                login=config.MT5_LOGIN,
                password=config.MT5_PASSWORD,
                server=config.MT5_SERVER,
            )
            if not ok:
                logger.error("MT5 login fallito: %s", mt5.last_error())
                return False

        self._connected = True
        info = mt5.account_info()
        logger.info(
            "MT5 connesso — Account: %s | Balance: %.2f %s",
            info.login, info.balance, info.currency
        )
        return True

    def disconnect(self) -> None:
        mt5.shutdown()
        self._connected = False
        logger.info("MT5 disconnesso")

    # ─── OHLCV — usato dai moduli avanzati ────────────────────────

    def get_ohlcv(
        self,
        symbol: str,
        timeframe: int,          # mt5.TIMEFRAME_* constant
        limit: int = 300,
    ) -> Optional[pd.DataFrame]:
        """
        Scarica barre OHLCV da MT5 e restituisce un DataFrame
        con colonne standard: [open, high, low, close, volume, timestamp].

        Compatibile con: RegimeDetector, MTFEngine, ScoringEngine, EntryFilterEngine.

        Parameters
        ----------
        symbol    : es. "EURUSD"
        timeframe : mt5.TIMEFRAME_H1, mt5.TIMEFRAME_H4, ecc.
        limit     : numero di barre da scaricare

        Returns
        -------
        DataFrame ordinato dal più vecchio al più recente, oppure None.
        """
        if not self._connected:
            logger.error("get_ohlcv chiamato prima di connect()")
            return None

        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, limit)
        if rates is None or len(rates) == 0:
            logger.warning("Nessun dato ricevuto per %s TF=%d", symbol, timeframe)
            return None

        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s")
        df = df.rename(columns={
            "open":       "open",
            "high":       "high",
            "low":        "low",
            "close":      "close",
            "tick_volume": "volume",
        })
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]
        df = df.sort_values("timestamp").reset_index(drop=True)

        return df

    def get_ohlcv_htf(self, symbol: str) -> Optional[pd.DataFrame]:
        """Shortcut: scarica il timeframe alto (HTF) configurato."""
        return self.get_ohlcv(symbol, config.TIMEFRAME_HTF, config.CANDLES_LOOKBACK_HTF)

    def get_ohlcv_ltf(self, symbol: str) -> Optional[pd.DataFrame]:
        """Shortcut: scarica il timeframe basso (LTF) configurato."""
        return self.get_ohlcv(symbol, config.TIMEFRAME, config.CANDLES_LOOKBACK)

    # ─── Spread ───────────────────────────────────────────────────

    def get_spread_pct(self, symbol: str) -> float:
        """
        Spread corrente espresso come percentuale del prezzo Ask.
        Usato dal filtro spread di EntryFilterEngine.
        """
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or tick.ask == 0:
            return 0.0
        spread = (tick.ask - tick.bid) / tick.ask * 100
        return round(spread, 5)

    def get_spread_points(self, symbol: str) -> float:
        """Spread in punti (per compatibilità con il vecchio risk_manager)."""
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return 0.0
        info = mt5.symbol_info(symbol)
        if info is None or info.point == 0:
            return 0.0
        return (tick.ask - tick.bid) / info.point

    # ─── Account ──────────────────────────────────────────────────

    def get_account_balance(self) -> float:
        """Balance corrente del conto MT5."""
        info = mt5.account_info()
        return float(info.balance) if info else 0.0

    def get_account_equity(self) -> float:
        info = mt5.account_info()
        return float(info.equity) if info else 0.0

    def get_open_positions(self) -> list[dict]:
        """
        Restituisce lista di posizioni aperte come dict.
        Compatibile con position_manager originale.
        """
        positions = mt5.positions_get()
        if not positions:
            return []
        result = []
        for p in positions:
            result.append({
                "ticket":     p.ticket,
                "symbol":     p.symbol,
                "type":       "long" if p.type == mt5.ORDER_TYPE_BUY else "short",
                "volume":     p.volume,
                "open_price": p.price_open,
                "sl":         p.sl,
                "tp":         p.tp,
                "profit":     p.profit,
                "magic":      p.magic,
                "comment":    p.comment,
            })
        return result

    # ─── Indicatori base (per entry_strategy e compatibilità) ─────

    def get_indicators(self, symbol: str) -> Optional[dict]:
        """
        Calcola gli indicatori base sul LTF e li restituisce come dict.
        Usato dall'entry_strategy originale (ora come fallback).
        I moduli avanzati calcolano i propri indicatori internamente.
        """
        df = self.get_ohlcv_ltf(symbol)
        if df is None or len(df) < config.EMA_SLOW + 10:
            return None

        close = df["close"]

        ema_fast = float(close.ewm(span=config.EMA_FAST, adjust=False).mean().iloc[-1])
        ema_slow = float(close.ewm(span=config.EMA_SLOW, adjust=False).mean().iloc[-1])

        # ATR
        high  = df["high"]
        low   = df["low"]
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr_series  = tr.ewm(span=config.ATR_PERIOD, adjust=False).mean()
        atr         = float(atr_series.iloc[-1])
        atr_avg     = float(atr_series.rolling(config.ATR_AVG_PERIOD).mean().iloc[-1])

        # RSI
        delta  = close.diff()
        gain   = delta.clip(lower=0)
        loss   = (-delta).clip(lower=0)
        avg_g  = gain.ewm(alpha=1 / config.RSI_PERIOD, min_periods=config.RSI_PERIOD, adjust=False).mean()
        avg_l  = loss.ewm(alpha=1 / config.RSI_PERIOD, min_periods=config.RSI_PERIOD, adjust=False).mean()
        rs     = avg_g / avg_l.replace(0, np.nan)
        rsi    = float((100 - 100 / (1 + rs)).iloc[-1])

        return {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "rsi":      rsi,
            "atr":      atr,
            "atr_avg":  atr_avg,
            "close":    float(close.iloc[-1]),
            "df":       df,             # DataFrame completo disponibile per i nuovi moduli
        }

    # ─── Informazioni simbolo ─────────────────────────────────────

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.warning("Symbol info non disponibile per %s", symbol)
            return None
        return {
            "point":          info.point,
            "digits":         info.digits,
            "contract_size":  info.trade_contract_size,
            "volume_min":     info.volume_min,
            "volume_max":     info.volume_max,
            "volume_step":    info.volume_step,
            "spread":         info.spread,
        }

    def get_current_price(self, symbol: str) -> Optional[tuple[float, float]]:
        """Restituisce (bid, ask) correnti."""
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return tick.bid, tick.ask
