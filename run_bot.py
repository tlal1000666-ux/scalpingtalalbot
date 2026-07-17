"""
بوت توصيات تداول - يشتغل مرة كل تشغيل (مصمم ليُستدعى دوريًا عبر GitHub Actions كل 30 دقيقة)

الوظيفة:
  1. يجلب آخر بيانات الشموع (30m) من Binance لقائمة الرموز في symbols.txt
  2. يحسب المؤشرات ويفحص شروط الدخول (نفس منطق الباكتست بالضبط)
  3. يتابع الصفقات "الافتراضية" المفتوحة ويغلقها عند SL/TP/وقف زمني
  4. يرسل كل إشارة دخول/خروج كرسالة تلجرام
  5. يسجل كل صفقة مغلقة في trades_log.csv (لمقارنة الأداء الحي بتوقعات الباكتست لاحقًا)

⚠️ هذا أداة توصيات وتتبع فقط — لا ينفذ أي صفقة حقيقية بنفسه، ولا يشكل نصيحة استثمارية.
"""
import os
import json
import csv
from datetime import datetime, timezone, timedelta

import pandas as pd

import strategy
from utils import fetch_klines, send_telegram_message, sleep_safe

STATE_FILE = "state.json"
TRADES_LOG_FILE = "trades_log.csv"
SYMBOLS_FILE = "symbols.txt"
INTERVAL = "30m"
INTERVAL_MINUTES = 30

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def load_symbols():
    with open(SYMBOLS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"open_positions": {}, "cooldown_until": {}, "last_candle_seen": {}}


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


def send(msg):
    print(msg.replace("\n", " | "))
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        return send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
    else:
        print("  [تنبيه] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID غير مضبوطة - لن يُرسل شيء فعليًا.")
        return False


def main():
    print(f"=== تشغيل البوت — {datetime.now(timezone.utc).isoformat()} ===")
    symbols = load_symbols()
    state = load_state()
    print(f"عدد الرموز المراقبة: {len(symbols)}")

    # ---------- 0) إعادة محاولة إرسال أي إشعار دخول فشل بتشغيلة سابقة ----------
    for sym, pos in state["open_positions"].items():
        if not pos.get("notified", True):  # الحقل غير موجود بصفقات قديمة = نعتبرها مُرسلة فعلاً
            msg = (
                f"🚨 <b>توصية دخول جديدة: {sym}</b>\n"
                f"سعر الدخول التقريبي: {fmt_price(pos['entry_price'])}\n"
                f"وقف الخسارة (SL): {fmt_price(pos['sl'])}\n"
                f"جني الأرباح (TP): {fmt_price(pos['tp'])}\n"
                f"حجم الصفقة المقترح: {strategy.POSITION_SIZE_PCT*100:.0f}% من رأس المال\n"
                f"قوة الإشارة: {pos.get('score', 0):.2f}\n"
                f"(إشعار مُعاد الإرسال بعد فشل سابق)\n"
                f"⚠️ هذه توصية آلية من نظام باكتست، وليست نصيحة مالية. تحقق دائمًا بنفسك قبل التنفيذ."
            )
            if send(msg):
                pos["notified"] = True

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
        print("لم يتم جلب أي بيانات صالحة. إيقاف التشغيل.")
        return

    # ---------- 2) حساب نظام السوق العام (متوسط عائد آخر 7 أيام لكل الرموز) ----------
    closes = {sym: df.set_index("open_time_utc")["close"] for sym, df in data.items()}
    pivot = pd.DataFrame(closes)
    returns_7d = pivot.pct_change(strategy.MARKET_REGIME_LOOKBACK_BARS)
    market_regime_series = returns_7d.mean(axis=1) * 100
    latest_market_regime = float(market_regime_series.iloc[-1]) if len(market_regime_series) else None
    print(f"نظام السوق العام (عائد 7 أيام): {latest_market_regime:.2f}%" if latest_market_regime is not None else "نظام السوق: غير متاح")

    now_iso = datetime.now(timezone.utc).isoformat()

    # ---------- 3) فحص الصفقات المفتوحة حاليًا (خروج SL / TP / وقف زمني) ----------
    open_positions = state["open_positions"]
    for sym in list(open_positions.keys()):
        if sym not in data:
            continue
        pos = open_positions[sym]
        last = data[sym].iloc[-1]
        entry_time = pd.Timestamp(pos["entry_time"])
        bars_elapsed = int((last["open_time_utc"] - entry_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))

        exit_price, exit_reason = None, None
        if last["low"] <= pos["sl"]:
            exit_price, exit_reason = pos["sl"], "SL 🔴"
        elif last["high"] >= pos["tp"]:
            exit_price, exit_reason = pos["tp"], "TP 🟢"
        elif bars_elapsed >= strategy.TIME_STOP_BARS:
            exit_price, exit_reason = last["close"], "وقف زمني ⏱️"

        if exit_price is not None:
            round_trip_cost = 0.30  # % (نفس افتراض الباكتست)
            pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100 - round_trip_cost
            emoji = "✅" if pnl_pct > 0 else "❌"
            msg = (
                f"{emoji} <b>إغلاق صفقة: {sym}</b>\n"
                f"السبب: {exit_reason}\n"
                f"سعر الدخول: {fmt_price(pos['entry_price'])}\n"
                f"سعر الخروج: {fmt_price(exit_price)}\n"
                f"النتيجة الصافية: {pnl_pct:+.2f}%\n"
                f"مدة الصفقة: {bars_elapsed} شمعة"
            )
            send(msg)
            append_trade_log({
                "pair": sym,
                "entry_time": pos["entry_time"],
                "exit_time": str(last["open_time_utc"]),
                "entry_price": pos["entry_price"],
                "exit_price": exit_price,
                "pnl_pct_net": round(pnl_pct, 4),
                "exit_reason": exit_reason,
                "score": pos.get("score", ""),
            })
            del open_positions[sym]
            cooldown_until = last["open_time_utc"] + timedelta(minutes=INTERVAL_MINUTES * strategy.COOLDOWN_BARS_AFTER_TRADE)
            state["cooldown_until"][sym] = str(cooldown_until)

    # ---------- 4) فحص إشارات دخول جديدة ----------
    candidates = []
    for sym, df in data.items():
        if sym in open_positions:
            continue
        cooldown_until = state["cooldown_until"].get(sym)
        last = df.iloc[-1]
        if cooldown_until and pd.Timestamp(last["open_time_utc"]) < pd.Timestamp(cooldown_until):
            continue
        # تفادي تكرار نفس الإشارة أكثر من مرة لنفس الشمعة
        last_seen = state["last_candle_seen"].get(sym)
        candle_key = str(last["open_time_utc"])
        if last_seen == candle_key:
            continue

        ok, score = strategy.check_entry_signal(last, latest_market_regime)
        if ok:
            candidates.append((sym, score, last))

    # فلتر الازدحام: لو عدد الإشارات بنفس اللحظة أكبر من الحد، تُرفض كلها
    if len(candidates) > strategy.CROWD_FILTER_MAX_SIGNALS:
        print(f"فلتر الازدحام: رُفضت {len(candidates)} إشارة (تجاوزت الحد {strategy.CROWD_FILTER_MAX_SIGNALS})")
        candidates = []

    # ترتيب حسب قوة الإشارة (score) وأخذ الأقوى أولاً ضمن السعة المتاحة
    candidates.sort(key=lambda x: x[1], reverse=True)
    available_slots = strategy.MAX_CONCURRENT_TRADES - len(open_positions)

    for sym, score, last in candidates:
        if available_slots <= 0:
            break
        entry_price = float(last["close"])  # أفضل تقدير متاح لحظة الإشارة (شمعة مغلقة للتو)
        sl_price, tp_price = strategy.compute_sl_tp(entry_price, float(last["atr"]))
        open_positions[sym] = {
            "entry_time": str(last["open_time_utc"]),
            "entry_price": entry_price,
            "sl": sl_price,
            "tp": tp_price,
            "score": round(float(score), 4),
            "notified": False,
        }
        available_slots -= 1
        msg = (
            f"🚨 <b>توصية دخول جديدة: {sym}</b>\n"
            f"سعر الدخول التقريبي: {fmt_price(entry_price)}\n"
            f"وقف الخسارة (SL): {fmt_price(sl_price)}\n"
            f"جني الأرباح (TP): {fmt_price(tp_price)}\n"
            f"حجم الصفقة المقترح: {strategy.POSITION_SIZE_PCT*100:.0f}% من رأس المال\n"
            f"قوة الإشارة: {score:.2f}\n"
            f"⚠️ هذه توصية آلية من نظام باكتست، وليست نصيحة مالية. تحقق دائمًا بنفسك قبل التنفيذ."
        )
        if send(msg):
            open_positions[sym]["notified"] = True

    # تحديث آخر شمعة تمت معالجتها لكل رمز (لمنع التكرار)
    for sym, df in data.items():
        state["last_candle_seen"][sym] = str(df.iloc[-1]["open_time_utc"])

    save_state(state)
    print(f"الصفقات المفتوحة حاليًا بعد هذا التشغيل: {len(open_positions)}")
    print("=== انتهى التشغيل ===")


if __name__ == "__main__":
    main()            continue
        pos = open_positions[sym]
        last = data[sym].iloc[-1]
        entry_time = pd.Timestamp(pos["entry_time"])
        bars_elapsed = int((last["open_time_utc"] - entry_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))

        exit_price, exit_reason = None, None
        if last["low"] <= pos["sl"]:
            exit_price, exit_reason = pos["sl"], "SL 🔴"
        elif last["high"] >= pos["tp"]:
            exit_price, exit_reason = pos["tp"], "TP 🟢"
        elif bars_elapsed >= strategy.TIME_STOP_BARS:
            exit_price, exit_reason = last["close"], "وقف زمني ⏱️"

        if exit_price is not None:
            round_trip_cost = 0.30  # % (نفس افتراض الباكتست)
            pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100 - round_trip_cost
            emoji = "✅" if pnl_pct > 0 else "❌"
            msg = (
                f"{emoji} <b>إغلاق صفقة: {sym}</b>\n"
                f"السبب: {exit_reason}\n"
                f"سعر الدخول: {fmt_price(pos['entry_price'])}\n"
                f"سعر الخروج: {fmt_price(exit_price)}\n"
                f"النتيجة الصافية: {pnl_pct:+.2f}%\n"
                f"مدة الصفقة: {bars_elapsed} شمعة"
            )
            send(msg)
            append_trade_log({
                "pair": sym,
                "entry_time": pos["entry_time"],
                "exit_time": str(last["open_time_utc"]),
                "entry_price": pos["entry_price"],
                "exit_price": exit_price,
                "pnl_pct_net": round(pnl_pct, 4),
                "exit_reason": exit_reason,
                "score": pos.get("score", ""),
            })
            del open_positions[sym]
            cooldown_until = last["open_time_utc"] + timedelta(minutes=INTERVAL_MINUTES * strategy.COOLDOWN_BARS_AFTER_TRADE)
            state["cooldown_until"][sym] = str(cooldown_until)

    # ---------- 4) فحص إشارات دخول جديدة ----------
    candidates = []
    for sym, df in data.items():
        if sym in open_positions:
            continue
        cooldown_until = state["cooldown_until"].get(sym)
        last = df.iloc[-1]
        if cooldown_until and pd.Timestamp(last["open_time_utc"]) < pd.Timestamp(cooldown_until):
            continue
        # تفادي تكرار نفس الإشارة أكثر من مرة لنفس الشمعة
        last_seen = state["last_candle_seen"].get(sym)
        candle_key = str(last["open_time_utc"])
        if last_seen == candle_key:
            continue

        ok, score = strategy.check_entry_signal(last, latest_market_regime)
        if ok:
            candidates.append((sym, score, last))

    # فلتر الازدحام: لو عدد الإشارات بنفس اللحظة أكبر من الحد، تُرفض كلها
    if len(candidates) > strategy.CROWD_FILTER_MAX_SIGNALS:
        print(f"فلتر الازدحام: رُفضت {len(candidates)} إشارة (تجاوزت الحد {strategy.CROWD_FILTER_MAX_SIGNALS})")
        candidates = []

    # ترتيب حسب قوة الإشارة (score) وأخذ الأقوى أولاً ضمن السعة المتاحة
    candidates.sort(key=lambda x: x[1], reverse=True)
    available_slots = strategy.MAX_CONCURRENT_TRADES - len(open_positions)

    for sym, score, last in candidates:
        if available_slots <= 0:
            break
        entry_price = float(last["close"])  # أفضل تقدير متاح لحظة الإشارة (شمعة مغلقة للتو)
        sl_price, tp_price = strategy.compute_sl_tp(entry_price, float(last["atr"]))
        open_positions[sym] = {
            "entry_time": str(last["open_time_utc"]),
            "entry_price": entry_price,
            "sl": sl_price,
            "tp": tp_price,
            "score": round(float(score), 4),
            "notified": False,
        }
        available_slots -= 1
        msg = (
            f"🚨 <b>توصية دخول جديدة: {sym}</b>\n"
            f"سعر الدخول التقريبي: {fmt_price(entry_price)}\n"
            f"وقف الخسارة (SL): {fmt_price(sl_price)}\n"
            f"جني الأرباح (TP): {fmt_price(tp_price)}\n"
            f"حجم الصفقة المقترح: {strategy.POSITION_SIZE_PCT*100:.0f}% من رأس المال\n"
            f"قوة الإشارة: {score:.2f}\n"
            f"⚠️ هذه توصية آلية من نظام باكتست، وليست نصيحة مالية. تحقق دائمًا بنفسك قبل التنفيذ."
        )
        if send(msg):
            open_positions[sym]["notified"] = True

    # تحديث آخر شمعة تمت معالجتها لكل رمز (لمنع التكرار)
    for sym, df in data.items():
        state["last_candle_seen"][sym] = str(df.iloc[-1]["open_time_utc"])

    save_state(state)
    print(f"الصفقات المفتوحة حاليًا بعد هذا التشغيل: {len(open_positions)}")
    print("=== انتهى التشغيل ===")


if __name__ == "__main__":
    main()
      
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
        elif text in ("/stats", "/احصائيات", "/إحصائيات"):
            handle_stats(state, chat_id)
        elif text in ("/signals", "/last", "/آخر"):
            handle_signals(state, chat_id)
        elif text in ("/start", "/help", "/مساعدة"):
            handle_help(chat_id)


def handle_balance(state, chat_id):
    balance = state.get("balance", STARTING_BALANCE)
    total_return_pct = (balance - STARTING_BALANCE) / STARTING_BALANCE * 100
    msg = (
        f"💰 <b>الرصيد الافتراضي الحالي</b>\n"
        f"الرصيد: ${balance:,.2f}\n"
        f"رأس المال الابتدائي: ${STARTING_BALANCE:,.2f}\n"
        f"العائد التراكمي: {total_return_pct:+.2f}%"
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
            f"  وقت الدخول: {pos['entry_time']}"
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
        f"📊 <b>إحصائيات الأداء الحي</b>\n"
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
        "🤖 <b>أوامر البوت المتاحة</b>\n\n"
        "/balance — الرصيد الافتراضي الحالي\n"
        "/positions — الصفقات المفتوحة الآن\n"
        "/stats — إحصائيات الأداء التراكمية\n"
        "/signals — آخر 10 إشارات دخول/خروج\n"
        "/help — عرض هذه القائمة\n\n"
        "⚠️ ملاحظة: البوت يفحص أوامرك بس وقت تشغيله (كل 30 دقيقة تقريبًا)، فالرد ممكن ياخذ لين نص ساعة."
    )
    reply(chat_id, msg)


# ============================================================
# المنطق الرئيسي: فحص السوق + إدارة الصفقات
# ============================================================
def main():
    print(f"=== تشغيل البوت — {datetime.now(timezone.utc).isoformat()} ===")
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
        print("لم يتم جلب أي بيانات صالحة. حفظ الحالة (بعد أي أوامر تمت معالجتها) والإيقاف.")
        save_state(state)
        return

    # ---------- 2) حساب نظام السوق العام ----------
    closes = {sym: df.set_index("open_time_utc")["close"] for sym, df in data.items()}
    pivot = pd.DataFrame(closes)
    returns_n = pivot.pct_change(strategy.MARKET_REGIME_LOOKBACK_BARS)
    market_regime_series = returns_n.mean(axis=1) * 100
    latest_market_regime = float(market_regime_series.iloc[-1]) if len(market_regime_series) else None
    print(f"نظام السوق العام: {latest_market_regime:.2f}%" if latest_market_regime is not None else "نظام السوق: غير متاح")

    # ---------- 3) فحص الصفقات المفتوحة حاليًا (خروج SL / TP / وقف زمني) ----------
    open_positions = state["open_positions"]
    for sym in list(open_positions.keys()):
        if sym not in data:
            continue
        pos = open_positions[sym]
        last = data[sym].iloc[-1]
        entry_time = pd.Timestamp(pos["entry_time"])
        bars_elapsed = int((last["open_time_utc"] - entry_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))

        exit_price, exit_reason = None, None
        if last["low"] <= pos["sl"]:
            exit_price, exit_reason = pos["sl"], "SL 🔴"
        elif last["high"] >= pos["tp"]:
            exit_price, exit_reason = pos["tp"], "TP 🟢"
        elif bars_elapsed >= strategy.TIME_STOP_BARS:
            exit_price, exit_reason = last["close"], "وقف زمني ⏱️"

        if exit_price is not None:
            round_trip_cost = 0.30  # % (نفس افتراض الباكتست)
            pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100 - round_trip_cost

            position_dollars = pos.get("position_dollars", state["balance"] * strategy.POSITION_SIZE_PCT)
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

            emoji = "✅" if pnl_pct > 0 else "❌"
            msg = (
                f"{emoji} <b>إغلاق صفقة: {sym}</b>\n"
                f"السبب: {exit_reason}\n"
                f"سعر الدخول: {fmt_price(pos['entry_price'])}\n"
                f"سعر الخروج: {fmt_price(exit_price)}\n"
                f"النتيجة الصافية: {pnl_pct:+.2f}% ({pnl_dollars:+,.2f}$)\n"
                f"مدة الصفقة: {bars_elapsed} شمعة\n"
                f"الرصيد الحالي: ${state['balance']:,.2f}"
            )
            push(msg)
            log_signal(state, "خروج", sym, f"{exit_reason} {pnl_pct:+.2f}%")
            append_trade_log({
                "pair": sym,
                "entry_time": pos["entry_time"],
                "exit_time": str(last["open_time_utc"]),
                "entry_price": pos["entry_price"],
                "exit_price": exit_price,
                "pnl_pct_net": round(pnl_pct, 4),
                "pnl_dollars": round(pnl_dollars, 2),
                "exit_reason": exit_reason,
                "score": pos.get("score", ""),
                "balance_after": round(state["balance"], 2),
            })
            del open_positions[sym]
            cooldown_until = last["open_time_utc"] + timedelta(minutes=INTERVAL_MINUTES * strategy.COOLDOWN_BARS_AFTER_TRADE)
            state["cooldown_until"][sym] = str(cooldown_until)

    # ---------- 4) فحص إشارات دخول جديدة ----------
    candidates = []
    for sym, df in data.items():
        if sym in open_positions:
            continue
        cooldown_until = state["cooldown_until"].get(sym)
        last = df.iloc[-1]
        if cooldown_until and pd.Timestamp(last["open_time_utc"]) < pd.Timestamp(cooldown_until):
            continue
        last_seen = state["last_candle_seen"].get(sym)
        candle_key = str(last["open_time_utc"])
        if last_seen == candle_key:
            continue

        ok, score = strategy.check_entry_signal(last, latest_market_regime)
        if ok:
            candidates.append((sym, score, last))

    if len(candidates) > strategy.CROWD_FILTER_MAX_SIGNALS:
        print(f"فلتر الازدحام: رُفضت {len(candidates)} إشارة (تجاوزت الحد {strategy.CROWD_FILTER_MAX_SIGNALS})")
        candidates = []

    candidates.sort(key=lambda x: x[1], reverse=True)
    available_slots = strategy.MAX_CONCURRENT_TRADES - len(open_positions)

    for sym, score, last in candidates:
        if available_slots <= 0:
            break
        entry_price = float(last["close"])
        sl_price, tp_price = strategy.compute_sl_tp(entry_price, float(last["atr"]))
        position_dollars = state.get("balance", STARTING_BALANCE) * strategy.POSITION_SIZE_PCT
        open_positions[sym] = {
            "entry_time": str(last["open_time_utc"]),
            "entry_price": entry_price,
            "sl": sl_price,
            "tp": tp_price,
            "score": round(float(score), 4),
            "position_dollars": round(position_dollars, 2),
        }
        available_slots -= 1
        msg = (
            f"🚨 <b>توصية دخول جديدة: {sym}</b>\n"
            f"سعر الدخول التقريبي: {fmt_price(entry_price)}\n"
            f"وقف الخسارة (SL): {fmt_price(sl_price)}\n"
            f"جني الأرباح (TP): {fmt_price(tp_price)}\n"
            f"حجم الصفقة المقترح: {strategy.POSITION_SIZE_PCT*100:.0f}% من رأس المال (${position_dollars:,.2f})\n"
            f"قوة الإشارة: {score:.2f}\n"
            f"⚠️ هذه توصية آلية من نظام باكتست، وليست نصيحة مالية. تحقق دائمًا بنفسك قبل التنفيذ."
        )
        push(msg)
        log_signal(state, "دخول", sym, f"دخول عند {fmt_price(entry_price)}")

    for sym, df in data.items():
        state["last_candle_seen"][sym] = str(df.iloc[-1]["open_time_utc"])

    save_state(state)
    print(f"الصفقات المفتوحة حاليًا: {len(open_positions)} | الرصيد: ${state['balance']:,.2f}")
    print("=== انتهى التشغيل ===")


if __name__ == "__main__":
    main()
