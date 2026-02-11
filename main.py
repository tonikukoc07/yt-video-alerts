import os
import json
import time
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import requests
from telegram import Bot


# ========= ENV =========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")  # puede ser canal (-100...) o grupo
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

TZ_NAME = os.environ.get("TZ", "Europe/Madrid")
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "15"))

PIN_LATEST = os.environ.get("PIN_LATEST", "0") == "1"  # "1" para fijar automático
STATE_FILE = "state.json"

RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"


# ========= HELPERS =========
def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


def safe_text(s: str) -> str:
    # Evita caracteres raros; NO usamos parse_mode
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
    # feedparser a veces trae published_parsed
    if getattr(entry, "published_parsed", None):
        return datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=ZoneInfo("UTC"))
    if getattr(entry, "updated_parsed", None):
        return datetime.fromtimestamp(time.mktime(entry.updated_parsed), tz=ZoneInfo("UTC"))
    return None


def is_live_entry(entry) -> bool:
    """
    YouTube RSS es inconsistente:
    - A veces trae yt_livebroadcastcontent=live (raro)
    - Para directos suele servir heurística:
      si 'updated' cambia tras 'published' y el vídeo es reciente, suele ser live/estreno.
    Como tú haces "Parte X" en directo, lo más fiable es:
      - Si la diferencia updated - published es pequeña y es muy reciente -> tratar como DIRECTO.
    """
    lf = getattr(entry, "yt_livebroadcastcontent", None)
    if lf and str(lf).lower() == "live":
        return True

    pub_dt = parse_entry_datetime(entry)
    upd_dt = None
    if getattr(entry, "updated_parsed", None):
        upd_dt = datetime.fromtimestamp(time.mktime(entry.updated_parsed), tz=ZoneInfo("UTC"))

    # Si no hay fechas, no podemos inferir
    if not pub_dt:
        return False

    now_utc = datetime.now(tz=ZoneInfo("UTC"))
    age_minutes = (now_utc - pub_dt).total_seconds() / 60.0

    # Heurística: si se publicó hace <= 12h y el updated está muy cerca, lo consideramos directo
    if upd_dt:
        diff_minutes = abs((upd_dt - pub_dt).total_seconds() / 60.0)
        if age_minutes <= 12 * 60 and diff_minutes <= 30:
            return True

    return False


def extract_thumb(entry) -> str | None:
    # media_thumbnail suele venir como lista de dicts
    try:
        if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            url = entry.media_thumbnail[0].get("url")
            if url:
                return url
    except Exception:
        pass
    return None


def extract_views(entry) -> int | None:
    """
    A veces viene como:
    entry.media_statistics = {'views': '24'}
    """
    try:
        ms = getattr(entry, "media_statistics", None)
        if isinstance(ms, dict) and "views" in ms:
            return int(ms["views"])
    except Exception:
        pass
    return None


def fetch_entries(limit: int):
    feed = feedparser.parse(RSS_URL)
    return feed.entries[:limit] if getattr(feed, "entries", None) else []


def format_local_time(dt_utc: datetime, tz_name: str) -> str:
    local = dt_utc.astimezone(ZoneInfo(tz_name))
    return local.strftime("%d/%m %H:%M")


def download_bytes(url: str, timeout=10) -> bytes | None:
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


# ========= TELEGRAM ACTIONS =========
def send_post(bot: Bot, chat_id: int, item: dict) -> int:
    """
    Devuelve message_id del post publicado (para poder fijarlo).
    """
    title = item["title"]
    link = item["link"]
    thumb_url = item.get("thumb")
    views = item.get("views")
    is_live = item.get("is_live", False)
    published_utc = item.get("published_utc")  # datetime en UTC

    header = "🔴 DIRECTO" if is_live else "🎥 NUEVO VÍDEO"

    lines = [header, f"✨ {title}"]
    if isinstance(views, int):
        lines.append(f"👀 {views} views")
    if isinstance(published_utc, datetime):
        lines.append(f"🕒 {format_local_time(published_utc, TZ_NAME)}")
    lines.append(f"👉 {link}")

    caption = "\n".join(lines)

    # Preferimos foto + caption (queda pro y muestra miniatura)
    if thumb_url:
        photo_bytes = download_bytes(thumb_url)
        if photo_bytes:
            msg = bot.send_photo(chat_id=chat_id, photo=photo_bytes, caption=caption)
            return msg.message_id

    msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    return msg.message_id


def pin_message(bot: Bot, chat_id: int, message_id: int):
    """
    Fija el mensaje sin notificar.
    """
    bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)


# ========= PRIORIDAD: DIRECTO ACTIVO -> PIN; si no -> ÚLTIMO VÍDEO =========
def choose_item_for_pin(entries) -> dict | None:
    """
    - Si hay algún entry considerado DIRECTO (según heurística) -> el más reciente live
    - Si no -> el entry más reciente normal
    """
    if not entries:
        return None

    parsed = []
    for e in entries:
        vid = getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None) or ""
        title = safe_text(getattr(e, "title", "") or "")
        link = safe_text(getattr(e, "link", "") or "")

        published_utc = parse_entry_datetime(e)
        is_live = is_live_entry(e)
        thumb = extract_thumb(e)
        views = extract_views(e)

        parsed.append({
            "vid": vid,
            "title": title,
            "link": link,
            "thumb": thumb,
            "views": views,
            "is_live": is_live,
            "published_utc": published_utc,
        })

    # Orden por publicado (más nuevo primero)
    def sort_key(x):
        dt = x.get("published_utc")
        return dt.timestamp() if isinstance(dt, datetime) else 0

    parsed.sort(key=sort_key, reverse=True)

    # Prioridad live
    lives = [x for x in parsed if x.get("is_live")]
    return lives[0] if lives else parsed[0]


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    last_notified_vid = state.get("last_notified_vid")  # evita duplicados
    last_pinned_vid = state.get("last_pinned_vid")      # controla el pin

    entries = fetch_entries(limit=SCAN_LIMIT)
    if not entries:
        print("No RSS entries.")
        return

    # 1) Detectar el más reciente para NOTIFICAR (si cambió)
    e0 = entries[0]
    latest_vid = getattr(e0, "yt_videoid", None) or getattr(e0, "yt_videoId", None) or ""
    latest_title = safe_text(getattr(e0, "title", "") or "")
    latest_link = safe_text(getattr(e0, "link", "") or "")

    latest_item = {
        "vid": latest_vid,
        "title": latest_title,
        "link": latest_link,
        "thumb": extract_thumb(e0),
        "views": extract_views(e0),
        "is_live": is_live_entry(e0),
        "published_utc": parse_entry_datetime(e0),
    }

    print("Loaded last_notified_vid =", last_notified_vid)
    print("Latest vid =", latest_vid, "title =", latest_title)

    # Primer run: inicializa y NO spamea
    if not last_notified_vid:
        state["last_notified_vid"] = latest_vid
        state["last_pinned_vid"] = state.get("last_pinned_vid") or ""
        save_state(state)
        print("Initialized last_notified_vid (no notification).")
    else:
        if latest_vid and latest_vid != last_notified_vid:
            msg_id = send_post(bot, chat_id, latest_item)
            state["last_notified_vid"] = latest_vid
            print("Notified. message_id =", msg_id)
        else:
            print("No new item to notify.")

    # 2) PIN: Prioridad DIRECTO si hay, si no último vídeo
    if PIN_LATEST:
        pin_item = choose_item_for_pin(entries)
        if pin_item:
            pin_vid = pin_item.get("vid", "")
            print("Pin candidate:", pin_vid, "| live:", pin_item.get("is_live"))

            # solo repinear si cambió el vídeo fijado (evita pin inútil)
            if pin_vid and pin_vid != last_pinned_vid:
                msg_id = send_post(bot, chat_id, pin_item)
                pin_message(bot, chat_id, msg_id)
                state["last_pinned_vid"] = pin_vid
                print("Pinned new item. message_id =", msg_id, "vid =", pin_vid)
            else:
                print("Pin unchanged. Skipping.")

    save_state(state)


def main():
    run_once()


if __name__ == "__main__":
    main()
