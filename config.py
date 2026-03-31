"""
config.py
=========
Central configuration module for the trading bot.
All tunable parameters are defined here to allow easy adjustment
without modifying business logic in other modules.
"""

import MetaTrader5 as mt5

# ---------------------------------------------------------------------------
# BROKER / ACCOUNT
# ---------------------------------------------------------------------------
MT5_LOGIN    = 0          # Replace with your demo account login
MT5_PASSWORD = ""         # Replace with your demo account password
MT5_SERVER   = ""         # Replace with your broker server name (e.g. "ICMarkets-Demo")

# ---------------------------------------------------------------------------
# SYMBOLS TO TRADE
# ---------------------------------------------------------------------------
SYMBOLS = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "XAUUSD",   # Gold – high volatility, good for ATR strategies
]

# ---------------------------------------------------------------------------
# TIMEFRAME
# ---------------------------------------------------------------------------
# Main execution timeframe for signal generation
TIMEFRAME = mt5.TIMEFRAME_H1   # 1-Hour candles

# Number of historical candles to fetch for indicator calculation
CANDLES_LOOKBACK = 300

# ---------------------------------------------------------------------------
# STRATEGY PARAMETERS
# ---------------------------------------------------------------------------
EMA_FAST        = 50     # Fast EMA period
EMA_SLOW        = 200    # Slow EMA period
RSI_PERIOD      = 14     # RSI look-back period
ATR_PERIOD      = 14     # ATR look-back period

RSI_BUY_LEVEL   = 35    # RSI threshold to consider a BUY pullback
RSI_SELL_LEVEL  = 65    # RSI threshold to consider a SELL pullback

# ATR filter: only trade when current ATR > ATR_MULTIPLIER * avg ATR
ATR_FILTER_MULTIPLIER = 1.0   # 1.0 = at or above average volatility

# ---------------------------------------------------------------------------
# RISK MANAGEMENT
# ---------------------------------------------------------------------------
RISK_PER_TRADE_PCT  = 1.0   # Risk 1% of balance per trade
MAX_OPEN_TRADES     = 3     # Never exceed this many simultaneous open trades
DAILY_LOSS_LIMIT_PCT  = 3.0 # Stop trading for the day after 3% drawdown
WEEKLY_LOSS_LIMIT_PCT = 5.0 # Stop trading for the week after 5% drawdown

# ---------------------------------------------------------------------------
# STOP LOSS / TAKE PROFIT
# ---------------------------------------------------------------------------
SL_ATR_MULTIPLIER = 1.5    # Stop loss = entry ± (ATR * multiplier)
RISK_REWARD_RATIO = 2.0    # Take profit = entry ± (SL_distance * RR)

# ---------------------------------------------------------------------------
# POSITION MANAGEMENT (break-even & trailing)
# ---------------------------------------------------------------------------
BREAKEVEN_R      = 1.0    # Move SL to entry when price is +1R in profit
TRAILING_START_R = 2.0    # Begin trailing stop when price is +2R in profit
# Trailing stop distance expressed as ATR multiplier
TRAILING_ATR_MULT = 1.0

# ---------------------------------------------------------------------------
# EXECUTION
# ---------------------------------------------------------------------------
MAGIC_NUMBER  = 20240101   # Unique identifier for trades placed by this bot
SLIPPAGE      = 10         # Maximum acceptable slippage in points
ORDER_COMMENT = "TradingBot_v1"

# ---------------------------------------------------------------------------
# NEWS FILTER
# ---------------------------------------------------------------------------
NEWS_FILTER_ENABLED      = True
NEWS_FILTER_MINUTES_BEFORE = 30   # Pause trading X minutes before high-impact news
NEWS_FILTER_MINUTES_AFTER  = 30   # Pause trading X minutes after high-impact news
# Future: plug in an economic calendar API key here
ECONOMIC_CALENDAR_API_KEY = ""

# ---------------------------------------------------------------------------
# LOOP TIMING
# ---------------------------------------------------------------------------
LOOP_INTERVAL_SECONDS = 60   # Main loop executes every 60 seconds

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
DB_PATH = "ai/trades.db"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
LOG_FILE  = "logs/bot.log"
LOG_LEVEL = "DEBUG"   # DEBUG | INFO | WARNING | ERROR
