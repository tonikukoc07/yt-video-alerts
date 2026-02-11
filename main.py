import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import requests
from telegram import Bot

# ========= ENV =========
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

        # SOLO señales fiables
        if '"isLiveNow":true' in html:
            return True
        if '"status":"LIVE"' in html:
            return True

        # señales de NO live
        if '"status":"ENDED"' in html:
            return False
        if '"LIVE_STREAM_OFFLINE"' in html:
            return False
        if '"isLiveNow":false' in html:
            return False

        return False
    except Exception:
        # si falla, no marcamos live
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
        link = safe_text(getattr(e, "link", "") or "")
        items.append({
            "vid": vid,
            "title": safe_text(getattr(e, "title", "") or ""),
            "link": link,
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
    for it in items:
        if is_live_now(it["link"]):
            return "live", it
    return "video", items[0]


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()

    # Estados para NOTIFY
    last_notified_vid = state.get("last_notified_vid")
    last_notified_kind = state.get("last_notified_kind")

    # Estados para PIN
    last_pinned_vid = state.get("last_pinned_vid")
    last_pinned_kind = state.get("last_pinned_kind")
    last_pinned_message_id = state.get("last_pinned_message_id")

    feed = feedparser.parse(RSS_URL)
    entries = feed.entries[:SCAN_LIMIT] if getattr(feed, "entries", None) else []
    items = parse_items(entries)

    if not items:
        print("No items.")
        return

    # --- NOTIFICACIÓN (si cambia vid o cambia estado con mismo vid) ---
    latest = items[0]
    latest_kind = "live" if is_live_now(latest["link"]) else "video"

    print("Latest:", latest["vid"], "kind:", latest_kind, "title:", latest["title"])
    print("State notify:", last_notified_vid, last_notified_kind)

    if not last_notified_vid:
        # primer run: inicializa sin avisar (evita spam)
        state["last_notified_vid"] = latest["vid"]
        state["last_notified_kind"] = latest_kind
        save_state(state)
        print("Initialized (no notify).")
    else:
        notify_needed = (latest["vid"] != last_notified_vid) or (
            latest["vid"] == last_notified_vid and latest_kind != last_notified_kind
        )

        if notify_needed:
            msg_id = send_post(bot, chat_id, latest_kind, latest)
            state["last_notified_vid"] = latest["vid"]
            state["last_notified_kind"] = latest_kind
            print("Notified msg_id:", msg_id)
            # Guardamos el id del mensaje si lo pinneamos luego
            last_sent_message_id = msg_id
        else:
            print("No new item to notify.")
            last_sent_message_id = None

    # --- PIN (Prioridad B) ---
    if PIN_LATEST:
        pin_kind, pin_item = choose_pin_target(items)
        pin_vid = pin_item["vid"]

        print("Pin candidate:", pin_vid, pin_kind)
        print("State pin:", last_pinned_vid, last_pinned_kind, last_pinned_message_id)

        # Si cambia el objetivo del pin (o cambia kind), repin
        pin_needed = (pin_vid != last_pinned_vid) or (pin_kind != last_pinned_kind)

        if pin_needed:
            # ✅ Importante: si JUSTO hemos publicado ese mismo item en NOTIFY,
            # reutilizamos ese message_id para fijar y NO repostear.
            if last_sent_message_id and pin_vid == state.get("last_notified_vid") and pin_kind == state.get("last_notified_kind"):
                msg_id_to_pin = last_sent_message_id
            else:
                msg_id_to_pin = send_post(bot, chat_id, pin_kind, pin_item)

            pin_message(bot, chat_id, msg_id_to_pin)

            state["last_pinned_vid"] = pin_vid
            state["last_pinned_kind"] = pin_kind
            state["last_pinned_message_id"] = msg_id_to_pin
            print("Pinned msg_id:", msg_id_to_pin)
        else:
            print("Pin unchanged.")

    save_state(state)


if __name__ == "__main__":
    run_once()
