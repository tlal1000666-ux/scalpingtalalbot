"""
منطق استراتيجية BOS + Order Block — النسخة المُحسّنة
==================== التحديث (بعد باكتست 3 شهور بدقة 1m) ====================
1) الإطار الزمني: 1h (أثبت تفوقه على 30m: PF 2.13 vs 1.54)
2) الدخول: Limit عند entry1 (مش Close) — هذا هو الـedge الأساسي
   باكتست 3 شهور: PF=4.22 عند entry1 vs PF=0.83 عند Close
3) تأكيد الارتداد: شمعة اللمس لازم تغلق فوق entry1 (رفض الانهيار)
4) ATR_MULT_SL = 1.5 | ATR_MULT_TP = 1.0 (الأمثل بالباكتست)
5) MAX_BARS_ACTIVE = 6 (6 ساعات على 1h)
6) MIN_SCORE_THRESHOLD = 0.6
===============================================================================
"""

import numpy as np
import pandas as pd

# ============================== إعدادات الاستراتيجية ==============================
PIVOT_LEN = 5
OB_LOOKBACK = 20
ATR_LEN = 14
MIN_ATR_PCT = 0.5
MAX_ATR_PCT = 5.0
VOLUME_MA_LEN = 20
MIN_VOLUME_RATIO = 0.0
MIN_PULLBACK_PCT = 0.30
MAX_BARS_ACTIVE = 6
ATR_MULT_SL = 1.5
ATR_MULT_TP = 1.0
MIN_TARGET_PCT = 1.5
MIN_STOP_PCT = 1.0
MIN_SCORE_THRESHOLD = 0.6
MIN_ATR_PCT_AT_ENTRY_STRICT = 0.0
EXCLUDED_SIGNAL_HOURS_UTC = set()
TRAIL_ARM_PCT = 0.65
TRAIL_EXIT_PCT = 0.55
COMMISSION_PCT_PER_SIDE = 0.10
SLIPPAGE_PCT_PER_SIDE = 0.05
ROUND_TRIP_COST_PCT = (COMMISSION_PCT_PER_SIDE + SLIPPAGE_PCT_PER_SIDE) * 2
NET_TARGET_MIN_PCT = 1.0
POSITION_SIZE_PCT = 1 / 3
MAX_CONCURRENT_TRADES = 3
MONTHLY_TRADE_CAP = 150
SYMBOL_COOLDOWN_HOURS = 12
MAX_POSITION_SIZE_USD = float("inf")
MONTHLY_STOP_PCT = float("-inf")
# ==========================================================================================================


def compute_atr(high, low, close, period=14):
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_pivot_high(high, length):
    is_pivot = high == high.rolling(length * 2 + 1, center=True, min_periods=length * 2 + 1).max()
    return high.where(is_pivot)


def compute_all_indicators(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("open_time_utc").reset_index(drop=True).copy()
    n = len(g)
    g["atr"] = compute_atr(g["high"], g["low"], g["close"], ATR_LEN)
    g["atr_pct"] = g["atr"] / g["close"] * 100
    pivot_high_raw = compute_pivot_high(g["high"], PIVOT_LEN).values
    last_swing_high = np.full(n, np.nan)
    current_val = np.nan
    for i in range(n):
        confirm_idx = i - PIVOT_LEN
        if confirm_idx >= 0 and not np.isnan(pivot_high_raw[confirm_idx]):
            current_val = pivot_high_raw[confirm_idx]
        last_swing_high[i] = current_val
    g["last_swing_high"] = last_swing_high
    if "volume" in g.columns:
        g["volume_ma"] = g["volume"].rolling(VOLUME_MA_LEN, min_periods=VOLUME_MA_LEN).mean()
        g["volume_ratio"] = g["volume"] / g["volume_ma"]
    else:
        g["volume_ratio"] = 1.0
    return g


def evaluate_signal_at(i, high, low, close, open_, atr, atr_pct, last_swing_high,
                       signal_hour_utc=None, volume_ratio=None):
    if signal_hour_utc is not None and signal_hour_utc in EXCLUDED_SIGNAL_HOURS_UTC:
        return None
    if np.isnan(last_swing_high[i]) or np.isnan(atr[i]):
        return None
    bullish_bos = (
        close[i] > last_swing_high[i]
        and close[i - 1] <= last_swing_high[i]
    )
    if not bullish_bos:
        return None
    atr_ok = MIN_ATR_PCT <= atr_pct[i] <= MAX_ATR_PCT
    if not atr_ok:
        return None
    if volume_ratio is not None:
        vr = volume_ratio[i]
        if np.isnan(vr) or vr < MIN_VOLUME_RATIO:
            return None
    ob_index = None
    for k in range(1, OB_LOOKBACK + 1):
        if i - k < 0:
            break
        if close[i - k] < open_[i - k]:
            ob_index = k
            break
    if ob_index is None:
        return None
    entry1 = high[i - ob_index]
    pullback_ok = entry1 <= close[i] * (1 - MIN_PULLBACK_PCT / 100)
    if not pullback_ok:
        return None
    atr_pct_at_entry = atr[i] / entry1 * 100
    if not (MIN_ATR_PCT <= atr_pct_at_entry <= MAX_ATR_PCT):
        return None
    if atr_pct_at_entry < MIN_ATR_PCT_AT_ENTRY_STRICT:
        return None
    sl_dist = atr[i] * ATR_MULT_SL
    tp_dist = atr[i] * ATR_MULT_TP
    sl_dist = max(sl_dist, entry1 * MIN_STOP_PCT / 100)
    tp_dist = max(tp_dist, entry1 * MIN_TARGET_PCT / 100)
    sl = entry1 - sl_dist
    tp = entry1 + tp_dist
    risk = entry1 - sl
    if risk <= 0:
        return None
    tp_pct_gross = tp_dist / entry1 * 100
    net_target_pct = tp_pct_gross - ROUND_TRIP_COST_PCT
    if net_target_pct < NET_TARGET_MIN_PCT:
        return None
    pullback_pct = (close[i] - entry1) / close[i] * 100
    risk_pct = risk / entry1 * 100
    atr_mid = (MIN_ATR_PCT + MAX_ATR_PCT) / 2
    atr_dist_from_mid = abs(atr_pct[i] - atr_mid) / (MAX_ATR_PCT - MIN_ATR_PCT)
    score = (
        0.45 * min(pullback_pct / 2.0, 1.0)
        + 0.35 * (1 - min(risk_pct / 5.0, 1.0))
        + 0.20 * (1 - min(atr_dist_from_mid, 1.0))
    )
    if score < MIN_SCORE_THRESHOLD:
        return None
    return {"entry1": float(entry1), "sl": float(sl), "tp": float(tp), "score": float(score)}


def check_new_signal(df, current_end=None):
    """يفحص آخر شمعة مغلقة فقط (مثل البوت الحي)."""
    if current_end is not None:
        df_view = df[df["open_time_utc"] <= current_end].copy()
    else:
        df_view = df.copy()
    if len(df_view) < 30:
        return None
    high = df_view["high"].values
    low = df_view["low"].values
    close = df_view["close"].values
    open_ = df_view["open"].values
    atr = df_view["atr"].values
    atr_pct = df_view["atr_pct"].values
    lsh = df_view["last_swing_high"].values
    vr = df_view["volume_ratio"].values if "volume_ratio" in df_view.columns else None
    i = len(df_view) - 1
    signal_hour = df_view.iloc[i]["open_time_utc"].hour
    res = evaluate_signal_at(i, high, low, close, open_, atr, atr_pct, lsh,
                             signal_hour_utc=signal_hour, volume_ratio=vr)
    if res is None:
        return None
    return {
        "signal_time": df_view.iloc[i]["open_time_utc"],
        "entry1": res["entry1"],
        "sl": res["sl"],
        "tp": res["tp"],
        "score": res["score"],
    }
