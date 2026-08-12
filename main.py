import os
import json
import time
import requests
import io
import re
from datetime import datetime
from telegram import Bot, InputMediaPhoto

STATE_FILE = "state.json"

# ==========================================================
# CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ==========================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

# Canal 1 Principal
CHANNEL_ID = os.environ.get("CHANNEL_ID") or "UC6efY3r4Oiy0ns4ZEAVw4_A"
CHAT_ID_GROUP_RAW = os.environ.get("CHAT_ID_GROUP") or "-1003839040942_5621" # Vídeos/Directos Ch1
CHAT_ID_POSTS_RAW = os.environ.get("CHAT_ID_POSTS") or "-1003839040942_5801" # Publicaciones Ch1

# Canal 2 Secundario (Directos)
CHANNEL_ID_DIRECTO = os.environ.get("CHANNEL_ID_DIRECTO") or "UCK4h49E7Bol5DD-szyOgFgQ"
CHAT_ID_GROUP_DIRECTO_RAW = os.environ.get("CHAT_ID_GROUP_DIRECTO") or "-1003839040942_5622" # Vídeos/Directos Ch2

# Hilo para Bienvenidas / General
WELCOME_THREAD_ID_RAW = os.environ.get("WELCOME_THREAD_ID") or "1"

# Canal de Registros / Logs
LOG_CHAT_ID_RAW = os.environ.get("LOG_CHAT_ID") or "-1003781665410"

YT_API_KEY = os.environ.get("YT_API_KEY", "")
TZ = os.environ.get("TZ", "Europe/Madrid")
PIN_LATEST = os.environ.get("PIN_LATEST", "1") == "1"
BASELINE_ONLY = os.environ.get("BASELINE_ONLY", "0") == "1"

def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")

def parse_target(raw_str, key_name="default"):
    if not raw_str:
        return None
    thread_id = None
    clean_str = str(raw_str).strip()
    
    if "_" in clean_str:
        parts = clean_str.split("_")
        clean_str = parts[0]
        if parts[1].isdigit():
            thread_id = int(parts[1])
            
    try:
        chat_id = int(clean_str)
    except ValueError:
        chat_id = clean_str
        
    return {"chat_id": chat_id, "thread_id": thread_id, "key": key_name}

def parse_thread_id(raw_val):
    if not raw_val:
        return 1
    val_str = str(raw_val).strip()
    if "_" in val_str:
        parts = val_str.split("_")
        if len(parts) > 1 and parts[1].isdigit():
            return int(parts[1])
    try:
        return int(val_str)
    except ValueError:
        return 1

def load_state():
    st = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
            
    if not isinstance(st, dict):
        st = {}
    
    for key in ["msg_ids", "msg_ids_posts", "vid_status", "pending_users", "pending_welcomes"]:
        if not isinstance(st.get(key), dict): st[key] = {}
    if "last_update_id" not in st or not isinstance(st["last_update_id"], int):
        st["last_update_id"] = 0
    return st

def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

def send_telegram_msg(bot, chat_id, text, thread_id=None, parse_mode="HTML"):
    """Envía un mensaje intentando con thread_id y cayendo a envío directo si es el tema General."""
    kwargs = {"parse_mode": parse_mode}
    if thread_id is not None:
        try:
            kwargs["message_thread_id"] = thread_id
            msg = bot.send_message(chat_id=chat_id, text=text, **kwargs)
            return msg.message_id
        except Exception as e:
            print(f"Aviso: No se pudo enviar con thread_id={thread_id} ({e}). Intentando envío directo al chat...")

    kwargs.pop("message_thread_id", None)
    msg = bot.send_message(chat_id=chat_id, text=text, **kwargs)
    return msg.message_id

def send_log(bot, text):
    """Envía un reporte de actividad al canal de logs privado."""
    log_target = parse_target(LOG_CHAT_ID_RAW, "log_channel")
    if not log_target:
        return
    try:
        log_text = f"🤖 <b>LOG BOT PRO:</b>\n{text}"
        send_telegram_msg(bot, log_target["chat_id"], log_text, thread_id=log_target.get("thread_id"))
    except Exception as e:
        print(f"Error enviando mensaje de log: {e}")

# ==========================================================
# MODERACIÓN Y BIENVENIDAS DE TELEGRAM
# ==========================================================
def check_user_compliance(user):
    reasons = []
    has_username = bool(user.username)
    first_name = (user.first_name or "").strip()
    has_valid_name = len(first_name) >= 3

    if not has_username:
        reasons.append("No tienes un @alias/nombre de usuario configurado.")
    if not has_valid_name:
        reasons.append("Tu nombre de perfil tiene menos de 3 letras o es solo un símbolo.")

    return len(reasons) == 0, reasons

def format_mention(user):
    if user.username:
        return f"@{user.username}"
    name = (user.first_name or "Usuario").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user.id}">{name}</a>'

def send_standard_welcome(bot, group_target, user, thread_id=1):
    mention = format_mention(user)
    text = (
        f"¡Hola {mention}, bienvenido/a a la comunidad! 👋\n\n"
        "La idea de este espacio es ayudarnos entre todos. Si encuentras alguna novedad, herramienta o información interesante que creas que nos puede servir a los demás, ¡compártela!\n\n"
        "También está totalmente permitido compartir enlaces o recursos si con eso ayudamos a resolver la duda de otro miembro.\n\n"
        "⚠️ <b>REQUISITO IMPORTANTE:</b>\n"
        "📌 Recuerda que según las normas del grupo es obligatorio tener un <b>nombre real configurado</b> en tu perfil de Telegram. Si tu cuenta no tiene nombre o solo tiene un punto/símbolo, por favor cámbialo en tus ajustes para evitar que los sistemas de moderación te expulsen.\n\n"
        "🛠️ <b>Antes de empezar:</b>\n"
        "👉 Tienes todas las descargas, tutoriales y enlaces en el <b>MENSAJE FIJADO</b> en la parte superior del chat. ¡Haz clic arriba del todo para verlo!\n"
        "👉 Escribe #normas para leer las reglas rápidas del grupo.\n\n"
        "¡Hagamos de esta una gran comunidad! 🚀"
    )
    return send_telegram_msg(bot, group_target["chat_id"], text, thread_id=thread_id)

def send_warning_message(bot, group_target, user, reasons, thread_id=1):
    mention = format_mention(user)
    reasons_text = "\n".join([f"• {r}" for r in reasons])
    text = (
        f"⚠️ <b>¡ATENCIÓN {mention}! TU PERFIL NO CUMPLE LAS NORMAS</b> ⚠️\n\n"
        "Para mantener el grupo seguro y ordenado, necesitamos que ajustes tu perfil:\n\n"
        f"<b>Motivo:</b>\n{reasons_text}\n\n"
        "<b>¿Cómo solucionarlo?</b>\n"
        "1️⃣ Entra en los Ajustes de Telegram.\n"
        "2️⃣ Ponte un <b>@alias / nombre de usuario</b>.\n"
        "3️⃣ Pon un <b>nombre de perfil con al menos 3 letras reales</b>.\n\n"
        "⏳ <b>Tienes 1 HORA para cambiarlo.</b> Si en 60 minutos no está corregido, el sistema te expulsará automáticamente (podrás volver a entrar en cuanto lo arregles)."
    )
    return send_telegram_msg(bot, group_target["chat_id"], text, thread_id=thread_id)

def process_moderation(bot, group_target, state, welcome_thread_id=1):
    if not group_target:
        return

    last_update_id = state.get("last_update_id", 0)
    try:
        updates = bot.get_updates(
            offset=last_update_id + 1 if last_update_id > 0 else None,
            timeout=5,
            allowed_updates=["message", "chat_member"]
        )

        for u in updates:
            state["last_update_id"] = u.update_id
            new_members = []

            if u.message and u.message.new_chat_members:
                for m in u.message.new_chat_members:
                    if not m.is_bot:
                        new_members.append(m)

            if u.chat_member and u.chat_member.new_chat_member:
                cm = u.chat_member.new_chat_member
                user = getattr(cm, 'user', None) or getattr(u.chat_member, 'user', None)
                if user and not user.is_bot:
                    status = getattr(cm, 'status', '')
                    if status in ["member", "administrator", "creator"]:
                        if user not in new_members:
                            new_members.append(user)

            for member in new_members:
                try:
                    is_compliant, reasons = check_user_compliance(member)
                    if is_compliant:
                        msg_id = send_standard_welcome(bot, group_target, member, thread_id=welcome_thread_id)
                        state["pending_welcomes"][str(msg_id)] = {
                            "msg_id": msg_id,
                            "sent_at": int(time.time())
                        }
                        send_log(bot, f"✅ Bienvenida enviada a {format_mention(member)} (Perfil correcto).")
                    else:
                        msg_id = send_warning_message(bot, group_target, member, reasons, thread_id=welcome_thread_id)
                        state["pending_users"][str(member.id)] = {
                            "user_id": member.id,
                            "joined_at": int(time.time()),
                            "warning_msg_id": msg_id
                        }
                        send_log(bot, f"⚠️ Aviso enviado a {format_mention(member)} por incumplir normas (60 min restantes).")
                except Exception as ex_m:
                    print(f"Error procesando bienvenida individual: {ex_m}")

    except Exception as e:
        print(f"Error en procesar actualizaciones de Telegram: {e}")

    # Expulsión tras 60 minutos si no cumplen las normas
    now = int(time.time())
    pending = state.get("pending_users", {})
    to_remove = []

    for user_id_str, pdata in list(pending.items()):
        user_id = pdata["user_id"]
        joined_at = pdata["joined_at"]
        warning_msg_id = pdata["warning_msg_id"]

        try:
            chat_member = bot.get_chat_member(chat_id=group_target["chat_id"], user_id=user_id)
            user = chat_member.user
            
            if chat_member.status in ["left", "kicked"]:
                to_remove.append(user_id_str)
                continue

            is_compliant, _ = check_user_compliance(user)

            if is_compliant:
                try:
                    bot.delete_message(chat_id=group_target["chat_id"], message_id=warning_msg_id)
                except Exception:
                    pass
                msg_id = send_standard_welcome(bot, group_target, user, thread_id=welcome_thread_id)
                state["pending_welcomes"][str(msg_id)] = {
                    "msg_id": msg_id,
                    "sent_at": int(time.time())
                }
                send_log(bot, f"🎉 El usuario {format_mention(user)} ha corregido su perfil a tiempo.")
                to_remove.append(user_id_str)
            elif now - joined_at >= 3600:
                try:
                    bot.delete_message(chat_id=group_target["chat_id"], message_id=warning_msg_id)
                except Exception:
                    pass

                try:
                    bot.ban_chat_member(chat_id=group_target["chat_id"], user_id=user_id)
                    bot.unban_chat_member(chat_id=group_target["chat_id"], user_id=user_id)
                    send_log(bot, f"🚫 Usuario <code>{user_id}</code> expulsado tras 60 min sin corregir su perfil.")
                except Exception as e:
                    print(f"Error al expulsar usuario {user_id}: {e}")

                to_remove.append(user_id_str)

        except Exception as e:
            print(f"Error comprobando usuario {user_id}: {e}")

    for uid in to_remove:
        if uid in state["pending_users"]:
            del state["pending_users"][uid]

    # Borrado de bienvenida a los 2 minutos (120 s)
    now = int(time.time())
    welcomes = state.get("pending_welcomes", {})
    welcomes_to_remove = []

    for msg_id_str, wdata in list(welcomes.items()):
        msg_id = wdata["msg_id"]
        sent_at = wdata["sent_at"]
        elapsed = now - sent_at

        if elapsed < 120:
            time.sleep(120 - elapsed)

        try:
            bot.delete_message(chat_id=group_target["chat_id"], message_id=msg_id)
        except Exception as e:
            print(f"Error borrando mensaje de bienvenida {msg_id}: {e}")

        welcomes_to_remove.append(msg_id_str)

    for wid in welcomes_to_remove:
        if wid in state["pending_welcomes"]:
            del state["pending_welcomes"][wid]

# ==========================================================
# FUNCIONES YOUTUBE Y ENVÍO DE AVISOS
# ==========================================================
def yt_get(url, params):
    params = dict(params)
    params["key"] = YT_API_KEY
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def get_recent_video_ids(channel_id):
    if not channel_id: return []
    vids = []

    if YT_API_KEY:
        try:
            data_live = yt_get("https://www.googleapis.com/youtube/v3/search", {
                "part": "snippet",
                "channelId": channel_id,
                "type": "video",
                "eventType": "live",
                "maxResults": 3
            })
            for item in data_live.get("items", []):
                vid = item.get("id", {}).get("videoId")
                if vid and vid not in vids:
                    vids.append(vid)
        except Exception as e:
            print(f"Error buscando directos activos para {channel_id}: {e}")

    playlist_id = "UU" + channel_id[2:]
    try:
        data = yt_get("https://www.googleapis.com/youtube/v3/playlistItems", {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": 5
        })
        for item in data.get("items", []):
            vid = item["snippet"]["resourceId"]["videoId"]
            if vid not in vids:
                vids.append(vid)
    except Exception as e:
        print(f"Error obteniendo playlist del canal {channel_id}: {e}")

    try:
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        r = requests.get(rss_url, timeout=15)
        if r.status_code == 200:
            found_vids = re.findall(r'<yt:videoId>(.*?)</yt:videoId>', r.text)
            for v in found_vids[:5]:
                if v not in vids:
                    vids.append(v)
    except Exception as e:
        print(f"Error obteniendo RSS de {channel_id}: {e}")

    return vids

def get_recent_community_posts(channel_id):
    if not channel_id: return []
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
                
        if not data: return []

        raw_posts = []
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
                    if raw_posts: break
        except Exception: pass

        if not raw_posts:
            def extract_posts(obj):
                found = []
                if isinstance(obj, dict):
                    if "backstagePostThreadRenderer" in obj:
                        found.append(obj["backstagePostThreadRenderer"])
                    for k, v in obj.items(): found.extend(extract_posts(v))
                elif isinstance(obj, list):
                    for item in obj: found.extend(extract_posts(item))
                return found

            seen = set()
            for p in extract_posts(data):
                try:
                    pid = p["post"]["backstagePostRenderer"]["postId"]
                    if pid not in seen:
                        seen.add(pid)
                        raw_posts.append(p)
                except: pass

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
                    "is_live": False, "viewers": None, "views": None, "start": None
                })
            except Exception: pass
        return posts
    except Exception: return []

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
    if thumb_url: thumb_url += f"?t={int(time.time())}"

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
        if len(title) > max_len: title = title[:max_len] + "..."
        return f"💬 <b>NUEVA PUBLICACIÓN</b>\n\n{title}\n\n👉 {info['link']}"

    if kind == "live":
        v_line = f"👀 {info['viewers']} viewers\n" if info['viewers'] else ""
        return f"🔴 <b>DIRECTO</b>\n✨ <b>{title}</b>\n{v_line}🕒 {iso_to_local(info['start'])}\n👉 {info['link']}"
    
    v_line = f"👀 {info['views']} views\n" if info['views'] else ""
    return f"🎥 <b>NUEVO VÍDEO</b>\n✨ <b>{title}</b>\n{v_line}🕒 {iso_to_local(info['start'])}\n👉 {info['link']}"

def send_post(bot, target, info, kind):
    cap = format_caption(info, kind)
    kwargs = {"parse_mode": "HTML"}
    if target.get("thread_id") is not None:
        kwargs["message_thread_id"] = target["thread_id"]

    if info.get('thumb'):
        try:
            r = requests.get(info['thumb'], timeout=20)
            msg = bot.send_photo(chat_id=target["chat_id"], photo=r.content, caption=cap, **kwargs)
            return msg.message_id
        except Exception as e:
            print(f"Error enviando foto a {target['chat_id']}: {e}")

    msg = bot.send_message(chat_id=target["chat_id"], text=cap, **kwargs)
    return msg.message_id

def update_msg(bot, target, mid, info, kind):
    cap = format_caption(info, kind)
    try:
        r = requests.get(info['thumb'], timeout=20)
        media = InputMediaPhoto(media=io.BytesIO(r.content), caption=cap, parse_mode="HTML")
        bot.edit_message_media(chat_id=target["chat_id"], message_id=mid, media=media)
        return True
    except Exception:
        try: 
            bot.edit_message_caption(chat_id=target["chat_id"], message_id=mid, caption=cap, parse_mode="HTML")
            return True
        except Exception:
            return False

def process_channel_videos(bot, target, channel_id, state):
    if not target or not channel_id:
        return

    vids = get_recent_video_ids(channel_id)
    newest_vid = vids[0] if vids else None

    if vids:
        vids.reverse()
        for vid in vids:
            info = yt_video_info(vid)
            if not info: continue

            kind = "live" if info["is_live"] else "video"
            mid = state["msg_ids"].get(vid)

            if BASELINE_ONLY:
                if not mid:
                    state["msg_ids"][vid] = -1
                    state["vid_status"][vid] = kind
                continue

            if not mid:
                mid = send_post(bot, target, info, kind)
                state["msg_ids"][vid] = mid
                state["vid_status"][vid] = kind
                send_log(bot, f"📢 Alerta de contenido publicada: <b>{info['title']}</b> ({kind.upper()}).")

                if PIN_LATEST and vid == newest_vid:
                    try:
                        if target.get("thread_id"):
                            bot.unpin_all_chat_messages(chat_id=target["chat_id"], message_thread_id=target["thread_id"])
                        else:
                            bot.unpin_all_chat_messages(chat_id=target["chat_id"])
                        bot.pin_chat_message(chat_id=target["chat_id"], message_id=mid, disable_notification=True)
                    except Exception as e:
                        print(f"Error al fijar mensaje: {e}")

            elif mid != -1:
                old_kind = state["vid_status"].get(vid)
                if old_kind != kind:
                    actual_mid = mid if isinstance(mid, int) else mid.get(target["key"])
                    if actual_mid:
                        if update_msg(bot, target, actual_mid, info, kind):
                            state["vid_status"][vid] = kind
                            send_log(bot, f"🔄 Estado de vídeo actualizado: <b>{info['title']}</b> pasó de {old_kind} a {kind}.")

def process_channel_posts(bot, target, channel_id, state):
    if not target or not channel_id:
        return

    posts = get_recent_community_posts(channel_id)
    if posts:
        latest_post = posts[0]
        post_id = latest_post["vid"]

        if BASELINE_ONLY:
            if post_id not in state["msg_ids_posts"]:
                state["msg_ids_posts"][post_id] = -1
        else:
            if post_id not in state["msg_ids_posts"]:
                mid = send_post(bot, target, latest_post, "post")
                state["msg_ids_posts"][post_id] = mid
                send_log(bot, f"💬 Nueva publicación de comunidad enviada.")

# ==========================================================
# EJECUCIÓN PRINCIPAL
# ==========================================================
def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    bot = Bot(token=TELEGRAM_TOKEN)
    state = load_state()

    target_ch1_vids = parse_target(CHAT_ID_GROUP_RAW, "ch1_vids")       # Hilo 5621
    target_ch1_posts = parse_target(CHAT_ID_POSTS_RAW, "ch1_posts")      # Hilo 5801
    target_ch2_vids = parse_target(CHAT_ID_GROUP_DIRECTO_RAW, "ch2_vids") # Hilo 5622

    welcome_thread_id = parse_thread_id(WELCOME_THREAD_ID_RAW)

    # 1. Moderación y bienvenidas
    if target_ch1_vids:
        process_moderation(bot, target_ch1_vids, state, welcome_thread_id=welcome_thread_id)

    # 2. Canal 1 (UC6efY3r4Oiy0ns4ZEAVw4_A) - Vídeos y Directos -> Hilo 5621
    if CHANNEL_ID and target_ch1_vids:
        process_channel_videos(bot, target_ch1_vids, CHANNEL_ID, state)

    # 3. Canal 1 (UC6efY3r4Oiy0ns4ZEAVw4_A) - Publicaciones Comunidad -> Hilo 5801
    if CHANNEL_ID and target_ch1_posts:
        process_channel_posts(bot, target_ch1_posts, CHANNEL_ID, state)

    # 4. Canal 2 (UCK4h49E7Bol5DD-szyOgFgQ) - Vídeos y Directos -> Hilo 5622
    if CHANNEL_ID_DIRECTO and target_ch2_vids:
        process_channel_videos(bot, target_ch2_vids, CHANNEL_ID_DIRECTO, state)

    save_state(state)

if __name__ == "__main__":
    run_once()
