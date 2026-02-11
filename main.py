import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import requests
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

TZ_NAME = os.environ.get("TZ", "Europe/Madrid")
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "15"))
PIN_LATEST = os.environ.get("PIN_LATEST", "0") == "1"

STATE_FILE = "state.json"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"


def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


def safe_text(s: str) -> str:
    return (s or "").replace("\u0000", "").strip()


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


def parse_entry_datetime(entry) -> datetime | None:
    if getattr(entry, "published_parsed", None):
        return datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=ZoneInfo("UTC"))
    if getattr(entry, "updated_parsed", None):
        return datetime.fromtimestamp(time.mktime(entry.updated_parsed), tz=ZoneInfo("UTC"))
    return None


def format_local_time(dt_utc: datetime) -> str:
    local = dt_utc.astimezone(ZoneInfo(TZ_NAME))
    return local.strftime("%d/%m %H:%M")


def extract_thumb(entry) -> str | None:
    try:
        if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            url = entry.media_thumbnail[0].get("url")
            if url:
                return url
    except Exception:
        pass
    return None


def download_bytes(url: str, timeout=10) -> bytes | None:
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


# ✅ LIVE REAL: SOLO si está EN VIVO AHORA MISMO
def is_live_now(video_url: str) -> bool:
    if not video_url:
        return False
    try:
        r = requests.get(video_url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text

        # Señales fiables para "en vivo ahora"
        if '"isLiveNow":true' in html:
            return True
        if '"status":"LIVE"' in html:
            return True

        # Si está ended/offline, seguro que NO es live
        if '"status":"ENDED"' in html:
            return False
        if '"LIVE_STREAM_OFFLINE"' in html:
            return False
        if '"isLiveNow":false' in html:
            return False

        return False
    except Exception:
        # Si falla la petición, mejor NO marcar live (evita falsos positivos)
        return False


def build_caption(kind: str, item: dict) -> str:
    header = "🔴 DIRECTO" if kind == "live" else "🎥 NUEVO VÍDEO"
    lines = [header, f"✨ {item['title']}"]
    if isinstance(item.get("published_utc"), datetime):
        lines.append(f"🕒 {format_local_time(item['published_utc'])}")
    lines.append(f"👉 {item['link']}")
    return "\n".join(lines)


def send_post(bot: Bot, chat_id: int, kind: str, item: dict) -> int:
    caption = build_caption(kind, item)
    thumb_url = item.get("thumb")

    if thumb_url:
        b = download_bytes(thumb_url)
        if b:
            msg = bot.send_photo(chat_id=chat_id, photo=b, caption=caption)
            return msg.message_id

    msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    return msg.message_id


def pin_message(bot: Bot, chat_id: int, message_id: int):
    bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)


def parse_items(entries):
    items = []
    for e in entries:
        vid = getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None) or ""
        if not vid:
            continue
        items.append({
            "vid": vid,
            "title": safe_text(getattr(e, "title", "") or ""),
            "link": safe_text(getattr(e, "link", "") or ""),
            "thumb": extract_thumb(e),
            "published_utc": parse_entry_datetime(e),
        })
    # más reciente primero
    def key_dt(x):
        dt = x.get("published_utc")
        return dt.timestamp() if isinstance(dt, datetime) else 0
    items.sort(key=key_dt, reverse=True)
    return items


def choose_pin_target(items):
    # Prioridad B: si hay live activo -> ese; si no -> el más reciente
    lives = []
    for it in items:
        if is_live_now(it["link"]):
            lives.append(it)
    if lives:
        return "live", lives[0]
    return "video", items[0]


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    last_notified_vid = state.get("last_notified_vid")  # para avisos
    last_pinned_vid = state.get("last_pinned_vid")      # para pin
    last_pinned_kind = state.get("last_pinned_kind")    # "live"/"video"

    feed = feedparser.parse(RSS_URL)
    entries = feed.entries[:SCAN_LIMIT] if getattr(feed, "entries", None) else []
    items = parse_items(entries)

    if not items:
        print("No items.")
        return

    # --- Notificación (nuevo contenido según el primero del feed) ---
    latest = items[0]
    latest_kind = "live" if is_live_now(latest["link"]) else "video"

    if not last_notified_vid:
        state["last_notified_vid"] = latest["vid"]
        save_state(state)
        print("Initialized (no notify).")
    else:
        if latest["vid"] != last_notified_vid:
            send_post(bot, chat_id, latest_kind, latest)
            state["last_notified_vid"] = latest["vid"]
            save_state(state)
            print("Notified:", latest["vid"])

    # --- Pin (Prioridad B) ---
    if PIN_LATEST:
        pin_kind, pin_item = choose_pin_target(items)
        pin_vid = pin_item["vid"]

        # ✅ Si cambió el vídeo fijado, o si es el MISMO vídeo pero cambió live->video (o al revés),
        # reposteamos y fijamos para que el icono cambie.
        needs_repin = (pin_vid != last_pinned_vid) or (pin_kind != last_pinned_kind)

        if needs_repin:
            msg_id = send_post(bot, chat_id, pin_kind, pin_item)
            pin_message(bot, chat_id, msg_id)
            state["last_pinned_vid"] = pin_vid
            state["last_pinned_kind"] = pin_kind
            save_state(state)
            print("Pinned:", pin_vid, "kind:", pin_kind, "msg:", msg_id)
        else:
            print("Pin unchanged.")


if __name__ == "__main__":
    run_once()
