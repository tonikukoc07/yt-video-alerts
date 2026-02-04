import os
import re
import json
import feedparser
from telegram import Bot

# ====== ENV ======
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

STATE_FILE = "state.json"
RUN_ONCE = os.environ.get("RUN_ONCE", "true").lower() == "true"


def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "last_video_id": None,
            "last_is_live": None,
            "last_notified_video_id": None,
            "last_notified_live_start_id": None,
            "last_notified_live_end_id": None,
        }
    except Exception:
        # Si se corrompe, arrancamos limpio
        return {
            "last_video_id": None,
            "last_is_live": None,
            "last_notified_video_id": None,
            "last_notified_live_start_id": None,
            "last_notified_live_end_id": None,
        }


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def extract_video_id(entry):
    vid = getattr(entry, "yt_videoid", None)
    if vid:
        return vid

    link = getattr(entry, "link", "") or ""
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", link)
    return m.group(1) if m else None


def extract_thumbnail(entry):
    media_thumbnail = getattr(entry, "media_thumbnail", None)
    if isinstance(media_thumbnail, list) and media_thumbnail:
        url = media_thumbnail[0].get("url")
        if url:
            return url

    vid = extract_video_id(entry)
    if vid:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    return None


def is_live_entry(entry) -> bool:
    """
    Heurística para detectar DIRECTO desde el RSS.
    """
    for key in ["yt_broadcast", "broadcast", "media_status", "status"]:
        val = getattr(entry, key, None)
        if isinstance(val, str) and "live" in val.lower():
            return True

    raw = str(entry).lower()
    if ("yt:live" in raw) or ("broadcast" in raw and "live" in raw):
        return True

    link = getattr(entry, "link", "") or ""
    if "/live" in link:
        return True

    return False


def fetch_latest():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return None

    e = feed.entries[0]
    vid = extract_video_id(e)
    title = getattr(e, "title", "") or ""
    link = getattr(e, "link", "") or ""
    thumb = extract_thumbnail(e)
    live = is_live_entry(e)

    return vid, title, link, live, thumb


def build_caption(title: str, link: str, live: bool) -> str:
    if live:
        header = "🔴 DIRECTO"
    else:
        header = "🎥 NUEVO VÍDEO"

    return f"{header}\n✨ {title}\n\n👉 {link}"


def build_live_ended_caption(title: str, link: str) -> str:
    return f"✅ DIRECTO FINALIZADO\n✨ {title}\n\n👉 {link}"


def send_photo_or_text(bot: Bot, chat_id: int, caption: str, thumb: str | None):
    try:
        if thumb:
            bot.send_photo(chat_id=chat_id, photo=thumb, caption=caption, parse_mode=None)
        else:
            bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    except Exception as e:
        print("Photo failed -> fallback text. Error:", repr(e))
        bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()

    latest = fetch_latest()
    print("RSS:", RSS_URL)
    print("STATE:", state)

    if not latest:
        print("No entries.")
        return

    vid, title, link, is_live, thumb = latest
    print("LATEST:", {"vid": vid, "is_live": is_live, "title": title})

    if not vid:
        print("No video id.")
        return

    # Caso A) Si es el MISMO vídeo que vimos la última vez:
    if vid == state.get("last_video_id"):
        prev_live = state.get("last_is_live")

        # A1) Pasó de LIVE -> NO LIVE  => avisar "Directo finalizado" (solo una vez)
        if prev_live is True and is_live is False:
            if state.get("last_notified_live_end_id") != vid:
                caption = build_live_ended_caption(title, link)
                send_photo_or_text(bot, chat_id, caption, thumb)
                state["last_notified_live_end_id"] = vid
                print("Sent LIVE ENDED for:", vid)
            else:
                print("Live ended already notified for:", vid)

        else:
            print("No change for same video.")

        # Actualizamos estado base
        state["last_video_id"] = vid
        state["last_is_live"] = is_live
        save_state(state)
        return

    # Caso B) Es un vídeo NUEVO (id distinto):
    # B1) Si es LIVE => avisar SOLO una vez al inicio
    if is_live:
        if state.get("last_notified_live_start_id") != vid:
            caption = build_caption(title, link, live=True)
            send_photo_or_text(bot, chat_id, caption, thumb)
            state["last_notified_live_start_id"] = vid
            print("Sent LIVE START for:", vid)
        else:
            print("Live start already notified for:", vid)

    # B2) Si NO es live => avisar SOLO una vez por vídeo
    else:
        if state.get("last_notified_video_id") != vid:
            caption = build_caption(title, link, live=False)
            send_photo_or_text(bot, chat_id, caption, thumb)
            state["last_notified_video_id"] = vid
            print("Sent NEW VIDEO for:", vid)
        else:
            print("Video already notified for:", vid)

    # Guardar estado actual
    state["last_video_id"] = vid
    state["last_is_live"] = is_live
    save_state(state)


if __name__ == "__main__":
    # En GitHub Actions se ejecuta una vez y termina (ideal con cron)
    run_once()
