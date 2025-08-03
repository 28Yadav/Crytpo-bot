# FILE: news.py

import requests
import datetime
import time
import threading

NEWS_API_KEY = "c2217974247f47128d8ac5b1ba2261da"
NEWS_ENDPOINT = "https://newsapi.org/v2/top-headlines"
KEYWORDS = ["elon musk", "trump", "investors", "fomc", "us", "crypto", "bitcoin", "ethereum"]
NEWS_CACHE_TTL = 60 * 60 * 6  # 6 hours
_last_news_fetch = 0
_last_headlines = []

OPENROUTER_API_KEY = "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"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "deepseek-chat"

TELEGRAM_TOKEN = "8201948535:AAG21iUDjcOCvOOlmk0dCe8J5c67GUDsoRI"
TELEGRAM_CHAT_ID = "7317122509"

last_news_signal = "no signal"
last_opposite_warning_time = 0


def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except Exception as e:
        print(f"[Telegram Error] {e}")


def fetch_news():
    global _last_news_fetch, _last_headlines
    now = time.time()
    if now - _last_news_fetch < NEWS_CACHE_TTL:
        return _last_headlines

    params = {
        "q": " OR ".join(KEYWORDS),
        "apiKey": NEWS_API_KEY,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 10
    }

    try:
        response = requests.get(NEWS_ENDPOINT, params=params)
        data = response.json()
        if data.get("status") == "ok":
            _last_news_fetch = now
            _last_headlines = [article['title'] + ' ' + article.get('description', '') for article in data["articles"]]
            return _last_headlines
        else:
            print(f"[News API Error] {data}")
            return []
    except Exception as e:
        print(f"[News Fetch Error] {e}")
        return []


def ask_deepseek_analysis(news_text):
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Please check if this news affects crypto market in a strong way: \"{news_text}\".\n"
                        "If this news affects the crypto market in a very strong way then reply with 'bullish' or 'bearish'.\n"
                        "Otherwise, reply with 'no signal'."
                    )
                }
            ]
        }
        res = requests.post(OPENROUTER_ENDPOINT, json=payload, headers=headers)
        res_json = res.json()
        reply = res_json["choices"][0]["message"]["content"].strip().lower()
        if reply in ["bullish", "bearish"]:
            return reply
    except Exception as e:
        print(f"[DeepSeek Error] {e}")
    return None


def is_news_signal():
    global last_news_signal
    headlines = fetch_news()
    for headline in headlines:
        signal = ask_deepseek_analysis(headline)
        if signal:
            last_news_signal = signal
            send_telegram_message(f"📢 News signal detected: {signal.upper()}\nHeadline: {headline}")
            return signal
    last_news_signal = "no signal"
    return "no signal"


def combine_signals(tech_signal, news_signal):
    if tech_signal and news_signal in ["bullish", "bearish"]:
        return tech_signal
    elif tech_signal and news_signal == "no signal":
        return tech_signal
    elif not tech_signal and news_signal in ["bullish", "bearish"]:
        return ("buy" if news_signal == "bullish" else "sell", None)
    return None


def check_opposite_news(current_side):
    global last_opposite_warning_time
    now = time.time()
    if now - last_opposite_warning_time < 300:
        return
    if last_news_signal == "bullish" and current_side == "sell":
        send_telegram_message("⚠️ You have opened SELL position but the news signal is opposite: BUY")
        last_opposite_warning_time = now
    elif last_news_signal == "bearish" and current_side == "buy":
        send_telegram_message("⚠️ You have opened BUY position but the news signal is opposite: SELL")
        last_opposite_warning_time = now


def start_news_loop():
    def loop():
        while True:
            is_news_signal()
            time.sleep(21600)  # 6 hours

    t = threading.Thread(target=loop, daemon=True)
    t.start()
