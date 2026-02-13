import os
import json
import time
import datetime
import feedparser
import requests
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
YT_API_KEY = os.environ.get("YT_API_KEY", "")

SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "25"))
PIN_LATEST = os.environ.get("PIN_LATEST", "1") == "1"
DELETE_LIVE_WHEN_FINALIZED = os.environ.get("DELETE_LIVE_WHEN_FINALIZED", "1") == "1"

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
            {"vid": vid, "title": title, "link": link, "thumb": thumb, "published_epoch": pub_epoch}
        )
    return out  # newest-first


def yt_api_video_info(video_id: str):
    """Fuente de verdad para LIVE y viewers/views."""
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

    # ✅ LIVE REAL: empezó y no terminó
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


def safe_delete(bot: Bot, chat_id: int, message_id: int):
    try:
        bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        print("delete failed (ignored):", repr(e))
        return False


def send_item(bot: Bot, chat_id: int, item: dict, state: dict, force_kind: str | None = None):
    """
    Publica en Telegram.
    force_kind:
      - None => decide por API en ese momento
      - "live" o "video" => fuerza el emoji/texto aunque el API diga otra cosa (lo usamos SOLO en transiciones)
    """
    vid = item["vid"]
    info = yt_api_video_info(vid)

    is_live_now = info["is_live_now"]
    if force_kind == "live":
        is_live_now = True
    elif force_kind == "video":
        is_live_now = False

    info_for_caption = dict(info)
    info_for_caption["is_live_now"] = is_live_now
    caption = build_caption(item, info_for_caption)

    message = None
    if item.get("thumb"):
        try:
            photo_bytes = download_bytes(item["thumb"])
            message = bot.send_photo(chat_id=chat_id, photo=photo_bytes, caption=caption)
        except Exception as e:
            print("send_photo failed; fallback to text:", repr(e))

    if message is None:
        message = bot.send_message(chat_id=chat_id, text=caption)

    state.setdefault("messages", {})
    state.setdefault("kind_by_vid", {})
    state["messages"][vid] = message.message_id
    state["kind_by_vid"][vid] = "live" if is_live_now else "video"

    return info, message.message_id, ("live" if is_live_now else "video")


def pick_pin_target(entries):
    """Prioridad: si hay live activo -> live; si no -> último vídeo."""
    if not entries:
        return None, None
    # live activo (si existe)
    for it in entries:
        try:
            info = yt_api_video_info(it["vid"])
            if info["is_live_now"]:
                return it, info
        except Exception as e:
            print("live check failed (ignored):", repr(e))
    # si no, el más reciente
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
    state.setdefault("notified", {})       # vid -> timestamp
    state.setdefault("messages", {})       # vid -> telegram message_id (último que publicó el bot para ese vid)
    state.setdefault("kind_by_vid", {})    # vid -> "live"/"video"
    state.setdefault("live_msg_by_vid", {})  # vid -> message_id del post LIVE (si lo hubo)
    state.setdefault("video_msg_by_vid", {}) # vid -> message_id del post VIDEO (si lo hubo)

    last_seen_epoch = int(state.get("last_seen_epoch", 0))

    entries = fetch_feed_entries(SCAN_LIMIT)
    if not entries:
        print("No RSS entries.")
        return

    newest_epoch = entries[0].get("published_epoch", 0)

    # ✅ Primera ejecución: baseline (no spam)
    if last_seen_epoch == 0:
        state["last_seen_epoch"] = newest_epoch or int(time.time())
        save_state(state)
        print("Initialized baseline last_seen_epoch =", state["last_seen_epoch"], "(no notifications)")
        return

    # 1) Notificar nuevos (por fecha)
    entries_sorted = sorted(entries, key=lambda x: x.get("published_epoch", 0))
    new_items = [it for it in entries_sorted if (it.get("published_epoch", 0) > last_seen_epoch)]

    for it in new_items:
        vid = it["vid"]
        if not vid:
            continue
        if vid in state["notified"]:
            continue

        try:
            info, mid, kind = send_item(bot, chat_id, it, state)
            state["notified"][vid] = int(time.time())

            if kind == "live":
                state["live_msg_by_vid"][vid] = mid
            else:
                state["video_msg_by_vid"][vid] = mid

            print("Notified:", vid, it["title"], "msg_id:", mid, "kind:", kind)
        except Exception as e:
            print("Notify failed:", vid, repr(e))

    if newest_epoch and newest_epoch > last_seen_epoch:
        state["last_seen_epoch"] = newest_epoch

    # 2) PIN PRO + Limpieza: si el live terminó -> borrar el live y dejar el vídeo
    if PIN_LATEST:
        target_item, target_info = pick_pin_target(entries)
        if target_item:
            vid = target_item["vid"]
            want_kind = "live" if target_info["is_live_now"] else "video"

            # Si antes lo teníamos como live y ahora ya NO es live => republicar como vídeo
            prev_kind = state["kind_by_vid"].get(vid)
            republish_to_video = (prev_kind == "live" and want_kind == "video")

            msg_id = state["messages"].get(vid)

            if msg_id is None or republish_to_video:
                # republica (forzando etiqueta para que sea consistente)
                force = "video" if want_kind == "video" else "live"
                info, msg_id, kind = send_item(bot, chat_id, target_item, state, force_kind=force)

                state["notified"].setdefault(vid, int(time.time()))
                if kind == "live":
                    state["live_msg_by_vid"][vid] = msg_id
                else:
                    state["video_msg_by_vid"][vid] = msg_id

                print("Pin target posted/reposted:", vid, "msg_id:", msg_id, "kind:", kind)

            # Pin: si cambia el message_id, unpin anterior y pin nuevo
            prev_pin_mid = state.get("pinned_message_id")
            if msg_id and prev_pin_mid != msg_id:
                if prev_pin_mid:
                    safe_unpin(bot, chat_id, prev_pin_mid)
                if safe_pin(bot, chat_id, msg_id):
                    state["pinned_message_id"] = msg_id
                    state["pinned_vid"] = vid
                    state["pinned_kind"] = want_kind

            # ✅ Si el directo terminó y tenemos un live_msg distinto, borrarlo
            if DELETE_LIVE_WHEN_FINALIZED and want_kind == "video":
                live_mid = state["live_msg_by_vid"].get(vid)
                video_mid = state["video_msg_by_vid"].get(vid)

                # borra solo si existe y no es el mismo mensaje
                if live_mid and video_mid and live_mid != video_mid:
                    safe_delete(bot, chat_id, live_mid)
                    # limpia para que no lo intente borrar otra vez
                    state["live_msg_by_vid"].pop(vid, None)

    save_state(state)
    print("State saved. last_seen_epoch =", state.get("last_seen_epoch"))


def main():
    run_once()


if __name__ == "__main__":
    main()
