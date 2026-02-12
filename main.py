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
            msg = bot.send_photo(chat_id=chat_id, photo=r.content, caption=caption)
            return msg.message_id
        except Exception as e:
            print("send_photo failed, fallback to text. Error:", repr(e))

    msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    return msg.message_id

def pin_message(bot: Bot, chat_id: int, message_id: int):
    try:
        bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except Exception as e:
        print("pin_chat_message failed:", repr(e))

# --- Logic: prioridad directo si hay, si no último vídeo ---
def pick_pin_target(entries: list[dict]) -> tuple[dict | None, bool, int | None]:
    """
    Devuelve (item, is_live_now, viewers)
    Prioridad:
      1) si alguno está LIVE ahora mismo -> el más reciente que esté live
      2) si no -> el más reciente del feed (vídeo)
    """
    if not entries:
        return None, False, None

    # Buscar directos activos (liveBroadcastContent == "live")
    for item in entries:
        details = yt_api_video_details(item["vid"])
        live_status = (details.get("live_status") or "none").lower()
        if live_status == "live":
            return item, True, details.get("concurrent_viewers")

    # Si no hay live, el último vídeo (el primero del feed)
    return entries[0], False, None

def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)
    must_env("YT_API_KEY", YT_API_KEY)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    sent_ids = set(state.get("sent_video_ids", []))  # evitar duplicados
    last_pinned = state.get("last_pinned_video_id")

    entries = fetch_feed_entries(SCAN_LIMIT)
    if not entries:
        print("No entries in RSS.")
        return

    # 1) Notificar SOLO si hay un ID nuevo en el feed (evita duplicados aunque lances manual 20 veces)
    latest = entries[0]
    latest_vid = latest["vid"]
    print("Latest RSS vid =", latest_vid, "title =", latest["title"])

    if "initialized" not in state:
        # primera vez: inicializa sin spamear
        state["initialized"] = True
        state["sent_video_ids"] = [latest_vid] if latest_vid else []
        state["last_pinned_video_id"] = None
        save_state(state)
        print("Initialized state (no notification).")
        return

    if latest_vid and latest_vid not in sent_ids:
        # Comprobar si es live ahora mismo con API (real)
        details = yt_api_video_details(latest_vid)
        is_live_now = (details.get("live_status") or "none").lower() == "live"
        viewers = details.get("concurrent_viewers") if is_live_now else None

        msg_id = send_post(bot, chat_id, latest, is_live_now, viewers)

        sent_ids.add(latest_vid)
        # guardamos un histórico pequeño para no crecer infinito
        state["sent_video_ids"] = list(sent_ids)[-200:]
        save_state(state)
        print("Notified new item:", latest_vid, "live:", is_live_now, "viewers:", viewers, "msg_id:", msg_id)
    else:
        print("No new item to notify (already sent or missing id).")

    # 2) Pin “pro”: si hay directo activo -> fijarlo, si no -> último vídeo
    if PIN_LATEST:
        pin_item, pin_is_live, pin_viewers = pick_pin_target(entries)
        if pin_item:
            pin_vid = pin_item["vid"]
            if pin_vid and pin_vid != last_pinned:
                # Publicamos un post (si no estaba publicado) para poder fijarlo
                # (Telegram solo puede fijar un mensaje existente del canal)
                details = yt_api_video_details(pin_vid)
                pin_is_live = (details.get("live_status") or "none").lower() == "live"
                pin_viewers = details.get("concurrent_viewers") if pin_is_live else None

                pin_msg_id = send_post(bot, chat_id, pin_item, pin_is_live, pin_viewers)
                pin_message(bot, chat_id, pin_msg_id)

                state["last_pinned_video_id"] = pin_vid
                save_state(state)
                print("Pinned:", pin_vid, "live:", pin_is_live, "viewers:", pin_viewers)
            else:
                print("Pin unchanged.")
        else:
            print("Nothing to pin.")

def main():
    run_once()

if __name__ == "__main__":
    main()
