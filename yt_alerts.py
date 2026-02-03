import time
import feedparser
import os
from telegram import Bot

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]

POLL_SECONDS = 120
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

bot = Bot(token=TELEGRAM_TOKEN)

def get_latest():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return None
    e = feed.entries[0]
    return e.yt_videoid, e.title, e.link

def main():
    last_sent = None
    first_run = True

    while True:
        try:
            latest = get_latest()
            if latest:
                vid, title, link = latest
                if first_run:
                    last_sent = vid
                    first_run = False
                elif vid != last_sent:
                    bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"🎬 Nuevo en YouTube:\n{title}\n{link}"
                    )
                    last_sent = vid
        except Exception as e:
            print("Error:", e)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
