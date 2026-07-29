"""
بوت توصيات تداول - استراتيجية BOS + Order Block (النسخة المُحسّنة)
يشتغل مرة كل تشغيل (مصمم ليُستدعى دوريًا كل ساعة)

==================== التحديث (بعد باكتست 3 شهور بدقة 1m) ====================
1) الإطار الزمني: 1h (أثبت تفوقه على 30m)
2) الإصلاح الجوهري: الدخول عند entry1 (Limit) — مش عند Close
   باكتست 3 شهور (224 رمز، 1577 صفقة، تحقق 1m):
   - دخول entry1: PF=4.22 | WR=87.5% | +348%
   - دخول Close:  PF=0.83 | WR=67.4% | -41%
3) تأكيد الارتداد: شمعة اللمس لازم تغلق فوق entry1 (رفض الانهيار)
4) ATR_MULT_SL=1.5 | ATR_MULT_TP=1.0 | MAX_BARS_ACTIVE=6
5) تم إزالة مسار الظل (Shadow) — غير مطلوب
===============================================================================

الوظيفة:
  1. يفحص أوامر تلجرام الجديدة (/balance /positions /pending /stats /signals /help)
  2. يجلب آخر بيانات الشموع (1h) من Binance لقائمة الرموز في symbols.txt
  3. يفحص الـsetups المعلّقة: تنفيذ (بعد تأكيد الارتداد) أو إلغاء بسبب Timeout
  4. يتابع الصفقات المفتوحة: يغلقها عند SL/TP/Trail/Timeout
  5. يفحص إشارات BOS+OB جديدة على الرموز الخالية من setup حاليًا
  6. يرسل كل حدث كرسالة تلجرام، ويحدّث الرصيد الافتراضي والإحصائيات
  7. يسجل كل صفقة مغلقة في trades_log.csv

⚠️ هذا أداة توصيات وتتبع فقط — لا ينفذ أي صفقة حقيقية بنفسه، ولا يشكل نصيحة استثمارية.
"""

import os
import json
import csv
from datetime import datetime, timezone, timedelta

import pandas as pd

import strategy
from utils import (fetch_klines, fetch_klines_1m_window,
                   send_telegram_message, get_telegram_updates, sleep_safe)

STATE_FILE = "state.json"
TRADES_LOG_FILE = "trades_log.csv"
SYMBOLS_FILE = "symbols.txt"
INTERVAL = "1h"
INTERVAL_MINUTES = 60
STARTING_BALANCE = 10000.0
MAX_SIGNAL_HISTORY = 20

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def load_symbols():
    with open(SYMBOLS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def default_state():
    return {
        "pending_setups": {},
        "open_positions": {},
        "last_candle_seen": {},
        "balance": STARTING_BALANCE,
        "stats": {"total_trades": 0, "wins": 0, "losses": 0, "gross_profit": 0.0, "gross_loss": 0.0},
        "last_update_id": 0,
        "signal_history": [],
    }


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        # إضافة أي مفاتيح ناقصة
        for k, v in default_state().items():
            if k not in state:
                state[k] = v
        # ترحيل: حذف حقول Shadow القديمة (لم تعد مستخدمة)
        shadow_keys = ["shadow_pending_setups", "shadow_open_positions",
                       "shadow_balance", "shadow_stats"]
        for k in shadow_keys:
            state.pop(k, None)
        return state
    return default_state()


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)


def append_trade_log(row: dict):
    file_exists = os.path.exists(TRADES_LOG_FILE)
    with open(TRADES_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def fmt_price(p):
    return f"{p:.6f}".rstrip("0").rstrip(".")


def tv_link(symbol):
    return f"https://www.tradingview.com/symbols/{symbol}/"


def push(msg):
    print(msg.replace("\n", " | "))
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
    else:
        print("  [تنبيه] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID غير مضبوطة.")


def reply(chat_id, msg):
    print(f"[رد على أمر] {msg.replace(chr(10), ' | ')}")
    if TELEGRAM_TOKEN:
        send_telegram_message(TELEGRAM_TOKEN, chat_id, msg)


def log_signal(state, kind, symbol, detail):
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "symbol": symbol,
        "detail": detail,
    }
    state["signal_history"].insert(0, entry)
    state["signal_history"] = state["signal_history"][:MAX_SIGNAL_HISTORY]


# ============================================================
# معالجة أوامر تلجرام
# ============================================================
def handle_commands(state):
    if not TELEGRAM_TOKEN:
        return
    offset = state.get("last_update_id", 0) + 1
    updates = get_telegram_updates(TELEGRAM_TOKEN, offset=offset)
    for update in updates:
        state["last_update_id"] = update["update_id"]
        msg = update.get("message")
        if not msg or "text" not in msg:
            continue
        chat_id = str(msg["chat"]["id"])
        if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
            continue
        text = msg["text"].strip().lower()
        if text in ("/balance", "/رصيد"):
            handle_balance(state, chat_id)
        elif text in ("/positions", "/الصفقات"):
            handle_positions(state, chat_id)
        elif text in ("/pending", "/معلقة"):
            handle_pending(state, chat_id)
        elif text in ("/stats", "/احصائيات", "/إحصائيات"):
            handle_stats(state, chat_id)
        elif text in ("/signals", "/last", "/آخر"):
            handle_signals(state, chat_id)
        elif text in ("/start", "/help", "/مساعدة"):
            handle_help(chat_id)
        elif text in ("/reset", "/تصفير"):
            handle_reset(state, chat_id)


def handle_balance(state, chat_id):
    balance = state.get("balance", STARTING_BALANCE)
    total_return_pct = (balance - STARTING_BALANCE) / STARTING_BALANCE * 100
    reply(chat_id, (
        f"💰 <b>الرصيد الافتراضي الحالي</b>\n"
        f"الرصيد: ${balance:,.2f}\n"
        f"رأس المال الابتدائي: ${STARTING_BALANCE:,.2f}\n"
        f"العائد التراكمي: {total_return_pct:+.2f}%"
    ))


def handle_positions(state, chat_id):
    positions = state.get("open_positions", {})
    if not positions:
        reply(chat_id, "📭 ما فيه صفقات مفتوحة حاليًا.")
        return
    lines = [f"📂 <b>الصفقات المفتوحة ({len(positions)})</b>\n"]
    for sym, pos in positions.items():
        lines.append(
            f"• <b>{sym}</b>\n"
            f"  دخول: {fmt_price(pos['entry_price'])} | SL: {fmt_price(pos['sl'])} | TP: {fmt_price(pos['tp'])}\n"
            f"  وقت التنفيذ: {pos['entry_time']}\n"
            f"  📈 <a href=\"{tv_link(sym)}\">TradingView</a>"
        )
    reply(chat_id, "\n\n".join(lines))


def handle_pending(state, chat_id):
    pending = state.get("pending_setups", {})
    if not pending:
        reply(chat_id, "📭 ما فيه أوامر معلّقة (Limit) حاليًا.")
        return
    lines = [f"⏳ <b>أوامر معلّقة بانتظار التنفيذ ({len(pending)})</b>\n"]
    for sym, p in pending.items():
        lines.append(
            f"• <b>{sym}</b>\n"
            f"  Entry (Limit): {fmt_price(p['entry1'])} | SL: {fmt_price(p['sl'])} | TP: {fmt_price(p['tp'])}\n"
            f"  وقت الإشارة: {p['signal_time']}\n"
            f"  📈 <a href=\"{tv_link(sym)}\">TradingView</a>"
        )
    reply(chat_id, "\n\n".join(lines))


def handle_stats(state, chat_id):
    s = state.get("stats", {})
    total = s.get("total_trades", 0)
    if total == 0:
        reply(chat_id, "📊 ما فيه صفقات مغلقة لسا.")
        return
    wins = s.get("wins", 0)
    losses = s.get("losses", 0)
    win_rate = wins / total * 100 if total else 0
    gp = s.get("gross_profit", 0.0)
    gl = s.get("gross_loss", 0.0)
    pf = (gp / gl) if gl > 0 else float("inf")
    balance = state.get("balance", STARTING_BALANCE)
    ret = (balance - STARTING_BALANCE) / STARTING_BALANCE * 100
    pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
    reply(chat_id, (
        f"📊 <b>إحصائيات الأداء — BOS + Order Block (1h)</b>\n"
        f"إجمالي الصفقات: {total}\n"
        f"رابحة: {wins} | خاسرة: {losses}\n"
        f"Win Rate: {win_rate:.2f}%\n"
        f"Profit Factor: {pf_str}\n"
        f"العائد التراكمي: {ret:+.2f}%\n"
        f"الرصيد الحالي: ${balance:,.2f}"
    ))


def handle_signals(state, chat_id):
    history = state.get("signal_history", [])
    if not history:
        reply(chat_id, "🕑 ما فيه إشارات مسجلة لسا.")
        return
    lines = ["🕑 <b>آخر الإشارات</b>\n"]
    for h in history[:10]:
        lines.append(f"• [{h['kind']}] {h['symbol']} — {h['detail']}")
    reply(chat_id, "\n".join(lines))


def handle_help(chat_id):
    reply(chat_id, (
        "🤖 <b>أوامر البوت — BOS + Order Block (1h)</b>\n\n"
        "/balance — الرصيد الافتراضي\n"
        "/positions — الصفقات المفتوحة\n"
        "/pending — الأوامر المعلّقة\n"
        "/stats — الإحصائيات التراكمية\n"
        "/signals — آخر 10 إشارات\n"
        "/reset — تصفير الرصيد والإحصائيات (يبدأ من جديد)\n"
        "/help — هذه القائمة\n\n"
        "⚠️ البوت يفحص كل ساعة تقريبًا."
    ))


def handle_reset(state, chat_id):
    """تصفير الرصيد والإحصائيات — يبدأ من $10,000 من جديد."""
    state["balance"] = STARTING_BALANCE
    state["stats"] = {"total_trades": 0, "wins": 0, "losses": 0, "gross_profit": 0.0, "gross_loss": 0.0}
    state["open_positions"] = {}
    state["pending_setups"] = {}
    state["signal_history"] = []
    reply(chat_id, (
        "🔄 <b>تم التصفير</b>\n"
        f"الرصيد: ${STARTING_BALANCE:,.2f}\n"
        "الإحصائيات: صفر\n"
        "الصفقات المفتوحة والمعلّقة: أُلغيت"
    ))


# ============================================================
# فحص الخروج (SL/TP/Trail/Timeout) مع دقة 1m
# ============================================================
def _check_exit_with_trail(pos, candle, bars_since_signal):
    """يفحص شمعة واحدة لصفقة مفتوحة بدقة 1m (مثل البوت الأصلي)."""
    sl, tp = pos["sl"], pos["tp"]
    armed = pos.get("armed", False)
    trail_arm_price = pos["entry_price"] + (tp - pos["entry_price"]) * strategy.TRAIL_ARM_PCT
    trail_exit_price = pos["entry_price"] + (tp - pos["entry_price"]) * strategy.TRAIL_EXIT_PCT

    # محاولة النزول لـ1m
    exit_price, exit_reason, armed_1m, unresolved = _resolve_position_1m(
        pos.get("_sym", ""), candle["open_time_utc"], sl, tp, armed, trail_arm_price, trail_exit_price
    )
    if not unresolved:
        armed = armed_1m
        if exit_price is not None:
            return exit_price, exit_reason, armed
        if bars_since_signal > strategy.MAX_BARS_ACTIVE:
            return candle["close"], "Timeout ⏱️", armed
        return None, None, armed

    # Fallback: منطق الشمعة الكبيرة (1h)
    if candle["high"] >= trail_arm_price:
        armed = True
    hit_sl = candle["low"] <= sl
    hit_tp = candle["high"] >= tp
    if hit_sl and hit_tp:
        order = _resolve_conflict_order(pos.get("_sym", ""), candle["open_time_utc"], sl, tp)
        if order == "TP":
            return tp, "TP 🟢", armed
        return sl, "SL 🔴", armed
    if hit_sl:
        return sl, "SL 🔴", armed
    if hit_tp:
        return tp, "TP 🟢", armed
    if armed and candle["low"] <= trail_exit_price:
        return trail_exit_price, "Trail-Lock 🔒", armed
    if bars_since_signal > strategy.MAX_BARS_ACTIVE:
        return candle["close"], "Timeout ⏱️", armed
    return None, None, armed


def _resolve_position_1m(symbol, candle_open_time, sl, tp, trail_armed_in, trail_arm_price, trail_exit_price):
    """يفحص بدقة 1m لنافذة زمنية محددة. يرجع (exit_price, exit_reason, trail_armed, unresolved).

    يستخدم fetch_klines_1m_window (بـ start_time/end_time) بدل limit=90،
    عشان يشتغل حتى لو البوت يفحص شمعة قديمة (catch-up بعد توقف).
    """
    window_start = pd.Timestamp(candle_open_time)
    window_end = window_start + pd.Timedelta(minutes=INTERVAL_MINUTES)
    try:
        start_ms = int(window_start.timestamp() * 1000)
        end_ms = int(window_end.timestamp() * 1000)
        df_1m = fetch_klines_1m_window(symbol, start_ms, end_ms)
    except Exception:
        return None, None, trail_armed_in, True
    if df_1m is None or df_1m.empty:
        return None, None, trail_armed_in, True
    mask = (df_1m["open_time_utc"] >= window_start) & (df_1m["open_time_utc"] < window_end)
    window_candles = df_1m[mask].sort_values("open_time_utc")
    if window_candles.empty:
        return None, None, trail_armed_in, True
    trail_armed = trail_armed_in
    for _, c in window_candles.iterrows():
        if c["low"] <= sl:
            return sl, "SL 🔴", trail_armed, False
        if c["high"] >= tp:
            return tp, "TP 🟢", trail_armed, False
        if c["high"] >= trail_arm_price:
            trail_armed = True
        if trail_armed and c["low"] <= trail_exit_price:
            return trail_exit_price, "Trail-Lock 🔒", trail_armed, False
    return None, None, trail_armed, False


def _resolve_conflict_order(symbol, candle_open_time, sl, tp):
    """عند تعارض SL+TP بنفس الشمعة، ينزل لـ1m لمعرفة أيهم أول.
    يستخدم نافذة زمنية محددة (start_time/end_time) بدل limit."""
    window_start = pd.Timestamp(candle_open_time)
    window_end = window_start + pd.Timedelta(minutes=INTERVAL_MINUTES)
    try:
        start_ms = int(window_start.timestamp() * 1000)
        end_ms = int(window_end.timestamp() * 1000)
        df_1m = fetch_klines_1m_window(symbol, start_ms, end_ms)
    except Exception:
        return None
    if df_1m is None or df_1m.empty:
        return None
    mask = (df_1m["open_time_utc"] >= window_start) & (df_1m["open_time_utc"] < window_end)
    window_candles = df_1m[mask].sort_values("open_time_utc")
    if window_candles.empty:
        return None
    for _, c in window_candles.iterrows():
        hit_sl = c["low"] <= sl
        hit_tp = c["high"] >= tp
        if hit_sl and hit_tp:
            return None
        if hit_sl:
            return "SL"
        if hit_tp:
            return "TP"
    return None


def _new_candles_since(df, last_seen_key):
    """يرجع الشموع الأحدث من last_seen_key."""
    if last_seen_key is None:
        return df.iloc[[-1]]
    try:
        last_seen_ts = pd.Timestamp(last_seen_key)
    except (ValueError, TypeError):
        return df.iloc[[-1]]
    new_rows = df[df["open_time_utc"] > last_seen_ts]
    if new_rows.empty:
        return new_rows
    return new_rows


# ============================================================
# المنطق الرئيسي
# ============================================================
def main():
    print(f"=== تشغيل بوت BOS+OB (1h) — {datetime.now(timezone.utc).isoformat()} ===")
    symbols = load_symbols()
    state = load_state()
    print(f"عدد الرموز المراقبة: {len(symbols)}")
    try:
        _run_cycle(state, symbols)
    except Exception as e:
        print(f"⚠️ صار خطأ أثناء التشغيل: {e}")
        raise
    finally:
        save_state(state)
        print(f"مفتوحة: {len(state.get('open_positions', {}))} | معلّقة: {len(state.get('pending_setups', {}))} | الرصيد: ${state.get('balance', STARTING_BALANCE):,.2f}")
        print("=== انتهى التشغيل ===")


def _run_cycle(state, symbols):
    # 0) أوامر تلجرام
    handle_commands(state)

    # 1) جلب البيانات
    data = {}
    for sym in symbols:
        try:
            df = fetch_klines(sym, interval=INTERVAL, limit=500)
            sleep_safe(0.2)
            if df is None or len(df) < 250:
                continue
            df = strategy.compute_all_indicators(df)
            data[sym] = df
        except Exception as e:
            print(f"  [تحذير] تخطي {sym}: {e}")
            continue

    if not data:
        print("لم يتم جلب أي بيانات صالحة.")
        return

    pending_setups = state["pending_setups"]
    open_positions = state["open_positions"]

    # 2) فحص الصفقات المفتوحة (SL/TP/Trail/Timeout)
    for sym in list(open_positions.keys()):
        if sym not in data:
            continue
        try:
            pos = open_positions[sym]
            df = data[sym]
            signal_time = pd.Timestamp(pos["signal_time"])
            entry_time = pd.Timestamp(pos.get("entry_time", pos["signal_time"]))

            last_seen_key = state["last_candle_seen"].get(sym)
            use_entry_fallback = last_seen_key is None
            if not use_entry_fallback:
                try:
                    if pd.Timestamp(last_seen_key) < entry_time:
                        use_entry_fallback = True
                except (ValueError, TypeError):
                    use_entry_fallback = True

            if use_entry_fallback:
                new_candles = df[df["open_time_utc"] >= entry_time].sort_values("open_time_utc")
            else:
                new_candles = _new_candles_since(df, last_seen_key)

            for _, last in new_candles.iterrows():
                bars_since_signal = int((last["open_time_utc"] - signal_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))
                pos["_sym"] = sym
                exit_price, exit_reason, pos["armed"] = _check_exit_with_trail(pos, last, bars_since_signal)
                if exit_price is not None:
                    _close_position(state, sym, pos, exit_price, exit_reason, last["open_time_utc"])
                    del open_positions[sym]
                    break
        except Exception as e:
            print(f"  [تحذير] تخطي فحص {sym}: {e}")
            continue

    # 3) فحص الأوامر المعلّقة: تفعيل أو إلغاء
    fillable = []
    for sym in list(pending_setups.keys()):
        if sym not in data:
            continue
        try:
            p = pending_setups[sym]
            df = data[sym]
            signal_time = pd.Timestamp(p["signal_time"])

            last_seen_key = state["last_candle_seen"].get(sym)
            use_signal_fallback = last_seen_key is None
            if not use_signal_fallback:
                try:
                    if pd.Timestamp(last_seen_key) < signal_time:
                        use_signal_fallback = True
                except (ValueError, TypeError):
                    use_signal_fallback = True

            if use_signal_fallback:
                new_candles = df[df["open_time_utc"] > signal_time].sort_values("open_time_utc")
            else:
                new_candles = _new_candles_since(df, last_seen_key)

            fill_candle = None
            for _, c in new_candles.iterrows():
                bars_since_signal = int((c["open_time_utc"] - signal_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))
                touched = c["low"] <= p["entry1"]
                # تأكيد الارتداد: شمعة اللمس لازم تغلق فوق entry1
                confirmed = touched and (c["close"] > p["entry1"])
                if confirmed:
                    fill_candle = c
                    break
                elif bars_since_signal > strategy.MAX_BARS_ACTIVE:
                    del pending_setups[sym]
                    log_signal(state, "إلغاء", sym, "انتهى وقت الأمر المعلّق بدون تنفيذ")
                    break

            if fill_candle is not None:
                remaining = new_candles[new_candles["open_time_utc"] >= fill_candle["open_time_utc"]]
                fillable.append((sym, p, fill_candle, remaining))
        except Exception as e:
            print(f"  [تحذير] تخطي setup {sym}: {e}")
            continue

    # تنفيذ حسب الأولوية (score)
    fillable.sort(key=lambda x: x[1]["score"], reverse=True)
    for sym, p, fill_candle, remaining in fillable:
        try:
            del pending_setups[sym]
            available_slots = strategy.MAX_CONCURRENT_TRADES - len(open_positions)
            if available_slots <= 0:
                log_signal(state, "إلغاء", sym, "المحفظة ممتلئة (3 صفقات)")
                continue

            # ===== الإصلاح الجوهري: الدخول عند entry1 (Limit) =====
            # الباك تيست أثبت: entry1 → PF=4.22 | Close → PF=0.83
            entry_price = p["entry1"]
            entry_time = fill_candle["open_time_utc"]

            position_dollars = state["balance"] * strategy.POSITION_SIZE_PCT
            pos = {
                "signal_time": p["signal_time"], "entry_time": str(entry_time),
                "entry_price": entry_price, "sl": p["sl"], "tp": p["tp"], "score": p["score"],
                "position_dollars": position_dollars, "armed": False,
            }

            push(
                f"⚡ تم تفعيل الصفقة (ACT)\n\n"
                f"💎 Pair: #{sym}\n"
                f"📅 وقت الفتح: {p['signal_time']}\n\n"
                f"Entry: {fmt_price(entry_price)}"
            )
            log_signal(state, "تنفيذ", sym, f"دخول عند {fmt_price(entry_price)}")

            # فحص شمعة التفعيل + الشموع التالية للـSL/TP
            signal_time = pd.Timestamp(pos["signal_time"])
            closed = False
            for _, c in remaining.iterrows():
                bars_since_signal = int((c["open_time_utc"] - signal_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))
                pos["_sym"] = sym
                exit_price, exit_reason, pos["armed"] = _check_exit_with_trail(pos, c, bars_since_signal)
                if exit_price is not None:
                    _close_position(state, sym, pos, exit_price, exit_reason, c["open_time_utc"])
                    closed = True
                    break
            if not closed:
                open_positions[sym] = pos
        except Exception as e:
            print(f"  [تحذير] خطأ تنفيذ {sym}: {e}")
            continue

    # 4) فحص إشارات جديدة
    for sym, df in data.items():
        if sym in open_positions or sym in pending_setups:
            continue
        try:
            last_seen = state["last_candle_seen"].get(sym)
            candle_key = str(df.iloc[-1]["open_time_utc"])
            if last_seen == candle_key:
                continue

            sig = strategy.check_new_signal(df)
            if not sig:
                continue

            pending_setups[sym] = {
                "signal_time": str(sig["signal_time"]),
                "entry1": sig["entry1"], "sl": sig["sl"], "tp": sig["tp"],
                "score": sig["score"],
            }

            time_str = pd.Timestamp(sig["signal_time"]).strftime("%d/%m/%Y %H:%M")
            push(
                f"⚡ Scalping Talal Bot ⚡\n"
                f"🌟 بسم الله توكلت على الله 🌟\n\n"
                f"💎 Pair: #{sym}\n"
                f"💎 Exchange: BINANCE\n"
                f"⏳ Timeframe: 1h\n"
                f"📅 Time: {time_str} (GMT+3)\n\n"
                f"💰 Entry ➤ {fmt_price(sig['entry1'])}\n\n"
                f"🎯 Target\n"
                f"1️⃣ T1 ➤ {fmt_price(sig['tp'])}\n"
                f"• From Entry: {(sig['tp']-sig['entry1'])/sig['entry1']*100:+.2f}%\n\n"
                f"🔴 SL ➤ {fmt_price(sig['sl'])}\n"
                f"• From Entry: {(sig['sl']-sig['entry1'])/sig['entry1']*100:+.2f}%\n\n"
                f"📊 نقاط الثقة (Score): {sig['score']*100:.0f}/100\n\n"
                f"⚡ كن ذكيًا في إدارة مراكزك، فإدارة الصفقة نصف النجاح\n\n"
                f"⚡ Scalping Talal Bot ⚡\n"
                f"🏢 @Dr_talaltrke\n"
                f"📊 <a href=\"{tv_link(sym)}\">فتح الشارت على TradingView</a>"
            )
            log_signal(state, "إشارة", sym, f"Setup معلّق عند {fmt_price(sig['entry1'])}")
        except Exception as e:
            print(f"  [تحذير] تخطي إشارة {sym}: {e}")
            continue

    # تحديث last_candle_seen
    for sym, df in data.items():
        state["last_candle_seen"][sym] = str(df.iloc[-1]["open_time_utc"])


def _close_position(state, sym, pos, exit_price, exit_reason, exit_time):
    """يغلق صفقة ويحدّث الرصيد والإحصائيات والسجل."""
    round_trip_cost = strategy.ROUND_TRIP_COST_PCT
    pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100 - round_trip_cost
    position_dollars = pos.get("position_dollars")
    if position_dollars is None:
        position_dollars = state["balance"] * strategy.POSITION_SIZE_PCT
    pnl_dollars = position_dollars * pnl_pct / 100
    state["balance"] = state.get("balance", STARTING_BALANCE) + pnl_dollars

    s = state["stats"]
    s["total_trades"] += 1
    if pnl_dollars > 0:
        s["wins"] += 1
        s["gross_profit"] += pnl_dollars
    else:
        s["losses"] += 1
        s["gross_loss"] += abs(pnl_dollars)

    is_tp = exit_reason.startswith("TP")
    is_sl = exit_reason.startswith("SL")
    is_win = pnl_pct > 0
    close_time_str = pd.Timestamp(exit_time).strftime("%d/%m/%Y %H:%M")

    if is_tp:
        header = "✅ تحقق الهدف ولله الحمد (WIN)"
    elif is_sl:
        header = "❌ ضرب وقف الخسارة (LOSS)"
    elif is_win:
        header = "✅ إغلاق رابح ولله الحمد (WIN)"
    else:
        header = "❌ إغلاق خاسر (LOSS)"

    total_return_pct = (state["balance"] - STARTING_BALANCE) / STARTING_BALANCE * 100
    msg = (
        f"{header}\n\n"
        f"💎 Pair: #{sym}\n"
        f"📅 وقت الفتح: {pos['signal_time']}\n"
        f"🕒 وقت الإغلاق: {close_time_str} (GMT+3)\n\n"
        f"Entry: {fmt_price(pos['entry_price'])}\n"
        f"Exit: {fmt_price(exit_price)}\n"
        f"PnL: {pnl_pct:+.2f}%\n\n"
        f"💰 رأس المال (افتراضي): ${state['balance']:,.2f}\n"
        f"📈 التغيّر التراكمي: {total_return_pct:+.2f}%"
    )
    push(msg)
    log_signal(state, "خروج", sym, f"{exit_reason} {pnl_pct:+.2f}%")
    append_trade_log({
        "pair": sym,
        "signal_time": pos["signal_time"],
        "entry_price": pos["entry_price"],
        "exit_time": str(exit_time),
        "exit_price": exit_price,
        "pnl_pct_net": round(pnl_pct, 4),
        "pnl_dollars": round(pnl_dollars, 2),
        "position_dollars": round(position_dollars, 2),
        "exit_reason": exit_reason,
        "score": pos.get("score", ""),
        "balance_after": round(state["balance"], 2),
    })


if __name__ == "__main__":
    main()
