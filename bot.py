import os
import feedparser
from telegram import Bot

# ==============================
# VARIABLES DE ENTORNO (Secrets)
# ==============================

# 🔴 NO pongas aquí el token, va en GitHub Secrets
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")  # 🔴 PON TU TOKEN EN SECRETS

# ✅ TUS DATOS
CHAT_ID = os.environ.get("CHAT_ID", "-5174186701")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "UC6efY3r4Oiy0ns4ZEAVw4_A")

RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# ==============================
# FUNCIONES
# ==============================

def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")

def fetch_latest():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return None

    entry = feed.entries[0]
    video_id = getattr(entry, "yt_videoid", None)
    title = getattr(entry, "title", "")
    link = getattr(entry, "link", "")

    return video_id, title, link

def main():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)

    latest = fetch_latest()
    if not latest:
        print("No videos found")
        return

    video_id, title, link = latest

    message = (
        "🎬 **Nuevo contenido en YouTube**\n\n"
        f"{title}\n"
        f"{link}"
    )

    bot.send_message(
        chat_id=int(CHAT_ID),
        text=message,
        parse_mode="Markdown"
    )

    print("Mensaje enviado:", video_id)

# ==============================
# MAIN
# ==============================

if __name__ == "__main__":
    main()
