import os
import re
import feedparser
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

TZ_NAME = os.environ.get("TZ", "Europe/Madrid")
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "15"))
KEEP_LAST = int(os.environ.get("KEEP_LAST", "10"))

RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"


def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


def safe_text(s: str) -> str:
    return (s or "").replace("\u0000", "").strip()


def format_local(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(ZoneInfo(TZ_NAME)).strftime("%d/%m %H:%M")


def download_bytes(url: str, timeout=12) -> bytes | None:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def is_live_now_by_watch(video_id: str) -> bool:
    """
    ✅ Directo real: SOLO True si 'isLiveNow:true' o status LIVE en el HTML del watch.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text

        if '"isLiveNow":true' in html:
            return True
        if '"status":"LIVE"' in html:
            return True

        return False
    except Exception:
        return False


def extract_video_id_from_text(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{6,})", text)
    return m.group(1) if m else None


def get_pinned_video_id(bot: Bot, chat_id: int) -> str | None:
    try:
        chat = bot.get_chat(chat_id)
        pinned = getattr(chat, "pinned_message", None)
        if not pinned:
            return None
        if getattr(pinned, "caption", None):
            return extract_video_id_from_text(pinned.caption)
        if getattr(pinned, "text", None):
            return extract_video_id_from_text(pinned.text)
        return None
    except Exception:
        return None


def fetch_entries(limit: int):
    feed = feedparser.parse(RSS_URL)
    if not getattr(feed, "entries", None):
        return []
    return feed.entries[:limit]


def parse_entry(e):
    vid = getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None)
    if not vid:
        return None

    title = safe_text(getattr(e, "title", "") or "")
    link = safe_text(getattr(e, "link", "") or "")

    published_dt = None
    try:
        if getattr(e, "published_parsed", None):
            import time as _time
            published_dt = datetime.fromtimestamp(_time.mktime(e.published_parsed), tz=ZoneInfo("UTC"))
    except Exception:
        published_dt = None

    thumb = None
    try:
        if hasattr(e, "media_thumbnail") and e.media_thumbnail:
            thumb = e.media_thumbnail[0].get("url")
    except Exception:
        thumb = None

    return {
        "vid": vid,
        "title": title,
        "link": link,
        "thumb": thumb,
        "published_utc": published_dt
    }


def build_caption(kind: str, item: dict) -> str:
    header = "🔴 DIRECTO" if kind == "live" else "🎥 NUEVO VÍDEO"
    lines = [header, f"✨ {item['title']}"]
    when = format_local(item.get("published_utc"))
    if when:
        lines.append(f"🕒 {when}")
    lines.append(f"👉 https://www.youtube.com/watch?v={item['vid']}")
    return "\n".join(lines)


def send_post(bot: Bot, chat_id: int, kind: str, item: dict):
    caption = build_caption(kind, item)

    if item.get("thumb"):
        b = download_bytes(item["thumb"])
        if b:
            return bot.send_photo(chat_id=chat_id, photo=b, caption=caption)

    return bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)


def pin_message(bot: Bot, chat_id: int, message_id: int):
    bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)


def main():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    entries = fetch_entries(SCAN_LIMIT)
    items = []
    for e in entries:
        it = parse_entry(e)
        if it:
            items.append(it)

    if not items:
        print("No RSS entries.")
        return

    # ✅ Prioridad B:
    # - Si alguno de los top items está EN DIRECTO AHORA -> ese
    # - Si no -> el más reciente (items[0])
    live_item = None
    for it in items:
        if is_live_now_by_watch(it["vid"]):
            live_item = it
            break

    if live_item:
        target_kind, target_item = "live", live_item
    else:
        target_kind, target_item = "video", items[0]

    pinned_vid = get_pinned_video_id(bot, chat_id)
    if pinned_vid == target_item["vid"]:
        print("Pinned already points to target. No action.")
        return

    msg = send_post(bot, chat_id, target_kind, target_item)
    pin_message(bot, chat_id, msg.message_id)
    print("Posted + pinned:", target_kind, target_item["vid"])


if __name__ == "__main__":
    main()
