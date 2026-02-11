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


RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
LIVE_URL = f"https://www.youtube.com/channel/{CHANNEL_ID}/live"


def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


def safe_text(s: str) -> str:
    return (s or "").replace("\u0000", "").strip()


def format_local(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(ZoneInfo(TZ_NAME)).strftime("%d/%m %H:%M")


def extract_video_id(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{6,})", text)
    return m.group(1) if m else None


def get_pinned_video_id(bot: Bot, chat_id: int) -> str | None:
    """Saca el videoId del mensaje fijado actual (si existe)."""
    try:
        chat = bot.get_chat(chat_id)
        pinned = getattr(chat, "pinned_message", None)
        if not pinned:
            return None
        if getattr(pinned, "caption", None):
            return extract_video_id(pinned.caption)
        if getattr(pinned, "text", None):
            return extract_video_id(pinned.text)
        return None
    except Exception:
        return None


def download_bytes(url: str, timeout=12) -> bytes | None:
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            },
        )
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def get_live_video_id_by_redirect() -> str | None:
    """
    ✅ Si hay DIRECTO público activo, /live suele terminar en ...watch?v=XXXX
    ✅ Si no, se queda en /channel/...
    """
    try:
        r = requests.get(
            LIVE_URL,
            timeout=12,
            allow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            },
        )
        final_url = r.url or ""
        vid = extract_video_id(final_url)
        return vid
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
        "published_utc": published_dt,
    }


def build_caption(kind: str, item: dict) -> str:
    header = "🔴 DIRECTO" if kind == "live" else "🎥 NUEVO VÍDEO"
    when = format_local(item.get("published_utc"))
    lines = [header, f"✨ {item['title']}"]
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

    # 1) Detectar directo público activo (si existe)
    live_vid = get_live_video_id_by_redirect()
    print("live_vid_by_redirect =", live_vid)

    # 2) Leer RSS para obtener info (título/miniatura) de live o del último vídeo
    entries = fetch_entries(SCAN_LIMIT)
    items = [parse_entry(e) for e in entries]
    items = [it for it in items if it]

    if not items:
        print("No RSS entries.")
        return

    # 3) Elegir objetivo PRIORIDAD B
    target_kind = "video"
    target_item = items[0]

    if live_vid:
        # Si el directo aparece dentro de los últimos N del RSS, cogemos su metadata
        for it in items:
            if it["vid"] == live_vid:
                target_kind = "live"
                target_item = it
                break
        else:
            # Si no está en RSS (a veces pasa), usamos el live_vid con fallback mínimo
            target_kind = "live"
            target_item = {
                "vid": live_vid,
                "title": "Directo en YouTube",
                "link": f"https://www.youtube.com/watch?v={live_vid}",
                "thumb": None,
                "published_utc": None,
            }

    # 4) Evitar duplicados: si ya está fijado ese mismo vídeo, no hacemos nada
    pinned_vid = get_pinned_video_id(bot, chat_id)
    print("pinned_vid =", pinned_vid, "target_vid =", target_item["vid"])

    if pinned_vid == target_item["vid"]:
        print("Pinned already points to target. No action.")
        return

    # 5) Publicar + fijar
    msg = send_post(bot, chat_id, target_kind, target_item)
    pin_message(bot, chat_id, msg.message_id)
    print("Posted + pinned:", target_kind, target_item["vid"])


if __name__ == "__main__":
    main()
