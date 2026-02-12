import os
import json
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests
from telegram import Bot


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")          # id numérico canal/grupo
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")    # UCxxxx

SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "15"))
TZ_NAME = os.environ.get("TZ", "Europe/Madrid")
PIN_LATEST = os.environ.get("PIN_LATEST", "0") == "1"

STATE_FILE = "state.json"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
LIVE_URL = f"https://www.youtube.com/channel/{CHANNEL_ID}/live"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


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
    return (s or "").replace("\u0000", "").strip()


def fmt_local(dt_utc: datetime | None, tz_name: str) -> str:
    if not dt_utc:
        return ""
    tz = ZoneInfo(tz_name)
    return dt_utc.astimezone(tz).strftime("%d/%m %H:%M")


def dt_from_entry(entry) -> datetime | None:
    for attr in ("updated_parsed", "published_parsed"):
        st = getattr(entry, attr, None)
        if st:
            return datetime(*st[:6], tzinfo=timezone.utc)
    return None


def extract_video_id_from_url(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", url)
    return m.group(1) if m else None


def fetch_live_video_id() -> str | None:
    """
    Si hay directo activo, /live suele redirigir a /watch?v=XXXX.
    Si NO hay directo, normalmente no termina en un watch válido.
    """
    try:
        r = requests.get(LIVE_URL, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
        final_url = r.url or ""
        vid = extract_video_id_from_url(final_url)

        # Extra: a veces /live devuelve HTML que contiene el watch id aunque no redirija limpio
        if not vid:
            m = re.search(r"watch\?v=([A-Za-z0-9_-]{6,})", r.text or "")
            vid = m.group(1) if m else None

        return vid
    except Exception as e:
        print("fetch_live_video_id error:", repr(e))
        return None


def _extract_json_object_after(html: str, marker: str) -> dict | None:
    idx = html.find(marker)
    if idx == -1:
        return None
    start = html.find("{", idx)
    if start == -1:
        return None

    depth = 0
    in_str = False
    esc = False

    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = html[start : i + 1]
                    try:
                        return json.loads(chunk)
                    except Exception:
                        return None
    return None


def fetch_watch_details(video_id: str) -> dict:
    """
    Devuelve:
      - is_live_now (bool)  -> SOLO true si está emitiendo ahora mismo
      - view_count (int|None)
      - title (str|None)
      - thumb (str|None)
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    html = r.text

    pr = _extract_json_object_after(html, "ytInitialPlayerResponse")
    if not pr:
        pr = _extract_json_object_after(html, "var ytInitialPlayerResponse")
    if not pr:
        return {"is_live_now": False, "view_count": None, "title": None, "thumb": None}

    is_live_now = False
    try:
        micro = pr.get("microformat", {}).get("playerMicroformatRenderer", {})
        live_details = micro.get("liveBroadcastDetails", {}) or {}
        is_live_now = bool(live_details.get("isLiveNow", False))
    except Exception:
        pass

    view_count = None
    try:
        vc = pr.get("videoDetails", {}).get("viewCount")
        if isinstance(vc, str) and vc.isdigit():
            view_count = int(vc)
        elif isinstance(vc, int):
            view_count = vc
    except Exception:
        pass

    title = None
    try:
        title = pr.get("videoDetails", {}).get("title")
    except Exception:
        pass

    thumb = None
    try:
        thumbs = pr.get("videoDetails", {}).get("thumbnail", {}).get("thumbnails", [])
        if thumbs:
            thumb = thumbs[-1].get("url")
    except Exception:
        pass

    return {"is_live_now": is_live_now, "view_count": view_count, "title": title, "thumb": thumb}


def fetch_latest_rss_entry():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return None
    e = feed.entries[0]

    vid = getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None)
    title = safe_text(getattr(e, "title", ""))
    link = safe_text(getattr(e, "link", "")) or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
    dt_utc = dt_from_entry(e)

    thumb = None
    try:
        if hasattr(e, "media_thumbnail") and e.media_thumbnail:
            thumb = e.media_thumbnail[0].get("url")
    except Exception:
        pass

    return {"vid": vid, "title": title, "link": link, "dt_utc": dt_utc, "thumb": thumb}


def send_post(bot: Bot, chat_id: int, item: dict) -> int | None:
    local_time = fmt_local(item.get("dt_utc"), TZ_NAME)
    is_live = item.get("is_live_now", False)

    header = "🔴 DIRECTO" if is_live else "🎥 NUEVO VÍDEO"
    lines = [header, f"✨ {safe_text(item.get('title',''))}"]

    if (not is_live) and item.get("view_count") is not None:
        lines.append(f"👀 {item['view_count']} views")

    if local_time:
        lines.append(f"🕒 {local_time}")

    lines.append(f"👉 {item.get('link','')}")
    caption = "\n".join(lines)

    thumb = item.get("thumb")
    if thumb:
        try:
            rr = requests.get(thumb, timeout=15, headers={"User-Agent": UA})
            rr.raise_for_status()
            msg = bot.send_photo(chat_id=chat_id, photo=rr.content, caption=caption)
            return getattr(msg, "message_id", None)
        except Exception as e:
            print("send_photo failed -> fallback text:", repr(e))

    msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    return getattr(msg, "message_id", None)


def pin_message(bot: Bot, chat_id: int, message_id: int):
    bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)


def unpin_message(bot: Bot, chat_id: int, message_id: int | None):
    if not message_id:
        return
    try:
        bot.unpin_chat_message(chat_id=chat_id, message_id=int(message_id))
    except Exception:
        try:
            bot.unpin_chat_message(chat_id=chat_id)
        except Exception:
            pass


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    last_notified = state.get("last_notified_key")
    last_pinned_mid = state.get("last_pinned_message_id")

    # 1) PRIORIDAD: si hay live -> usamos el vídeo del /live
    live_vid = fetch_live_video_id()
    if live_vid:
        details = fetch_watch_details(live_vid)
        is_live_now = bool(details.get("is_live_now", False))

        # OJO: /live puede apuntar a un watch aunque no esté LIVE NOW (a veces)
        if is_live_now:
            item = {
                "vid": live_vid,
                "title": safe_text(details.get("title") or "Directo"),
                "link": f"https://www.youtube.com/watch?v={live_vid}",
                "thumb": details.get("thumb"),
                "dt_utc": datetime.now(timezone.utc),
                "is_live_now": True,
                "view_count": details.get("view_count"),
            }

            key = f"live:{live_vid}"
            print("LIVE detected:", key, item["title"])

            if not last_notified:
                state["last_notified_key"] = key
                save_state(state)
                print("Initialized state (no notification):", key)
                return

            if key == last_notified:
                print("Already notified live:", key)
                return

            mid = send_post(bot, chat_id, item)

            if PIN_LATEST and mid:
                unpin_message(bot, chat_id, last_pinned_mid)
                pin_message(bot, chat_id, int(mid))
                state["last_pinned_message_id"] = int(mid)

            state["last_notified_key"] = key
            save_state(state)
            print("Notified live and updated state.")
            return

    # 2) Si NO hay live activo -> usamos RSS (último vídeo)
    latest = fetch_latest_rss_entry()
    if not latest or not latest.get("vid"):
        print("No RSS entry.")
        return

    vid = latest["vid"]
    details = fetch_watch_details(vid)

    item = {
        "vid": vid,
        "title": latest["title"] or safe_text(details.get("title") or ""),
        "link": latest["link"] or f"https://www.youtube.com/watch?v={vid}",
        "thumb": latest.get("thumb") or details.get("thumb"),
        "dt_utc": latest.get("dt_utc") or datetime.now(timezone.utc),
        "is_live_now": False,  # aquí ya es “no live”
        "view_count": details.get("view_count"),
    }

    key = f"video:{vid}"
    print("VIDEO target:", key, item["title"])

    if not last_notified:
        state["last_notified_key"] = key
        save_state(state)
        print("Initialized state (no notification):", key)
        return

    if key == last_notified:
        print("Already notified video:", key)
        return

    mid = send_post(bot, chat_id, item)

    if PIN_LATEST and mid:
        unpin_message(bot, chat_id, last_pinned_mid)
        pin_message(bot, chat_id, int(mid))
        state["last_pinned_message_id"] = int(mid)

    state["last_notified_key"] = key
    save_state(state)
    print("Notified video and updated state.")


if __name__ == "__main__":
    run_once()
