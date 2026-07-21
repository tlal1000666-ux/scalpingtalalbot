"""
منطق استراتيجية BOS + Order Block (نسخة حية - سببية بالكامل، بدون أي نظر للمستقبل)
مرجع الباكتست: backtest_bos_orderblock_scored.py

الفرق الجوهري عن استراتيجية Mean Reversion القديمة: هذه الاستراتيجية صفقاتها
Limit Order مش Market — الإشارة (BOS) بتفتح "إعداد معلّق" (pending setup)،
والصفقة بتتفعّل فقط لو السعر رجع لمس سعر الدخول المحدد خلال MAX_BARS_ACTIVE شمعة.
"""
import numpy as np
import pandas as pd

# --- إعدادات الاستراتيجية (مطابقة تمامًا لملف الباكتست المُتحقق منه) ---
PIVOT_LEN = 5              # طول الفراكتال لتحديد Swing High
OB_LOOKBACK = 20           # أقصى عدد شموع للبحث عن Order Block قبل شمعة BOS
ATR_LEN = 14
MIN_ATR_PCT = 0.5          # % - أقل تقلب مقبول
MAX_ATR_PCT = 5.0          # % - أعلى تقلب مقبول
MIN_PULLBACK_PCT = 0.10    # % - أقل مسافة بين قمة الـOB والسعر الحالي
TARGET_RR = 1.05           # نسبة الهدف للمخاطرة
SL_BUFFER_ATR = 0.10       # هامش إضافي تحت قاع الـOB بوحدات ATR
MAX_BARS_ACTIVE = 24       # أقصى عدد شموع لانتظار التنفيذ أو إغلاق الصفقة زمنيًا

# --- تكاليف التنفيذ الواقعية ---
COMMISSION_PCT_PER_SIDE = 0.10   # % عمولة المنصة لكل جهة (دخول + خروج = 0.20% إجمالي)
SLIPPAGE_PCT_PER_SIDE = 0.05     # % انزلاق سعري متوقع لكل جهة (أوامر السوق/التنفيذ الفعلي غالبًا أسوأ من السعر النظري)
# التكلفة الكاملة لدورة كاملة (دخول+خروج): (COMMISSION + SLIPPAGE) * 2
ROUND_TRIP_COST_PCT = (COMMISSION_PCT_PER_SIDE + SLIPPAGE_PCT_PER_SIDE) * 2

# --- إدارة المحفظة ---
STARTING_BALANCE = 10000.0
MAX_CONCURRENT_TRADES = 3
POSITION_SIZE_PCT = 1 / 3


def compute_atr(high, low, close, period=14):
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_pivot_high_raw(high, length):
    """يطابق ta.pivothigh(high, length, length): قمة "خام" تحتاج بيانات مستقبلية للتأكيد."""
    is_pivot = high == high.rolling(length * 2 + 1, center=True, min_periods=length * 2 + 1).max()
    return high.where(is_pivot)


def compute_all_indicators(g: pd.DataFrame) -> pd.DataFrame:
    """
    يحسب كل المؤشرات لرمز واحد. g يجب أن تكون مرتبة زمنيًا وتحتوي open/high/low/close.

    ⚠️ ملاحظة سببية مهمة: last_swing_high[i] بيُحسب من pivot_high_raw[i-PIVOT_LEN]،
    يعني بيحتاج بيانات لغاية index i بالظبط (مفيش أي نظر للمستقبل بالنسبة لآخر شمعة
    مغلقة "i" وقت الفحص الحي) — نفس المنطق المُتحقق منه في الباكتست بالضبط.
    """
    g = g.sort_values("open_time_utc").reset_index(drop=True).copy()
    n = len(g)

    g["atr"] = compute_atr(g["high"], g["low"], g["close"], ATR_LEN)
    g["atr_pct"] = g["atr"] / g["close"] * 100

    pivot_high_raw = compute_pivot_high_raw(g["high"], PIVOT_LEN).values
    last_swing_high = np.full(n, np.nan)
    current_val = np.nan
    for i in range(n):
        confirm_idx = i - PIVOT_LEN
        if confirm_idx >= 0 and not np.isnan(pivot_high_raw[confirm_idx]):
            current_val = pivot_high_raw[confirm_idx]
        last_swing_high[i] = current_val
    g["last_swing_high"] = last_swing_high

    return g


def check_new_signal(df: pd.DataFrame):
    """
    يفحص هل آخر شمعة مغلقة في df كوّنت إشارة BOS + Order Block جديدة.
    يرجع None لو مفيش إشارة، أو dict فيه (entry1, sl, tp, score) لو في إشارة.
    """
    n = len(df)
    i = n - 1
    if i < max(ATR_LEN, PIVOT_LEN * 2 + 5):
        return None

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    open_ = df["open"].values
    atr = df["atr"].values
    atr_pct = df["atr_pct"].values
    last_swing_high = df["last_swing_high"].values

    bullish_bos = (
        not np.isnan(last_swing_high[i])
        and close[i] > last_swing_high[i]
        and close[i - 1] <= last_swing_high[i]
    )
    if not bullish_bos:
        return None

    atr_ok = MIN_ATR_PCT <= atr_pct[i] <= MAX_ATR_PCT
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
    if not (atr_ok and pullback_ok):
        return None

    risk = entry1 - sl
    tp = entry1 + risk * TARGET_RR

    # --- السكور: يُستخدم لو أكتر من رمز اتزاحموا على نفس مكان الدخول في نفس اللحظة ---
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
        "entry1": float(entry1),
        "sl": float(sl),
        "tp": float(tp),
        "score": float(score),
        "signal_time": str(df["open_time_utc"].iloc[i]),
    }


def apply_entry_slippage(limit_price):
    """أمر Limit شراء: الانزلاق المتوقع بيخلي التنفيذ الفعلي أعلى شوية من السعر النظري."""
    return limit_price * (1 + SLIPPAGE_PCT_PER_SIDE / 100)


def apply_exit_slippage(exit_price, is_stop_loss):
    """
    عند SL: التنفيذ الفعلي غالبًا أسوأ (أقل) من سعر الوقف النظري (انزلاق ضد الصفقة).
    عند TP: نفترض تنفيذ متحفظ (نفس المنطق - انزلاق ضد الصفقة بشكل طفيف).
    """
    if is_stop_loss:
        return exit_price * (1 - SLIPPAGE_PCT_PER_SIDE / 100)
    return exit_price * (1 - SLIPPAGE_PCT_PER_SIDE / 100)


def compute_net_pnl_pct(entry_price, exit_price):
    """العائد الصافي % بعد خصم العمولة (لسه العمولة بس، الانزلاق مطبّق مسبقًا على الأسعار)."""
    gross_pct = (exit_price - entry_price) / entry_price * 100
    return gross_pct - COMMISSION_PCT_PER_SIDE * 2
