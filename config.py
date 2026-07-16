"""
config.py
=========
Configurazione centralizzata del bot unificato.

Contiene tutti i parametri del bot originale (MT5, simboli, risk base)
più i parametri del nuovo sistema adattivo (regime, scoring, self-improvement).

Tutti i moduli importano da qui — non ci sono valori hard-coded altrove.
"""

import MetaTrader5 as mt5

# ---------------------------------------------------------------------------
# BROKER / ACCOUNT  (originale, invariato)
# ---------------------------------------------------------------------------
MT5_LOGIN    = 0        # Inserire il login del conto demo
MT5_PASSWORD = ""       # Inserire la password
MT5_SERVER   = ""       # Es: "ICMarkets-Demo"

# ---------------------------------------------------------------------------
# SIMBOLI DA TRADARE
# ---------------------------------------------------------------------------
SYMBOLS = [
    "EURUSD",
    "GBPUSD",
    "XAUUSD",   
    "XUSTEC",
    "XUS500",
    "XUS30",

]

# ---------------------------------------------------------------------------
# TIMEFRAME
# ---------------------------------------------------------------------------
# Timeframe principale (LTF) per entry di precisione
TIMEFRAME     = mt5.TIMEFRAME_H1    # 1H — entry LTF
TIMEFRAME_HTF = mt5.TIMEFRAME_H4   # 4H — trend / regime HTF

CANDLES_LOOKBACK     = 300   # Barre LTF da scaricare
CANDLES_LOOKBACK_HTF = 250   # Barre HTF da scaricare

# ---------------------------------------------------------------------------
# INDICATORI  (usati da market_data.py e dai moduli avanzati)
# ---------------------------------------------------------------------------
EMA_FAST   = 50
EMA_SLOW   = 200
EMA_SIGNAL = 21       # EMA intermedia per il trend score

RSI_PERIOD     = 14
RSI_BUY_LEVEL  = 35   # Soglia oversold per LONG
RSI_SELL_LEVEL = 65   # Soglia overbought per SHORT

ATR_PERIOD           = 14
ATR_AVG_PERIOD       = 50   # Finestra media ATR per ratio
ATR_FILTER_MULTIPLIER = 1.0

VOLUME_MA_PERIOD        = 20
VOLUME_SPIKE_MULTIPLIER = 1.5

BB_PERIOD = 20
BB_STD    = 2.0

ADX_PERIOD = 14

# ---------------------------------------------------------------------------
# MARKET REGIME DETECTION  (nuovo)
# ---------------------------------------------------------------------------
REGIME_ADX_TRENDING_THRESHOLD = 25
REGIME_ADX_STRONG_THRESHOLD   = 40
REGIME_BBW_RANGING_THRESHOLD  = 0.04
REGIME_ATR_HIGH_VOL_MULTIPLIER = 1.5
REGIME_ATR_LOW_VOL_MULTIPLIER  = 0.5
REGIME_LOOKBACK               = 100

# ---------------------------------------------------------------------------
# CONFIDENCE SCORING  (nuovo)
# ---------------------------------------------------------------------------
# I pesi devono sommare a 1.0
SCORE_WEIGHT_TREND      = 0.30
SCORE_WEIGHT_MOMENTUM   = 0.20
SCORE_WEIGHT_VOLUME     = 0.20
SCORE_WEIGHT_STRUCTURE  = 0.15
SCORE_WEIGHT_VOLATILITY = 0.15

SCORE_MIN_CONFIDENCE  = 65   # 0–100: soglia minima per procedere
SCORE_MIN_QUALITY     = 60   # Qualità minima del trade
SCORE_MAX_RISK        = 40   # Score di rischio massimo accettabile

SCORE_TRENDING_BOOST = 8     # Bonus confidence in regime trending
SCORE_RANGING_BOOST  = 5     # Bonus confidence in regime ranging

# ---------------------------------------------------------------------------
# ENTRY FILTERS  (nuovo)
# ---------------------------------------------------------------------------
FILTER_FAKEOUT_LOOKBACK     = 5
FILTER_SWEEP_ATR_FACTOR     = 0.3
FILTER_SR_PROXIMITY_FACTOR  = 0.5
FILTER_BODY_RATIO_MIN       = 0.4
FILTER_MAX_SPREAD_PCT       = 0.05

SESSION_FILTER_ENABLED   = True
# Sessioni permesse — London: 08–17 UTC, New York: 13–22 UTC
SESSION_ALLOWED = ["london", "new_york"]

# ---------------------------------------------------------------------------
# RISK MANAGEMENT
# ---------------------------------------------------------------------------
RISK_PER_TRADE_PCT   = 1.0    # Rischio massimo per trade (% del balance)
MAX_OPEN_TRADES      = 3
DAILY_LOSS_LIMIT_PCT  = 3.0
WEEKLY_LOSS_LIMIT_PCT = 5.0

SL_ATR_MULTIPLIER  = 1.5
RISK_REWARD_RATIO  = 2.0      # TP = SL_distance × RR (usato come TP minimo)
TP_ATR_MULTIPLIER  = 3.0      # TP = entry ± ATR × questo valore
KELLY_FRACTION     = 0.25     # Frazione Kelly per sizing

# Trailing stop / break-even (originale)
BREAKEVEN_R       = 1.0
TRAILING_START_R  = 2.0
TRAILING_ATR_MULT = 1.0

# ---------------------------------------------------------------------------
# RISK OF RUIN PROTECTION  (nuovo)
# ---------------------------------------------------------------------------
RUIN_MAX_DAILY_LOSS_PCT      = 3.0
RUIN_MAX_CONSEC_LOSSES       = 4
RUIN_PAUSE_HOURS             = 4
RUIN_VOL_KILL_MULTIPLIER     = 2.5
RUIN_DRAWDOWN_KILL_PCT       = 8.0

# ---------------------------------------------------------------------------
# SELF-IMPROVEMENT  (nuovo)
# ---------------------------------------------------------------------------
SELF_IMPROVE_ENABLED          = True
SELF_IMPROVE_MIN_TRADES       = 30
SELF_IMPROVE_ADAPTATION_RATE  = 0.05
SELF_IMPROVE_EMA_DECAY        = 0.95
SELF_IMPROVE_RETUNE_HOURS     = 24

# Parametri auto-tunable e i loro range (min, max)
SELF_IMPROVE_TUNABLE = {
    "SCORE_MIN_CONFIDENCE": (55, 80),
    "RSI_BUY_LEVEL":        (25, 45),
    "RSI_SELL_LEVEL":       (55, 75),
    "SL_ATR_MULTIPLIER":    (1.0, 2.5),
    "TP_ATR_MULTIPLIER":    (2.0, 5.0),
}

# ---------------------------------------------------------------------------
# EXECUTION  (originale)
# ---------------------------------------------------------------------------
MAGIC_NUMBER  = 20240101
SLIPPAGE      = 10
ORDER_COMMENT = "AdaptiveBot_v2"

# ---------------------------------------------------------------------------
# NEWS FILTER  (originale)
# ---------------------------------------------------------------------------
NEWS_FILTER_ENABLED        = False
NEWS_FILTER_MINUTES_BEFORE = 30
NEWS_FILTER_MINUTES_AFTER  = 30
ECONOMIC_CALENDAR_API_KEY  = ""

# ---------------------------------------------------------------------------
# LOOP TIMING
# ---------------------------------------------------------------------------
LOOP_INTERVAL_SECONDS = 60

# ---------------------------------------------------------------------------
# DATABASE  (originale)
# ---------------------------------------------------------------------------
DB_PATH = "ai/trades.db"

# ---------------------------------------------------------------------------
# PERFORMANCE WARNINGS
# ---------------------------------------------------------------------------
PERF_MIN_SHARPE_WARN   = 0.5
PERF_MIN_WIN_RATE_WARN = 0.40
PERF_MAX_DD_WARN       = 0.06
PERF_TRACK_FILE        = "state/performance.json"

# ---------------------------------------------------------------------------
# BACKTESTING
# ---------------------------------------------------------------------------
BT_WALK_FORWARD_FOLDS  = 5
BT_OOS_RATIO           = 0.30
BT_MONTE_CARLO_RUNS    = 1000
BT_COMMISSION_PCT      = 0.05
BT_SLIPPAGE_PCT        = 0.02

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
LOG_FILE  = "logs/bot.log"
LOG_LEVEL = "INFO"

# ---------------------------------------------------------------------------
# HELPER: costruisce i dict-config per i moduli avanzati
# (evita di passare il modulo config direttamente ai nuovi moduli)
# ---------------------------------------------------------------------------

def build_advanced_config() -> dict:
    """
    Restituisce un dizionario compatibile con i moduli avanzati
    (regime, mtf, scoring, filters, risk_engine, tracker, backtest).
    Centralizza la traduzione da costanti Python al formato dict.
    """
    return {
        "regime": {
            "adx_trending_threshold":  REGIME_ADX_TRENDING_THRESHOLD,
            "adx_strong_threshold":    REGIME_ADX_STRONG_THRESHOLD,
            "bbw_ranging_threshold":   REGIME_BBW_RANGING_THRESHOLD,
            "atr_high_vol_multiplier": REGIME_ATR_HIGH_VOL_MULTIPLIER,
            "atr_low_vol_multiplier":  REGIME_ATR_LOW_VOL_MULTIPLIER,
            "regime_lookback":         REGIME_LOOKBACK,
        },
        "indicators": {
            "ema_fast":                EMA_FAST,
            "ema_slow":                EMA_SLOW,
            "ema_signal":              EMA_SIGNAL,
            "rsi_period":              RSI_PERIOD,
            "rsi_oversold":            RSI_BUY_LEVEL,
            "rsi_overbought":          RSI_SELL_LEVEL,
            "atr_period":              ATR_PERIOD,
            "atr_avg_period":          ATR_AVG_PERIOD,
            "adx_period":              ADX_PERIOD,
            "bb_period":               BB_PERIOD,
            "bb_std":                  BB_STD,
            "volume_ma_period":        VOLUME_MA_PERIOD,
            "volume_spike_multiplier": VOLUME_SPIKE_MULTIPLIER,
        },
        "scoring": {
            "weights": {
                "trend":      SCORE_WEIGHT_TREND,
                "momentum":   SCORE_WEIGHT_MOMENTUM,
                "volume":     SCORE_WEIGHT_VOLUME,
                "structure":  SCORE_WEIGHT_STRUCTURE,
                "volatility": SCORE_WEIGHT_VOLATILITY,
            },
            "min_confidence":          SCORE_MIN_CONFIDENCE,
            "min_trade_quality":       SCORE_MIN_QUALITY,
            "max_risk_score":          SCORE_MAX_RISK,
            "trending_confidence_boost": SCORE_TRENDING_BOOST,
            "ranging_confidence_boost":  SCORE_RANGING_BOOST,
        },
        "entry_filters": {
            "fake_breakout_lookback":      FILTER_FAKEOUT_LOOKBACK,
            "liquidity_sweep_atr_factor":  FILTER_SWEEP_ATR_FACTOR,
            "sr_proximity_atr_factor":     FILTER_SR_PROXIMITY_FACTOR,
            "candle_body_ratio_min":       FILTER_BODY_RATIO_MIN,
            "max_spread_pct":              FILTER_MAX_SPREAD_PCT,
            "session_filter": {
                "enabled":          SESSION_FILTER_ENABLED,
                "allowed_sessions": SESSION_ALLOWED,
            },
        },
        "risk": {
            "account_balance":       0.0,   # verrà aggiornato a runtime
            "max_risk_per_trade_pct": RISK_PER_TRADE_PCT,
            "max_open_positions":    MAX_OPEN_TRADES,
            "atr_sl_multiplier":     SL_ATR_MULTIPLIER,
            "atr_tp_multiplier":     TP_ATR_MULTIPLIER,
            "kelly_fraction":        KELLY_FRACTION,
            "trailing_stop": {
                "enabled":               True,
                "activation_r_multiple": BREAKEVEN_R,
                "trail_atr_multiplier":  TRAILING_ATR_MULT,
            },
        },
        "ruin_protection": {
            "max_daily_loss_pct":       RUIN_MAX_DAILY_LOSS_PCT,
            "max_consecutive_losses":   RUIN_MAX_CONSEC_LOSSES,
            "pause_hours_after_consec": RUIN_PAUSE_HOURS,
            "vol_kill_switch_multiplier": RUIN_VOL_KILL_MULTIPLIER,
            "drawdown_kill_switch_pct": RUIN_DRAWDOWN_KILL_PCT,
        },
        "self_improve": {
            "enabled":                  SELF_IMPROVE_ENABLED,
            "min_trades_before_adapt":  SELF_IMPROVE_MIN_TRADES,
            "adaptation_rate":          SELF_IMPROVE_ADAPTATION_RATE,
            "ema_decay":                SELF_IMPROVE_EMA_DECAY,
            "retune_interval_hours":    SELF_IMPROVE_RETUNE_HOURS,
            "tunable":                  list(SELF_IMPROVE_TUNABLE.keys()),
            "bounds":                   SELF_IMPROVE_TUNABLE,
        },
        "performance": {
            "track_file":       PERF_TRACK_FILE,
            "min_sharpe_warn":  PERF_MIN_SHARPE_WARN,
            "min_win_rate_warn": PERF_MIN_WIN_RATE_WARN,
            "max_drawdown_warn": PERF_MAX_DD_WARN,
        },
        "backtest": {
            "walk_forward_folds": BT_WALK_FORWARD_FOLDS,
            "oos_ratio":          BT_OOS_RATIO,
            "monte_carlo_runs":   BT_MONTE_CARLO_RUNS,
            "commission_pct":     BT_COMMISSION_PCT,
            "slippage_pct":       BT_SLIPPAGE_PCT,
        },
    }
