import os
import feedparser
from telegram import Bot

# ===== VARIABLES DE ENTORNO (GitHub Secrets) =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"


def must_env(name, value):
    if not value:
        raise RuntimeError(f"❌ Missing env var: {name}")


def fetch_latest_video():
    feed = feedparser.parse(RSS_URL)

    if not feed.entries:
        return None

    entry = feed.entries[0]
    video_id = getattr(entry, "yt_videoid", None)
    title = getattr(entry, "title", "")
    link = getattr(entry, "link", "")

    return video_id, title, link


def main():
    # Validar secrets
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)

    latest = fetch_latest_video()
    if not latest:
        print("⚠️ No videos found")
        return

    video_id, title, link = latest

    msg = (
        "🎬 Nuevo en YouTube\n\n"
        f"{title}\n\n"
        f"{link}"
    )

    bot.send_message(
        chat_id=int(CHAT_ID),
        text=msg,
        parse_mode=None  # 🔑 evita TODOS los errores de Telegram
    )

    print("✅ Mensaje enviado:", video_id)


if __name__ == "__main__":
    main()
