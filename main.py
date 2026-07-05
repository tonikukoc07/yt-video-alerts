import os
import json
import time
import requests
import io
import re
import html
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
    # Aseguramos que existan las claves necesarias, añadimos 'community_posts'
    for key in ["msg_ids", "vid_status", "community_posts"]:
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

def get_recent_video_ids():
    """Trae los 3 últimos vídeos de la lista Uploads."""
    playlist_id = "UU" + CHANNEL_ID[2:]
    data = yt_get("https://www.googleapis.com/youtube/v3/playlistItems", {
        "part": "snippet",
        "playlistId": playlist_id,
        "maxResults": 3
    })
    items = data.get("items", [])
    return [item["snippet"]["resourceId"]["videoId"] for item in items]

def yt_video_info(video_id):
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

def get_latest_community_post():
    """
    Extrae la última publicación (Community Post) rastreando el HTML.
    La API oficial no lo soporta, así que lo hacemos manualmente.
    """
    try:
        url = f"https://www.youtube.com/channel/{CHANNEL_ID}/community"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "es-ES,es;q=0.9"
        }
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code != 200: return None
        
        # Buscamos los datos iniciales que YouTube carga en la web
        match = re.search(r'var ytInitialData = (\{.*?\});</script>', r.text)
        if not match: return None
        
        data = json.loads(match.group(1))
        
        # Función recursiva para buscar el post en el caos del JSON de YouTube
        def find_key(obj, key):
            if isinstance(obj, dict):
                if key in obj: return obj[key]
                for k, v in obj.items():
                    res = find_key(v, key)
                    if res: return res
            elif isinstance(obj, list):
                for item in obj:
                    res = find_key(item, key)
                    if res: return res
            return None

        post_renderer = find_key(data, 'backstagePostRenderer')
        if not post_renderer: return None
        
        post_id = post_renderer.get('postId')
        if not post_id: return None
        
        # Extraer el texto de la publicación
        text_runs = post_renderer.get('contentText', {}).get('runs', [])
        text = "".join([run.get('text', '') for run in text_runs])
        
        # Extraer la imagen (si ha subido alguna)
        image_url = None
        attachments = post_renderer.get('backstageAttachment', {})
        if 'backstageImageRenderer' in attachments:
            thumbs = attachments['backstageImageRenderer'].get('image', {}).get('thumbnails', [])
            if thumbs: image_url = thumbs[-1].get('url')
        elif 'postMultiImageRenderer' in attachments:
            # Si sube varias imágenes, pillamos la primera
            images = attachments['postMultiImageRenderer'].get('images', [])
            if images:
                thumbs = images[0].get('backstageImageRenderer', {}).get('image', {}).get('thumbnails', [])
                if thumbs: image_url = thumbs[-1].get('url')
                
        return {
            "id": post_id,
            "text": text.strip(),
            "image": image_url,
            "link": f"https://www.youtube.com/post/{post_id}"
        }
    except Exception:
        return None

def iso_to_local(iso_str):
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ)
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(tz).strftime("%d/%m %H:%M")
    except: return iso_str

def format_caption(info, kind):
    title = info["title"].replace("<", "&lt;").replace(">", "&gt;")
    if kind == "live":
        v_line = f"👀 {info['viewers']} viewers\n" if info['viewers'] else ""
        return f"🔴 <b>DIRECTO</b>\n✨ <b>{title}</b>\n{v_line}🕒 {iso_to_local(info['start'])}\n👉 {info['link']}"
    
    v_line = f"👀 {info['views']} views\n" if info['views'] else ""
    return f"🎥 <b>NUEVO VÍDEO</b>\n✨ <b>{title}</b>\n{v_line}🕒 {iso_to_local(info['start'])}\n👉 {info['link']}"

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
        r = requests.get(info['thumb'], timeout=20)
        media = InputMediaPhoto(media=io.BytesIO(r.content), caption=cap, parse_mode="HTML")
        bot.edit_message_media(chat_id=chat_id, message_id=mid, media=media)
        return True
    except:
        try: 
            bot.edit_message_caption(chat_id=chat_id, message_id=mid, caption=cap, parse_mode="HTML")
            return True
        except: return False

def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)
    state = load_state()

    # --- PARTE 1: GESTIÓN DE VÍDEOS Y DIRECTOS ---
    vids = get_recent_video_ids()
    if vids:
        newest_vid = vids[0]
        vids.reverse() # Procesamos de más antiguo a más nuevo

        for vid in vids:
            info = yt_video_info(vid)
            if not info: continue

            kind = "live" if info["is_live"] else "video"

            mid = state["msg_ids"].get(vid)
            if not mid:
                mid = state["msg_ids"].get(f"live:{vid}") or state["msg_ids"].get(f"video:{vid}")
                if mid:
                    state["msg_ids"][vid] = mid

            if BASELINE_ONLY:
                if not mid:
                    state["msg_ids"][vid] = -1
                    state["vid_status"][vid] = kind
                continue

            if not mid:
                mid = send_post(bot, chat_id, info, kind)
                state["msg_ids"][vid] = mid
                state["vid_status"][vid] = kind

                if PIN_LATEST and vid == newest_vid:
                    try:
                        bot.unpin_all_chat_messages(chat_id=chat_id)
                        bot.pin_chat_message(chat_id=chat_id, message_id=mid, disable_notification=True)
                    except: pass

            elif mid != -1:
                old_kind = state["vid_status"].get(vid)
                if not old_kind:
                    old_kind = "live" if f"live:{vid}" in state.get("msg_ids", {}) else "video"

                if old_kind != kind:
                    if update_msg(bot, chat_id, mid, info, kind):
                        state["vid_status"][vid] = kind

    # --- PARTE 2: GESTIÓN DE PUBLICACIONES (COMMUNITY) ---
    post = get_latest_community_post()
    if post:
        post_id = post["id"]
        if BASELINE_ONLY:
            if post_id not in state.get("community_posts", {}):
                state["community_posts"][post_id] = -1
        else:
            if post_id not in state.get("community_posts", {}):
                safe_text = html.escape(post['text'])
                
                # Limitamos el texto porque Telegram no deja poner descripciones gigantes en las fotos
                if len(safe_text) > 850:
                    safe_text = safe_text[:850] + " [...]"
                
                if safe_text:
                    cap = f"💬 <b>NUEVA PUBLICACIÓN</b>\n\n{safe_text}\n\n👉 {post['link']}"
                else:
                    cap = f"💬 <b>NUEVA PUBLICACIÓN</b>\n\n👉 {post['link']}"
                
                mid = None
                
                # Si tiene foto, intentamos mandar el mensaje con foto
                if post['image']:
                    try:
                        r = requests.get(post['image'], timeout=20)
                        msg = bot.send_photo(chat_id=chat_id, photo=r.content, caption=cap, parse_mode="HTML")
                        mid = msg.message_id
                    except: pass
                
                # Si falló la foto o no tenía, lo mandamos solo como texto
                if not mid:
                    try:
                        msg = bot.send_message(chat_id=chat_id, text=cap, parse_mode="HTML")
                        mid = msg.message_id
                    except: pass
                
                # Guardamos para no repetir
                if mid:
                    state["community_posts"][post_id] = mid

    save_state(state)

if __name__ == "__main__":
    run_once()
