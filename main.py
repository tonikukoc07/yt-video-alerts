import os
import json
import time
import subprocess
import feedparser
import requests
from datetime import datetime
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "15"))
TZ = os.environ.get("TZ", "Europe/Madrid")

PIN_LATEST = os.environ.get("PIN_LATEST", "0") == "1"
FORCE_SEND = os.environ.get("FORCE_SEND", "0") == "1"

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
    return (s or "").replace("\u0000", "")


def fetch_entries(limit: int):
    feed = feedparser.parse(RSS_URL)
    entries = getattr(feed, "entries", []) or []
    return entries[: max(1, limit)]


def get_vid_from_entry(e):
    return getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None)


def get_thumb_from_entry(e):
    try:
        if hasattr(e, "media_thumbnail") and e.media_thumbnail:
            return e.media_thumbnail[0].get("url")
    except Exception:
        pass
    return None


def yt_is_live_now(video_url: str) -> bool:
    """
    Detecta LIVE REAL (ahora mismo) usando yt-dlp.
    Esto es mucho más fiable que RSS.
    """
    try:
        # -J: JSON info
        # --no-warnings para logs limpios
        out = subprocess.check_output(
            ["yt-dlp", "--no-warnings", "-J", video_url],
            stderr=subprocess.STDOUT,
            timeout=25,
            text=True,
        )
        info = json.loads(out)

        # Campos típicos:
        # is_live: true/false
        # live_status: "is_live" / "was_live" / "not_live" / ...
        is_live = bool(info.get("is_live", False))
        live_status = str(info.get("live_status", "")).lower()

        # Queremos SOLO si está en vivo ahora mismo
        if is_live:
            return True
        if live_status in ("is_live", "live"):
            return True
        return False

    except Exception as e:
        print("yt-dlp live check failed:", repr(e))
        return False


def format_local_time(dt_str: str) -> str:
    """
    RSS trae published en UTC ISO. Si falla, devuelve vacío.
    """
    try:
        # Ej: 2026-02-04T14:01:31+00:00 o 2026-02-04T14:00:54+00:00
        # feedparser suele dar e.published / e.published_parsed
        # Usamos published_parsed si está
        return ""
    except Exception:
        return ""


def send_to_telegram(bot: Bot, chat_id: int, item: dict, pin: bool = False):
    title = item["title"]
    link = item["link"]
    thumb = item.get("thumb")
    is_live = item.get("is_live", False)
    views = item.get("views")  # opcional
    when = item.get("when")    # opcional

    header = "🔴 DIRECTO" if is_live else "🎥 NUEVO VÍDEO"

    lines = [header, f"✨ {title}"]
    if views is not None:
        lines.append(f"👀 {views} views")
    if when:
        lines.append(f"🕒 {when}")
    lines.append(f"👉 {link}")

    caption = "\n".join(lines)

    sent_msg = None

    # Enviar con miniatura (si hay)
    if thumb:
        try:
            r = requests.get(thumb, timeout=10)
            r.raise_for_status()
            sent_msg = bot.send_photo(chat_id=chat_id, photo=r.content, caption=caption)
        except Exception as e:
            print("send_photo failed, fallback to text. Error:", repr(e))

    if sent_msg is None:
        sent_msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)

    # Fijar (pin) si procede
    if pin and sent_msg:
        try:
            bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id, disable_notification=True)
        except Exception as e:
            print("pin failed:", repr(e))


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    last_key = state.get("last_notified_key")  # guardamos "VID:<id>" o "LIVE:<id>"
    print("Loaded last_notified_key =", last_key)

    entries = fetch_entries(SCAN_LIMIT)
    if not entries:
        print("No entries in RSS.")
        return

    # Tomamos el más reciente del RSS
    e = entries[0]
    vid = get_vid_from_entry(e)
    title = safe_text(getattr(e, "title", "") or "")
    link = safe_text(getattr(e, "link", "") or "")
    thumb = get_thumb_from_entry(e)

    if not vid or not link:
        print("Missing vid/link in latest entry.")
        return

    # ✅ LIVE real (ahora mismo) con yt-dlp
    is_live_now = yt_is_live_now(link)

    # clave para evitar duplicados:
    # Si está live: LIVE:<id>
    # Si no: VID:<id>
    key = f"LIVE:{vid}" if is_live_now else f"VID:{vid}"

    print("Latest:", {"vid": vid, "title": title, "is_live_now": is_live_now, "key": key})

    # Primera ejecución sin estado: inicializa sin avisar (para no spamear lo viejo)
    if not last_key and not FORCE_SEND:
        state["last_notified_key"] = key
        save_state(state)
        print("Initialized state with", key, "(no notification)")
        return

    # Si ya se notificó y no forzamos, salir
    if key == last_key and not FORCE_SEND:
        print("Already notified:", key)
        return

    # Enviar y actualizar estado
    item = {
        "vid": vid,
        "title": title,
        "link": link,
        "thumb": thumb,
        "is_live": is_live_now,
        # Si quieres views, habría que leerlos desde yt-dlp también (lo puedo añadir luego)
    }

    # ✅ PIN: “Prioridad: si hay directo activo -> fíjalo; si no -> último vídeo”
    # Con esta lógica: siempre pinneamos lo que enviamos.
    pin_this = PIN_LATEST

    send_to_telegram(bot, chat_id, item, pin=pin_this)

    state["last_notified_key"] = key
    save_state(state)
    print("Notified and updated state to", key)


def main():
    run_once()


if __name__ == "__main__":
    main()
