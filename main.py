import os
import json
import time
import feedparser
import requests
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "0"))  # en Actions no lo usamos (cron manda)
STATE_FILE = "state.json"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"


def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def safe_text(s: str) -> str:
    # evita problemas raros con entidades; NO usamos parse_mode
    return (s or "").replace("\u0000", "")


def fetch_latest_entry():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return None

    e = feed.entries[0]

    # video id
    vid = getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None)

    title = safe_text(getattr(e, "title", "") or "")
    link = safe_text(getattr(e, "link", "") or "")

    # thumbnail (normalmente viene en media_thumbnail)
    thumb = None
    try:
        if hasattr(e, "media_thumbnail") and e.media_thumbnail:
            thumb = e.media_thumbnail[0].get("url")
    except Exception:
        thumb = None

    # detectar live si el feed lo trae (a veces no aparece)
    live_flag = None
    try:
        # algunos feeds lo traen como yt_livebroadcastcontent / ytc...
        live_flag = getattr(e, "yt_livebroadcastcontent", None)
    except Exception:
        live_flag = None

    is_live = (str(live_flag).lower() == "live")

    return {
        "vid": vid,
        "title": title,
        "link": link,
        "thumb": thumb,
        "is_live": is_live,
    }


def send_to_telegram(bot: Bot, chat_id: int, item: dict):
    title = item["title"]
    link = item["link"]
    thumb = item["thumb"]
    is_live = item["is_live"]

    header = "🔴 DIRECTO" if is_live else "🎥 NUEVO VÍDEO"
    caption = f"{header}\n✨ {title}\n👉 {link}"

    # Intentar enviar con miniatura (queda “pro”)
    if thumb:
        try:
            # telegram necesita un “file-like” o URL descargada; descargamos rápido
            r = requests.get(thumb, timeout=10)
            r.raise_for_status()
            bot.send_photo(chat_id=chat_id, photo=r.content, caption=caption)
            return
        except Exception as e:
            print("send_photo failed, fallback to text. Error:", repr(e))

    # fallback a texto
    bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    last_vid = state.get("last_video_id")
    print("Loaded last_video_id =", last_vid)

    latest = fetch_latest_entry()
    if not latest:
        print("No entries in RSS.")
        return

    vid = latest["vid"]
    print("Latest vid =", vid, "title =", latest["title"])

    # 1) Si es el primer run SIN state, inicializa y NO avisa (para no spamear con lo antiguo)
    if not last_vid:
        state["last_video_id"] = vid
        save_state(state)
        print("Initialized state with last_video_id =", vid, "(no notification)")
        return

    # 2) Si cambió -> notifica
    if vid and vid != last_vid:
        send_to_telegram(bot, chat_id, latest)
        state["last_video_id"] = vid
        save_state(state)
        print("Notified and updated state to", vid)
    else:
        print("No new video/directo. Skipping.")


def main():
    # En GitHub Actions: ejecutar una vez y salir
    run_once()

    # Si algún día lo ejecutas en Railway/servidor, puedes activar loop:
    if POLL_SECONDS and POLL_SECONDS > 0:
        while True:
            time.sleep(POLL_SECONDS)
            run_once()


if __name__ == "__main__":
    main()
