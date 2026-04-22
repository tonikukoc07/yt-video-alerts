import os
import json
import time
import requests
import io
from datetime import datetime, timezone
from telegram import Bot, InputMediaPhoto
from telegram.error import BadRequest

STATE_FILE = "state.json"

# Variables de entorno
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
YT_API_KEY = os.environ.get("YT_API_KEY", "")
TZ = os.environ.get("TZ", "Europe/Madrid")
PIN_LATEST = os.environ.get("PIN_LATEST", "1") == "1"
BASELINE_ONLY = os.environ.get("BASELINE_ONLY", "0") == "1"

def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")

def load_state():
    if not os.path.exists(STATE_FILE): return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception: return {}
    for key in ["notified", "msg_ids", "pinned", "thumbs"]:
        if not isinstance(st.get(key), dict): st[key] = {}
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

def get_latest_video_id():
    """
    Usa la lista de 'Uploads' del canal (Cuesta 1 punto).
    Convierte el Channel ID (UC...) en Playlist ID (UU...)
    """
    playlist_id = "UU" + CHANNEL_ID[2:]
    data = yt_get("https://www.googleapis.com/youtube/v3/playlistItems", {
        "part": "snippet",
        "playlistId": playlist_id,
        "maxResults": 1
    })
    items = data.get("items", [])
    return items[0]["snippet"]["resourceId"]["videoId"] if items else None

def yt_video_info(video_id):
    """ Obtiene detalles del vídeo (Cuesta 1 punto) """
    data = yt_get("https://www.googleapis.com/youtube/v3/videos", {
        "part": "snippet,statistics,liveStreamingDetails",
        "id": video_id
    })
    items = data.get("items", [])
    if not items: return None
    v = items[0]
    snippet = v.get("snippet", {})
    live = v.get("liveStreamingDetails", {})
    stats = v.get("statistics", {})

    # URL de imagen con bypass de caché
    thumbs = snippet.get("thumbnails", {}) or {}
    thumb_url = (thumbs.get("maxres") or thumbs.get("high") or thumbs.get("default", {})).get("url")
    if thumb_url:
        thumb_url += f"?t={int(time.time())}"

    return {
        "vid": video_id,
        "title": snippet.get("title", ""),
        "thumb": thumb_url,
        "link": f"https://www.youtube.com/watch?v={video_id}",
        "is_live": snippet.get("liveBroadcastContent") == "live",
        "viewers": live.get("concurrentViewers"),
        "views": stats.get("viewCount"),
        "start": live.get("actualStartTime") or snippet.get("publishedAt")
    }

def iso_to_local(iso_str):
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ)
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(tz).strftime("%d/%m %H:%M")
    except: return iso_str

def format_caption(info, kind):
    title = info["title"].replace("<", "&lt;").replace(">", "&gt;") # Seguridad HTML
    if kind == "live":
        v_line = f"👀 {info['viewers']} viewers\n" if info['viewers'] else ""
        return f"🔴 <b>DIRECTO</b>\n✨ <b>{title}</b>\n{v_line}🕒 {iso_to_local(info['start'])}\n\n👉 {info['link']}"
    
    v_line = f"👀 {info['views']} views\n" if info['views'] else ""
    return f"🎥 <b>NUEVO VÍDEO</b>\n✨ <b>{title}</b>\n{v_line}🕒 {iso_to_local(info['start'])}\n\n👉 {info['link']}"

def send_post(bot, chat_id, info, kind):
    cap = format_caption(info, kind)
    if info['thumb']:
        try:
            r = requests.get(info['thumb'], timeout=20)
            msg = bot.send_photo(chat_id=chat_id, photo=r.content, caption=cap, parse_mode="HTML")
            return msg.message_id
        except: pass
    return bot.send_message(chat_id=chat_id, text=cap, parse_mode="HTML").message_id

def update_msg(bot, chat_id, mid, info, kind):
    cap = format_caption(info, kind)
    try:
        # Forzamos cambio de foto al pasar a video
        r = requests.get(info['thumb'], timeout=20)
        media = InputMediaPhoto(media=io.BytesIO(r.content), caption=cap, parse_mode="HTML")
        bot.edit_message_media(chat_id=chat_id, message_id=mid, media=media)
        return True
    except:
        try: return bot.edit_message_caption(chat_id=chat_id, message_id=mid, caption=cap, parse_mode="HTML")
        except: return False

def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)
    state = load_state()

    vid = get_latest_video_id()
    if not vid: return
    info = yt_video_info(vid)
    if not info: return

    kind = "live" if info["is_live"] else "video"
    notif_key = f"{kind}:{vid}"

    # Modo Baseline (Silencioso)
    if BASELINE_ONLY:
        state["notified"][notif_key] = int(time.time())
        save_state(state)
        return

    mid = None
    # 1. ¿Ya se notificó como directo pero ahora es vídeo? -> EDITAR
    live_mid = state["msg_ids"].get(f"live:{vid}")
    if live_mid and kind == "video" and f"video:{vid}" not in state["notified"]:
        if update_msg(bot, chat_id, live_mid, info, "video"):
            state["notified"][notif_key] = int(time.time())
            mid = live_mid

    # 2. Notificación normal (Nuevo)
    elif notif_key not in state["notified"]:
        mid = send_post(bot, chat_id, info, kind)
        state["notified"][notif_key] = int(time.time())
        state["msg_ids"][notif_key] = mid

    # 3. Pin Logic
    if PIN_LATEST and mid:
        try:
            bot.unpin_all_chat_messages(chat_id=chat_id)
            bot.pin_chat_message(chat_id=chat_id, message_id=mid, disable_notification=True)
        except: pass

    save_state(state)

if __name__ == "__main__":
    run_once()
