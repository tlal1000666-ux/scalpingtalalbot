"""
بوت توصيات تداول - استراتيجية BOS + Order Block (يشتغل مرة كل تشغيل، كل 30 دقيقة)

الوظيفة:
  1. يفحص أوامر تلجرام الجديدة (/balance /positions /stats /signals /help) ويرد عليها
  2. يجلب آخر بيانات الشموع (30m) من Binance لقائمة الرموز في symbols.txt
  3. يحسب مؤشرات BOS + Order Block (نفس منطق الباكتست بالضبط - سببي 100%)
  4. يدير دورة حياة كل إعداد:
       إشارة BOS جديدة → "إعداد معلّق" (Limit Order) → تعبئة عند لمس السعر → صفقة مفتوحة → خروج SL/TP/وقف زمني
  5. يطبّق العمولة + الانزلاق السعري (Slippage) على كل تنفيذ فعلي
  6. عند ازدحام أكتر من إعداد بيتعبّى بنفس اللحظة والأماكن المتاحة أقل، ياخد الأعلى score
  7. يرسل كل إشارة/تعبئة/خروج كرسالة تلجرام، ويحدّث الرصيد الافتراضي والإحصائيات
  8. يسجل كل صفقة مغلقة في trades_log.csv

⚠️ هذا أداة توصيات وتتبع فقط — لا ينفذ أي صفقة حقيقية بنفسه، ولا يشكل نصيحة استثمارية.
"""
import os
import json
import csv
from datetime import datetime, timezone, timedelta

import pandas as pd

import strategy
from utils import fetch_klines, send_telegram_message, get_telegram_updates, sleep_safe

STATE_FILE = "state.json"
TRADES_LOG_FILE = "trades_log.csv"
SYMBOLS_FILE = "symbols.txt"
INTERVAL = "30m"
INTERVAL_MINUTES = 30
STARTING_BALANCE = strategy.STARTING_BALANCE
MAX_SIGNAL_HISTORY = 20

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def load_symbols():
    with open(SYMBOLS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def default_state():
    return {
        "open_positions": {},      # صفقات اتعبّت فعليًا (limit order اتنفذ)
        "pending_setups": {},      # إعدادات معلّقة (limit order لسه مستني يتلمس)
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


def close_position(state, sym, pos, exit_price_raw, exit_reason, is_stop_loss, last_open_time):
    """يغلق صفقة مفتوحة: يطبّق انزلاق الخروج + العمولة، يحدّث الرصيد والإحصائيات، ويسجل ويبلّغ."""
    exit_price = strategy.apply_exit_slippage(exit_price_raw, is_stop_loss)
    pnl_pct = strategy.compute_net_pnl_pct(pos["entry_price"], exit_price)

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
        f"سعر الدخول (بعد الانزلاق): {fmt_price(pos['entry_price'])}\n"
        f"سعر الخروج (بعد الانزلاق): {fmt_price(exit_price)}\n"
        f"النتيجة الصافية (بعد عمولة+انزلاق): {pnl_pct:+.2f}% ({pnl_dollars:+,.2f}$)\n"
        f"الرصيد الحالي: ${state['balance']:,.2f}"
    )
    push(msg)
    log_signal(state, "خروج", sym, f"{exit_reason} {pnl_pct:+.2f}%")
    append_trade_log({
        "pair": sym,
        "signal_time": pos.get("signal_time", ""),
        "entry_time": pos["entry_time"],
        "exit_time": str(last_open_time),
        "entry_price": round(pos["entry_price"], 8),
        "exit_price": round(exit_price, 8),
        "pnl_pct_net": round(pnl_pct, 4),
        "pnl_dollars": round(pnl_dollars, 2),
        "exit_reason": exit_reason,
        "score": pos.get("score", ""),
        "balance_after": round(state["balance"], 2),
    })


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
        elif text in ("/pending", "/المعلقة"):
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


def handle_pending(state, chat_id):
    pending = state.get("pending_setups", {})
    if not pending:
        reply(chat_id, "📭 ما فيه إعدادات معلّقة حاليًا.")
        return
    lines = [f"⏳ <b>إعدادات معلّقة بانتظار التنفيذ ({len(pending)})</b>\n"]
    for sym, p in pending.items():
        lines.append(
            f"• <b>{sym}</b>\n"
            f"  دخول مستهدف: {fmt_price(p['entry1'])} | SL: {fmt_price(p['sl'])} | TP: {fmt_price(p['tp'])}\n"
            f"  وقت الإشارة: {p['signal_time']}"
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
        "/positions — الصفقات المفتوحة فعليًا الآن\n"
        "/pending — الإعدادات المعلّقة (لسه ما اتلمستش)\n"
        "/stats — إحصائيات الأداء التراكمية\n"
        "/signals — آخر 10 إشارات دخول/خروج\n"
        "/help — عرض هذه القائمة\n\n"
        "⚠️ ملاحظة: البوت يفحص أوامرك بس وقت تشغيله (كل 30 دقيقة تقريبًا)، فالرد ممكن ياخذ لين نص ساعة."
    )
    reply(chat_id, msg)


# ============================================================
# المنطق الرئيسي
# ============================================================
def main():
    print(f"=== تشغيل البوت — {datetime.now(timezone.utc).isoformat()} ===")
    symbols = load_symbols()
    state = load_state()
    print(f"عدد الرموز المراقبة: {len(symbols)}")

    handle_commands(state)

    # ---------- 1) جلب البيانات وحساب المؤشرات ----------
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

    open_positions = state["open_positions"]
    pending_setups = state["pending_setups"]

    # ---------- 2) فحص الصفقات المفتوحة فعليًا (خروج SL / TP / وقف زمني) ----------
    for sym in list(open_positions.keys()):
        if sym not in data:
            continue
        pos = open_positions[sym]
        last = data[sym].iloc[-1]
        signal_time = pd.Timestamp(pos.get("signal_time", pos["entry_time"]))
        bars_since_signal = int((last["open_time_utc"] - signal_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))

        exit_price_raw, exit_reason, is_sl = None, None, False
        if last["low"] <= pos["sl"]:
            exit_price_raw, exit_reason, is_sl = pos["sl"], "SL 🔴", True
        elif last["high"] >= pos["tp"]:
            exit_price_raw, exit_reason, is_sl = pos["tp"], "TP 🟢", False
        elif bars_since_signal > strategy.MAX_BARS_ACTIVE:
            exit_price_raw, exit_reason, is_sl = last["close"], "وقف زمني ⏱️", False

        if exit_price_raw is not None:
            close_position(state, sym, pos, exit_price_raw, exit_reason, is_sl, last["open_time_utc"])
            del open_positions[sym]

    # ---------- 3) فحص الإعدادات المعلّقة (هل اتلمس سعر الدخول؟) ----------
    newly_filled = []  # [(sym, pos_dict, score)]
    for sym in list(pending_setups.keys()):
        if sym not in data:
            continue
        p = pending_setups[sym]
        last = data[sym].iloc[-1]
        signal_time = pd.Timestamp(p["signal_time"])
        bars_since_signal = int((last["open_time_utc"] - signal_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))

        if last["low"] <= p["entry1"]:
            entry_price = strategy.apply_entry_slippage(p["entry1"])
            pos = {
                "entry_time": str(last["open_time_utc"]),
                "signal_time": p["signal_time"],
                "entry_price": entry_price,
                "sl": p["sl"],
                "tp": p["tp"],
                "score": p["score"],
            }
            # فحص خروج فوري لو نفس شمعة التعبئة لمست SL أو TP كمان
            if last["low"] <= p["sl"]:
                close_position(state, sym, pos, p["sl"], "SL 🔴 (بنفس شمعة التعبئة)", True, last["open_time_utc"])
                del pending_setups[sym]
                continue
            elif last["high"] >= p["tp"]:
                close_position(state, sym, pos, p["tp"], "TP 🟢 (بنفس شمعة التعبئة)", False, last["open_time_utc"])
                del pending_setups[sym]
                continue
            newly_filled.append((sym, pos, p["score"]))
            del pending_setups[sym]
        elif bars_since_signal > strategy.MAX_BARS_ACTIVE:
            print(f"  [إلغاء] {sym}: انتهت مهلة الإعداد المعلّق بدون تنفيذ.")
            del pending_setups[sym]

    # تطبيق الأماكن المتاحة على الإعدادات اللي اتعبّت هالتشغيلة، بترتيب السكور عند الازدحام
    newly_filled.sort(key=lambda x: x[2], reverse=True)
    available_slots = strategy.MAX_CONCURRENT_TRADES - len(open_positions)
    for sym, pos, score in newly_filled:
        if available_slots <= 0:
            print(f"  [رفض ازدحام] {sym}: الأماكن ممتلئة، السكور {score:.3f} لم يكفِ.")
            continue
        position_dollars = state.get("balance", STARTING_BALANCE) * strategy.POSITION_SIZE_PCT
        pos["position_dollars"] = round(position_dollars, 2)
        open_positions[sym] = pos
        available_slots -= 1
        msg = (
            f"✅ <b>تعبئة صفقة: {sym}</b>\n"
            f"سعر الدخول (بعد الانزلاق): {fmt_price(pos['entry_price'])}\n"
            f"وقف الخسارة (SL): {fmt_price(pos['sl'])}\n"
            f"جني الأرباح (TP): {fmt_price(pos['tp'])}\n"
            f"حجم الصفقة: {strategy.POSITION_SIZE_PCT*100:.1f}% (${position_dollars:,.2f})\n"
            f"قوة الإشارة: {score:.3f}"
        )
        push(msg)
        log_signal(state, "دخول", sym, f"تعبئة عند {fmt_price(pos['entry_price'])}")

    # ---------- 4) فحص إشارات BOS + Order Block جديدة ----------
    new_candidates = []
    for sym, df in data.items():
        if sym in open_positions or sym in pending_setups:
            continue
        last = df.iloc[-1]
        candle_key = str(last["open_time_utc"])
        if state["last_candle_seen"].get(sym) == candle_key:
            continue
        sig = strategy.check_new_signal(df)
        if sig:
            new_candidates.append((sym, sig))

    for sym, sig in new_candidates:
        pending_setups[sym] = sig
        msg = (
            f"🚨 <b>إعداد جديد (BOS + Order Block): {sym}</b>\n"
            f"سعر الدخول المستهدف (Limit): {fmt_price(sig['entry1'])}\n"
            f"وقف الخسارة (SL): {fmt_price(sig['sl'])}\n"
            f"جني الأرباح (TP): {fmt_price(sig['tp'])}\n"
            f"قوة الإشارة: {sig['score']:.3f}\n"
            f"مهلة التنفيذ: {strategy.MAX_BARS_ACTIVE} شمعة (~{strategy.MAX_BARS_ACTIVE * INTERVAL_MINUTES // 60} ساعة)\n"
            f"⚠️ إعداد معلّق فقط، لسه ما دخلناش الصفقة. توصية آلية، مش نصيحة مالية."
        )
        push(msg)
        log_signal(state, "إعداد", sym, f"معلّق عند {fmt_price(sig['entry1'])}")

    for sym, df in data.items():
        state["last_candle_seen"][sym] = str(df.iloc[-1]["open_time_utc"])

    save_state(state)
    print(
        f"الصفقات المفتوحة: {len(open_positions)} | المعلّقة: {len(pending_setups)} | "
        f"الرصيد: ${state['balance']:,.2f}"
    )
    print("=== انتهى التشغيل ===")


if __name__ == "__main__":
    main()
