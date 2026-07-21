"""
بوت توصيات تداول - استراتيجية BOS + Order Block
يشتغل مرة كل تشغيل (مصمم ليُستدعى دوريًا كل 30 دقيقة)

الفرق عن نسخة Mean Reversion: الدخول هون Limit عند قمة Order Block، مش فوري.
لهيك في حالتين لكل رمز:
  - pending_setups : إشارة اتكشفت وبتستنى السعر يلمس entry1 (أمر معلّق)
  - open_positions : الأمر اتنفذ فعلاً وبيستنى SL/TP/Timeout

الوظيفة:
  1. يفحص أوامر تلجرام الجديدة (/balance /positions /pending /stats /signals /help)
  2. يجلب آخر بيانات الشموع (30m) من Binance لقائمة الرموز في symbols.txt
  3. يفحص الـsetups المعلّقة: تنفيذ أو إلغاء بسبب Timeout
  4. يتابع الصفقات المفتوحة: يغلقها عند SL/TP/Timeout
  5. يفحص إشارات BOS+OB جديدة على الرموز الخالية من setup حاليًا
  6. يرسل كل حدث كرسالة تلجرام، ويحدّث الرصيد الافتراضي والإحصائيات
  7. يسجل كل صفقة مغلقة في trades_log_bos.csv

⚠️ هذا أداة توصيات وتتبع فقط — لا ينفذ أي صفقة حقيقية بنفسه، ولا يشكل نصيحة استثمارية.
"""
import os
import json
import csv
from datetime import datetime, timezone, timedelta

import pandas as pd

import strategy as strategy
from utils import fetch_klines, send_telegram_message, get_telegram_updates, sleep_safe

STATE_FILE = "state_bos.json"
TRADES_LOG_FILE = "trades_log_bos.csv"
SYMBOLS_FILE = "symbols.txt"
INTERVAL = "30m"
INTERVAL_MINUTES = 30
STARTING_BALANCE = 10000.0
MAX_SIGNAL_HISTORY = 20

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def load_symbols():
    with open(SYMBOLS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def default_state():
    return {
        "pending_setups": {},   # رمز -> {signal_time, entry1, sl, tp, score}
        "open_positions": {},   # رمز -> {signal_time, entry_time, entry_price, sl, tp, score}
        "last_candle_seen": {},
        "balance": STARTING_BALANCE,
        "stats": {"total_trades": 0, "wins": 0, "losses": 0, "gross_profit": 0.0, "gross_loss": 0.0},
        "last_update_id": 0,
        "signal_history": [],
        "symbol_cooldown_until": {},   # رمز -> ISO timestamp (ممنوع دخول جديد قبله)
    }


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        for k, v in default_state().items():
            if k not in state:
                state[k] = v
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
    """رابط شارت الرمز على TradingView (نفترض الرمز بصيغة Binance، مثلاً BTCUSDT)."""
    return f"https://www.tradingview.com/symbols/{symbol}/"


def push(msg):
    print(msg.replace("\n", " | "))
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
    else:
        print("  [تنبيه] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID غير مضبوطة - لن يُرسل شيء فعليًا.")


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


def update_monthly_guard(state, now):
    """يحدّث حالة (وقف الشهر) بناءً على الرصيد الحالي مقارنة برصيد بداية الشهر."""
    month_key = now.strftime("%Y-%m")
    if state.get("month_key") != month_key:
        state["month_key"] = month_key
        state["month_start_balance"] = state.get("balance", STARTING_BALANCE)
        state["month_stopped"] = False

    start = state.get("month_start_balance", STARTING_BALANCE)
    balance = state.get("balance", STARTING_BALANCE)
    monthly_return_pct = (balance / start - 1) * 100 if start else 0

    if not state.get("month_stopped") and monthly_return_pct <= strategy.MONTHLY_STOP_PCT:
        state["month_stopped"] = True
        push(
            f"🛑 <b>وقف شهري مفعّل</b>\n"
            f"الخسارة الشهرية وصلت {monthly_return_pct:.2f}% (الحد {strategy.MONTHLY_STOP_PCT}%)\n"
            f"لن تُفتح صفقات جديدة لحد نهاية الشهر — الصفقات المفتوحة حاليًا هتكمل عادي."
        )
    return state.get("month_stopped", False)


def is_symbol_in_cooldown(state, sym, now):
    until_str = state.get("symbol_cooldown_until", {}).get(sym)
    if not until_str:
        return False
    return now < pd.Timestamp(until_str)


def set_symbol_cooldown(state, sym, exit_time):
    until = pd.Timestamp(exit_time) + timedelta(hours=strategy.SYMBOL_COOLDOWN_HOURS)
    state.setdefault("symbol_cooldown_until", {})[sym] = str(until)


# ============================================================
# معالجة أوامر تلجرام التفاعلية
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


def handle_balance(state, chat_id):
    balance = state.get("balance", STARTING_BALANCE)
    total_return_pct = (balance - STARTING_BALANCE) / STARTING_BALANCE * 100
    month_start = state.get("month_start_balance", STARTING_BALANCE)
    month_return_pct = (balance / month_start - 1) * 100 if month_start else 0
    status_line = "🛑 موقوف (تعدى حد الخسارة الشهري)" if state.get("month_stopped") else "✅ شغال عادي"
    msg = (
        f"💰 <b>الرصيد الافتراضي الحالي</b>\n"
        f"الرصيد: ${balance:,.2f}\n"
        f"رأس المال الابتدائي: ${STARTING_BALANCE:,.2f}\n"
        f"العائد التراكمي: {total_return_pct:+.2f}%\n"
        f"عائد الشهر الحالي: {month_return_pct:+.2f}%\n"
        f"حالة الدخول الجديد: {status_line}"
    )
    reply(chat_id, msg)


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
        reply(chat_id, "📊 ما فيه صفقات مغلقة لسا لحساب الإحصائيات.")
        return
    wins = s.get("wins", 0)
    losses = s.get("losses", 0)
    win_rate = wins / total * 100 if total else 0
    gross_profit = s.get("gross_profit", 0.0)
    gross_loss = s.get("gross_loss", 0.0)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    balance = state.get("balance", STARTING_BALANCE)
    total_return_pct = (balance - STARTING_BALANCE) / STARTING_BALANCE * 100

    pf_str = f"{profit_factor:.2f}" if profit_factor != float("inf") else "∞"
    msg = (
        f"📊 <b>إحصائيات الأداء الحي — BOS + Order Block</b>\n"
        f"إجمالي الصفقات: {total}\n"
        f"رابحة: {wins} | خاسرة: {losses}\n"
        f"Win Rate: {win_rate:.2f}%\n"
        f"Profit Factor: {pf_str}\n"
        f"العائد التراكمي: {total_return_pct:+.2f}%\n"
        f"الرصيد الحالي: ${balance:,.2f}"
    )
    reply(chat_id, msg)


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
    msg = (
        "🤖 <b>أوامر البوت المتاحة — BOS + Order Block</b>\n\n"
        "/balance — الرصيد الافتراضي الحالي\n"
        "/positions — الصفقات المفتوحة (اتنفذت فعلاً)\n"
        "/pending — الأوامر المعلّقة (بانتظار لمس سعر الدخول)\n"
        "/stats — إحصائيات الأداء التراكمية\n"
        "/signals — آخر 10 إشارات دخول/خروج\n"
        "/help — عرض هذه القائمة\n\n"
        "⚠️ ملاحظة: البوت يفحص أوامرك بس وقت تشغيله (كل 30 دقيقة تقريبًا)، فالرد ممكن ياخذ لين نص ساعة."
    )
    reply(chat_id, msg)


# ============================================================
# مساعد: تحديد الشموع "الفايتة" (الجديدة) لكل رمز منذ آخر تشغيل
# ============================================================
# سقف أمان لعدد الشموع يلي منعوّض عليها بتشغيلة وحدة، حتى لو البوت كان طافي
# لفترة طويلة جدًا (60 شمعة * 30 دقيقة = 30 ساعة تعويض كحد أقصى بكل تشغيلة)
CATCHUP_MAX_BARS = 60


def get_new_bar_positions(df, last_seen_str):
    """يرجع لستة positions (مواقع) بـ df للشموع الأحدث من last_seen_str، مرتبة زمنيًا تصاعديًا.
    - أول مرة نشوف فيها الرمز (ما فيه last_seen): نرجع بس آخر شمعة (نفس السلوك القديم،
      حتى ما نفحص 500 شمعة تاريخية دفعة وحدة كإنها "جديدة").
    - لو البوت كان طافي وفاتته أكثر من شمعة: نرجع كل الشموع الفايتة بالترتيب، مع سقف أمان
      CATCHUP_MAX_BARS لتفادي معالجة فجوة ضخمة جدًا بتشغيلة وحدة."""
    n = len(df)
    if not last_seen_str:
        return [n - 1]
    last_seen_ts = pd.Timestamp(last_seen_str)
    times = df["open_time_utc"]
    positions = [k for k in range(n) if pd.Timestamp(times.iloc[k]) > last_seen_ts]
    if len(positions) > CATCHUP_MAX_BARS:
        positions = positions[-CATCHUP_MAX_BARS:]
    return positions


# ============================================================
# المنطق الرئيسي
# ============================================================
def main():
    print(f"=== تشغيل بوت BOS+OB — {datetime.now(timezone.utc).isoformat()} ===")
    symbols = load_symbols()
    state = load_state()
    print(f"عدد الرموز المراقبة: {len(symbols)}")

    # ---------- 0) معالجة أي أوامر تفاعلية جديدة ----------
    handle_commands(state)

    # ---------- 1) جلب البيانات وحساب المؤشرات لكل رمز ----------
    data = {}
    for sym in symbols:
        df = fetch_klines(sym, interval=INTERVAL, limit=500)
        sleep_safe(0.2)
        if df is None or len(df) < 250:
            continue
        df = strategy.compute_all_indicators(df)
        data[sym] = df

    if not data:
        print("لم يتم جلب أي بيانات صالحة. حفظ الحالة والإيقاف.")
        save_state(state)
        return

    pending_setups = state["pending_setups"]
    open_positions = state["open_positions"]

    now = datetime.now(timezone.utc)
    month_stopped = update_monthly_guard(state, now)

    # ---------- 2) تحديد الشموع الفايتة لكل رمز، ومعرفة هل البوت كان متوقف فترة ----------
    # كل رمز يخزن state["last_candle_seen"][sym] = آخر شمعة اتفحصت فعليًا. لو البوت
    # طفى (مثلاً جيتهاب اكشنز ما اشتغل، أو تأخر) وفاتته أكثر من شمعة، منفحصهم كلهم
    # بالترتيب الزمني (مش بس آخر وحدة) - هيك ما تفوت ولا إشارة دخول ولا SL ولا TP.
    new_bar_positions = {sym: get_new_bar_positions(df, state["last_candle_seen"].get(sym)) for sym, df in data.items()}
    max_gap = max((len(v) for v in new_bar_positions.values()), default=0)

    if max_gap > 1:
        push(
            f"⏸️ <b>تعويض تشغيل فايت</b>\n"
            f"يبدو إن البوت كان متوقف أو ما اشتغل بوقته (لحد {max_gap} شمعة فايتة على بعض الرموز).\n"
            f"جاري فحص كل شمعة فايتة بالترتيب الزمني (دخول Limit / SL / TP / إشارات جديدة) بدل ما نتجاهلها."
        )

    # ---------- 3) المرور على كل رمز، شمعة-شمعة، بالترتيب الزمني ----------
    for sym, df in data.items():
        positions = new_bar_positions.get(sym, [])
        if not positions:
            continue  # نفس الشمعة يلي فحصناها آخر مرة - ما فيه شي جديد، ما منكرر الفحص

        for pos_idx in positions:
            bar = df.iloc[pos_idx]
            bar_time = bar["open_time_utc"]

            # --- أ) عنده صفقة مفتوحة فعلاً: فحص SL/TP/Timeout على هالشمعة بالذات ---
            if sym in open_positions:
                p = open_positions[sym]
                signal_time = pd.Timestamp(p["signal_time"])
                bars_since_signal = int((bar_time - signal_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))

                exit_price, exit_reason = None, None
                if bar["low"] <= p["sl"]:
                    exit_price, exit_reason = p["sl"], "SL 🔴"
                elif bar["high"] >= p["tp"]:
                    exit_price, exit_reason = p["tp"], "TP 🟢"
                elif bars_since_signal > strategy.MAX_BARS_ACTIVE:
                    exit_price, exit_reason = bar["close"], "Timeout ⏱️"

                if exit_price is not None:
                    _close_position(state, sym, p, exit_price, exit_reason, bar_time)
                    if exit_reason.startswith("SL"):
                        set_symbol_cooldown(state, sym, bar_time)
                    del open_positions[sym]
                continue  # ما منفحص Pending ولا إشارة جديدة بنفس الشمعة يلي فيها صفقة مفتوحة

            # --- ب) عنده أمر معلّق (Limit): فحص تنفيذ أو انتهاء صلاحية على هالشمعة ---
            if sym in pending_setups:
                p = pending_setups[sym]
                signal_time = pd.Timestamp(p["signal_time"])
                bars_since_signal = int((bar_time - signal_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))

                if bar["low"] <= p["entry1"]:
                    del pending_setups[sym]

                    if month_stopped:
                        log_signal(state, "إلغاء", sym, "اترفض التنفيذ - الوقف الشهري مفعّل")
                        continue

                    if len(open_positions) >= strategy.MAX_CONCURRENT_TRADES:
                        log_signal(state, "إلغاء", sym, "اترفض التنفيذ - المحفظة ممتلئة (3 صفقات)")
                        continue

                    entry_price = p["entry1"]
                    position_dollars = min(state["balance"] * strategy.POSITION_SIZE_PCT, strategy.MAX_POSITION_SIZE_USD)

                    # نفس منطق الباكتست: نتحقق فورًا هل نفس الشمعة يلي نفّذت فيها لمست SL أو TP كمان
                    exit_price, exit_reason = None, None
                    if bar["low"] <= p["sl"]:
                        exit_price, exit_reason = p["sl"], "SL 🔴"
                    elif bar["high"] >= p["tp"]:
                        exit_price, exit_reason = p["tp"], "TP 🟢"

                    if exit_price is not None:
                        _close_position(state, sym, {
                            "signal_time": p["signal_time"], "entry_time": str(bar_time),
                            "entry_price": entry_price, "sl": p["sl"], "tp": p["tp"], "score": p["score"],
                            "position_dollars": position_dollars,
                        }, exit_price, exit_reason, bar_time)
                        if exit_reason.startswith("SL"):
                            set_symbol_cooldown(state, sym, bar_time)
                    else:
                        open_positions[sym] = {
                            "signal_time": p["signal_time"],
                            "entry_time": str(bar_time),
                            "entry_price": entry_price,
                            "sl": p["sl"], "tp": p["tp"], "score": p["score"],
                            "position_dollars": position_dollars,
                        }
                        time_str = pd.Timestamp(bar_time).strftime("%d %b %Y • %H:%M")
                        push(
                            f"📥 <b>تم تنفيذ الأمر</b>\n"
                            f"🪙 <b>{sym}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"💰 سعر التنفيذ: <b>{fmt_price(entry_price)}</b>\n"
                            f"🛑 وقف الخسارة: <b>{fmt_price(p['sl'])}</b>\n"
                            f"🎯 جني الأرباح: <b>{fmt_price(p['tp'])}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📈 <a href=\"{tv_link(sym)}\">فتح الشارت على TradingView</a>\n"
                            f"🕒 {time_str} UTC"
                        )
                        log_signal(state, "تنفيذ", sym, f"دخول عند {fmt_price(entry_price)}")
                elif bars_since_signal > strategy.MAX_BARS_ACTIVE:
                    del pending_setups[sym]
                    log_signal(state, "إلغاء", sym, "انتهى وقت الأمر المعلّق بدون تنفيذ")
                continue

            # --- ج) ما عنده لا صفقة ولا أمر معلّق: فحص إشارة BOS+OB جديدة على هالشمعة ---
            if month_stopped:
                continue
            if is_symbol_in_cooldown(state, sym, bar_time):
                continue

            g = df.iloc[: pos_idx + 1]  # نفس منطق check_new_signal الأصلي، بس على شمعة تاريخية بدل آخر وحدة فقط
            sig = strategy.check_new_signal(g)
            if sig:
                pending_setups[sym] = {
                    "signal_time": str(sig["signal_time"]),
                    "entry1": sig["entry1"], "sl": sig["sl"], "tp": sig["tp"],
                    "score": sig["score"],
                }
                time_str = pd.Timestamp(sig["signal_time"]).strftime("%d %b %Y • %H:%M")
                push(
                    f"🚨 <b>إشارة دخول جديدة</b>\n"
                    f"🪙 <b>{sym}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💰 سعر الدخول (Limit): <b>{fmt_price(sig['entry1'])}</b>\n"
                    f"🛑 وقف الخسارة: <b>{fmt_price(sig['sl'])}</b>\n"
                    f"🎯 جني الأرباح: <b>{fmt_price(sig['tp'])}</b>\n"
                    f"📦 حجم الصفقة: <b>{strategy.POSITION_SIZE_PCT*100:.0f}% من رأس المال</b>\n"
                    f"📊 قوة الإشارة: <b>{sig['score']*100:.0f}%</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📈 <a href=\"{tv_link(sym)}\">فتح الشارت على TradingView</a>\n"
                    f"🕒 {time_str} UTC\n"
                    f"⏳ بانتظار لمس سعر الدخول (حد أقصى {strategy.MAX_BARS_ACTIVE} شمعة)\n"
                    f"⚠️ توصية آلية من نظام باكتست، وليست نصيحة مالية."
                )
                log_signal(state, "إشارة", sym, f"Setup معلّق عند {fmt_price(sig['entry1'])}")

        # خلصنا كل الشموع الفايتة لهالرمز - نحدّث آخر شمعة اتفحصت حتى ما نعيد فحصها مرة ثانية
        state["last_candle_seen"][sym] = str(df.iloc[positions[-1]]["open_time_utc"])

    save_state(state)
    print(f"مفتوحة: {len(open_positions)} | معلّقة: {len(pending_setups)} | الرصيد: ${state['balance']:,.2f}")
    print("=== انتهى التشغيل ===")


def _close_position(state, sym, pos, exit_price, exit_reason, exit_time):
    """يغلق صفقة (سواء فُتحت هالدورة أو دورة سابقة) ويحدّث الرصيد والإحصائيات والسجل."""
    round_trip_cost = strategy.ROUND_TRIP_COST_PCT
    pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100 - round_trip_cost

    # حجم الصفقة المحفوظ وقت الفتح (مش الرصيد الحالي وقت الإغلاق - كان هذا باگ)
    position_dollars = pos.get("position_dollars")
    if position_dollars is None:
        # احتياط لصفقات قديمة محفوظة في state_bos.json قبل هذا التصحيح
        position_dollars = min(state["balance"] * strategy.POSITION_SIZE_PCT, strategy.MAX_POSITION_SIZE_USD)
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

    # مدة الصفقة بالشموع (من لحظة التنفيذ الفعلي لحظة الخروج)
    entry_time_raw = pos.get("entry_time", pos.get("signal_time"))
    bars_held = int((pd.Timestamp(exit_time) - pd.Timestamp(entry_time_raw)) / pd.Timedelta(minutes=INTERVAL_MINUTES))
    bars_held = max(bars_held, 0)

    is_tp = exit_reason.startswith("TP")
    is_sl = exit_reason.startswith("SL")
    is_win = pnl_pct > 0

    sep = "━━━━━━━━━━━━━━━━━━"
    tv = tv_link(sym)
    body = (
        f"🪙 <b>{sym}</b>\n{sep}\n"
        f"📥 الدخول : {fmt_price(pos['entry_price'])}\n"
        f"📤 الخروج : {fmt_price(exit_price)}\n"
    )

    if is_tp:
        header = "🟢🟢🟢🟢🟢🟢🟢\n🏆 <b>تم تحقيق الهدف</b>\n"
        reason_line = "🎯 السبب: جني الأرباح\n"
        result_lines = f"📈 العائد : <b>{pnl_pct:+.2f}%</b>\n💵 الربح : <b>{pnl_dollars:+,.2f}$</b>\n"
        footer = f"💚 تمت الصفقة بنجاح."
    elif is_sl:
        header = "🔴🔴🔴🔴🔴\n🛑 <b>تم تفعيل وقف الخسارة</b>\n"
        reason_line = ""
        result_lines = f"📉 العائد : <b>{pnl_pct:+.2f}%</b>\n💸 الخسارة : <b>{pnl_dollars:+,.2f}$</b>\n"
        footer = ""
    elif is_win:  # Timeout بربح
        header = "🟢🟢🟢✨🟢🟢\n💚 <b>إغلاق رابح</b>\n"
        reason_line = "⏱️ السبب: انتهاء مدة الصفقة\n"
        result_lines = f"📈 العائد : <b>{pnl_pct:+.2f}%</b>\n💵 الربح : <b>{pnl_dollars:+,.2f}$</b>\n"
        footer = "✨ تم الاحتفاظ بالأرباح حتى نهاية مدة الصفقة."
    else:  # Timeout بخسارة
        header = "🟠\n⏱️ <b>انتهاء مدة الصفقة</b>\n"
        reason_line = ""
        result_lines = f"📉 العائد : <b>{pnl_pct:+.2f}%</b>\n💸 الخسارة : <b>{pnl_dollars:+,.2f}$</b>\n"
        footer = ""

    msg = (
        f"{header}{body}{reason_line}{result_lines}"
        f"⏳ مدة الصفقة : {bars_held} شمعة\n{sep}\n"
        f"📈 <a href=\"{tv}\">فتح الشارت على TradingView</a>\n"
        f"💼 الرصيد الحالي <b>${state['balance']:,.2f}</b>"
        + (f"\n{footer}" if footer else "")
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
