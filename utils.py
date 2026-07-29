"""أدوات مساعدة: جلب بيانات Binance + إرسال رسائل تلجرام."""
import time
import requests
import pandas as pd

BINANCE_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"


def fetch_klines(symbol: str, interval: str = "1h", limit: int = 500,
                 start_time: int = None, end_time: int = None,
                 strip_last: bool = True, retries: int = 2):
    """يجلب شموع OHLCV من Binance.

    Args:
        symbol: رمز الزوج (مثل BTCUSDT)
        interval: الإطار الزمني (1h, 1m, etc.)
        limit: عدد الشموع المطلوبة
        start_time: (اختياري) بداية الفترة بالمللي ثانية — لجلب نافذة زمنية محددة
        end_time: (اختياري) نهاية الفترة بالمللي ثانية
        strip_last: هل نتجاهل آخر شمعة (قد تكون مفتوحة). True للشموع الكبيرة، False لـ1m
        retries: عدد محاولات إعادة المحاولة
    """
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_time is not None:
        params["startTime"] = int(start_time)
    if end_time is not None:
        params["endTime"] = int(end_time)

    raw = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
            if resp.status_code == 429:
                wait = 3 * (attempt + 1)
                print(f"  [تحذير] 429 (rate limit) لـ{symbol} - انتظار {wait}s...")
                time.sleep(wait)
                continue
            if resp.status_code == 451:
                # Geo-restricted — نستخدم mirror بديل
                alt_url = "https://api.binance.com/api/v3/klines"
                resp = requests.get(alt_url, params=params, timeout=15)
                if resp.status_code != 200:
                    return None
            resp.raise_for_status()
            raw = resp.json()
            break
        except Exception as e:
            print(f"  [تحذير] فشل جلب {symbol} (محاولة {attempt+1}/{retries+1}): {e}")
            if attempt < retries:
                time.sleep(1)

    if not raw:
        return None

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "num_trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df["open_time_utc"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["pair"] = symbol

    # نتجاهل آخر شمعة فقط لو لسا مفتوحة (close_time في المستقبل)
    if strip_last and len(df) > 1:
        now_utc = pd.Timestamp.now(tz="UTC")
        if df.iloc[-1]["close_time"] > now_utc:
            df = df.iloc[:-1]
    df = df.reset_index(drop=True)
    return df[["pair", "open_time_utc", "open", "high", "low", "close", "volume"]]


def fetch_klines_1m_window(symbol: str, window_start_ms: int, window_end_ms: int):
    """يجلب شموع 1m لنافذة زمنية محددة بالضبط (لدقة فحص SL/TP/Trail).

    يستخدم start_time/end_time بدل limit عشان يضمن تغطية النافذة كاملة
    حتى لو البوت يفحص شمعة قديمة (catch-up بعد توقف).
    """
    # نضيف هامش دقيقة قبل وبعد
    start = window_start_ms - 60_000
    end = window_end_ms + 60_000
    # limit=120 كافي لتغطية 60 دقيقة + هامش
    return fetch_klines(symbol, interval="1m", limit=120,
                        start_time=start, end_time=end, strip_last=False)


def send_telegram_message(token: str, chat_id: str, text: str):
    url = TELEGRAM_API_URL.format(token=token)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [تحذير] فشل إرسال تلجرام: {e}")


def get_telegram_updates(token: str, offset: int = 0):
    """يجلب الرسائل الجديدة منذ آخر offset."""
    url = TELEGRAM_UPDATES_URL.format(token=token)
    params = {"offset": offset, "timeout": 0}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as e:
        print(f"  [تحذير] فشل جلب رسائل تلجرام: {e}")
        return []


def sleep_safe(seconds: float = 0.25):
    """تهدئة بين طلبات API لتجنب rate limits."""
    time.sleep(seconds)
