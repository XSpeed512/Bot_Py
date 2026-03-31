"""
execution/broker_connector.py
==============================
Low-level interface to MetaTrader 5.

Every interaction with the MT5 terminal is centralised here so the
rest of the codebase remains broker-agnostic.  Replacing this module
is all that would be required to switch to a different broker API.

Public functions
----------------
connect()           – Initialise and log-in to MT5.
disconnect()        – Shut down the MT5 connection gracefully.
get_price()         – Return the current Bid/Ask for a symbol.
open_trade()        – Send a market order (BUY or SELL).
modify_sl()         – Update the stop-loss of an open position.
get_positions()     – Retrieve all positions opened by this bot.
close_trade()       – Close a specific open position at market.
"""

from __future__ import annotations

import time
from typing import Optional

import MetaTrader5 as mt5

import config
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect() -> bool:
    """
    Initialise the MT5 terminal and authenticate with the configured
    demo account credentials.

    Returns
    -------
    bool
        True if the connection and login succeeded, False otherwise.
    """
    logger.info("Connecting to MetaTrader 5 terminal …")

    if not mt5.initialize():
        logger.error("mt5.initialize() failed – error: %s", mt5.last_error())
        return False

    # If credentials are provided, perform an explicit login
    if config.MT5_LOGIN and config.MT5_PASSWORD and config.MT5_SERVER:
        authorised = mt5.login(
            login=config.MT5_LOGIN,
            password=config.MT5_PASSWORD,
            server=config.MT5_SERVER,
        )
        if not authorised:
            logger.error(
                "MT5 login failed for account %s – error: %s",
                config.MT5_LOGIN,
                mt5.last_error(),
            )
            mt5.shutdown()
            return False
        logger.info("Logged in to account %s on %s", config.MT5_LOGIN, config.MT5_SERVER)
    else:
        logger.warning(
            "No login credentials set in config.py – using currently open terminal session."
        )

    account_info = mt5.account_info()
    if account_info:
        logger.info(
            "Account: %s | Balance: %.2f %s | Leverage: 1:%s",
            account_info.login,
            account_info.balance,
            account_info.currency,
            account_info.leverage,
        )
    return True


def disconnect() -> None:
    """Shut down the MT5 connection."""
    mt5.shutdown()
    logger.info("MT5 connection closed.")


# ---------------------------------------------------------------------------
# Market data helpers
# ---------------------------------------------------------------------------

def get_price(symbol: str) -> Optional[dict]:
    """
    Return the current Bid and Ask prices for *symbol*.

    Parameters
    ----------
    symbol : str
        E.g. ``"EURUSD"``.

    Returns
    -------
    dict or None
        ``{"bid": float, "ask": float}`` or None on error.
    """
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        logger.error("Could not retrieve tick for %s – error: %s", symbol, mt5.last_error())
        return None
    return {"bid": tick.bid, "ask": tick.ask}


def get_account_balance() -> float:
    """Return the current account balance, or 0.0 on failure."""
    info = mt5.account_info()
    if info is None:
        logger.error("Could not retrieve account info – error: %s", mt5.last_error())
        return 0.0
    return info.balance


def get_account_equity() -> float:
    """Return the current account equity, or 0.0 on failure."""
    info = mt5.account_info()
    if info is None:
        return 0.0
    return info.equity


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------

def open_trade(
    symbol: str,
    order_type: str,          # "BUY" or "SELL"
    lot_size: float,
    stop_loss: float,
    take_profit: float,
) -> Optional[int]:
    """
    Send a market order to MT5.

    Parameters
    ----------
    symbol     : str    Instrument name.
    order_type : str    ``"BUY"`` or ``"SELL"``.
    lot_size   : float  Volume in lots.
    stop_loss  : float  Absolute SL price.
    take_profit: float  Absolute TP price.

    Returns
    -------
    int or None
        The MT5 position ticket on success, None on failure.
    """
    price_info = get_price(symbol)
    if price_info is None:
        return None

    if order_type.upper() == "BUY":
        mt5_order_type = mt5.ORDER_TYPE_BUY
        price = price_info["ask"]
    elif order_type.upper() == "SELL":
        mt5_order_type = mt5.ORDER_TYPE_SELL
        price = price_info["bid"]
    else:
        logger.error("Unknown order type: %s", order_type)
        return None

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        logger.error("Symbol info not available for %s", symbol)
        return None

    # Normalise price to correct decimal places
    digits = symbol_info.digits
    price        = round(price, digits)
    stop_loss    = round(stop_loss, digits)
    take_profit  = round(take_profit, digits)
    lot_size     = round(lot_size, 2)

    request = {
        "action":      mt5.TRADE_ACTION_DEAL,
        "symbol":      symbol,
        "volume":      lot_size,
        "type":        mt5_order_type,
        "price":       price,
        "sl":          stop_loss,
        "tp":          take_profit,
        "deviation":   config.SLIPPAGE,
        "magic":       config.MAGIC_NUMBER,
        "comment":     config.ORDER_COMMENT,
        "type_time":   mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error(
            "order_send failed for %s %s %.2f lots – retcode: %s",
            order_type, symbol, lot_size,
            result.retcode if result else mt5.last_error(),
        )
        return None

    logger.info(
        "TRADE OPENED | %s %s | Lots: %.2f | Price: %.5f | SL: %.5f | TP: %.5f | Ticket: %s",
        order_type, symbol, lot_size, price, stop_loss, take_profit, result.order,
    )
    return result.order


# ---------------------------------------------------------------------------
# Position management
# ---------------------------------------------------------------------------

def modify_sl(ticket: int, new_sl: float, symbol: str) -> bool:
    """
    Update the stop-loss of an open position identified by *ticket*.

    Parameters
    ----------
    ticket  : int    MT5 position ticket.
    new_sl  : float  New stop-loss price.
    symbol  : str    Required for price normalisation.

    Returns
    -------
    bool  True on success.
    """
    position = _get_position_by_ticket(ticket)
    if position is None:
        logger.warning("modify_sl: position %s not found.", ticket)
        return False

    symbol_info = mt5.symbol_info(symbol)
    digits = symbol_info.digits if symbol_info else 5
    new_sl = round(new_sl, digits)

    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl":       new_sl,
        "tp":       position.tp,
    }

    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error(
            "modify_sl failed for ticket %s – retcode: %s",
            ticket, result.retcode if result else mt5.last_error(),
        )
        return False

    logger.info("SL MODIFIED | Ticket: %s | New SL: %.5f", ticket, new_sl)
    return True


def get_positions(symbol: Optional[str] = None) -> list:
    """
    Return all open positions placed by this bot (filtered by MAGIC_NUMBER).

    Parameters
    ----------
    symbol : str, optional
        If provided, return only positions for that symbol.

    Returns
    -------
    list of MT5 position objects.
    """
    if symbol:
        positions = mt5.positions_get(symbol=symbol)
    else:
        positions = mt5.positions_get()

    if positions is None:
        return []

    # Filter to only positions opened by this bot
    return [p for p in positions if p.magic == config.MAGIC_NUMBER]


def close_trade(ticket: int, symbol: str, lot_size: float, order_type: int) -> bool:
    """
    Close an open position at current market price.

    Parameters
    ----------
    ticket     : int   MT5 position ticket.
    symbol     : str   Instrument name.
    lot_size   : float Volume to close (partial close supported).
    order_type : int   MT5 position type constant (ORDER_TYPE_BUY / SELL).

    Returns
    -------
    bool  True on success.
    """
    price_info = get_price(symbol)
    if price_info is None:
        return False

    # Close direction is opposite to opening direction
    if order_type == mt5.ORDER_TYPE_BUY:
        close_type = mt5.ORDER_TYPE_SELL
        price = price_info["bid"]
    else:
        close_type = mt5.ORDER_TYPE_BUY
        price = price_info["ask"]

    request = {
        "action":      mt5.TRADE_ACTION_DEAL,
        "position":    ticket,
        "symbol":      symbol,
        "volume":      round(lot_size, 2),
        "type":        close_type,
        "price":       price,
        "deviation":   config.SLIPPAGE,
        "magic":       config.MAGIC_NUMBER,
        "comment":     f"close_{config.ORDER_COMMENT}",
        "type_time":   mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error(
            "close_trade failed for ticket %s – retcode: %s",
            ticket, result.retcode if result else mt5.last_error(),
        )
        return False

    logger.info("TRADE CLOSED | Ticket: %s | Symbol: %s | Price: %.5f", ticket, symbol, price)
    return True


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_position_by_ticket(ticket: int):
    """Return a single MT5 position object by ticket, or None."""
    positions = mt5.positions_get()
    if positions is None:
        return None
    for pos in positions:
        if pos.ticket == ticket:
            return pos
    return None
