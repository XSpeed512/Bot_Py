"""
utils/logger.py
===============
Centralised logging configuration for the trading bot.

All modules import `get_logger(__name__)` to obtain a properly
configured logger that writes both to the console and to a rotating
log file at logs/bot.log.
"""

import logging
import os
from logging.handlers import RotatingFileHandler


def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger configured with file + console handlers.

    The first call for a given name sets up the handlers; subsequent
    calls return the same logger instance (Python's logging registry
    ensures this).

    Parameters
    ----------
    name : str
        Usually ``__name__`` of the calling module.

    Returns
    -------
    logging.Logger
    """
    # Import here to avoid circular dependency at module level
    from config import LOG_FILE, LOG_LEVEL

    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if logger already configured
    if logger.handlers:
        return logger

    numeric_level = getattr(logging, LOG_LEVEL.upper(), logging.DEBUG)
    logger.setLevel(numeric_level)

    # ------------------------------------------------------------------
    # Formatter
    # ------------------------------------------------------------------
    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ------------------------------------------------------------------
    # Console handler
    # ------------------------------------------------------------------
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # ------------------------------------------------------------------
    # Rotating file handler  (10 MB per file, keep 5 backups)
    # ------------------------------------------------------------------
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
