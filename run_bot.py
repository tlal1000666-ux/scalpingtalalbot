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

STATE_FILE = "state.json"
TRADES_LOG_FILE = "trades_log.csv"
SHADOW_TRADES_LOG_FILE = "shadow_trades_log.csv"   # سجل الصفقات الموازية (بدون حد تزامن)
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
        # ---- تتبّع موازي (Shadow) بدون قيد MAX_CONCURRENT_TRADES ----
        # مسار كامل موازي: نفس الإشارات بالضبط، بس بدون حد 3 صفقات متزامنة.
        # لا يؤثر إطلاقًا على open_positions/pending_setups/balance/stats الحقيقية،
        # ولا يرسل رسائل تلجرام - فقط لأغراض المقارنة/الإحصاء.
        "shadow_pending_setups": {},
        "shadow_open_positions": {},
        "shadow_balance": STARTING_BALANCE,
        "shadow_stats": {"total_trades": 0, "wins": 0, "losses": 0, "gross_profit": 0.0, "gross_loss": 0.0},
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


def append_shadow_trade_log(row: dict):
    """سجل منفصل تمامًا لصفقات المسار الموازي (بدون حد تزامن) - لا يمس trades_log.csv الحقيقي."""
    file_exists = os.path.exists(SHADOW_TRADES_LOG_FILE)
    with open(SHADOW_TRADES_LOG_FILE, "a", newline="", encoding="utf-8") as f:
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
        elif text in ("/shadow", "/بدون_حد", "/بدونحد"):
            handle_shadow_stats(state, chat_id)
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


def handle_shadow_stats(state, chat_id):
    """إحصائيات كاملة لمسار الظل - يوضح كيف كان ممكن يصير الأداء لو ما فيه قيد
    على عدد الصفقات المتزامنة (MAX_CONCURRENT_TRADES). لا يؤثر إطلاقًا على الحقيقي."""
    s = state.get("shadow_stats", {})
    total = s.get("total_trades", 0)
    balance = state.get("shadow_balance", STARTING_BALANCE)
    total_return_pct = (balance - STARTING_BALANCE) / STARTING_BALANCE * 100
    open_count = len(state.get("shadow_open_positions", {}))
    pending_count = len(state.get("shadow_pending_setups", {}))

    if total == 0:
        msg = (
            f"🌓 <b>إحصائيات بدون حد (Shadow — بدون قيد {strategy.MAX_CONCURRENT_TRADES} صفقات)</b>\n"
            f"ما فيه صفقات مغلقة لسا بهالمسار.\n"
            f"مفتوحة الآن: {open_count} | معلّقة: {pending_count}\n"
            f"الرصيد الافتراضي: ${balance:,.2f}"
        )
        reply(chat_id, msg)
        return

    wins = s.get("wins", 0)
    losses = s.get("losses", 0)
    win_rate = wins / total * 100 if total else 0
    gross_profit = s.get("gross_profit", 0.0)
    gross_loss = s.get("gross_loss", 0.0)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    pf_str = f"{profit_factor:.2f}" if profit_factor != float("inf") else "∞"

    # مقارنة سريعة مع المسار الحقيقي لنفس اللحظة
    real_s = state.get("stats", {})
    real_total = real_s.get("total_trades", 0)
    real_balance = state.get("balance", STARTING_BALANCE)
    real_return_pct = (real_balance - STARTING_BALANCE) / STARTING_BALANCE * 100

    msg = (
        f"🌓 <b>إحصائيات بدون حد (Shadow — بدون قيد {strategy.MAX_CONCURRENT_TRADES} صفقات متزامنة)</b>\n"
        f"إجمالي الصفقات: {total}\n"
        f"رابحة: {wins} | خاسرة: {losses}\n"
        f"Win Rate: {win_rate:.2f}%\n"
        f"Profit Factor: {pf_str}\n"
        f"العائد التراكمي: {total_return_pct:+.2f}%\n"
        f"الرصيد الافتراضي: ${balance:,.2f}\n"
        f"مفتوحة الآن: {open_count} | معلّقة: {pending_count}\n\n"
        f"📌 <b>مقارنة مع الحقيقي (بحد {strategy.MAX_CONCURRENT_TRADES} صفقات)</b>\n"
        f"صفقات حقيقية: {real_total} | عائد حقيقي: {real_return_pct:+.2f}%\n"
        f"الفرق بالعائد: {(total_return_pct - real_return_pct):+.2f}%"
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
        "/shadow — إحصائيات كاملة بدون حد على عدد الصفقات المتزامنة (للمقارنة فقط)\n"
        "/help — عرض هذه القائمة\n\n"
        "⚠️ ملاحظة: البوت يفحص أوامرك بس وقت تشغيله (كل 30 دقيقة تقريبًا)، فالرد ممكن ياخذ لين نص ساعة."
    )
    reply(chat_id, msg)


def _resolve_conflict_order(symbol, candle_open_time, sl, tp):
    """يستخدم فقط لما شمعة 30m توحدة تلمس SL وTP الاثنين (حالة تعارض).
    ينزل لفريم الدقيقة (1m) لنفس نافذة وقت الشمعة عشان يعرف بالضبط أيهم صار أول.
    يرجع 'SL' أو 'TP' حسب أيهم انلمس أول على فريم الدقيقة.
    لو ما قدرنا نجيب بيانات الدقيقة أو ما لقينا فيها لمس واضح (بيانات ناقصة)،
    نرجع None ليعتمد المستدعي الافتراض المحافظ (SL أول) كـfallback آمن."""
    window_start = pd.Timestamp(candle_open_time)
    window_end = window_start + pd.Timedelta(minutes=INTERVAL_MINUTES)

    # limit=90 كافي لتغطية 30 دقيقة + هامش، حتى لو فيه تأخير بسيط بين آخر شمعة دقيقة متوفرة والوقت الحالي
    df_1m = fetch_klines(symbol, interval="1m", limit=90)
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
            # نفس التعارض بس على فريم الدقيقة كمان (نادر جدًا) - ما فيه دقة أكتر ممكنة، نوقف هون
            return None
        if hit_sl:
            return "SL"
        if hit_tp:
            return "TP"
    return None


def _new_candles_since(df: pd.DataFrame, last_seen_key):
    """يرجع كل الشموع المغلقة الأحدث من last_seen_key (بالترتيب الزمني تصاعديًا).
    لو last_seen_key غير موجود (أول مرة نشوف هالرمز بهاد السياق)، نرجع بس آخر شمعة
    تفاديًا لإعادة فحص كامل التاريخ. هاد بيحل مشكلة 'قفزة الشموع': لو فاتت دورة تشغيل
    أو اتأخرت، منلحق نفحص كل شمعة انقفلت بالمنتصف بدل ما نشوف بس الأحدث."""
    if last_seen_key is None:
        return df.iloc[[-1]]
    try:
        last_seen_ts = pd.Timestamp(last_seen_key)
    except (ValueError, TypeError):
        return df.iloc[[-1]]

    mask = df["open_time_utc"] > last_seen_ts
    new_rows = df[mask]
    if new_rows.empty:
        # ما فيه شمعة أحدث اتقفلت (مثلاً نفس الشمعة القديمة لسا آخر وحدة) - نرجع فاضي
        return new_rows
    return new_rows


# ============================================================
# المنطق الرئيسي
# ============================================================
def main():
    print(f"=== تشغيل بوت BOS+OB — {datetime.now(timezone.utc).isoformat()} ===")
    symbols = load_symbols()
    state = load_state()
    print(f"عدد الرموز المراقبة: {len(symbols)}")

    try:
        _run_cycle(state, symbols)
    except Exception as e:
        # لازم نحفظ الحالة حتى لو صار استثناء بأي مرحلة - وإلا أي إشارة/تفعيل/إغلاق
        # اترسل فعليًا كرسالة تلجرام بهالدورة بيضيع من الحالة، وبيترسل تاني بالتشغيل الجاي
        # (نفس مشكلة "تكرار إرسال نفس الإشارة").
        print(f"⚠️ صار خطأ أثناء التشغيل: {e}")
        raise
    finally:
        save_state(state)
        print(f"مفتوحة: {len(state.get('open_positions', {}))} | معلّقة: {len(state.get('pending_setups', {}))} | الرصيد: ${state.get('balance', STARTING_BALANCE):,.2f}")
        print(f"[Shadow/بدون حد] مفتوحة: {len(state.get('shadow_open_positions', {}))} | معلّقة: {len(state.get('shadow_pending_setups', {}))} | الرصيد: ${state.get('shadow_balance', STARTING_BALANCE):,.2f} | صفقات مغلقة: {state.get('shadow_stats', {}).get('total_trades', 0)}")
        print("=== انتهى التشغيل ===")


def _run_cycle(state, symbols):
    # ---------- 0) معالجة أي أوامر تفاعلية جديدة ----------
    handle_commands(state)

    # ---------- 1) جلب البيانات وحساب المؤشرات لكل رمز ----------
    # كل رمز معزول بـtry/except مستقل: فشل رمز واحد (شبكة، بيانات ناقصة، إلخ)
    # ما لازم يوقف معالجة باقي الرموز - وإلا أي إشارة/تفعيل صار لرموز سابقة بنفس
    # الدورة بيضيع لأنه الكود بيتوقف قبل ما يوصل لقسم معالجتها.
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
            print(f"  [تحذير] تخطي {sym} بسبب خطأ بجلب/معالجة البيانات: {e}")
            continue

    if not data:
        print("لم يتم جلب أي بيانات صالحة. حفظ الحالة والإيقاف.")
        return

    pending_setups = state["pending_setups"]
    open_positions = state["open_positions"]
    shadow_pending_setups = state["shadow_pending_setups"]
    shadow_open_positions = state["shadow_open_positions"]

    # ---------- 2) فحص الصفقات المفتوحة فعليًا (SL / TP / Timeout) ----------
    # نفحص كل شمعة جديدة اتقفلت من آخر تشغيل ناجح (مو بس آخر وحدة)، بالترتيب الزمني،
    # ونوقف عند أول شمعة تحقق خروج - تمامًا متل منطق الباكتست.
    for sym in list(open_positions.keys()):
        if sym not in data:
            continue
        try:
            pos = open_positions[sym]
            df = data[sym]
            signal_time = pd.Timestamp(pos["signal_time"])
            entry_time = pd.Timestamp(pos.get("entry_time", pos["signal_time"]))

            # نفس الحماية: صفقة مفتوحة فعليًا لازم نفحصها من entry_time على الأقل،
            # حتى لو last_candle_seen مفقود أو أقدم من وقت الدخول (بوت كان متعطل مثلًا).
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

                hit_sl = last["low"] <= pos["sl"]
                hit_tp = last["high"] >= pos["tp"]

                exit_price, exit_reason = None, None
                if hit_sl and hit_tp:
                    # تعارض: نفس الشمعة لمست SL وTP - ننزل لفريم الدقيقة للتأكد أيهم صار أول
                    order = _resolve_conflict_order(sym, last["open_time_utc"], pos["sl"], pos["tp"])
                    if order == "TP":
                        exit_price, exit_reason = pos["tp"], "TP 🟢"
                    else:
                        exit_price, exit_reason = pos["sl"], "SL 🔴"  # fallback محافظ لو ما قدرنا نتأكد
                elif hit_sl:
                    exit_price, exit_reason = pos["sl"], "SL 🔴"
                elif hit_tp:
                    exit_price, exit_reason = pos["tp"], "TP 🟢"
                elif bars_since_signal > strategy.MAX_BARS_ACTIVE:
                    exit_price, exit_reason = last["close"], "Timeout ⏱️"

                if exit_price is not None:
                    _close_position(state, sym, pos, exit_price, exit_reason, last["open_time_utc"])
                    del open_positions[sym]
                    break
        except Exception as e:
            print(f"  [تحذير] تخطي فحص صفقة {sym} المفتوحة بسبب خطأ: {e}")
            continue

    # ---------- 2ب) فحص صفقات الظل المفتوحة (نفس منطق خطوة 2 بالضبط) ----------
    # مسار موازٍ مستقل تمامًا: لا يقرأ ولا يكتب على open_positions/state["last_candle_seen"]
    # الحقيقية، فما فيه أي احتمال يأثر على المسار الحقيقي.
    for sym in list(shadow_open_positions.keys()):
        if sym not in data:
            continue
        try:
            pos = shadow_open_positions[sym]
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

                hit_sl = last["low"] <= pos["sl"]
                hit_tp = last["high"] >= pos["tp"]

                exit_price, exit_reason = None, None
                if hit_sl and hit_tp:
                    order = _resolve_conflict_order(sym, last["open_time_utc"], pos["sl"], pos["tp"])
                    if order == "TP":
                        exit_price, exit_reason = pos["tp"], "TP 🟢"
                    else:
                        exit_price, exit_reason = pos["sl"], "SL 🔴"
                elif hit_sl:
                    exit_price, exit_reason = pos["sl"], "SL 🔴"
                elif hit_tp:
                    exit_price, exit_reason = pos["tp"], "TP 🟢"
                elif bars_since_signal > strategy.MAX_BARS_ACTIVE:
                    exit_price, exit_reason = last["close"], "Timeout ⏱️"

                if exit_price is not None:
                    _close_shadow_position(state, sym, pos, exit_price, exit_reason, last["open_time_utc"])
                    del shadow_open_positions[sym]
                    break
        except Exception as e:
            print(f"  [تحذير] تخطي فحص صفقة ظل {sym} المفتوحة بسبب خطأ: {e}")
            continue

    # ---------- 3) فحص الأوامر المعلّقة: تفعيل + متابعة SL/TP/Timeout بنفس الدورة ----------
    # بنمشي شمعة-شمعة بالترتيب الزمني لكل setup معلّق. أول ما تنلمس entry1 بشمعة معينة
    # (تفعيل)، منكمل *بنفس الحلقة* نفحص باقي الشموع الجديدة يلي بعدها (لو موجودة بنفس
    # الدورة) لمعرفة هل ضربت SL أو TP أو لسا مفتوحة - بدل ما نستنى الدورة الجاية.
    # هيك منغطي حالة: "تفعّلت بشمعة قديمة وضربت الهدف بشمعة تالية بنفس هالتشغيل".
    fillable = []
    for sym in list(pending_setups.keys()):
        if sym not in data:
            continue
        try:
            p = pending_setups[sym]
            df = data[sym]
            signal_time = pd.Timestamp(p["signal_time"])

            # حماية إضافية: setup معلّق لازم منطقيًا نفحصه من signal_time على الأقل،
            # حتى لو last_candle_seen مفقود أو (بغلط) أقدم/أحدث من الإشارة نفسها.
            # هيك ما بتضيع شمعة تفعيل أو TP صارت وقت البوت كان متعطل ومالوش last_candle_seen محدّث.
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
                if c["low"] <= p["entry1"]:
                    fill_candle = c
                    break
                elif bars_since_signal > strategy.MAX_BARS_ACTIVE:
                    del pending_setups[sym]
                    log_signal(state, "إلغاء", sym, "انتهى وقت الأمر المعلّق بدون تنفيذ")
                    break

            if fill_candle is not None:
                # نمرر باقي الشموع (من نفس شمعة التفعيل وطالع) عشان نكمل فحص SL/TP بنفس الدورة
                remaining = new_candles[new_candles["open_time_utc"] >= fill_candle["open_time_utc"]]
                fillable.append((sym, p, fill_candle, remaining))
        except Exception as e:
            print(f"  [تحذير] تخطي فحص setup معلّق {sym} بسبب خطأ: {e}")
            continue

    fillable.sort(key=lambda x: x[1]["score"], reverse=True)
    for sym, p, fill_candle, remaining in fillable:
        try:
            del pending_setups[sym]
            available_slots = strategy.MAX_CONCURRENT_TRADES - len(open_positions)
            if available_slots <= 0:
                log_signal(state, "إلغاء", sym, "اترفض التنفيذ - المحفظة ممتلئة (3 صفقات)")
                continue

            entry_price = p["entry1"]
            entry_time = fill_candle["open_time_utc"]
            # حجم الصفقة يتحدد وقت التنفيذ (الفتح) ويتثبّت لحد الإغلاق - مش وقت الإغلاق
            position_dollars = state["balance"] * strategy.POSITION_SIZE_PCT
            pos = {
                "signal_time": p["signal_time"], "entry_time": str(entry_time),
                "entry_price": entry_price, "sl": p["sl"], "tp": p["tp"], "score": p["score"],
                "position_dollars": position_dollars,
            }

            signal_time_iso = pos["signal_time"]
            push(
                f"⚡ تم تفعيل الصفقة (ACT) \n\n"
                f"💎 Pair: #{sym}\n"
                f"📅 وقت الفتح: {signal_time_iso}\n\n"
                f"Entry: {fmt_price(entry_price)}"
            )
            log_signal(state, "تنفيذ", sym, f"دخول عند {fmt_price(entry_price)}")

            # نفحص شمعة التفعيل نفسها ثم أي شموع تالية (من نفس هالدورة) للـSL/TP/Timeout
            signal_time = pd.Timestamp(pos["signal_time"])
            closed = False
            for _, c in remaining.iterrows():
                bars_since_signal = int((c["open_time_utc"] - signal_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))

                hit_sl = c["low"] <= pos["sl"]
                hit_tp = c["high"] >= pos["tp"]

                exit_price, exit_reason = None, None
                if hit_sl and hit_tp:
                    # تعارض: نفس الشمعة لمست SL وTP - ننزل لفريم الدقيقة للتأكد أيهم صار أول
                    order = _resolve_conflict_order(sym, c["open_time_utc"], pos["sl"], pos["tp"])
                    if order == "TP":
                        exit_price, exit_reason = pos["tp"], "TP 🟢"
                    else:
                        exit_price, exit_reason = pos["sl"], "SL 🔴"  # fallback محافظ لو ما قدرنا نتأكد
                elif hit_sl:
                    exit_price, exit_reason = pos["sl"], "SL 🔴"
                elif hit_tp:
                    exit_price, exit_reason = pos["tp"], "TP 🟢"
                elif bars_since_signal > strategy.MAX_BARS_ACTIVE:
                    exit_price, exit_reason = c["close"], "Timeout ⏱️"

                if exit_price is not None:
                    _close_position(state, sym, pos, exit_price, exit_reason, c["open_time_utc"])
                    closed = True
                    break

            if not closed:
                open_positions[sym] = pos
        except Exception as e:
            print(f"  [تحذير] خطأ أثناء تنفيذ/متابعة {sym}: {e}")
            continue

    # ---------- 3ب) نفس setups القابلة للتفعيل (fillable) بس على مسار الظل - بدون قيد تزامن ----------
    # نعيد استخدام نفس fillable المحسوبة فوق (نفس الإشارات، نفس شموع التفعيل) لتفادي
    # إعادة حساب الإشارة مرتين. الفرق الوحيد: ما فيه available_slots <= 0 يوقف الدخول.
    for sym, p, fill_candle, remaining in fillable:
        try:
            entry_price = p["entry1"]
            entry_time = fill_candle["open_time_utc"]
            position_dollars = state["shadow_balance"] * strategy.POSITION_SIZE_PCT
            pos = {
                "signal_time": p["signal_time"], "entry_time": str(entry_time),
                "entry_price": entry_price, "sl": p["sl"], "tp": p["tp"], "score": p["score"],
                "position_dollars": position_dollars,
            }

            signal_time = pd.Timestamp(pos["signal_time"])
            closed = False
            for _, c in remaining.iterrows():
                bars_since_signal = int((c["open_time_utc"] - signal_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))

                hit_sl = c["low"] <= pos["sl"]
                hit_tp = c["high"] >= pos["tp"]

                exit_price, exit_reason = None, None
                if hit_sl and hit_tp:
                    order = _resolve_conflict_order(sym, c["open_time_utc"], pos["sl"], pos["tp"])
                    if order == "TP":
                        exit_price, exit_reason = pos["tp"], "TP 🟢"
                    else:
                        exit_price, exit_reason = pos["sl"], "SL 🔴"
                elif hit_sl:
                    exit_price, exit_reason = pos["sl"], "SL 🔴"
                elif hit_tp:
                    exit_price, exit_reason = pos["tp"], "TP 🟢"
                elif bars_since_signal > strategy.MAX_BARS_ACTIVE:
                    exit_price, exit_reason = c["close"], "Timeout ⏱️"

                if exit_price is not None:
                    _close_shadow_position(state, sym, pos, exit_price, exit_reason, c["open_time_utc"])
                    closed = True
                    break

            if not closed:
                shadow_open_positions[sym] = pos
            # ملاحظة: shadow_pending_setups[sym] لو كان موجود اترفع فوق بخطوة 3ج (نفس pending_setups تمامًا)،
            # وهون منشيله بعد ما صار fill عشان ما يضل معلّق بالخطأ.
            shadow_pending_setups.pop(sym, None)
        except Exception as e:
            print(f"  [تحذير] خطأ أثناء تنفيذ/متابعة ظل {sym}: {e}")
            continue

    # ---------- 3ج) إلغاء shadow setups يلي انتهى وقتها بدون تفعيل (نفس منطق الحقيقي) ----------
    # الحقيقي بيلغي setups منتهية الوقت جوا نفس الحلقة (سطر del pending_setups[sym] فوق)، ومنعمّل
    # shadow_pending_setups[sym] هون كمان (بنفس فحص bars_since_signal) عشان ما يضل معلّق للأبد.
    for sym in list(shadow_pending_setups.keys()):
        if sym not in data:
            continue
        try:
            p = shadow_pending_setups[sym]
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

            for _, c in new_candles.iterrows():
                bars_since_signal = int((c["open_time_utc"] - signal_time) / pd.Timedelta(minutes=INTERVAL_MINUTES))
                if c["low"] <= p["entry1"]:
                    break  # اترفع بخطوة 3ب أصلاً - هون فقط للإلغاء بسبب انتهاء الوقت
                elif bars_since_signal > strategy.MAX_BARS_ACTIVE:
                    del shadow_pending_setups[sym]
                    break
        except Exception as e:
            print(f"  [تحذير] تخطي فحص shadow setup معلّق {sym} بسبب خطأ: {e}")
            continue

    # ---------- 4) فحص إشارات BOS+OB جديدة (بس على رموز خالية من setup حاليًا) ----------
    # ملاحظة مهمة: الحقيقي والظل ممكن يختلفوا بأي رمز معطى (مثلاً الظل فاضي من setup
    # بينما الحقيقي مشغول فيه بسبب قيد التزامن، أو العكس) - فمنفحص كل مسار بشرطه المستقل
    # الخاص فيه، مش شرط واحد موحّد، وإلا رح تنحرم إشارة عن مسار خالي بس لأنه المسار التاني مشغول.
    for sym, df in data.items():
        real_busy = sym in open_positions or sym in pending_setups
        shadow_busy = sym in shadow_open_positions or sym in shadow_pending_setups
        if real_busy and shadow_busy:
            continue
        try:
            last_seen = state["last_candle_seen"].get(sym)
            candle_key = str(df.iloc[-1]["open_time_utc"])
            if last_seen == candle_key:
                continue

            sig = strategy.check_new_signal(df)
            if not sig:
                continue

            if not real_busy:
                pending_setups[sym] = {
                    "signal_time": str(sig["signal_time"]),
                    "entry1": sig["entry1"], "sl": sig["sl"], "tp": sig["tp"],
                    "score": sig["score"],
                }
            if not shadow_busy:
                # نفس الإشارة بالضبط تتغذى لمسار الظل كمان - بدون إعادة حساب strategy.check_new_signal
                shadow_pending_setups[sym] = {
                    "signal_time": str(sig["signal_time"]),
                    "entry1": sig["entry1"], "sl": sig["sl"], "tp": sig["tp"],
                    "score": sig["score"],
                }

            if not real_busy:
                time_str = pd.Timestamp(sig["signal_time"]).strftime("%d/%m/%Y %H:%M")
                push(
                    f"⚡ Scalping Talal Bot ⚡\n"
                    f"🌟 بسم الله توكلت على الله 🌟\n\n"
                    f"💎 Pair: #{sym}\n"
                    f"💎 Exchange: BINANCE\n"
                    f"⏳ Timeframe: 5m\n"
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
            print(f"  [تحذير] تخطي فحص إشارة جديدة لـ{sym} بسبب خطأ: {e}")
            continue

    for sym, df in data.items():
        state["last_candle_seen"][sym] = str(df.iloc[-1]["open_time_utc"])


def _close_position(state, sym, pos, exit_price, exit_reason, exit_time):
    """يغلق صفقة (سواء فُتحت هالدورة أو دورة سابقة) ويحدّث الرصيد والإحصائيات والسجل."""
    round_trip_cost = strategy.ROUND_TRIP_COST_PCT
    pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100 - round_trip_cost

    position_dollars = pos.get("position_dollars")
    if position_dollars is None:
        # احتياط لصفقات قديمة محفوظة في state.json قبل هذا التصحيح
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

    # مدة الصفقة بالشموع (من لحظة التنفيذ الفعلي لحظة الخروج)
    entry_time_raw = pos.get("entry_time", pos.get("signal_time"))
    bars_held = int((pd.Timestamp(exit_time) - pd.Timestamp(entry_time_raw)) / pd.Timedelta(minutes=INTERVAL_MINUTES))
    bars_held = max(bars_held, 0)

    is_tp = exit_reason.startswith("TP")
    is_sl = exit_reason.startswith("SL")
    is_win = pnl_pct > 0

    signal_time_iso = pos["signal_time"]
    close_time_str = pd.Timestamp(exit_time).strftime("%d/%m/%Y %H:%M")

    if is_tp:
        header = "✅ تحقق الهدف ولله الحمد (WIN) "
    elif is_sl:
        header = "❌ ضرب وقف الخسارة (LOSS)"
    elif is_win:  # Timeout بربح
        header = "✅ إغلاق رابح ولله الحمد (WIN) "
    else:  # Timeout بخسارة
        header = "❌ إغلاق خاسر (LOSS)"

    total_return_pct = (state["balance"] - STARTING_BALANCE) / STARTING_BALANCE * 100

    msg = (
        f"{header}\n\n"
        f"💎 Pair: #{sym}\n"
        f"📅 وقت الفتح: {signal_time_iso}\n"
        f"🕒 وقت الإغلاق: {close_time_str} (GMT+3)\n\n"
        f"Entry: {fmt_price(pos['entry_price'])}\n"
        f"Exit: {fmt_price(exit_price)}\n"
        f"PnL: {pnl_pct:+.2f}%\n\n"
        f"💰 رأس المال (افتراضي/محاكاة): ${state['balance']:,.2f}\n"
        f"📈 التغيّر التراكمي على رأس المال: {total_return_pct:+.2f}%"
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


def _close_shadow_position(state, sym, pos, exit_price, exit_reason, exit_time):
    """نفس حساب _close_position بالضبط (PnL، رصيد، إحصائيات) بس على المسار الموازي
    shadow_* - بدون إرسال تلجرام وبدون تسجيل بـsignal_history أو trades_log.csv الحقيقي."""
    round_trip_cost = strategy.ROUND_TRIP_COST_PCT
    pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100 - round_trip_cost

    position_dollars = pos.get("position_dollars")
    if position_dollars is None:
        position_dollars = state["shadow_balance"] * strategy.POSITION_SIZE_PCT
    pnl_dollars = position_dollars * pnl_pct / 100
    state["shadow_balance"] = state.get("shadow_balance", STARTING_BALANCE) + pnl_dollars

    s = state["shadow_stats"]
    s["total_trades"] += 1
    if pnl_dollars > 0:
        s["wins"] += 1
        s["gross_profit"] += pnl_dollars
    else:
        s["losses"] += 1
        s["gross_loss"] += abs(pnl_dollars)

    append_shadow_trade_log({
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
        "shadow_balance_after": round(state["shadow_balance"], 2),
    })


if __name__ == "__main__":
    main()
