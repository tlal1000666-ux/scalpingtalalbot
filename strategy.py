"""
منطق استراتيجية BOS + Order Block - نفس منطق backtest_bos_orderblock_scored.py بالضبط
(بدون أي تعديل بالشروط)، بس مُعاد هيكلته كواجهة "فحص آخر شمعة" يستخدمها بوت حي
(بدل حلقة تمشي على تاريخ كامل دفعة وحدة).

الفرق الجوهري عن استراتيجية Mean Reversion: الدخول هون مش فوري - أول ما تنكشف
إشارة BOS+OB، بتصير "Setup معلّق" (Pending) بانتظار السعر يلمس قمة الـOrder Block
(دخول Limit). لهيك run_bot_bos.py بيحتاج يتتبّع حالتين منفصلتين لكل رمز:
  1. pending_setups: إشارة اتكشفت وبتستنى تنفيذ (fill)
  2. open_positions: اتنفذت فعلاً وبتستنى SL/TP/Timeout
"""
import numpy as np
import pandas as pd

# ============================== إعدادات الاستراتيجية (من الباكتست الأصلي) ==============================
PIVOT_LEN = 5
OB_LOOKBACK = 20
ATR_LEN = 14
MIN_ATR_PCT = 0.5
MAX_ATR_PCT = 5.0
MIN_PULLBACK_PCT = 0.10
TARGET_RR = 1.05
SL_BUFFER_ATR = 0.10
MAX_BARS_ACTIVE = 24          # أقصى عمر للـ setup كامل (من لحظة الإشارة، معلّق أو مفتوح)

COMMISSION_PCT_PER_SIDE = 0.10
SLIPPAGE_PCT_PER_SIDE = 0.05
ROUND_TRIP_COST_PCT = (COMMISSION_PCT_PER_SIDE + SLIPPAGE_PCT_PER_SIDE) * 2  # 0.30%

# --- إدارة المحفظة (لغرض التوصية فقط) ---
POSITION_SIZE_PCT = 1 / 3
MAX_CONCURRENT_TRADES = 3
# ==========================================================================================================


def compute_atr(high, low, close, period=14):
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_pivot_high(high, length):
    """يطابق ta.pivothigh(high, length, length): قمة مؤكدة بعد 'length' شمعة من كل جهة."""
    is_pivot = high == high.rolling(length * 2 + 1, center=True, min_periods=length * 2 + 1).max()
    return high.where(is_pivot)


def compute_all_indicators(g: pd.DataFrame) -> pd.DataFrame:
    """يحسب ATR وآخر Swing High مؤكد لكل شمعة. g لازم تكون مرتبة زمنيًا."""
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

    return g


def check_new_signal(g: pd.DataFrame):
    """
    يفحص آخر شمعة مغلقة (الصف الأخير بـ g) بحثًا عن إشارة BOS + Order Block جديدة.
    يرجع dict فيه (entry1, sl, tp, score, signal_time) لو في إشارة، وإلا None.

    ملاحظة: هاد بس "كشف" الإشارة (نفس لحظة signal_bar بالباكتست) - التنفيذ الفعلي
    (fill) بيصير لاحقًا لما low شمعة جاية تلمس entry1، وده بتتكفل فيه run_bot_bos.py.
    """
    n = len(g)
    min_bars = max(ATR_LEN, PIVOT_LEN * 2 + 5)
    if n < min_bars + 1:
        return None

    i = n - 1  # آخر شمعة مغلقة
    high = g["high"].values
    low = g["low"].values
    close = g["close"].values
    open_ = g["open"].values
    atr = g["atr"].values
    atr_pct = g["atr_pct"].values
    last_swing_high = g["last_swing_high"].values

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
    sl = low[i - ob_index] - atr[i] * SL_BUFFER_ATR
    pullback_ok = entry1 <= close[i] * (1 - MIN_PULLBACK_PCT / 100)
    if not pullback_ok:
        return None

    risk = entry1 - sl
    if risk <= 0:
        return None
    tp = entry1 + risk * TARGET_RR

    pullback_pct = (close[i] - entry1) / close[i] * 100
    risk_pct = risk / entry1 * 100
    atr_mid = (MIN_ATR_PCT + MAX_ATR_PCT) / 2
    atr_dist_from_mid = abs(atr_pct[i] - atr_mid) / (MAX_ATR_PCT - MIN_ATR_PCT)
    score = (
        0.45 * min(pullback_pct / 2.0, 1.0)
        + 0.35 * (1 - min(risk_pct / 5.0, 1.0))
        + 0.20 * (1 - min(atr_dist_from_mid, 1.0))
    )

    return {
        "signal_time": g["open_time_utc"].iloc[i],
        "entry1": float(entry1),
        "sl": float(sl),
        "tp": float(tp),
        "score": float(score),
    }
