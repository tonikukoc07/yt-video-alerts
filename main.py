import os
import json
import feedparser
import requests
from telegram import Bot
import yt_dlp
from datetime import datetime
import pytz

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "10"))
STATE_FILE = "state.json"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def get_video_info(video_url):
    ydl_opts = {
        "quiet": True,
        "skip_download": True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        return {
            "is_live": info.get("is_live", False),
            "view_count": info.get("view_count", 0),
        }

def fetch_latest():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return None
    e = feed.entries[0]
    return {
        "vid": e.yt_videoid,
        "title": e.title,
        "link": e.link,
        "thumb": e.media_thumbnail[0]["url"] if hasattr(e, "media_thumbnail") else None
    }

def send(bot, chat_id, item):
    info = get_video_info(item["link"])
    is_live = info["is_live"]
    views = info["view_count"]

    icon = "🔴 DIRECTO" if is_live else "🎥 NUEVO VÍDEO"

    now = datetime.now(pytz.timezone("Europe/Madrid")).strftime("%d/%m %H:%M")

    caption = (
        f"{icon}\n"
        f"✨ {item['title']}\n"
        f"👀 {views} views\n"
        f"🕒 {now}\n"
        f"👉 {item['link']}"
    )

    if item["thumb"]:
        img = requests.get(item["thumb"]).content
        msg = bot.send_photo(chat_id=chat_id, photo=img, caption=caption)
    else:
        msg = bot.send_message(chat_id=chat_id, text=caption)

    bot.pin_chat_message(chat_id, msg.message_id)

def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    last_id = state.get("last_video_id")

    latest = fetch_latest()
    if not latest:
        return

    if not last_id:
        state["last_video_id"] = latest["vid"]
        save_state(state)
        return

    if latest["vid"] != last_id:
        send(bot, chat_id, latest)
        state["last_video_id"] = latest["vid"]
        save_state(state)

if __name__ == "__main__":
    main()
