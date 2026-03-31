# Trading Bot — Professional Algorithmic Trading Foundation

A modular, production-ready Python trading bot built on **MetaTrader 5**.  
Designed to grow a €5,000 demo account to €10,000 while maintaining aggressive capital protection.

---

## Project Structure

```
trading_bot/
│
├── config.py                   # All tunable parameters (symbols, risk, timeframes …)
├── main.py                     # Entry point — main loop
├── requirements.txt
│
├── data/
│   ├── __init__.py
│   └── market_data.py          # OHLC download + EMA/RSI/ATR calculation
│
├── strategy/
│   ├── __init__.py
│   └── entry_strategy.py       # Signal generator (BUY / SELL / NONE)
│
├── risk/
│   ├── __init__.py
│   ├── risk_manager.py         # Lot sizing, daily/weekly loss limits
│   └── position_manager.py     # Break-even and trailing stop logic
│
├── execution/
│   ├── __init__.py
│   └── broker_connector.py     # All MT5 calls centralised here
│
├── news/
│   ├── __init__.py
│   └── news_filter.py          # Economic calendar stub (plug-in ready)
│
├── ai/
│   ├── __init__.py
│   └── learning_module.py      # SQLite trade journal + performance stats
│
├── utils/
│   ├── __init__.py
│   └── logger.py               # Rotating file + console logger
│
└── logs/
    └── bot.log                 # Created automatically at runtime
```

---

## Requirements

| Requirement | Details |
|---|---|
| **OS** | Windows 10 / 11 (MT5 Python API is Windows-only) |
| **Python** | 3.11 or higher |
| **MetaTrader 5** | Terminal installed and logged in to a demo account |
| **Broker** | Any MT5-compatible broker (e.g. IC Markets, Pepperstone, XM) |

---

## Installation

### 1. Clone / download the project

```bash
git clone https://github.com/your-repo/trading_bot.git
cd trading_bot
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux  (for dev/testing only — MT5 won't work here)
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure the bot

Open `config.py` and set your account credentials:

```python
MT5_LOGIN    = 12345678          # Your demo account number
MT5_PASSWORD = "your_password"   # Your demo account password
MT5_SERVER   = "ICMarkets-Demo"  # Your broker's server name
```

> **Finding your server name:** In MetaTrader 5 → File → Login to Trade Account  
> The server dropdown shows the exact string to use.

Adjust other parameters to suit your risk tolerance:

```python
SYMBOLS              = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
RISK_PER_TRADE_PCT   = 1.0    # 1% risk per trade
MAX_OPEN_TRADES      = 3      # Never exceed 3 simultaneous trades
DAILY_LOSS_LIMIT_PCT = 3.0    # Halt trading after 3% daily drawdown
WEEKLY_LOSS_LIMIT_PCT= 5.0    # Halt trading after 5% weekly drawdown
SL_ATR_MULTIPLIER    = 1.5    # Stop loss = 1.5 × ATR
RISK_REWARD_RATIO    = 2.0    # Take profit = 2 × risk distance
```

---

## Running the Bot

### Pre-flight checklist

- [ ] MetaTrader 5 terminal is **open and logged in**
- [ ] Demo account is **funded** (€5,000 recommended)
- [ ] **AutoTrading** is enabled in MT5 (green button in toolbar)
- [ ] Correct `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` set in `config.py`

### Start

```bash
cd trading_bot
python main.py
```

The bot will:
1. Connect to MT5 and log account details
2. Initialise the SQLite trade database (`ai/trades.db`)
3. Begin scanning every 60 seconds

### Stop

Press `Ctrl+C` — the bot completes the current iteration, then shuts down cleanly.

---

## Strategy Overview

| Component | Rule |
|---|---|
| **Trend** | EMA 50 vs EMA 200 (H1 timeframe) |
| **Pullback — BUY** | RSI < 35 |
| **Pullback — SELL** | RSI > 65 |
| **Volatility filter** | ATR ≥ average ATR (no entries in flat markets) |
| **Stop loss** | Entry ± (ATR × 1.5) |
| **Take profit** | Entry ± (SL distance × 2.0) |
| **Break-even** | SL moves to entry when +1R profit |
| **Trailing stop** | ATR-based trail activates at +2R profit |

---

## Risk Rules

| Rule | Value |
|---|---|
| Risk per trade | 1% of account balance |
| Maximum open trades | 3 |
| Daily loss limit | 3% → trading halted for the day |
| Weekly loss limit | 5% → trading halted for the week |
| Position sizing | Fixed-fractional (ATR-adjusted SL) |

---

## Trade Database

Every trade is automatically saved to `ai/trades.db` (SQLite).

**Schema:**

| Column | Type | Description |
|---|---|---|
| `symbol` | TEXT | Instrument name |
| `direction` | TEXT | BUY / SELL |
| `entry` | REAL | Entry price |
| `stop_loss` | REAL | Original SL |
| `take_profit` | REAL | TP price |
| `lot_size` | REAL | Volume in lots |
| `result` | TEXT | WIN / LOSS / BREAKEVEN / OPEN |
| `pnl` | REAL | Monetary P&L |
| `risk_reward` | REAL | Achieved R-multiple |
| `open_time` | TEXT | ISO timestamp |
| `close_time` | TEXT | ISO timestamp |
| `ticket` | INTEGER | MT5 position ticket |

Query example:
```python
from ai.learning_module import load_trades, get_performance_stats

stats = get_performance_stats()
print(f"Win rate: {stats['win_rate_pct']:.1f}%")
print(f"Total P&L: €{stats['total_pnl']:.2f}")

trades = load_trades(symbol="EURUSD", result="WIN")
```

---

## Logs

Logs are written to `logs/bot.log` (rotating, 10 MB per file, 5 backups).

```
2024-01-15 09:32:14 | INFO     | execution.broker_connector      | Logged in to account 12345678 on ICMarkets-Demo
2024-01-15 09:32:15 | INFO     | strategy.entry_strategy         | SIGNAL BUY | EURUSD | EMA50=1.09821 EMA200=1.09412 RSI=32.4 ATR=0.00072
2024-01-15 09:32:15 | INFO     | execution.broker_connector      | TRADE OPENED | BUY EURUSD | Lots: 0.10 | Price: 1.09834 | SL: 1.09726 | TP: 1.10050 | Ticket: 987654
2024-01-15 09:33:22 | INFO     | risk.position_manager           | BREAK-EVEN | EURUSD ticket=987654 | Moving SL 1.09726 → 1.09834 (R=1.02)
```

---

## Expanding the Bot

The architecture is designed for straightforward extension:

### Add a new strategy
Create `strategy/my_new_strategy.py` following the same pattern as `entry_strategy.py`.  
Call it from `main.py` alongside or instead of the existing strategy.

### Add real news filtering
Edit `news/news_filter.py` → implement `_fetch_high_impact_events()` with your preferred calendar API (ForexFactory, Trading Economics, etc.).

### Add machine learning
The `ai/learning_module.py` database is ready.  Build models in a new `ai/ml_model.py` module:

```python
from ai.learning_module import load_trades
import pandas as pd

trades = load_trades()
df = pd.DataFrame(trades)
# Train a classifier, neural network, etc.
```

### Add portfolio management
Create `portfolio/portfolio_manager.py` to track cross-symbol exposure, correlation limits, and capital allocation.

### Cloud deployment
1. Use a Windows VPS (AWS, Azure, Vultr) with MT5 installed.
2. Run as a Windows Service or scheduled task.
3. Pipe logs to CloudWatch / Datadog for monitoring.

---

## Disclaimer

> This software is provided for **educational and research purposes only**.  
> Algorithmic trading carries significant financial risk.  
> Always test thoroughly on a **demo account** before any live deployment.  
> Past performance does not guarantee future results.  
> The authors accept no liability for financial losses.
