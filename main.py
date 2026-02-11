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


def fetch_live_now_from_channel() -> tuple[str, str] | None:
    """
    Devuelve (video_id, title_guess) si hay directo activo.
    Usa /live del canal y busca señales 'isLiveNow' y el videoId.
    """
    url = f"https://www.youtube.com/channel/{CHANNEL_ID}/live"
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text

        # si no hay live, a veces no aparece 'isLiveNow":true'
        if '"isLiveNow":true' not in html and '"LIVE_NOW"' not in html and '"status":"LIVE"' not in html:
            return None

        m = re.search(r'"videoId":"([A-Za-z0-9_-]{6,})"', html)
        if not m:
            return None
        vid = m.group(1)

        # título (no siempre está fácil); hacemos un intento suave
        t = ""
        mt = re.search(r'"title":"([^"]{3,120})"', html)
        if mt:
            t = mt.group(1)
        return (vid, t)
    except Exception:
        return None


def fetch_latest_from_rss():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return None

    e = feed.entries[0]
    vid = getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None)
    title = safe_text(getattr(e, "title", "") or "")
    link = safe_text(getattr(e, "link", "") or "")

    published_dt = None
    try:
        if getattr(e, "published_parsed", None):
            published_dt = datetime.fromtimestamp(
                __import__("time").mktime(e.published_parsed),
                tz=ZoneInfo("UTC")
            )
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


def build_caption(kind: str, title: str, link: str, published_utc: datetime | None):
    header = "🔴 DIRECTO" if kind == "live" else "🎥 NUEVO VÍDEO"
    lines = [header]
    if title:
        lines.append(f"✨ {title}")
    when = format_local(published_utc)
    if when:
        lines.append(f"🕒 {when}")
    lines.append(f"👉 {link}")
    return "\n".join(lines)


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

        # Si es foto, el link estará en caption
        if getattr(pinned, "caption", None):
            return extract_video_id_from_text(pinned.caption)

        # Si es texto
        if getattr(pinned, "text", None):
            return extract_video_id_from_text(pinned.text)

        return None
    except Exception:
        return None


def send_and_pin(bot: Bot, chat_id: int, kind: str, vid: str, title: str, thumb_url: str | None, published_utc: datetime | None):
    link = f"https://www.youtube.com/watch?v={vid}"
    caption = build_caption(kind, title, link, published_utc)

    if thumb_url:
        b = download_bytes(thumb_url)
        if b:
            msg = bot.send_photo(chat_id=chat_id, photo=b, caption=caption)
        else:
            msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    else:
        msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)

    # Fijar (sin reposts extra)
    bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id, disable_notification=True)


def main():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    # 1) Prioridad B: si hay directo activo -> ese
    live = fetch_live_now_from_channel()
    if live:
        vid, title_guess = live
        pinned_vid = get_pinned_video_id(bot, chat_id)
        if pinned_vid == vid:
            print("Pinned already points to LIVE vid. No action.")
            return

        # miniatura estándar (si no hay en RSS)
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        send_and_pin(bot, chat_id, "live", vid, title_guess or "Estamos en directo", thumb, None)
        print("Posted + pinned LIVE:", vid)
        return

    # 2) Si no hay directo -> último vídeo del RSS
    latest = fetch_latest_from_rss()
    if not latest or not latest.get("vid"):
        print("No RSS entries.")
        return

    vid = latest["vid"]
    pinned_vid = get_pinned_video_id(bot, chat_id)
    if pinned_vid == vid:
        print("Pinned already points to latest VIDEO. No action.")
        return

    send_and_pin(
        bot,
        chat_id,
        "video",
        vid,
        latest.get("title", ""),
        latest.get("thumb"),
        latest.get("published_utc")
    )
    print("Posted + pinned VIDEO:", vid)


if __name__ == "__main__":
    main()
