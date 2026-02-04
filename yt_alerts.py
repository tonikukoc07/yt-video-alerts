import os
import time
import feedparser
from telegram import Bot

# 🔴🔴🔴 AQUÍ NO ESCRIBAS EL TOKEN DIRECTO 🔴🔴🔴
# El token SIEMPRE va en Railway → Variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")  # <<< AQUI PONES TU TOKEN (EN RAILWAY) >>>

# ✅ ESTE ES TU CHAT_ID (grupo Cacharrear con Juan)
CHAT_ID = os.environ.get("CHAT_ID", "-5174186701")

# ✅ ESTE ES TU CANAL DE YOUTUBE
CHANNEL_ID = os.environ.get("CHANNEL_ID", "UC6efY3r4Oiy0ns4ZEAVw4_A")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")

def fetch_latest():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return None
    e = feed.entries[0]
    vid = getattr(e, "yt_videoid", None)
    title = getattr(e, "title", "")
    link = getattr(e, "link", "")
    return vid, title, link

def main():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)

    last_sent = None
    first_run = True

    print("Bot started. Polling RSS:", RSS_URL)

    while True:
        try:
            latest = fetch_latest()
            if latest:
                vid, title, link = latest
                if first_run:
                    last_sent = vid
                    first_run = False
                    print("Initialized last_sent =", last_sent)
                else:
                    if vid and vid != last_sent:
                        msg = f"🎬 Nuevo en YouTube:\n{title}\n{link}"
                        bot.send_message(chat_id=int(CHAT_ID), text=msg)
                        print("Sent:", vid)
                        last_sent = vid
        except Exception as e:
            print("Error:", repr(e))

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
