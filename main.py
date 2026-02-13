import os
import json
import time
import datetime
import feedparser
import requests
from telegram import Bot

# -------------------------
# ENV
# -------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
YT_API_KEY = os.environ.get("YT_API_KEY", "")

SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "25"))  # subimos ventana para pillar "Parte 10" si sale "Parte 11" enseguida
TZ = os.environ.get("TZ", "Europe/Madrid")
PIN_LATEST = os.environ.get("PIN_LATEST", "1") == "1"

STATE_FILE = "state.json"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
YOUTUBE_VIDEOS_API = "https://www.googleapis.com/youtube/v3/videos"


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


def to_epoch(published_parsed):
    if not published_parsed:
        return 0
    return int(time.mktime(published_parsed))


def fmt_local_time(epoch: int) -> str:
    # En Linux, con TZ env, fromtimestamp usa esa TZ
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

        out.append(
            {
                "vid": vid,
                "title": title,
                "link": link,
                "thumb": thumb,
                "published_epoch": pub_epoch,
            }
        )
    return out  # normalmente newest-first


def yt_api_video_info(video_id: str):
    """
    Devuelve:
      - is_live_now: True solo si está EN VIVO ahora mismo (actualStartTime y sin actualEndTime)
      - concurrent_viewers: int|None (solo si live)
      - view_count: int|None
      - actual_start_epoch: int|None
      - actual_end_epoch: int|None
    """
    if not video_id:
        return {
            "is_live_now": False,
            "concurrent_viewers": None,
            "view_count": None,
            "actual_start_epoch": None,
            "actual_end_epoch": None,
        }

    params = {"part": "liveStreamingDetails,statistics", "id": video_id, "key": YT_API_KEY}
    r = requests.get(YOUTUBE_VIDEOS_API, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    items = data.get("items", [])
    if not items:
        return {
            "is_live_now": False,
            "concurrent_viewers": None,
            "view_count": None,
            "actual_start_epoch": None,
            "actual_end_epoch": None,
        }

    item = items[0]
    live = item.get("liveStreamingDetails", {}) or {}
    stats = item.get("statistics", {}) or {}

    actual_start = live.get("actualStartTime")
    actual_end = live.get("actualEndTime")

    is_live_now = bool(actual_start) and not bool(actual_end)

    concurrent = live.get("concurrentViewers")
    view_count = stats.get("viewCount")

    def iso_to_epoch(iso):
        if not iso:
            return None
        try:
            dt = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
            return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
        except Exception:
            return None

    return {
        "is_live_now": is_live_now,
        "concurrent_viewers": int(concurrent) if concurrent is not None else None,
        "view_count": int(view_count) if view_count is not None else None,
        "actual_start_epoch": iso_to_epoch(actual_start),
        "actual_end_epoch": iso_to_epoch(actual_end),
    }


def download_bytes(url: str):
    if not url:
        return None
    r = requests.get(url, timeout=15)
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


def safe_pin(bot: Bot, chat_id: int, message_id: int):
    try:
        bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
        return True
    except Exception as e:
        print("pin failed (ignored):", repr(e))
        return False


def safe_unpin(bot: Bot, chat_id: int, message_id: int):
    try:
        bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        print("unpin failed (ignored):", repr(e))
        return False


def send_item(bot: Bot, chat_id: int, item: dict, state: dict, reason: str = ""):
    vid = item["vid"]
    info = yt_api_video_info(vid)
    caption = build_caption(item, info)

    if reason:
        # opcional: etiqueta interna (no visible) en logs
        print(f"Sending {vid} reason={reason} live={info['is_live_now']}")

    message = None
    if item.get("thumb"):
        try:
            photo_bytes = download_bytes(item["thumb"])
            message = bot.send_photo(chat_id=chat_id, photo=photo_bytes, caption=caption)
        except Exception as e:
            print("send_photo failed; fallback to text:", repr(e))

    if message is None:
        message = bot.send_message(chat_id=chat_id, text=caption)

    # Guardamos message_id para poder fijar SIN repost extra
    state.setdefault("messages", {})
    state["messages"][vid] = message.message_id

    # Guardamos el tipo de ese post (live/video) por si luego queremos “finalizar directo”
    state.setdefault("posted_kind", {})
    state["posted_kind"][vid] = "live" if info["is_live_now"] else "video"

    return info, message.message_id


def choose_pin_target(entries):
    """
    Opción B:
      - si hay DIRECTO activo ahora mismo → fijar el más reciente que esté live
      - si no → fijar el más reciente del feed
    """
    if not entries:
        return None

    # entries viene newest-first
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


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)
    must_env("YT_API_KEY", YT_API_KEY)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    state.setdefault("notified", {})      # vid -> epoch notified
    state.setdefault("messages", {})      # vid -> telegram message_id
    state.setdefault("posted_kind", {})   # vid -> 'live'/'video'
    last_seen_epoch = int(state.get("last_seen_epoch", 0))

    entries = fetch_feed_entries(SCAN_LIMIT)
    if not entries:
        print("No RSS entries.")
        return

    newest_epoch = entries[0].get("published_epoch", 0)

    # Primera ejecución: baseline sin spamear
    if last_seen_epoch == 0:
        state["last_seen_epoch"] = newest_epoch or int(time.time())
        save_state(state)
        print("Initialized baseline last_seen_epoch =", state["last_seen_epoch"], "(no notifications)")
        return

    # Procesar “nuevos” por published_epoch
    entries_sorted = sorted(entries, key=lambda x: x.get("published_epoch", 0))
    new_items = [it for it in entries_sorted if (it.get("published_epoch", 0) > last_seen_epoch)]

    for it in new_items:
        vid = it["vid"]
        if not vid:
            continue
        if vid in state["notified"]:
            continue  # evita duplicados aunque lo lances a mano

        try:
            info, mid = send_item(bot, chat_id, it, state, reason="new_upload")
            state["notified"][vid] = int(time.time())
            print("Notified:", vid, it["title"], "msg_id:", mid, "live:", info["is_live_now"])
        except Exception as e:
            print("Notify failed:", vid, repr(e))

    # actualizar último visto al más nuevo del feed
    if newest_epoch and newest_epoch > last_seen_epoch:
        state["last_seen_epoch"] = newest_epoch

    # -------------------------
    # PIN PRO (Opción B)
    # -------------------------
    if PIN_LATEST:
        target = choose_pin_target(entries)
        if target:
            target_item, target_info = target
            target_vid = target_item["vid"]
            want_kind = "live" if target_info["is_live_now"] else "video"

            prev_pin_vid = state.get("pinned_vid")
            prev_pin_mid = state.get("pinned_message_id")
            prev_pin_kind = state.get("pinned_kind")  # live/video

            # Si seguimos fijando el mismo vídeo pero cambió de live->video,
            # REPUBLICAMOS una versión "🎥" y re-fijamos (esto es lo pro).
            republish_needed = False
            if prev_pin_vid == target_vid:
                if prev_pin_kind == "live" and want_kind == "video":
                    republish_needed = True

            # Asegurar que existe el mensaje del objetivo
            msg_id = state["messages"].get(target_vid)

            if msg_id is None or republish_needed:
                try:
                    # Si republish_needed: publicamos de nuevo (ahora ya será 🎥) y cambiamos pin al nuevo msg_id
                    info, msg_id = send_item(
                        bot, chat_id, target_item, state,
                        reason="pin_republish" if republish_needed else "pin_missing_post"
                    )
                    # OJO: no marcamos "notified" si ya lo estaba, pero si no lo estaba, lo marcamos
                    state["notified"].setdefault(target_vid, int(time.time()))
                    want_kind = "live" if info["is_live_now"] else "video"
                    print("Pin target posted/reposted:", target_vid, "msg_id:", msg_id, "kind:", want_kind)
                except Exception as e:
                    print("Could not post target for pin (ignored):", repr(e))
                    msg_id = None

            if msg_id:
                # Si ya está fijado EXACTAMENTE ese message_id, nada
                if state.get("pinned_message_id") == msg_id:
                    pass
                else:
                    # desfijar anterior si existe
                    if prev_pin_mid:
                        safe_unpin(bot, chat_id, prev_pin_mid)

                    # fijar nuevo
                    ok = safe_pin(bot, chat_id, msg_id)
                    if ok:
                        state["pinned_message_id"] = msg_id
                        state["pinned_vid"] = target_vid
                        state["pinned_kind"] = want_kind
                        print("Pinned:", target_vid, "message_id:", msg_id, "kind:", want_kind)

    save_state(state)
    print("State saved. last_seen_epoch =", state.get("last_seen_epoch"))


def main():
    run_once()


if __name__ == "__main__":
    main()
