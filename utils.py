"""أدوات مساعدة: جلب بيانات Binance + إرسال رسائل تلجرام."""
import time
import requests
import pandas as pd

BINANCE_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"


def fetch_klines(symbol: str, interval: str = "30m", limit: int = 500, retries: int = 2) -> pd.DataFrame | None:
    """يجلب شموع OHLCV من Binance (نقطة نهاية عامة، بدون حاجة لمفتاح API).
    يعيد المحاولة تلقائيًا لو صار 429 (تجاوز حد الطلبات)."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    raw = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
            if resp.status_code == 429:
                wait = 2 * (attempt + 1)
                print(f"  [تحذير] 429 (rate limit) لـ{symbol} - إعادة محاولة بعد {wait} ثانية...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            raw = resp.json()
            break
        except Exception as e:
            print(f"  [تحذير] فشل جلب بيانات {symbol} (محاولة {attempt+1}/{retries+1}): {e}")
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
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["pair"] = symbol
    # نتجاهل آخر شمعة لأنها قد تكون لسا مفتوحة (غير مغلقة)
    df = df.iloc[:-1].reset_index(drop=True)
    return df[["pair", "open_time_utc", "open", "high", "low", "close", "volume"]]


def send_telegram_message(token: str, chat_id: str, text: str):
    url = TELEGRAM_API_URL.format(token=token)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [تحذير] فشل إرسال رسالة تلجرام: {e}")


def get_telegram_updates(token: str, offset: int = 0):
    """يجلب الرسائل الجديدة المرسلة للبوت منذ آخر offset معروف."""
    url = TELEGRAM_UPDATES_URL.format(token=token)
    params = {"offset": offset, "timeout": 0}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", [])
    except Exception as e:
        print(f"  [تحذير] فشل جلب رسائل تلجرام: {e}")
        return []


def sleep_safe(seconds: float = 0.25):
    """تهدئة بسيطة بين طلبات API لتجنب rate limits."""
    time.sleep(seconds)
