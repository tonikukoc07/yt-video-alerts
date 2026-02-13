import os
import json
import time
import datetime
import feedparser
import requests
from telegram import Bot

# ========= ENV =========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
YT_API_KEY = os.environ.get("YT_API_KEY", "")

SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "25"))
PIN_LATEST = os.environ.get("PIN_LATEST", "1") == "1"
DELETE_LIVE_WHEN_FINALIZED = os.environ.get("DELETE_LIVE_WHEN_FINALIZED", "1") == "1"

# Si alguna vez se te desmadra y quieres “resetear” sin spamear:
# ejecuta una vez con BASELINE_ONLY=1 y ya vuelve a la normalidad.
BASELINE_ONLY = os.environ.get("BASELINE_ONLY", "0") == "1"

STATE_FILE = "state.json"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
YOUTUBE_VIDEOS_API = "https://www.googleapis.com/youtube/v3/videos"


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


def sanitize_state(state):
    """Autorepara state.json viejo/roto para evitar duplicados y crashes."""
    if not isinstance(state, dict):
        state = {}

    def ensure_dict(k):
        if not isinstance(state.get(k), dict):
            state[k] = {}

    ensure_dict("notified")           # vid -> epoch cuando se notificó
    ensure_dict("msg_live")           # vid -> message_id del post LIVE
    ensure_dict("msg_video")          # vid -> message_id del post VIDEO
    ensure_dict("last_kind")          # vid -> "live"/"video"
    if not isinstance(state.get("last_seen_epoch", 0), int):
        state["last_seen_epoch"] = 0

    # Pin tracking
    if "pinned_message_id" in state and not isinstance(state["pinned_message_id"], int):
        try:
            state["pinned_message_id"] = int(state["pinned_message_id"])
        except Exception:
            state.pop("pinned_message_id", None)

    return state


def to_epoch(published_parsed):
    if not published_parsed:
        return 0
    return int(time.mktime(published_parsed))


def fmt_local_time(epoch: int) -> str:
    dt = datetime.datetime.fromtimestamp(epoch)
    return dt.strftime("%d/%m %H:%M")


def fetch_feed_entries(limit: int):
    feed = feedparser.parse(RSS_URL)
    entries = feed.entries[: max(1, limit)]
    out = []
    for e in entries:
        vid = getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None)
        title = safe_text(getattr(e, "title", ""))
        link = safe_text(getattr(e, "link", ""))
        thumb = None
        try:
            if hasattr(e, "media_thumbnail") and e.media_thumbnail:
                thumb = e.media_thumbnail[0].get("url")
        except Exception:
            thumb = None
        pub_epoch = to_epoch(getattr(e, "published_parsed", None)) or 0
        out.append({"vid": vid, "title": title, "link": link, "thumb": thumb, "published_epoch": pub_epoch})
    return out  # newest-first


def yt_api_video_info(video_id: str):
    """Devuelve si está EN DIRECTO AHORA MISMO + viewers o views."""
    if not video_id:
        return {"is_live_now": False, "concurrent_viewers": None, "view_count": None}

    params = {"part": "liveStreamingDetails,statistics", "id": video_id, "key": YT_API_KEY}
    r = requests.get(YOUTUBE_VIDEOS_API, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    items = data.get("items", [])
    if not items:
        return {"is_live_now": False, "concurrent_viewers": None, "view_count": None}

    item = items[0]
    live = item.get("liveStreamingDetails", {}) or {}
    stats = item.get("statistics", {}) or {}

    actual_start = live.get("actualStartTime")
    actual_end = live.get("actualEndTime")

    is_live_now = bool(actual_start) and not bool(actual_end)

    concurrent = live.get("concurrentViewers")
    view_count = stats.get("viewCount")

    return {
        "is_live_now": is_live_now,
        "concurrent_viewers": int(concurrent) if concurrent is not None else None,
        "view_count": int(view_count) if view_count is not None else None,
    }


def download_bytes(url: str):
    if not url:
        return None
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.content


def build_caption(item, info):
    is_live = info["is_live_now"]
    header = "🔴 DIRECTO" if is_live else "🎥 NUEVO VÍDEO"

    when_epoch = item.get("published_epoch") or 0
    when_txt = fmt_local_time(when_epoch) if when_epoch else ""

    line_views = ""
    if is_live and info.get("concurrent_viewers") is not None:
        line_views = f"👀 {info['concurrent_viewers']} viewers"
    elif (not is_live) and info.get("view_count") is not None:
        line_views = f"👀 {info['view_count']} views"

    parts = [header, f"✨ {item['title']}"]
    if line_views:
        parts.append(line_views)
    if when_txt:
        parts.append(f"🕒 {when_txt}")
    parts.append(f"👉 {item['link']}")
    return "\n".join(parts)


def tg_pin(bot, chat_id, message_id):
    try:
        bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
        return True
    except Exception as e:
        print("pin failed:", repr(e))
        return False


def tg_unpin(bot, chat_id, message_id):
    try:
        bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        print("unpin failed:", repr(e))
        return False


def tg_delete(bot, chat_id, message_id):
    try:
        bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        print("delete failed:", repr(e))
        return False


def send_post(bot, chat_id, item, info, kind):
    """Envía el post (foto si puede) y devuelve message_id."""
    caption = build_caption(item, info)
    msg = None

    if item.get("thumb"):
        try:
            photo_bytes = download_bytes(item["thumb"])
            msg = bot.send_photo(chat_id=chat_id, photo=photo_bytes, caption=caption)
        except Exception as e:
            print("send_photo failed -> fallback text:", repr(e))

    if msg is None:
        msg = bot.send_message(chat_id=chat_id, text=caption)

    return msg.message_id


def choose_pin_target(entries):
    """
    Prioridad:
      1) Si hay algún directo activo -> ese
      2) Si no -> el último (entries[0])
    """
    for it in entries:
        try:
            info = yt_api_video_info(it["vid"])
            if info["is_live_now"]:
                return it, info
        except Exception as e:
            print("live check failed (ignored):", repr(e))

    it = entries[0]
    info = yt_api_video_info(it["vid"])
    return it, info


# ========= MAIN RUN =========
def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)
    must_env("YT_API_KEY", YT_API_KEY)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = sanitize_state(load_state())

    entries = fetch_feed_entries(SCAN_LIMIT)
    if not entries:
        print("No RSS entries.")
        save_state(state)
        return

    newest_epoch = entries[0].get("published_epoch", 0)

    # Baseline-only: NO notifica, solo fija el “punto de partida”
    if BASELINE_ONLY or state.get("last_seen_epoch", 0) == 0:
        state["last_seen_epoch"] = newest_epoch or int(time.time())
        save_state(state)
        print("Initialized baseline last_seen_epoch =", state["last_seen_epoch"], "(no notifications)")
        return

    last_seen_epoch = state["last_seen_epoch"]

    # 1) Notificar nuevos (por published_epoch) SIN duplicar
    entries_sorted = sorted(entries, key=lambda x: x.get("published_epoch", 0))
    new_items = [it for it in entries_sorted if it.get("published_epoch", 0) > last_seen_epoch]

    for it in new_items:
        vid = it["vid"]
        if not vid:
            continue
        if vid in state["notified"]:
            continue

        info = yt_api_video_info(vid)
        kind = "live" if info["is_live_now"] else "video"

        # Si ya hay mensaje de ese kind guardado -> no reenviar
        if kind == "live" and vid in state["msg_live"]:
            state["notified"][vid] = int(time.time())
            continue
        if kind == "video" and vid in state["msg_video"]:
            state["notified"][vid] = int(time.time())
            continue

        mid = send_post(bot, chat_id, it, info, kind)
        state["notified"][vid] = int(time.time())
        state["last_kind"][vid] = kind
        if kind == "live":
            state["msg_live"][vid] = mid
        else:
            state["msg_video"][vid] = mid

        print("Notified:", vid, it["title"], "kind:", kind, "mid:", mid)

    if newest_epoch > last_seen_epoch:
        state["last_seen_epoch"] = newest_epoch

    # 2) Conversión PRO: si un vid que teníamos como LIVE ya NO está live -> publicar VIDEO y borrar LIVE
    # (Esto arregla el “acabó el directo y se quedó 🔴 para siempre”.)
    for vid, live_mid in list(state["msg_live"].items()):
        try:
            info_now = yt_api_video_info(vid)
        except Exception as e:
            print("live recheck failed:", vid, repr(e))
            continue

        if not info_now["is_live_now"]:
            # Ya terminó -> asegurar post de VIDEO
            if vid not in state["msg_video"]:
                # buscamos el item en el feed (si está en SCAN_LIMIT)
                item = next((x for x in entries if x["vid"] == vid), None)
                if item:
                    mid_video = send_post(bot, chat_id, item, info_now, "video")
                    state["msg_video"][vid] = mid_video
                    state["last_kind"][vid] = "video"
                    state["notified"].setdefault(vid, int(time.time()))
                    print("Converted to video:", vid, "video_mid:", mid_video)
                else:
                    # si no está en el feed (muy raro), no hacemos nada
                    pass

            # borrar el LIVE antiguo (si procede)
            if DELETE_LIVE_WHEN_FINALIZED and vid in state["msg_video"]:
                # si el LIVE estaba pineado, lo desanclamos antes
                if state.get("pinned_message_id") == live_mid:
                    tg_unpin(bot, chat_id, live_mid)
                tg_delete(bot, chat_id, live_mid)
                state["msg_live"].pop(vid, None)

    # 3) Pin siempre correcto
    if PIN_LATEST:
        target_item, target_info = choose_pin_target(entries)
        target_vid = target_item["vid"]
        target_kind = "live" if target_info["is_live_now"] else "video"

        # Asegurar que existe el mensaje que vamos a pinear
        if target_kind == "live":
            if target_vid not in state["msg_live"]:
                mid = send_post(bot, chat_id, target_item, target_info, "live")
                state["msg_live"][target_vid] = mid
                state["last_kind"][target_vid] = "live"
                state["notified"].setdefault(target_vid, int(time.time()))
            pin_mid = state["msg_live"][target_vid]
        else:
            if target_vid not in state["msg_video"]:
                mid = send_post(bot, chat_id, target_item, target_info, "video")
                state["msg_video"][target_vid] = mid
                state["last_kind"][target_vid] = "video"
                state["notified"].setdefault(target_vid, int(time.time()))
            pin_mid = state["msg_video"][target_vid]

        prev_pin_mid = state.get("pinned_message_id")

        if prev_pin_mid != pin_mid:
            if prev_pin_mid:
                tg_unpin(bot, chat_id, prev_pin_mid)
            if tg_pin(bot, chat_id, pin_mid):
                state["pinned_message_id"] = pin_mid
                state["pinned_vid"] = target_vid
                state["pinned_kind"] = target_kind

    save_state(state)
    print("State saved. last_seen_epoch =", state.get("last_seen_epoch"))


def main():
    run_once()


if __name__ == "__main__":
    main()
