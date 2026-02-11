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
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

TZ_NAME = os.environ.get("TZ", "Europe/Madrid")
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "25"))
PIN_LATEST = os.environ.get("PIN_LATEST", "0") == "1"

STATE_FILE = "state.json"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"


# ========= HELPERS =========
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


def format_local_time(dt_utc: datetime, tz_name: str) -> str:
    local = dt_utc.astimezone(ZoneInfo(tz_name))
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


def extract_views(entry) -> int | None:
    try:
        ms = getattr(entry, "media_statistics", None)
        if isinstance(ms, dict) and "views" in ms:
            return int(ms["views"])
    except Exception:
        pass

    # fallback: buscar views en string (por si feedparser lo mapea raro)
    try:
        raw = str(entry)
        m = re.search(r'views["\']\s*[:=]\s*["\']?(\d+)', raw)
        if m:
            return int(m.group(1))
    except Exception:
        pass

    return None


# ========= LIVE DETECTION (REAL) =========
def is_live_now(video_url: str) -> bool:
    """
    🔥 Lo importante:
    - Solo True si el vídeo está EN VIVO ahora mismo.
    - Si ya terminó, devuelve False.
    """
    if not video_url:
        return False
    try:
        r = requests.get(video_url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text

        # Señales típicas en el JSON embebido
        true_signals = [
            '"isLiveNow":true',
            '"isLiveContent":true',
            '"LIVE_NOW"',
            '"status":"LIVE"',
        ]
        false_signals = [
            '"isLiveNow":false',
            '"status":"ENDED"',
            '"LIVE_STREAM_OFFLINE"',
        ]

        has_true = any(s in html for s in true_signals)
        has_false = any(s in html for s in false_signals)

        return has_true and not has_false
    except Exception:
        # Si falla la petición, mejor NO marcarlo como live (evita falsos positivos)
        return False


# ========= TELEGRAM =========
def download_bytes(url: str, timeout=10) -> bytes | None:
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def build_caption(kind: str, item: dict) -> str:
    header = "🔴 DIRECTO" if kind == "live" else "🎥 NUEVO VÍDEO"

    lines = [header, f"✨ {item['title']}"]

    if isinstance(item.get("views"), int):
        lines.append(f"👀 {item['views']} views")

    if isinstance(item.get("published_utc"), datetime):
        lines.append(f"🕒 {format_local_time(item['published_utc'], TZ_NAME)}")

    lines.append(f"👉 {item['link']}")
    return "\n".join(lines)


def send_post(bot: Bot, chat_id: int, kind: str, item: dict) -> int:
    caption = build_caption(kind, item)
    thumb_url = item.get("thumb")

    if thumb_url:
        photo_bytes = download_bytes(thumb_url)
        if photo_bytes:
            msg = bot.send_photo(chat_id=chat_id, photo=photo_bytes, caption=caption)
            return msg.message_id

    msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    return msg.message_id


def pin_message(bot: Bot, chat_id: int, message_id: int):
    bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)


# ========= CORE LOGIC (Prioridad B) =========
def parse_entries(entries):
    parsed = []
    for e in entries:
        vid = getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None) or ""
        if not vid:
            continue
        title = safe_text(getattr(e, "title", "") or "")
        link = safe_text(getattr(e, "link", "") or "")
        parsed.append({
            "vid": vid,
            "title": title,
            "link": link,
            "thumb": extract_thumb(e),
            "views": extract_views(e),
            "published_utc": parse_entry_datetime(e),
        })
    return parsed


def choose_pin_target(items):
    """
    Regla B:
    - Si hay DIRECTO activo (live_now=True) -> fijar ese (el más reciente por published)
    - Si no -> fijar el último vídeo (más reciente por published)
    """
    if not items:
        return None

    def key_dt(x):
        dt = x.get("published_utc")
        return dt.timestamp() if isinstance(dt, datetime) else 0

    items_sorted = sorted(items, key=key_dt, reverse=True)

    # Buscar directos reales (live now)
    lives = []
    for it in items_sorted:
        if is_live_now(it["link"]):
            lives.append(it)

    if lives:
        return ("live", lives[0])

    return ("video", items_sorted[0])


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    last_notified_vid = state.get("last_notified_vid")
    last_pinned_vid = state.get("last_pinned_vid")

    feed = feedparser.parse(RSS_URL)
    entries = feed.entries[:SCAN_LIMIT] if getattr(feed, "entries", None) else []
    if not entries:
        print("No RSS entries.")
        return

    items = parse_entries(entries)
    if not items:
        print("No valid items.")
        return

    # --- Notificación: si cambió el entry[0], avisamos ---
    latest = items[0]
    latest_vid = latest["vid"]
    latest_kind = "live" if is_live_now(latest["link"]) else "video"

    print("Loaded last_notified_vid =", last_notified_vid)
    print("Latest vid =", latest_vid, "| kind =", latest_kind, "| title =", latest["title"])

    # Primer run: inicializa sin spamear
    if not last_notified_vid:
        state["last_notified_vid"] = latest_vid
        save_state(state)
        print("Initialized state (no notification).")
    else:
        if latest_vid != last_notified_vid:
            send_post(bot, chat_id, latest_kind, latest)
            state["last_notified_vid"] = latest_vid
            print("Notified new:", latest_vid)
        else:
            print("No new item to notify.")

    # --- Pin: Prioridad B (directo activo > último vídeo) ---
    if PIN_LATEST:
        pin_kind, pin_item = choose_pin_target(items)
        pin_vid = pin_item["vid"]

        print("Pin candidate:", pin_vid, "| kind =", pin_kind)

        if pin_vid != last_pinned_vid:
            msg_id = send_post(bot, chat_id, pin_kind, pin_item)
            pin_message(bot, chat_id, msg_id)
            state["last_pinned_vid"] = pin_vid
            print("Pinned:", pin_vid, "message_id =", msg_id)
        else:
            print("Pin unchanged.")

    save_state(state)


if __name__ == "__main__":
    run_once()
