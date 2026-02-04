import os
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import Bot
from telegram.error import BadRequest

STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")          # Canal: -100xxxxxxxxxx
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")    # YouTube channel_id
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))

RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# Namespaces del feed de YouTube
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

MADRID_TZ = ZoneInfo("Europe/Madrid")


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


def parse_iso_dt(s: str) -> datetime | None:
    if not s:
        return None
    # YouTube viene tipo: 2026-02-04T14:01:31+00:00
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def fetch_latest_from_rss():
    """
    Devuelve dict con:
      video_id, title, link, thumb, views, published_utc, live_status
    live_status: "live" | "upcoming" | "none" | ""
    """
    r = requests.get(RSS_URL, timeout=20)
    r.raise_for_status()

    root = ET.fromstring(r.text)

    entry = root.find("atom:entry", NS)
    if entry is None:
        return None

    video_id = (entry.findtext("yt:videoId", default="", namespaces=NS) or "").strip()
    title = (entry.findtext("atom:title", default="", namespaces=NS) or "").strip()
    link_el = entry.find("atom:link[@rel='alternate']", NS)
    link = (link_el.get("href") if link_el is not None else "").strip()

    published = (entry.findtext("atom:published", default="", namespaces=NS) or "").strip()
    published_utc = parse_iso_dt(published)

    # liveBroadcastContent suele venir en directos / upcoming
    live_status = (entry.findtext("yt:liveBroadcastContent", default="", namespaces=NS) or "").strip().lower()
    # valores típicos: live / upcoming / none
    if live_status not in ("live", "upcoming", "none"):
        live_status = ""

    # miniatura + vistas (media:group)
    thumb = ""
    views = ""
    media_group = entry.find("media:group", NS)
    if media_group is not None:
        thumb_el = media_group.find("media:thumbnail", NS)
        if thumb_el is not None:
            thumb = (thumb_el.get("url") or "").strip()

        stats_el = media_group.find("media:community/media:statistics", NS)
        if stats_el is not None:
            views = (stats_el.get("views") or "").strip()

    return {
        "video_id": video_id,
        "title": title,
        "link": link,
        "thumb": thumb,
        "views": views,
        "published_utc": published_utc,
        "live_status": live_status,
    }


def format_message(data: dict):
    # Hora España
    published_local_str = ""
    if data.get("published_utc"):
        dt_local = data["published_utc"].astimezone(MADRID_TZ)
        published_local_str = dt_local.strftime("%d/%m %H:%M")

    is_live = (data.get("live_status") == "live")
    header = "🔴 DIRECTO" if is_live else "🎥 NUEVO VÍDEO"

    # vistas (si vienen)
    views = data.get("views")
    views_line = f"👀 {views} views\n" if views else ""

    # Mensaje final (sin parse_mode para evitar errores por caracteres raros)
    msg = (
        f"{header}\n"
        f"✨ {data.get('title','')}\n"
        f"{views_line}"
        f"🕒 {published_local_str}\n"
        f"👉 {data.get('link','')}"
    ).strip()

    return msg, is_live


def safe_unpin(bot: Bot, chat_id: int, message_id: int | None):
    if not message_id:
        return
    try:
        bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        # si ya no existe / no se puede, lo ignoramos
        pass


def safe_pin(bot: Bot, chat_id: int, message_id: int):
    try:
        bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except Exception:
        # si falta permiso de fijar en el canal, no rompemos el bot
        pass


def send_post(bot: Bot, chat_id: int, msg: str, thumb_url: str):
    """
    Intenta enviar con foto (miniatura).
    Si falla, fallback a texto.
    Devuelve message_id del post (si se pudo).
    """
    try:
        if thumb_url:
            m = bot.send_photo(chat_id=chat_id, photo=thumb_url, caption=msg, parse_mode=None)
            return m.message_id
        else:
            m = bot.send_message(chat_id=chat_id, text=msg, parse_mode=None)
            return m.message_id
    except BadRequest as e:
        # fallback por si Telegram se queja de la foto/caption
        try:
            m = bot.send_message(chat_id=chat_id, text=msg, parse_mode=None)
            return m.message_id
        except Exception:
            raise e


def main():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    last_video_id = state.get("last_video_id")
    last_pinned_message_id = state.get("last_pinned_message_id")

    latest = fetch_latest_from_rss()
    if not latest or not latest.get("video_id"):
        print("No entries in RSS.")
        return

    vid = latest["video_id"]

    # ✅ Evitar duplicados
    if vid == last_video_id:
        print("No new video. last_video_id =", last_video_id)
        return

    msg, is_live = format_message(latest)

    # ✅ Publicar + miniatura
    print("New item detected:", vid, "| live:", is_live)
    message_id = send_post(bot, chat_id, msg, latest.get("thumb", ""))

    # ✅ Pin automático del último aviso (desfija el anterior)
    safe_unpin(bot, chat_id, last_pinned_message_id)
    safe_pin(bot, chat_id, message_id)

    # ✅ Guardar estado
    state["last_video_id"] = vid
    state["last_pinned_message_id"] = message_id
    save_state(state)

    print("Posted OK. message_id =", message_id)


if __name__ == "__main__":
    main()
