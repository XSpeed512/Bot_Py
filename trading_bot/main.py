"""
main.py
=======
Trading bot entry point and main execution loop.

Execution flow (every LOOP_INTERVAL_SECONDS)
--------------------------------------------
1.  Connect to MT5.
2.  Initialise database and risk tracker.
3.  Enter infinite loop:
    a. Check news filter – pause if high-impact event is near.
    b. Check risk limits  – stop trading if daily/weekly limit breached.
    c. For each symbol:
       i.   Fetch OHLC + indicators.
       ii.  Generate entry signal (BUY / SELL / NONE).
       iii. If signal: calculate SL, TP, lot size.
       iv.  Open trade if all conditions pass.
       v.   Record trade in database.
    d. Manage open positions (break-even / trailing SL).
    e. Sleep until next iteration.

Run with:
    python main.py
"""

import sys
import time
import signal

import MetaTrader5 as mt5

import config
from execution.broker_connector import (
    connect,
    disconnect,
    get_price,
    open_trade,
    get_positions,
)
from data.market_data        import get_prepared_data
from strategy.entry_strategy import generate_signal, get_atr, get_last_close
from risk.risk_manager       import (
    initialise_tracker,
    is_trading_allowed,
    calculate_lot_size,
    calculate_sl_tp,
)
from risk.position_manager   import manage_positions
from news.news_filter        import check_news
from ai.learning_module      import initialize_database, save_trade, get_performance_stats
from utils.logger            import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Graceful shutdown on SIGINT / SIGTERM
# ---------------------------------------------------------------------------
_running = True

def _signal_handler(signum, frame):
    global _running
    logger.info("Shutdown signal received – stopping bot after current iteration …")
    _running = False

signal.signal(signal.SIGINT,  _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_bot() -> None:
    """
    Initialise all subsystems and enter the main trading loop.
    """
    logger.info("=" * 60)
    logger.info("  TRADING BOT STARTING UP")
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # 1. Broker connection
    # ------------------------------------------------------------------
    if not connect():
        logger.critical("Failed to connect to MT5 – exiting.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Database and risk tracker
    # ------------------------------------------------------------------
    initialize_database()
    initialise_tracker()

    # Print startup performance summary if prior trades exist
    stats = get_performance_stats()
    if stats.get("total_trades", 0) > 0:
        logger.info(
            "Historical stats | Trades: %d | Win rate: %.1f%% | Avg R:R: %.2f | PnL: %.2f",
            stats["total_trades"],
            stats["win_rate_pct"],
            stats["avg_rr"],
            stats["total_pnl"],
        )

    # ------------------------------------------------------------------
    # 3. Main loop
    # ------------------------------------------------------------------
    logger.info(
        "Entering main loop (interval=%ds, symbols=%s)",
        config.LOOP_INTERVAL_SECONDS,
        config.SYMBOLS,
    )

    while _running:
        try:
            _run_one_iteration()
        except Exception as exc:
            # Catch-all so a single bad iteration never kills the bot
            logger.exception("Unhandled exception in main loop: %s", exc)

        # Sleep (check shutdown flag every second for responsiveness)
        for _ in range(config.LOOP_INTERVAL_SECONDS):
            if not _running:
                break
            time.sleep(1)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    logger.info("Bot shutdown complete.")
    disconnect()


def _run_one_iteration() -> None:
    """
    Execute one full scan cycle across all configured symbols.
    """
    logger.debug("-" * 40 + " NEW ITERATION " + "-" * 40)

    # ------------------------------------------------------------------
    # a. News filter
    # ------------------------------------------------------------------
    if not check_news():
        logger.info("NEWS FILTER: trading paused – high-impact event imminent.")
        return

    # ------------------------------------------------------------------
    # b. Global risk check
    # ------------------------------------------------------------------
    if not is_trading_allowed():
        # is_trading_allowed() logs the reason internally
        _manage_open_positions_only()
        return

    # ------------------------------------------------------------------
    # c. Per-symbol signal loop
    # ------------------------------------------------------------------
    atr_map: dict[str, float] = {}   # Collect ATRs for position manager

    for symbol in config.SYMBOLS:
        try:
            _process_symbol(symbol, atr_map)
        except Exception as exc:
            logger.error("Error processing symbol %s: %s", symbol, exc)

    # ------------------------------------------------------------------
    # d. Position management (break-even / trailing stop)
    # ------------------------------------------------------------------
    manage_positions(atr_map)


def _process_symbol(symbol: str, atr_map: dict) -> None:
    """
    Full pipeline for a single symbol: data → signal → risk → execution.

    Parameters
    ----------
    symbol  : str       Instrument name.
    atr_map : dict      Shared dictionary updated with this symbol's ATR.
    """
    # ------------------------------------------------------------------
    # Fetch data + indicators
    # ------------------------------------------------------------------
    df = get_prepared_data(symbol)
    if df is None or df.empty:
        logger.warning("No data for %s – skipping.", symbol)
        return

    # Store ATR for position manager
    atr = get_atr(df)
    atr_map[symbol] = atr

    # ------------------------------------------------------------------
    # Generate entry signal
    # ------------------------------------------------------------------
    signal = generate_signal(df, symbol)

    if signal == "NONE":
        return   # No trade setup – nothing to do

    # ------------------------------------------------------------------
    # Check if we already have an open position in this symbol/direction
    # ------------------------------------------------------------------
    existing = get_positions(symbol)
    if existing:
        logger.debug(
            "%s: already has %d open position(s) – skipping new entry.",
            symbol, len(existing),
        )
        return

    # ------------------------------------------------------------------
    # Calculate SL / TP and lot size
    # ------------------------------------------------------------------
    price_info = get_price(symbol)
    if price_info is None:
        return

    entry_price = price_info["ask"] if signal == "BUY" else price_info["bid"]

    stop_loss, take_profit = calculate_sl_tp(symbol, signal, entry_price, atr)

    sl_distance = abs(entry_price - stop_loss)
    lot_size    = calculate_lot_size(symbol, sl_distance)

    if lot_size <= 0:
        logger.warning("%s: Lot size calculated as 0 – skipping trade.", symbol)
        return

    logger.info(
        "TRADE SETUP | %s %s | Entry: %.5f | SL: %.5f | TP: %.5f | Lots: %.2f",
        signal, symbol, entry_price, stop_loss, take_profit, lot_size,
    )

    # ------------------------------------------------------------------
    # Open trade
    # ------------------------------------------------------------------
    ticket = open_trade(symbol, signal, lot_size, stop_loss, take_profit)

    if ticket:
        # Persist to database for future ML analysis
        save_trade(
            symbol      = symbol,
            direction   = signal,
            entry       = entry_price,
            stop_loss   = stop_loss,
            take_profit = take_profit,
            lot_size    = lot_size,
            ticket      = ticket,
        )
    else:
        logger.error("Trade not opened for %s %s – order rejected.", signal, symbol)


def _manage_open_positions_only() -> None:
    """
    When new entries are blocked (risk limits) we still manage existing
    positions so break-even and trailing stops continue to operate.
    """
    atr_map: dict[str, float] = {}

    for symbol in config.SYMBOLS:
        try:
            df = get_prepared_data(symbol)
            if df is not None and not df.empty:
                atr_map[symbol] = get_atr(df)
        except Exception as exc:
            logger.error("Error fetching ATR for %s: %s", symbol, exc)

    manage_positions(atr_map)


# ---------------------------------------------------------------------------
# Script entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_bot()
