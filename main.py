"""
main.py
=======
Bot Loop Principale — Versione Unificata

Fix applicati rispetto alla versione precedente:
  - BUG FIX: import uuid spostato in cima al file (era dentro il loop)
  - BUG FIX: _record_closed_trades ora usa MT5 history per exit_price
    invece di leggere dal DB (che era ancora OPEN al momento della lettura)
  - BUG FIX: adv_cfg ricreato dopo ogni adapt_parameters
  - BUG FIX: open_positions passato correttamente all'entry_strategy
  - BUG FIX: pnl calcolato usando contract_size da MT5 (supporta XAUUSD e indici)
  - BUG FIX: reconciliation registra i trade anche nel PerformanceTracker
"""

from __future__ import annotations

import argparse
import sys
import time
import signal
import uuid
from datetime import datetime, timezone

import MetaTrader5 as mt5

import config
from utils.logger import get_logger
from data.market_data import MarketData
from execution.broker_connector import open_trade, get_positions, get_closed_deal_by_ticket
from execution.reconciliation import reconcile_trades
from risk.risk_manager import RiskManager
from risk.position_manager import manage_positions
from strategy.entry_strategy import EntryStrategy, TradeSignal
from ai.learning_module import LearningModule
from news.news_filter import NewsEvent
from self_improve.tracker import PerformanceTracker, TradeRecord

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Gestione shutdown graceful
# ---------------------------------------------------------------------------

_running = True

def _on_shutdown(sig, frame):
    global _running
    logger.info("Segnale di shutdown ricevuto. Chiusura bot in corso...")
    _running = False

signal.signal(signal.SIGINT,  _on_shutdown)
signal.signal(signal.SIGTERM, _on_shutdown)


# ---------------------------------------------------------------------------
# Stato trade attivi (in memoria)
# ---------------------------------------------------------------------------

# Mappa symbol → TradeSignal dell'ultima entry aperta
_active_signals: dict[str, TradeSignal] = {}


# ---------------------------------------------------------------------------
# Loop principale
# ---------------------------------------------------------------------------

def run(paper_mode: bool = False) -> None:
    logger.info("=" * 60)
    logger.info("AdaptiveBot avviato | paper=%s | simboli=%s", paper_mode, config.SYMBOLS)
    logger.info("=" * 60)

    # ── Inizializzazione moduli ────────────────────────────────────
    market_data  = MarketData()
    risk_manager = RiskManager()
    learning_mod = LearningModule()
    news_filter  = NewsEvent()

    adv_cfg = config.build_advanced_config()
    tracker = PerformanceTracker(adv_cfg)
    entry_strategy = EntryStrategy(tracker)

    # Connessione MT5
    if not market_data.connect():
        logger.critical("Impossibile connettersi a MT5. Uscita.")
        sys.exit(1)

    logger.info("MT5 connesso. Avvio loop trading.")

    # ── Riconciliazione trade all'avvio ────────────────────────────
    logger.info("Verifica trade chiusi mentre il bot era offline...")
    # FIX: passiamo anche tracker e market_data per aggiornare le statistiche
    recon_report = reconcile_trades(learning_mod, tracker, market_data)
    if recon_report["reconciled"] > 0:
        logger.info(
            "Riconciliazione completata: %d aggiornati | %d ancora aperti | %d falliti",
            recon_report["reconciled"],
            recon_report["still_open"],
            recon_report["failed"],
        )

    # ── Loop principale ────────────────────────────────────────────
    while _running:
        try:
            loop_start = time.time()
            now        = datetime.now(tz=timezone.utc)
            logger.info("=" * 60)
            logger.debug("─── Ciclo %s ───", now.strftime("%Y-%m-%d %H:%M:%S"))

            # 1. Sincronizza balance account
            balance = market_data.get_account_balance()
            risk_manager.update_account(balance)

            # 2. Controlla limiti giornalieri/settimanali
            if risk_manager.is_daily_limit_reached() or risk_manager.is_weekly_limit_reached():
                logger.warning("Limite perdita raggiunto — nessun nuovo trade oggi")
                time.sleep(config.LOOP_INTERVAL_SECONDS)
                continue

            # 3. Gestisci posizioni aperte (break-even, trailing)
            atr_map = _build_atr_map(market_data)
            manage_positions(atr_map)

            # 4. Controlla posizioni chiuse e registra trade
            _record_closed_trades(
                market_data, risk_manager, tracker, learning_mod, balance
            )

            # 5. Conta posizioni aperte
            open_positions = get_positions()
            open_count     = len(open_positions)

            if open_count >= config.MAX_OPEN_TRADES:
                logger.debug("Massimo posizioni aperte (%d) — skip nuove entry", open_count)
                _sleep_to_next_candle(loop_start)
                continue

            # 6. Scansiona simboli per nuovi segnali
            for symbol in config.SYMBOLS:

                # Salta se già in posizione su questo simbolo
                symbol_positions = [p for p in open_positions if p.symbol == symbol]
                if symbol_positions:
                    continue

                # Controlla news filter
                if news_filter.is_news_time(symbol, now):
                    logger.info("%s: news imminenti — skip", symbol)
                    continue

                # FIX: passa il numero reale di posizioni aperte
                signal = entry_strategy.check_entry_signal(
                    symbol=symbol,
                    market_data=market_data,
                    risk_manager=risk_manager,
                    open_positions_count=open_count,
                )

                if signal is None:
                    continue

                # Paper mode: logga senza eseguire
                if paper_mode:
                    logger.info(
                        "[PAPER] %s %s | entry=%.5f SL=%.5f TP=%.5f | "
                        "vol=%.2f lotti | conf=%.1f | regime=%s",
                        signal.symbol, signal.direction.upper(),
                        signal.entry_price, signal.stop_loss, signal.take_profit,
                        signal.volume, signal.confidence, signal.regime,
                    )
                    continue

                # Esegui ordine su MT5
                ticket = open_trade(
                    symbol=signal.symbol,
                    order_type=signal.direction,
                    volume=signal.volume,
                    sl=signal.stop_loss,
                    tp=signal.take_profit,
                )

                if ticket:
                    logger.info("✅ Ordine inviato | %s ticket=%d", symbol, ticket)
                    signal._ticket = ticket  # FIX: salva ticket per _record_closed_trades
                    _active_signals[symbol] = signal
                    learning_mod.record_trade_open(
                        symbol=symbol,
                        direction=signal.direction,
                        entry_price=signal.entry_price,
                        sl=signal.stop_loss,
                        tp=signal.take_profit,
                        volume=signal.volume,
                        confidence=signal.confidence,
                        trade_quality=signal.trade_quality,
                        risk_score=signal.risk_score,
                        regime=signal.regime,
                        ticket=ticket,
                        trend_score=signal.trend_score,
                        momentum_score=signal.momentum_score,
                        volume_score=signal.volume_score,
                        structure_score=signal.structure_score,
                        volatility_score=signal.volatility_score,
                    )
                else:
                    logger.error("❌ Ordine fallito per %s", symbol)

            # 7. Adattamento periodico parametri
            if tracker.should_adapt():
                updated_cfg = tracker.adapt_parameters(adv_cfg)
                _apply_adapted_params(updated_cfg)
                # FIX: ricrea adv_cfg dopo l'adattamento
                adv_cfg = config.build_advanced_config()
                entry_strategy.update_config_from_tracker()
                logger.info("Parametri adattati dopo %d trade", len(tracker.trades))

            _sleep_to_next_candle(loop_start)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.exception("Errore non gestito nel loop principale: %s", e)
            time.sleep(30)

    market_data.disconnect()
    logger.info("Bot fermato.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_atr_map(market_data: MarketData) -> dict[str, float]:
    """Calcola ATR corrente per ogni simbolo (usato da manage_positions)."""
    atr_map = {}
    for symbol in config.SYMBOLS:
        indicators = market_data.get_indicators(symbol)
        if indicators:
            atr_map[symbol] = indicators["atr"]
    return atr_map


def _record_closed_trades(
    market_data: MarketData,
    risk_manager: RiskManager,
    tracker: PerformanceTracker,
    learning_mod: LearningModule,
    current_balance: float,
) -> None:
    """
    Verifica i trade in _active_signals che MT5 ha chiuso.
    
    FIX: recupera exit_price direttamente dalla history MT5 (non dal DB,
    che era ancora OPEN al momento della lettura) e usa contract_size
    reale per calcolare il PnL correttamente su tutti gli strumenti.
    """
    if not _active_signals:
        return

    open_symbols = {p.symbol for p in get_positions()}

    for symbol in list(_active_signals.keys()):
        if symbol in open_symbols:
            continue   # Ancora aperto

        signal = _active_signals.pop(symbol)

        # FIX: leggi exit_price dalla history MT5, non dal DB
        # Il DB è ancora OPEN in questo momento — verrà aggiornato dopo
        closed_deal = get_closed_deal_by_ticket(signal._ticket) if hasattr(signal, "_ticket") else None

        # Fallback: cerca nel DB aperto il ticket
        if closed_deal is None:
            trade_db = learning_mod.get_open_trade_by_symbol(symbol)
            if trade_db and trade_db.get("ticket"):
                closed_deal = get_closed_deal_by_ticket(trade_db["ticket"])

        if closed_deal is None:
            logger.warning("Trade %s chiuso ma deal non trovato nella history MT5", symbol)
            continue

        exit_price  = closed_deal["close_price"]
        profit_mt5  = closed_deal["profit"]   # PnL reale da MT5 — più affidabile
        close_time  = closed_deal["close_time"]

        # Converti close_time
        if isinstance(close_time, (int, float)):
            close_time_iso = datetime.fromtimestamp(close_time, tz=timezone.utc).isoformat()
        else:
            close_time_iso = str(close_time)

        # Determina exit_reason
        from execution.reconciliation import _determine_exit_reason
        trade_dict = {
            "entry": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "direction": signal.direction,
        }
        exit_reason = _determine_exit_reason(trade_dict, exit_price)

        # Aggiorna DB con dati reali
        if hasattr(signal, "_ticket") and signal._ticket:
            learning_mod.update_closed_trade(
                ticket=signal._ticket,
                exit_price=exit_price,
                close_time=close_time_iso,
                exit_reason=exit_reason,
                pnl=profit_mt5,
            )

        # FIX: usa profit_mt5 direttamente — include swap/commissioni
        # e gestisce correttamente XAUUSD, indici, etc.
        pnl    = profit_mt5
        sl_dist = abs(signal.entry_price - signal.stop_loss)

        # FIX: contract_size da MT5 per R-multiple corretto
        sym_info      = mt5.symbol_info(symbol)
        contract_size = sym_info.trade_contract_size if sym_info else 100000
        r_mult = (
            pnl / (sl_dist * signal.volume * contract_size)
            if sl_dist > 0 and signal.volume > 0 and contract_size > 0
            else 0.0
        )

        trade_record = TradeRecord(
            trade_id=str(uuid.uuid4())[:8],
            symbol=symbol,
            direction=signal.direction,
            regime=signal.regime,
            entry_price=signal.entry_price,
            exit_price=exit_price,
            position_size=signal.volume,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            pnl=round(pnl, 2),
            pnl_pct=pnl / (signal.entry_price * signal.volume) if signal.entry_price > 0 else 0.0,
            r_multiple=round(r_mult, 3),
            duration_bars=0,
            entry_time="",
            exit_time=close_time_iso,
            confidence=signal.confidence,
            trade_quality=signal.trade_quality,
            risk_score=signal.risk_score,
            trend_score=signal.trend_score,
            momentum_score=signal.momentum_score,
            volume_score=signal.volume_score,
            structure_score=signal.structure_score,
            volatility_score=signal.volatility_score,
            was_winner=pnl > 0,
            exit_reason=exit_reason,
        )

        tracker.record_trade(trade_record, current_balance)
        risk_manager.update_after_trade(was_winner=pnl > 0)

        logger.info(
            "Trade chiuso: %s %s | PnL=%.2f | R=%.2f | WR cumulativo=%.1f%%",
            symbol, signal.direction.upper(),
            pnl, r_mult, tracker.win_rate * 100,
        )


def _apply_adapted_params(updated_cfg: dict) -> None:
    """Applica i parametri adattati al modulo config globale."""
    mapping = {
        "SCORE_MIN_CONFIDENCE": ("scoring", "min_confidence"),
        "RSI_BUY_LEVEL":        ("indicators", "rsi_oversold"),
        "RSI_SELL_LEVEL":       ("indicators", "rsi_overbought"),
        "SL_ATR_MULTIPLIER":    ("risk", "atr_sl_multiplier"),
        "TP_ATR_MULTIPLIER":    ("risk", "atr_tp_multiplier"),
    }
    for config_key, (section, param) in mapping.items():
        if section in updated_cfg and param in updated_cfg[section]:
            new_val = updated_cfg[section][param]
            setattr(config, config_key, new_val)
            logger.debug("Config adattato: %s = %.4f", config_key, new_val)


def _sleep_to_next_candle(loop_start: float) -> None:
    elapsed = time.time() - loop_start
    sleep   = max(0.0, config.LOOP_INTERVAL_SECONDS - elapsed)
    if sleep > 0:
        time.sleep(sleep)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AdaptiveBot — MT5 Trading Bot")
    parser.add_argument("--paper", action="store_true",
                        help="Paper trading: logga segnali senza inviare ordini")
    args = parser.parse_args()
    run(paper_mode=args.paper)


if __name__ == "__main__":
    main()
