import os
import json
import time
import requests
from datetime import datetime, timezone
from telegram import Bot
from telegram.error import BadRequest

STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
YT_API_KEY = os.environ.get("YT_API_KEY", "")

SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "15"))
TZ = os.environ.get("TZ", "Europe/Madrid")

PIN_LATEST = os.environ.get("PIN_LATEST", "1") == "1"
# OJO: NO borramos mensajes aunque esta variable exista:
DELETE_LIVE_WHEN_FINALIZED = os.environ.get("DELETE_LIVE_WHEN_FINALIZED", "1") == "1"
BASELINE_ONLY = os.environ.get("BASELINE_ONLY", "0") == "1"


def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return {}

    # Reparaciones defensivas ante “memoria corrupta”
    if not isinstance(st, dict):
        st = {}
    if not isinstance(st.get("notified"), dict):
        st["notified"] = {}
    if not isinstance(st.get("msg_ids"), dict):
        st["msg_ids"] = {}  # {"live:VID": 123, "video:VID": 456}
    if not isinstance(st.get("pinned"), dict):
        st["pinned"] = {}  # {"message_id": 123, "kind": "live|video", "vid": "..."}

    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def yt_get(url, params):
    params = dict(params)
    params["key"] = YT_API_KEY
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def yt_search_live_now():
    # Devuelve el primer directo activo (si existe), si no: None
    data = yt_get(
        "https://www.googleapis.com/youtube/v3/search",
        {
            "part": "snippet",
            "channelId": CHANNEL_ID,
            "eventType": "live",
            "type": "video",
            "maxResults": 5,
            "order": "date",
        },
    )
    items = data.get("items", [])
    if not items:
        return None
    vid = items[0]["id"]["videoId"]
    return vid


def yt_latest_public_video_id():
    data = yt_get(
        "https://www.googleapis.com/youtube/v3/search",
        {
            "part": "snippet",
            "channelId": CHANNEL_ID,
            "type": "video",
            "maxResults": 1,
            "order": "date",
        },
    )
    items = data.get("items", [])
    if not items:
        return None
    return items[0]["id"]["videoId"]


def yt_video_info(video_id):
    data = yt_get(
        "https://www.googleapis.com/youtube/v3/videos",
        {
            "part": "snippet,statistics,liveStreamingDetails",
            "id": video_id,
            "maxResults": 1,
        },
    )

    items = data.get("items", [])
    if not items:
        return None

    v = items[0]
    snippet = v.get("snippet", {})
    stats = v.get("statistics", {})
    live = v.get("liveStreamingDetails", {})

    title = snippet.get("title", "")
    thumbs = snippet.get("thumbnails", {}) or {}
    thumb = (
        (thumbs.get("maxres") or {}).get("url")
        or (thumbs.get("high") or {}).get("url")
        or (thumbs.get("medium") or {}).get("url")
        or (thumbs.get("default") or {}).get("url")
    )

    live_flag = (snippet.get("liveBroadcastContent") or "").lower()  # live / none / upcoming
    is_live_now = (live_flag == "live")

    concurrent = None
    if "concurrentViewers" in live:
        try:
            concurrent = int(live["concurrentViewers"])
        except Exception:
            concurrent = None

    view_count = None
    if "viewCount" in stats:
        try:
            view_count = int(stats["viewCount"])
        except Exception:
            view_count = None

    published_at = snippet.get("publishedAt")  # ISO
    actual_start = live.get("actualStartTime")
    actual_end = live.get("actualEndTime")

    return {
        "vid": video_id,
        "title": title,
        "thumb": thumb,
        "link": f"https://www.youtube.com/watch?v={video_id}",
        "is_live_now": is_live_now,
        "concurrent_viewers": concurrent,
        "view_count": view_count,
        "published_at": published_at,
        "actual_start": actual_start,
        "actual_end": actual_end,
    }


def iso_to_local(iso_str):
    """
    Convierte ISO de YouTube (normalmente UTC con 'Z') a hora local según TZ env (por defecto Europe/Madrid).
    Ej: 2026-02-16T08:22:00Z -> 16/02 09:22 (Madrid en invierno)
    """
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
        tz = ZoneInfo(TZ or "Europe/Madrid")

        s = (iso_str or "").strip()
        if not s:
            return ""

        if s.endswith("Z"):
            s = s[:-1] + "+00:00"

        dt = datetime.fromisoformat(s)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(tz).strftime("%d/%m %H:%M")
    except Exception:
        return iso_str


def format_caption(info, kind):
    # kind: "live" or "video"
    title = info["title"]
    link = info["link"]

    if kind == "live":
        viewers = info.get("concurrent_viewers")
        viewers_line = f"👀 {viewers} viewers\n" if viewers is not None else ""
        when = info.get("actual_start") or info.get("published_at") or ""
        when_line = f"🕒 {iso_to_local(when)}\n" if when else ""
        return f"🔴 DIRECTO\n✨ {title}\n{viewers_line}{when_line}👉 {link}".strip()

    views = info.get("view_count")
    views_line = f"👀 {views} views\n" if views is not None else ""
    when = info.get("published_at") or ""
    when_line = f"🕒 {iso_to_local(when)}\n" if when else ""
    return f"🎥 NUEVO VÍDEO\n✨ {title}\n{views_line}{when_line}👉 {link}".strip()


def send_post(bot: Bot, chat_id: int, info, kind):
    caption = format_caption(info, kind)
    thumb = info.get("thumb")

    if thumb:
        try:
            r = requests.get(thumb, timeout=20)
            r.raise_for_status()
            msg = bot.send_photo(chat_id=chat_id, photo=r.content, caption=caption)
            return msg.message_id
        except Exception:
            pass

    msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    return msg.message_id


def try_pin(bot: Bot, chat_id: int, message_id: int):
    try:
        bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
        return True
    except Exception:
        return False


def try_unpin_all(bot: Bot, chat_id: int):
    # ✅ Quita TODOS los pins (NO borra mensajes)
    try:
        bot.unpin_all_chat_messages(chat_id=chat_id)
        return True
    except Exception:
        return False


def try_edit_caption(bot: Bot, chat_id: int, message_id: int, caption: str):
    try:
        bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=caption)
        return True
    except BadRequest:
        return False
    except Exception:
        return False


def try_edit_text(bot: Bot, chat_id: int, message_id: int, text: str):
    # Por si el post fue texto (no foto)
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=None)
        return True
    except BadRequest:
        return False
    except Exception:
        return False


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)
    must_env("YT_API_KEY", YT_API_KEY)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    notified = state.setdefault("notified", {})
    msg_ids = state.setdefault("msg_ids", {})
    pinned = state.setdefault("pinned", {})

    # 1) ¿Hay directo activo ahora mismo?
    live_vid = yt_search_live_now()

    # Si BASELINE_ONLY: solo fija baseline y guarda SIN postear.
    if BASELINE_ONLY:
        if live_vid:
            notified[f"live:{live_vid}"] = int(time.time())
        else:
            latest_vid = yt_latest_public_video_id()
            if latest_vid:
                # IMPORTANTE: marcamos también live: y video: por si venías de líos previos
                notified[f"video:{latest_vid}"] = int(time.time())

        state["last_seen_epoch"] = int(time.time())
        save_state(state)
        print("BASELINE_ONLY: state updated (no notifications).")
        return

    # 2) Si hay live → prioridad live
    if live_vid:
        info = yt_video_info(live_vid)
        if not info:
            print("Live vid found but no info.")
            return

        if f"live:{live_vid}" not in notified:
            mid = send_post(bot, chat_id, info, kind="live")
            msg_ids[f"live:{live_vid}"] = mid
            notified[f"live:{live_vid}"] = int(time.time())
            print("Notified LIVE:", live_vid, "msg_id", mid)
        else:
            mid = msg_ids.get(f"live:{live_vid}")

        # ✅ PIN: desancla todos y ancla SOLO el más reciente
        if PIN_LATEST and mid:
            if pinned.get("message_id") != mid:
                try_unpin_all(bot, chat_id)
                ok = try_pin(bot, chat_id, mid)
                if ok:
                    pinned.update({"message_id": mid, "kind": "live", "vid": live_vid})
                    print("Pinned LIVE msg_id", mid)

        state["last_seen_epoch"] = int(time.time())
        save_state(state)
        print("State saved. last_seen_epoch =", state["last_seen_epoch"])
        return

    # 3) No hay live → último vídeo público
    latest_vid = yt_latest_public_video_id()
    if not latest_vid:
        print("No latest video found.")
        return

    info = yt_video_info(latest_vid)
    if not info:
        print("Latest vid found but no info.")
        return

    # ✅ CLAVE ANTI-DUPLICADOS:
    # Si este vídeo ya fue notificado como LIVE antes (misma VID),
    # NO publicamos un nuevo mensaje. Editamos el mensaje del live para convertirlo a "NUEVO VÍDEO".
    live_mid_same_vid = msg_ids.get(f"live:{latest_vid}")
    if live_mid_same_vid and f"video:{latest_vid}" not in notified:
        new_cap = format_caption(info, kind="video")

        # Intentamos editar caption (si era foto), si falla probamos texto.
        edited = try_edit_caption(bot, chat_id, live_mid_same_vid, new_cap)
        if not edited:
            edited = try_edit_text(bot, chat_id, live_mid_same_vid, new_cap)

        if edited:
            # Lo registramos como "video" usando el MISMO message_id (así nunca duplica)
            msg_ids[f"video:{latest_vid}"] = live_mid_same_vid
            notified[f"video:{latest_vid}"] = int(time.time())
            print("Converted LIVE->VIDEO by editing msg_id", live_mid_same_vid)

            # Y el pin apunta a ese mismo mensaje
            mid = live_mid_same_vid
        else:
            # Si no se pudo editar (casos raros), caemos al flujo normal (publicar vídeo)
            mid = None
    else:
        mid = None

    # Flujo normal de vídeo si no hemos convertido por edición
    if mid is None:
        if f"video:{latest_vid}" not in notified:
            mid = send_post(bot, chat_id, info, kind="video")
            msg_ids[f"video:{latest_vid}"] = mid
            notified[f"video:{latest_vid}"] = int(time.time())
            print("Notified VIDEO:", latest_vid, "msg_id", mid)
        else:
            mid = msg_ids.get(f"video:{latest_vid}")
            if mid:
                cap = format_caption(info, kind="video")
                # Si ya existía el mensaje, intentamos actualizarlo
                if not try_edit_caption(bot, chat_id, mid, cap):
                    try_edit_text(bot, chat_id, mid, cap)

    # ✅ PIN: desancla todos y ancla SOLO el más reciente (NO borra mensajes)
    if PIN_LATEST and mid:
        if pinned.get("message_id") != mid:
            try_unpin_all(bot, chat_id)
            ok = try_pin(bot, chat_id, mid)
            if ok:
                pinned.update({"message_id": mid, "kind": "video", "vid": latest_vid})
                print("Pinned VIDEO msg_id", mid)

    state["last_seen_epoch"] = int(time.time())
    save_state(state)
    print("State saved. last_seen_epoch =", state["last_seen_epoch"])


def main():
    run_once()


if __name__ == "__main__":
    main()
