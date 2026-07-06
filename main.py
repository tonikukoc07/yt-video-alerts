import os
import json
import time
import requests
import io
import re
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
    
    for key in ["msg_ids", "msg_ids_posts", "vid_status"]:
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
    playlist_id = "UU" + CHANNEL_ID[2:]
    data = yt_get("https://www.googleapis.com/youtube/v3/playlistItems", {
        "part": "snippet",
        "playlistId": playlist_id,
        "maxResults": 3
    })
    items = data.get("items", [])
    return [item["snippet"]["resourceId"]["videoId"] for item in items]

def get_recent_community_posts(channel_id):
    url = f"https://www.youtube.com/channel/{channel_id}/community"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Accept-Language": "es-ES,es;q=0.9"
    }
    try:
        r = requests.get(url, headers=headers, timeout=20)
        
        data = None
        for pattern in [r'var ytInitialData\s*=\s*({.*?});', r'window\["ytInitialData"\]\s*=\s*({.*?});', r'ytInitialData\s*=\s*({.*?});(?:</script>|\n)']:
            match = re.search(pattern, r.text)
            if match:
                data = json.loads(match.group(1))
                break
                
        if not data:
            print("No se encontró la base de datos interna de YouTube.")
            return []

        raw_posts = []
        
        # 1. Intentar ruta limpia estructurada (mantiene orden cronológico perfecto)
        try:
            tabs = data.get("contents", {}).get("twoColumnBrowseResultsRenderer", {}).get("tabs", [])
            for tab in tabs:
                tr = tab.get("tabRenderer", {})
                canonical = tr.get("endpoint", {}).get("browseEndpoint", {}).get("canonicalBaseUrl", "").lower()
                if "community" in canonical or tr.get("selected"):
                    s_contents = tr.get("content", {}).get("sectionListRenderer", {}).get("contents", [])
                    for sc in s_contents:
                        if "itemSectionRenderer" in sc:
                            items = sc.get("itemSectionRenderer", {}).get("contents", [])
                            for item in items:
                                if "backstagePostThreadRenderer" in item:
                                    raw_posts.append(item["backstagePostThreadRenderer"])
                    if raw_posts:
                        break
        except Exception as e:
            print(f"Error en ruta estructurada: {e}")

        # 2. Si la ruta limpia falla, usar rastreador recursivo como salvavidas
        if not raw_posts:
            def extract_posts(obj):
                found = []
                if isinstance(obj, dict):
                    if "backstagePostThreadRenderer" in obj:
                        found.append(obj["backstagePostThreadRenderer"])
                    for k, v in obj.items():
                        found.extend(extract_posts(v))
                elif isinstance(obj, list):
                    for item in obj:
                        found.extend(extract_posts(item))
                return found

            seen = set()
            for p in extract_posts(data):
                try:
                    pid = p["post"]["backstagePostRenderer"]["postId"]
                    if pid not in seen:
                        seen.add(pid)
                        raw_posts.append(p)
                except:
                    pass

        posts = []
        for item in raw_posts:
            try:
                post_renderer = item["post"]["backstagePostRenderer"]
                post_id = post_renderer["postId"]
                
                text = ""
                if "contentText" in post_renderer and "runs" in post_renderer["contentText"]:
                    text = "".join([run.get("text", "") for run in post_renderer["contentText"]["runs"]])
                
                thumb_url = None
                attachment = post_renderer.get("backstageAttachment", {})
                if "backstageImageRenderer" in attachment:
                    thumbnails = attachment["backstageImageRenderer"].get("image", {}).get("thumbnails", [])
                    if thumbnails: thumb_url = thumbnails[-1]["url"]
                elif "postMultiImageRenderer" in attachment:
                    images = attachment["postMultiImageRenderer"].get("images", [])
                    if images:
                        thumbnails = images[0].get("backstageImageRenderer", {}).get("image", {}).get("thumbnails", [])
                        if thumbnails: thumb_url = thumbnails[-1]["url"]
                            
                posts.append({
                    "vid": post_id,
                    "title": text or "Publicación sin texto",
                    "thumb": thumb_url,
                    "link": f"https://www.youtube.com/post/{post_id}",
                    "is_live": False,
                    "viewers": None,
                    "views": None,
                    "start": None
                })
            except Exception as e:
                print(f"Error extrayendo datos de un post: {e}")
                
        return posts
    except Exception as e:
        print(f"Error de conexión al canal: {e}")
    return []

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

def iso_to_local(iso_str):
    if not iso_str: return ""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ)
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(tz).strftime("%d/%m %H:%M")
    except: return iso_str

def format_caption(info, kind):
    title = info["title"].replace("<", "&lt;").replace(">", "&gt;")
    
    if kind == "post":
        max_len = 900 if info.get('thumb') else 4000
        if len(title) > max_len:
            title = title[:max_len] + "..."
        return f"💬 <b>NUEVA PUBLICACIÓN</b>\n\n{title}\n\n👉 {info['link']}"

    if kind == "live":
        v_line = f"👀 {info['viewers']} viewers\n" if info['viewers'] else ""
        return f"🔴 <b>DIRECTO</b>\n✨ <b>{title}</b>\n{v_line}🕒 {iso_to_local(info['start'])}\n👉 {info['link']}"
    
    v_line = f"👀 {info['views']} views\n" if info['views'] else ""
    return f"🎥 <b>NUEVO VÍDEO</b>\n✨ <b>{title}</b>\n{v_line}🕒 {iso_to_local(info['start'])}\n👉 {info['link']}"

def send_post(bot, chat_id, info, kind):
    cap = format_caption(info, kind)
    if info.get('thumb'):
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

    # ========================================
    # 1. PROCESAR VÍDEOS / DIRECTOS
    # ========================================
    vids = get_recent_video_ids()
    newest_vid = vids[0] if vids else None
    if vids:
        vids.reverse() 
        for vid in vids:
            info = yt_video_info(vid)
            if not info: continue

            kind = "live" if info["is_live"] else "video"
            mid = state["msg_ids"].get(vid)
            if not mid: mid = state["msg_ids"].get(f"live:{vid}") or state["msg_ids"].get(f"video:{vid}")
            
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
                if not old_kind: old_kind = "live" if f"live:{vid}" in state.get("msg_ids", {}) else "video"
                if old_kind != kind:
                    if update_msg(bot, chat_id, mid, info, kind):
                        state["vid_status"][vid] = kind

    # ========================================
    # 2. PROCESAR PUBLICACIONES (COMUNIDAD)
    # ========================================
    posts = get_recent_community_posts(CHANNEL_ID)
    if posts:
        # ATENCIÓN: Solo evaluamos la publicación número 0 (la más nueva del canal)
        latest_post = posts[0]
        post_id = latest_post["vid"]
        
        if BASELINE_ONLY:
            if post_id not in state["msg_ids_posts"]:
                state["msg_ids_posts"][post_id] = -1
        else:
            # Si el post más reciente no está en la memoria de state.json, se publica.
            # Cualquier post anterior o intermedio es completamente ignorado.
            if post_id not in state["msg_ids_posts"]:
                mid = send_post(bot, chat_id, latest_post, "post")
                state["msg_ids_posts"][post_id] = mid

    save_state(state)

if __name__ == "__main__":
    run_once()
