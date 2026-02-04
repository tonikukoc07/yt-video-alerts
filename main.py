import os
import re
import pathlib
import feedparser
import requests
from telegram import Bot

# ===== Secrets / env =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# Archivo de estado (lo persistimos con GitHub Actions Cache)
STATE_DIR = pathlib.Path(".state")
STATE_FILE = STATE_DIR / "last_video_id.txt"


def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


def read_last_id():
    try:
        return STATE_FILE.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def write_last_id(video_id: str):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(video_id, encoding="utf-8")


def fetch_latest_entry():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return None
    return feed.entries[0]


def get_video_fields(entry):
    vid = getattr(entry, "yt_videoid", None)
    title = getattr(entry, "title", "")
    link = getattr(entry, "link", "")

    # Miniatura (si viene en el feed)
    thumb = None
    try:
        if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            thumb = entry.media_thumbnail[0].get("url")
    except Exception:
        pass

    return vid, title, link, thumb


def is_live_video(video_id: str) -> bool:
    """
    YouTube RSS no trae un campo fiable para "LIVE" en todos los casos.
    Hacemos heurística leyendo el HTML del watch y buscando flags típicas.
    """
    if not video_id:
        return False

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        html = r.text

        # Señales habituales en el HTML/JSON incrustado
        patterns = [
            r'"isLiveContent"\s*:\s*true',
            r'"isLive"\s*:\s*true',
            r'"liveBroadcastDetails"',
            r'"status"\s*:\s*"LIVE"',
        ]
        return any(re.search(p, html) for p in patterns)
    except Exception:
        return False


def main():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)

    entry = fetch_latest_entry()
    if not entry:
        print("No entries in RSS")
        return

    vid, title, link, thumb = get_video_fields(entry)

    last_id = read_last_id()
    print("Last ID:", last_id)
    print("Latest ID:", vid)

    # ✅ 1) Evitar duplicados + ✅ 3) Guardar último avisado
    if not vid or vid == last_id:
        print("Nothing new. Skipping.")
        return

    live = is_live_video(vid)
    tag = "🔴 DIRECTO" if live else "🎥 Nuevo vídeo"

    # ✅ 5) Formato bonito
    msg = (
        f"{tag}\n\n"
        f"✨ {title}\n\n"
        f"👉 {link}"
    )

    # ✅ 4) Enviar miniatura si existe, si no texto normal
    try:
        if thumb:
            bot.send_photo(
                chat_id=int(CHAT_ID),
                photo=thumb,
                caption=msg,
                parse_mode=None
            )
        else:
            bot.send_message(
                chat_id=int(CHAT_ID),
                text=msg,
                parse_mode=None,
                disable_web_page_preview=False
            )
    except Exception as e:
        print("Telegram send failed, fallback to text. Error:", repr(e))
        bot.send_message(
            chat_id=int(CHAT_ID),
            text=msg,
            parse_mode=None
        )

    write_last_id(vid)
    print("Sent & saved last id:", vid)


if __name__ == "__main__":
    main()
