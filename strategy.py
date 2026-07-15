"""
منطق الاستراتيجية - نفس المعاملات المستخدمة بالباكتست بالضبط (بدون أي تعديل)
مرجع: backtest_final_strategy_v4_optimized.py
"""
import numpy as np
import pandas as pd

# --- المؤشرات ---
EMA_FAST, EMA_MID, EMA_SLOW = 9, 21, 50
VWAP_WINDOW = 48
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
BB_PERIOD, BB_STD = 20, 2
STOCH_PERIOD = 14
CCI_PERIOD = 20
WILLR_PERIOD = 14
ATR_PERIOD = 14
SWING_WINDOW = 5

# --- شروط الدخول ---
RSI_THRESHOLD = 30
SWING_LOW_DIST_MIN = 0.0
SWING_LOW_DIST_MAX = 1.0
MIN_CONFLUENCE = 2
CCI_ENTRY_THRESHOLD = -180

# --- فلتر نظام السوق ---
MARKET_REGIME_LOOKBACK_BARS = 336   # 7 أيام على فريم 30m
MARKET_REGIME_THRESHOLD = -5.0

# --- إدارة الصفقة ---
SL_ATR_MULT = 3.41
TP_ATR_MULT = 2.26
TIME_STOP_BARS = 4
COOLDOWN_BARS_AFTER_TRADE = 4

# --- إدارة المحفظة (لغرض التوصية فقط) ---
POSITION_SIZE_PCT = 0.33
MAX_CONCURRENT_TRADES = 3
CROWD_FILTER_MAX_SIGNALS = 8


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def compute_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_macd_hist(close, fast=12, slow=26, signal=9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    return macd_line - signal_line


def compute_atr(high, low, close, period=14):
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_bollinger(close, period=20, mult=2):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return mid + mult * std, mid, mid - mult * std


def compute_stochastic_k(high, low, close, period=14):
    lowest = low.rolling(period).min()
    highest = high.rolling(period).max()
    return 100 * (close - lowest) / (highest - lowest)


def compute_cci(high, low, close, period=20):
    tp = (high + low + close) / 3
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - sma) / (0.015 * mad)


def compute_williams_r(high, low, close, period=14):
    highest = high.rolling(period).max()
    lowest = low.rolling(period).min()
    return -100 * (highest - close) / (highest - lowest)


def compute_vwap_rolling(high, low, close, volume, window=48):
    typical_price = (high + low + close) / 3
    return (typical_price * volume).rolling(window).sum() / volume.rolling(window).sum()


def compute_swing_low_distance_pct(low, close, window=5):
    is_swing_low = low == low.rolling(window * 2 + 1, center=True, min_periods=window * 2 + 1).min()
    swing_low_price = low.where(is_swing_low).ffill()
    return (close - swing_low_price) / swing_low_price * 100


def compute_all_indicators(g: pd.DataFrame) -> pd.DataFrame:
    """يحسب كل المؤشرات لرمز واحد. g يجب أن تكون مرتبة زمنيًا وتحتوي open/high/low/close/volume."""
    g = g.sort_values("open_time_utc").reset_index(drop=True).copy()
    g["ema_fast"] = ema(g["close"], EMA_FAST)
    g["ema_mid"] = ema(g["close"], EMA_MID)
    g["ema_slow"] = ema(g["close"], EMA_SLOW)
    g["vwap"] = compute_vwap_rolling(g["high"], g["low"], g["close"], g["volume"], VWAP_WINDOW)
    g["rsi"] = compute_rsi(g["close"], RSI_PERIOD)
    g["macd_hist"] = compute_macd_hist(g["close"], MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    _, _, g["bb_low"] = compute_bollinger(g["close"], BB_PERIOD, BB_STD)
    g["stoch_k"] = compute_stochastic_k(g["high"], g["low"], g["close"], STOCH_PERIOD)
    g["cci"] = compute_cci(g["high"], g["low"], g["close"], CCI_PERIOD)
    g["willr"] = compute_williams_r(g["high"], g["low"], g["close"], WILLR_PERIOD)
    g["atr"] = compute_atr(g["high"], g["low"], g["close"], ATR_PERIOD)
    g["dist_swing_low_pct"] = compute_swing_low_distance_pct(g["low"], g["close"], SWING_WINDOW)
    return g


def check_entry_signal(row, market_regime_return):
    """يفحص شروط الدخول على شمعة واحدة (آخر شمعة مغلقة). يرجع (True/False, score)."""
    if any(pd.isna(row[c]) for c in ["rsi", "bb_low", "dist_swing_low_pct", "vwap", "atr", "cci", "stoch_k", "willr"]):
        return False, 0.0

    if market_regime_return is None or market_regime_return < MARKET_REGIME_THRESHOLD:
        return False, 0.0

    swing_low_ok = SWING_LOW_DIST_MIN <= row["dist_swing_low_pct"] <= SWING_LOW_DIST_MAX

    entry_conditions = (
        row["close"] < row["vwap"]
        and row["ema_fast"] < row["ema_mid"] < row["ema_slow"]
        and row["rsi"] < RSI_THRESHOLD
        and row["macd_hist"] < 0
        and row["low"] <= row["bb_low"]
        and swing_low_ok
        and row["cci"] <= CCI_ENTRY_THRESHOLD
    )
    if not entry_conditions:
        return False, 0.0

    confluence = sum([row["stoch_k"] < 20, row["cci"] < -100, row["willr"] < -80])
    if confluence < MIN_CONFLUENCE:
        return False, 0.0

    score = (
        0.30 * (RSI_THRESHOLD - row["rsi"]) / RSI_THRESHOLD
        + 0.25 * min(abs(row["cci"]) / 300, 1)
        + 0.20 * min(abs(row["willr"]) / 100, 1)
        + 0.15 * (1 - min(row["stoch_k"] / 20, 1))
        + 0.10 * (1 - min(abs(row["dist_swing_low_pct"]) / 1.0, 1))
    )
    return True, score


def compute_sl_tp(entry_price, atr_value):
    sl_price = entry_price - SL_ATR_MULT * atr_value
    tp_price = entry_price + TP_ATR_MULT * atr_value
    return sl_price, tp_price
