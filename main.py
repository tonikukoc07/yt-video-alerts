import os
import json
import time
import feedparser
import requests
from datetime import datetime
from telegram import Bot

# --- ENV ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")  # YouTube Channel ID (UCxxxx)
YT_API_KEY = os.environ.get("YT_API_KEY", "")  # Google API key (YouTube Data API v3)

TZ = os.environ.get("TZ", "Europe/Madrid")
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "15"))  # cuantos items del feed revisar
PIN_LATEST = os.environ.get("PIN_LATEST", "1") == "1"  # fijar prioridad: directo si hay, si no último vídeo

STATE_FILE = "state.json"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# --- Helpers ---
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

def parse_yt_datetime(dt_str: str) -> datetime | None:
    # YouTube Atom: 2026-02-04T14:01:31+00:00
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        return None

def fmt_local_time(iso_str: str) -> str:
    # Formato simple: DD/MM HH:MM (hora local del runner; con TZ en workflow suele bastar)
    dt = parse_yt_datetime(iso_str)
    if not dt:
        return ""
    try:
        # el runner ya usa TZ si la exportas en workflow (TZ=Europe/Madrid)
        return dt.astimezone().strftime("%d/%m %H:%M")
    except Exception:
        return dt.strftime("%d/%m %H:%M")

# --- YouTube Data API ---
def yt_api_video_details(video_id: str) -> dict:
    """
    Devuelve:
      - live_status: live/none/upcoming
      - concurrent_viewers: int|None (solo si live)
      - title (por si acaso)
    """
    if not video_id:
        return {}

    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,liveStreamingDetails",
        "id": video_id,
        "key": YT_API_KEY,
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    items = data.get("items", [])
    if not items:
        return {}

    it = items[0]
    snippet = it.get("snippet", {}) or {}
    live = it.get("liveStreamingDetails", {}) or {}

    live_status = snippet.get("liveBroadcastContent", "none")  # live / none / upcoming
    viewers = live.get("concurrentViewers")
    try:
        viewers = int(viewers) if viewers is not None else None
    except Exception:
        viewers = None

    return {
        "live_status": live_status,
        "concurrent_viewers": viewers,
        "api_title": snippet.get("title"),
    }

# --- RSS ---
def fetch_feed_entries(limit: int):
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return []

    entries = []
    for e in feed.entries[:limit]:
        vid = getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None)
        title = safe_text(getattr(e, "title", "") or "")
        link = safe_text(getattr(e, "link", "") or "")
        published = safe_text(getattr(e, "published", "") or "")
        updated = safe_text(getattr(e, "updated", "") or "")

        thumb = None
        try:
            if hasattr(e, "media_thumbnail") and e.media_thumbnail:
                thumb = e.media_thumbnail[0].get("url")
        except Exception:
            thumb = None

        entries.append({
            "vid": vid,
            "title": title,
            "link": link,
            "thumb": thumb,
            "published": published,
            "updated": updated,
        })
    return entries

# --- Telegram ---
def send_post(bot: Bot, chat_id: int, item: dict, is_live_now: bool, viewers: int | None):
    title = item["title"]
    link = item["link"]
    thumb = item["thumb"]
    when = fmt_local_time(item.get("published") or item.get("updated") or "")

    header = "🔴 DIRECTO" if is_live_now else "🎥 NUEVO VÍDEO"

    lines = [header, f"✨ {title}"]
    if viewers is not None and is_live_now:
        lines.append(f"👀 {viewers} viewers")
    if when:
        lines.append(f"🕒 {when}")
    lines.append(f"👉 {link}")

    caption = "\n".join(lines)

    if thumb:
        try:
            r = requests.get(thumb, timeout=10)
            r.raise_for_status()
            msg = bot.send_photo(chat_id=chat_id,
