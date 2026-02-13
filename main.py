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


def sanitize_state(state: dict) -> dict:
    """
    Autorepara estados viejos/rotos para evitar crashes.
    Queremos dicts en estas claves:
      notified, messages, kind_by_vid, live_msg_by_vid, video_msg_by_vid, last_msg_by_vid
    """
    if not isinstance(state, dict):
        state = {}

    def ensure_dict(key: str):
        v = state.get(key)
        if isinstance(v, dict):
            return
        # si era lista u otra cosa -> lo tiramos a dict vacío
        state[key] = {}

    ensure_dict("notified")
    ensure_dict("messages")
    ensure_dict("kind_by_vid")
    ensure_dict("live_msg_by_vid")
    ensure_dict("video_msg_by_vid")
    ensure_dict("last_msg_by_vid")

    # pinned_* deben ser valores simples
    if "pinned_message_id" in state and not isinstance(state["pinned_message_id"], int):
        try:
            state["pinned_message_id"] = int(state["pinned_message_id"])
        except Exception:
            state.pop("pinned_message_id", None)

    if "last_seen_epoch" in state and not isinstance(state["last_seen_epoch"], int):
        try:
            state["last_seen_epoch"] = int(state["last_seen_epoch"])
        except Exception:
            state["last_seen_epoch"] = 0

    return state


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

    state["messages"][vid] = message.message_id
    kind = "live" if is_live_now else "video"
    state["kind_by_vid"][vid] = kind

    if kind == "live":
        state["live_msg_by_vid"][vid] = message.message_id
    else:
        state["video_msg_by_vid"][vid] = message.message_id

    return info, message.message_id, kind


def pick_pin_target(entries):
    if not entries:
        return None, None
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

    state = sanitize_state(load_state())

    last_seen_epoch = int(state.get("last_seen_epoch", 0))

    entries = fetch_feed_entries(SCAN_LIMIT)
    if not entries:
        print("No RSS entries.")
        save_state(state)
        return

    newest_epoch = entries[0].get("published_epoch", 0)

    if last_seen_epoch == 0:
        state["last_seen_epoch"] = newest_epoch or int(time.time())
        save_state(state)
        print("Initialized baseline last_seen_epoch =", state["last_seen_epoch"], "(no notifications)")
        return

    # Notificar nuevos por fecha
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
            print("Notified:", vid, it["title"], "msg_id:", mid, "kind:", kind)
        except Exception as e:
            print("Notify failed:", vid, repr(e))

    if newest_epoch and newest_epoch > last_seen_epoch:
        state["last_seen_epoch"] = newest_epoch

    # PIN PRO + limpieza live->video
    if PIN_LATEST:
        target_item, target_info = pick_pin_target(entries)
        if target_item:
            vid = target_item["vid"]
            want_kind = "live" if target_info["is_live_now"] else "video"

            prev_kind = state["kind_by_vid"].get(vid)
            current_mid = state["messages"].get(vid)

            if current_mid:
                state["last_msg_by_vid"][vid] = current_mid

            republish_to_video = (prev_kind == "live" and want_kind == "video")

            if current_mid is None or republish_to_video:
                if republish_to_video and current_mid:
                    state["live_msg_by_vid"].setdefault(vid, current_mid)

                force = "video" if want_kind == "video" else "live"
                _, new_mid, new_kind = send_item(bot, chat_id, target_item, state, force_kind=force)
                state["notified"].setdefault(vid, int(time.time()))
                current_mid = new_mid
                want_kind = new_kind

            prev_pin_mid = state.get("pinned_message_id")
            if current_mid and prev_pin_mid != current_mid:
                if prev_pin_mid:
                    safe_unpin(bot, chat_id, prev_pin_mid)
                if safe_pin(bot, chat_id, current_mid):
                    state["pinned_message_id"] = current_mid
                    state["pinned_vid"] = vid
                    state["pinned_kind"] = want_kind

            if DELETE_LIVE_WHEN_FINALIZED and want_kind == "video":
                live_mid = state["live_msg_by_vid"].get(vid)
                video_mid = state["video_msg_by_vid"].get(vid)

                if not live_mid:
                    maybe = state["last_msg_by_vid"].get(vid)
                    if maybe and maybe != video_mid:
                        live_mid = maybe

                if live_mid and video_mid and live_mid != video_mid:
                    if state.get("pinned_message_id") == live_mid:
                        safe_unpin(bot, chat_id, live_mid)
                    safe_delete(bot, chat_id, live_mid)
                    state["live_msg_by_vid"].pop(vid, None)

    save_state(state)
    print("State saved. last_seen_epoch =", state.get("last_seen_epoch"))


def main():
    run_once()


if __name__ == "__main__":
    main()
