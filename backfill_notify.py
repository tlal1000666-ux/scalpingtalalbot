"""
سكربت تعويضي - يُشغَّل مرة واحدة يدويًا فقط.
يقرأ trades_log.csv الموجود ويرسل رسالة تلجرام لكل صفقة مسجلة فيه
(مفيد لو صفقات قديمة اتسجلت بالملف لكن رسالتها ضاعت بسبب تعارض git).

⚠️ لا يحذف أو يعدّل trades_log.csv إطلاقًا - قراءة فقط.
⚠️ شغّله مرة وحدة بس، وإلا رح يعيد إرسال نفس الرسائل من جديد كل مرة تشغّله.
"""
import os
import csv
import time

from utils import send_telegram_message

TRADES_LOG_FILE = "trades_log.csv"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def fmt_price(p):
    return f"{float(p):.6f}".rstrip("0").rstrip(".")


def main():
    if not os.path.exists(TRADES_LOG_FILE):
        print("ما فيه trades_log.csv لسا.")
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("التوكن أو الشات آي دي غير مضبوطين.")
        return

    with open(TRADES_LOG_FILE, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"عدد الصفقات المسجلة بالملف: {len(rows)}")

    for row in rows:
        pnl_pct = float(row["pnl_pct_net"])
        pnl_dollars = float(row["pnl_dollars"])
        emoji = "✅" if pnl_pct > 0 else "❌"
        msg = (
            f"📋 <b>(إشعار متأخر) صفقة مسجلة سابقًا: {row['pair']}</b>\n"
            f"وقت الدخول: {row['entry_time']}\n"
            f"وقت الخروج: {row['exit_time']}\n"
            f"سعر الدخول: {fmt_price(row['entry_price'])}\n"
            f"سعر الخروج: {fmt_price(row['exit_price'])}\n"
            f"السبب: {row['exit_reason']}\n"
            f"النتيجة الصافية: {emoji} {pnl_pct:+.2f}% ({pnl_dollars:+,.2f}$)\n"
            f"الرصيد بعدها: ${float(row['balance_after']):,.2f}"
        )
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
        print(f"أُرسلت: {row['pair']} ({row['entry_time']} -> {row['exit_time']})")
        time.sleep(1)

    print("انتهى الإرسال. لا تُشغّل هذا السكربت مرة ثانية إلا لو أضيفت صفقات جديدة تبي تُعاد إشعارها.")


if __name__ == "__main__":
    main()
