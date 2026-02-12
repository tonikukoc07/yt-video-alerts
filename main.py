import os
import json
import feedparser
import requests
from datetime import datetime
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
YT_API_KEY = os.environ.get("YT_API_KEY", "")

TZ = os.environ.get("TZ", "Europe/Madrid")
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "15"))
PIN_LATEST = os.environ.get("PIN_LATEST", "1") == "1"

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


def parse_yt_datetime(dt_str: str):
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_local_time(iso_str: str) -> str:
    dt = parse_yt_datetime(iso_str)
    if not dt:
        return ""
    try:
        return dt.astimezone().strftime("%d/%m %H:%M")
    except Exception:
        return dt.strftime("%d/%m %H:%M")


def yt_api_video_details(video_id: str) -> dict:
    if not video_id:
        return {}
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {"part": "snippet,liveStreamingDetails", "id": video_id, "key": YT_API_KEY}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    items = data.get("items", [])
    if not items:
        return {}

    it = items[0]
    snippet = it.get("snippet", {}) or {}
    live = it.get("liveStreamingDetails", {}) or {}

    live_status = (snippet.get("liveBroadcastContent", "none") or "none").lower()  # live/none/upcoming
    viewers = live.get("concurrentViewers")
    try:
        viewers = int(viewers) if viewers is not None else None
    except Exception:
        viewers = None

    return {"live_status": live_status, "concurrent_viewers": viewers}


def fetch_feed_entries(limit: int):
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return []

    out = []
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

        out.append(
            {
                "vid": vid,
                "title": title,
                "link": link,
                "thumb": thumb,
                "published": published,
                "updated": updated,
            }
        )
    return out


def send_post(bot: Bot, chat_id: int, item: dict, is_live_now: bool, viewers: int | None):
    title = item["title"]
    link = item["link"]
    thumb = item["thumb"]
    when = fmt_local_time(item.get("published") or item.get("updated") or "")

    header = "🔴 DIRECTO" if is_live_now else "🎥 NUEVO VÍDEO"

    lines = [header, f"✨ {title}"]
    if is_live_now and viewers is not None:
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
        except Exception:
            pass

    msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    return msg.message_id


def pin_message(bot: Bot, chat_id: int, message_id: int):
    try:
        bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except Exception:
        # en canales debe ser admin con permiso de fijar
        pass


def pick_pin_target(entries: list[dict]):
    # 1) si hay alguno LIVE ahora -> el más reciente que esté live
    for item in entries:
        d = yt_api_video_details(item["vid"])
        if (d.get("live_status") or "none") == "live":
            return item, True, d.get("concurrent_viewers")

    # 2) si no, el último del feed
    return entries[0], False, None


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)
    must_env("YT_API_KEY", YT_API_KEY)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    sent_ids = set(state.get("sent_video_ids", []))
    msg_map = state.get("msg_map", {})  # vid -> telegram_message_id
    last_pinned_vid = state.get("last_pinned_video_id")

    entries = fetch_feed_entries(SCAN_LIMIT)
    if not entries:
        print("No entries in RSS.")
        return

    latest = entries[0]
    latest_vid = latest["vid"]

    if "initialized" not in state:
        # primera vez: inicializa sin spamear
        state["initialized"] = True
        if latest_vid:
            state["sent_video_ids"] = [latest_vid]
        state["msg_map"] = {}
        state["last_pinned_video_id"] = None
        save_state(state)
        print("Initialized (no notification).")
        return

    # --- 1) publicar SOLO si es nuevo ---
    if latest_vid and latest_vid not in sent_ids:
        d = yt_api_video_details(latest_vid)
        is_live_now = (d.get("live_status") or "none") == "live"
        viewers = d.get("concurrent_viewers") if is_live_now else None

        mid = send_post(bot, chat_id, latest, is_live_now, viewers)

        sent_ids.add(latest_vid)
        msg_map[str(latest_vid)] = int(mid)

        state["sent_video_ids"] = list(sent_ids)[-200:]
        state["msg_map"] = msg_map
        save_state(state)
        print("Posted:", latest_vid, "msg_id:", mid)
    else:
        print("No new item to notify.")

    # --- 2) pin sin duplicar ---
    if PIN_LATEST:
        pin_item, pin_is_live, pin_viewers = pick_pin_target(entries)
        pin_vid = pin_item["vid"] if pin_item else None

        if pin_vid and pin_vid != last_pinned_vid:
            # Si ya existe mensaje para ese vídeo, lo fijamos
            existing_mid = msg_map.get(str(pin_vid))
            if existing_mid:
                pin_message(bot, chat_id, int(existing_mid))
                state["last_pinned_video_id"] = pin_vid
                save_state(state)
                print("Pinned existing message:", pin_vid, existing_mid)
            else:
                # Si no existe (porque es antiguo), lo publicamos 1 vez y lo fijamos (1 post, no 2)
                d = yt_api_video_details(pin_vid)
                pin_is_live = (d.get("live_status") or "none") == "live"
                pin_viewers = d.get("concurrent_viewers") if pin_is_live else None

                mid = send_post(bot, chat_id, pin_item, pin_is_live, pin_viewers)
                msg_map[str(pin_vid)] = int(mid)
                sent_ids.add(pin_vid)

                pin_message(bot, chat_id, int(mid))

                state["sent_video_ids"] = list(sent_ids)[-200:]
                state["msg_map"] = msg_map
                state["last_pinned_video_id"] = pin_vid
                save_state(state)
                print("Posted+Pinned (single):", pin_vid, mid)


def main():
    run_once()


if __name__ == "__main__":
    main()
