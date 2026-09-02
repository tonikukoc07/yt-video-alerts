import os
import json
import time
import asyncio
import requests
import io
import re
from datetime import datetime, timezone
from threading import Thread
from flask import Flask
from telegram import Bot, InputMediaPhoto

STATE_MARKER = "🤖 <b>ESTADO DEL BOT (NO BORRAR)</b>"
LAST_SAVED_JSON = ""

# ==========================================================
# CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ==========================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

CHANNEL_ID = os.environ.get("CHANNEL_ID") or "UC6efY3r4Oiy0ns4ZEAVw4_A"
CHAT_ID_GROUP_RAW = os.environ.get("CHAT_ID_GROUP") or "-1003839040942_5621"
CHAT_ID_POSTS_RAW = os.environ.get("CHAT_ID_POSTS") or "-1003839040942_5801"

CHANNEL_ID_DIRECTO = os.environ.get("CHANNEL_ID_DIRECTO") or "UCK4h49E7Bol5DD-szyOgFgQ"
CHAT_ID_GROUP_DIRECTO_RAW = os.environ.get("CHAT_ID_GROUP_DIRECTO") or "-1003839040942_5622"

WELCOME_THREAD_ID_RAW = os.environ.get("WELCOME_THREAD_ID", "")
LOG_CHAT_ID_RAW = os.environ.get("LOG_CHAT_ID") or "-1003781665410"

YT_API_KEY = os.environ.get("YT_API_KEY", "")
TZ = os.environ.get("TZ", "Europe/Madrid")
BASELINE_ONLY = os.environ.get("BASELINE_ONLY", "0") == "1"

PROCESSED_USERS_CACHE = {}

# ==========================================================
# SERVIDOR WEB DUMMY PARA RENDER
# ==========================================================
app = Flask(__name__)

@app.route('/', methods=['GET', 'POST', 'HEAD'])
def health_check():
    return "Bot en vivo 24/7", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# ==========================================================
# FUNCIONES AUXILIARES Y ESTADO EN TELEGRAM
# ==========================================================
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
        return None
    val_str = str(raw_val).strip()
    if "_" in val_str:
        parts = val_str.split("_")
        if len(parts) > 1 and parts[1].isdigit():
            return int(parts[1])
    try:
        return int(val_str)
    except ValueError:
        return None

def prune_state(st):
    for key in ["msg_ids", "vid_status", "msg_ids_posts"]:
        if isinstance(st.get(key), dict) and len(st[key]) > 100:
            keys = list(st[key].keys())
            for k in keys[:-80]:
                del st[key][k]

    if isinstance(st.get("seen_posts"), list) and len(st["seen_posts"]) > 200:
        st["seen_posts"] = st["seen_posts"][-150:]

async def load_state_from_telegram(bot, log_target):
    global LAST_SAVED_JSON
    st = {
        "msg_ids": {}, "msg_ids_posts": {}, "vid_status": {},
        "pending_users": {}, "pending_welcomes": {},
        "processed_welcome_users": {}, "seen_posts": [], "last_update_id": 0
    }
    state_msg_id = 1040  # Mensaje fijo en https://t.me/c/3781665410/1040
    if not log_target:
        return st, state_msg_id

    chat_id = log_target["chat_id"]
    try:
        # Carga directa e infalible mediante forward del mensaje 1040
        fwd = await bot.forward_message(chat_id=chat_id, from_chat_id=chat_id, message_id=state_msg_id)
        msg_text = fwd.text or fwd.caption or ""
        try:
            await bot.delete_message(chat_id=chat_id, message_id=fwd.message_id)
        except Exception:
            pass

        match = re.search(r"<pre>(.*?)</pre>", msg_text, re.DOTALL)
        if match:
            st = json.loads(match.group(1))
            print("✅ Estado cargado con éxito desde el mensaje 1040.", flush=True)
    except Exception as e:
        print(f"⚠️ Aviso al cargar mensaje 1040: {e}. Reintentando con chat info...", flush=True)
        try:
            chat = await bot.get_chat(chat_id)
            if chat.pinned_message and STATE_MARKER in (chat.pinned_message.text or chat.pinned_message.caption or ""):
                msg_text = chat.pinned_message.text or chat.pinned_message.caption or ""
                match = re.search(r"<pre>(.*?)</pre>", msg_text, re.DOTALL)
                if match:
                    st = json.loads(match.group(1))
        except Exception as ex_pin:
            print(f"Error cargando desde mensaje fijado: {ex_pin}", flush=True)

    st.setdefault("msg_ids", {})
    st.setdefault("msg_ids_posts", {})
    st.setdefault("vid_status", {})
    st.setdefault("pending_users", {})
    st.setdefault("pending_welcomes", {})
    st.setdefault("processed_welcome_users", {})
    st.setdefault("seen_posts", [])
    st.setdefault("last_update_id", 0)

    prune_state(st)
    LAST_SAVED_JSON = json.dumps(st, ensure_ascii=False)
    return st, state_msg_id

async def save_state_to_telegram(bot, log_target, state, state_msg_id):
    global LAST_SAVED_JSON
    if not log_target or not state_msg_id:
        return state_msg_id

    prune_state(state)
    json_str = json.dumps(state, ensure_ascii=False)

    if json_str == LAST_SAVED_JSON:
        return state_msg_id

    text = f"{STATE_MARKER}\n<pre>{json_str}</pre>"

    try:
        await bot.edit_message_text(
            chat_id=log_target["chat_id"],
            message_id=state_msg_id,
            text=text,
            parse_mode="HTML"
        )
        LAST_SAVED_JSON = json_str
    except Exception as e:
        if "message is not modified" in str(e).lower():
            LAST_SAVED_JSON = json_str
        else:
            print(f"Error guardando estado en Telegram: {e}", flush=True)
    
    return state_msg_id

async def send_telegram_msg(bot, chat_id, text, thread_id=None, parse_mode="HTML"):
    kwargs = {"parse_mode": parse_mode}
    if thread_id is not None:
        try:
            kwargs["message_thread_id"] = thread_id
            msg = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            return msg.message_id
        except Exception as e:
            print(f"Aviso: No se pudo enviar con thread_id={thread_id} ({e}). Enviando al principal.", flush=True)

    kwargs.pop("message_thread_id", None)
    msg = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    return msg.message_id

async def send_log(bot, text):
    log_target = parse_target(LOG_CHAT_ID_RAW, "log_channel")
    if not log_target:
        return
    try:
        log_text = f"🤖 <b>LOG BOT PRO:</b>\n{text}"
        await send_telegram_msg(bot, log_target["chat_id"], log_text, thread_id=log_target.get("thread_id"))
    except Exception as e:
        print(f"Error enviando log: {e}", flush=True)

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

async def send_standard_welcome(bot, group_target, user, thread_id=None):
    mention = format_mention(user)
    text = (
        f"¡Hola {mention}, bienvenido/a a la comunidad! 👋\n\n"
        "La idea de este espacio es ayudarnos entre todos. Si encuentras alguna novedad, herramienta o información interesante que creas que nos puede servir a los demás, ¡compártela!\n\n"
        "También está totalmente permitido compartir enlaces o recursos si con eso ayudamos a resolver la duda de otro miembro.\n\n"
        "📌 <b>NUESTROS TEMAS Y SECCIONES:</b>\n"
        '#️⃣ <a href="https://t.me/CacharrearconJuan/1">General</a>\n'
        '🎮 <a href="https://t.me/CacharrearconJuan/8847">PS5</a>\n'
        '📺 <a href="https://t.me/CacharrearconJuan/8768">Móviles, Android TV &amp; Fire Stick</a>\n'
        '🎬 <a href="https://t.me/CacharrearconJuan/5621">Vídeos</a>\n'
        '📝 <a href="https://t.me/CacharrearconJuan/5801">Publicaciones</a>\n'
        '🔴 <a href="https://t.me/CacharrearconJuan/5622">Directos</a>\n'
        '📁 <a href="https://t.me/CacharrearconJuan/6528">Juegos</a>\n'
        '🔬 <a href="https://t.me/CacharrearconJuan/5626">Proyectos</a>\n'
        '💻 <a href="https://t.me/CacharrearconJuan/8907">Informática</a>\n'
        '📚 <a href="https://t.me/CacharrearconJuan/5623">Menú</a>\n'
        '🚗 <a href="https://t.me/CacharrearconJuan/7757">GTA 6</a>\n'
        '🛒 <a href="https://t.me/CacharrearconJuan/8478">Chollos y Ofertas</a>\n\n'
        "⚠️ <b>REQUISITO IMPORTANTE:</b>\n"
        "Recuerda que según las normas del grupo es obligatorio tener un <b>nombre real configurado</b> en tu perfil de Telegram. Si tu cuenta no tiene nombre o solo tiene un punto/símbolo, por favor cámbialo en tus ajustes para evitar que los sistemas de moderación te expulsen.\n\n"
        "🛠️ <b>Antes de empezar:</b>\n"
        "👉 Tienes todas las descargas, tutoriales y enlaces en el <b>MENSAJE FIJADO</b> en la parte superior del chat.\n"
        "👉 Escribe #normas para leer las reglas rápidas del grupo.\n\n"
        "¡Hagamos de esta una gran comunidad! 🚀"
    )
    return await send_telegram_msg(bot, group_target["chat_id"], text, thread_id=thread_id)

async def send_warning_message(bot, group_target, user, reasons, thread_id=None):
    mention = format_mention(user)
    reasons_text = "\n".join([f"• {r}" for r in reasons])
    text = (
        f"⚠️ <b>¡ATENCIÓN {mention}! TU PERFIL NO CUMPLE LAS NORMAS</b> ⚠️\n\n"
        "Para mantener el grupo seguro y ordenado, necesitamos que ajustes tu perfil:\n\n"
        f"<b>Motivo:</b>\n{reasons_text}\n\n"
        "<b>¿Cómo solucionarlo?</b>\n"
        "1️⃣ Entra en los Ajustes de Telegram.\n"
        "2️⃣ Ponte un <b>@alias / nombre de usuario</b>.\n"
        "3️⃣ Pon un <b>nombre de perfil con al menos 3 letras reales</b>.\n"
        "4️⃣ Cuando actualices los datos de tu perfil que te faltaban, saluda en el grupo para que el bot reconozca la actualización.\n\n"
        "⏳ <b>Tienes 1 HORA para cambiarlo.</b> Si en 60 minutos no está corregido, el sistema te expulsará automáticamente (podrás volver a entrar en cuanto lo arregles)."
    )
    return await send_telegram_msg(bot, group_target["chat_id"], text, thread_id=thread_id)

async def process_moderation(bot, group_target, state, log_target, state_msg_id, welcome_thread_id=None):
    if not group_target:
        return state_msg_id

    now = int(time.time())
    actual_thread_id = welcome_thread_id

    for uid, ts in list(PROCESSED_USERS_CACHE.items()):
        if now - ts > 600:
            del PROCESSED_USERS_CACHE[uid]

    processed_users = state.setdefault("processed_welcome_users", {})
    for uid_str, ts in list(processed_users.items()):
        if now - ts > 3600:
            del processed_users[uid_str]

    pending = state.get("pending_users", {})
    last_update_id = state.get("last_update_id", 0)

    try:
        updates = await bot.get_updates(
            offset=last_update_id + 1 if last_update_id > 0 else None,
            timeout=1,
            allowed_updates=["message", "chat_member"]
        )

        if updates:
            candidates = []
            for u in updates:
                state["last_update_id"] = max(state.get("last_update_id", 0), u.update_id)

                if u.message and u.message.from_user:
                    sender = u.message.from_user
                    s_str = str(sender.id)
                    if s_str in pending:
                        is_comp, _ = check_user_compliance(sender)
                        if is_comp:
                            w_msg_id = pending[s_str]["warning_msg_id"]
                            try:
                                await bot.delete_message(chat_id=group_target["chat_id"], message_id=w_msg_id)
                            except Exception:
                                pass
                            msg_id = await send_standard_welcome(bot, group_target, sender, thread_id=actual_thread_id)
                            state["pending_welcomes"][str(msg_id)] = {
                                "msg_id": msg_id,
                                "sent_at": int(time.time())
                            }
                            await send_log(bot, f"🎉 El usuario {format_mention(sender)} ha corregido su perfil a tiempo.")
                            del pending[s_str]

                if u.message and u.message.new_chat_members:
                    for m in u.message.new_chat_members:
                        if not m.is_bot:
                            candidates.append(m)

                if u.chat_member and u.chat_member.new_chat_member:
                    cm = u.chat_member.new_chat_member
                    user = getattr(cm, 'user', None) or getattr(u.chat_member, 'user', None)
                    if user and not user.is_bot:
                        status = getattr(cm, 'status', '')
                        if status in ["member", "administrator", "creator"]:
                            candidates.append(user)

            unique_members = {m.id: m for m in candidates}

            for member_id, member in unique_members.items():
                m_str = str(member_id)

                if m_str in processed_users or m_str in pending or member_id in PROCESSED_USERS_CACHE:
                    continue

                PROCESSED_USERS_CACHE[member_id] = now
                processed_users[m_str] = now

                try:
                    is_compliant, reasons = check_user_compliance(member)
                    if is_compliant:
                        msg_id = await send_standard_welcome(bot, group_target, member, thread_id=actual_thread_id)
                        state["pending_welcomes"][str(msg_id)] = {
                            "msg_id": msg_id,
                            "sent_at": int(time.time())
                        }
                        await send_log(bot, f"✅ Bienvenida enviada a {format_mention(member)}.")
                    else:
                        msg_id = await send_warning_message(bot, group_target, member, reasons, thread_id=actual_thread_id)
                        pending[m_str] = {
                            "user_id": member.id,
                            "joined_at": int(time.time()),
                            "warning_msg_id": msg_id
                        }
                        await send_log(bot, f"⚠️ Aviso enviado a {format_mention(member)}.")
                except Exception as ex_m:
                    print(f"Error procesando bienvenida: {ex_m}", flush=True)

            await save_state_to_telegram(bot, log_target, state, state_msg_id)

    except Exception as e:
        print(f"Error en actualizaciones de Telegram: {e}", flush=True)

    to_remove = []
    for user_id_str, pdata in list(pending.items()):
        user_id = pdata["user_id"]
        joined_at = pdata["joined_at"]
        warning_msg_id = pdata["warning_msg_id"]

        try:
            chat_member = await bot.get_chat_member(chat_id=group_target["chat_id"], user_id=user_id)
            user = chat_member.user
            
            if chat_member.status in ["left", "kicked"]:
                to_remove.append(user_id_str)
                continue

            is_compliant, _ = check_user_compliance(user)

            if is_compliant:
                try:
                    await bot.delete_message(chat_id=group_target["chat_id"], message_id=warning_msg_id)
                except Exception:
                    pass
                msg_id = await send_standard_welcome(bot, group_target, user, thread_id=actual_thread_id)
                state["pending_welcomes"][str(msg_id)] = {
                    "msg_id": msg_id,
                    "sent_at": int(time.time())
                }
                await send_log(bot, f"🎉 El usuario {format_mention(user)} ha corregido su perfil.")
                to_remove.append(user_id_str)
            elif now - joined_at >= 3600:
                try:
                    await bot.delete_message(chat_id=group_target["chat_id"], message_id=warning_msg_id)
                except Exception:
                    pass

                try:
                    await bot.ban_chat_member(chat_id=group_target["chat_id"], user_id=user_id)
                    await bot.unban_chat_member(chat_id=group_target["chat_id"], user_id=user_id)
                    await send_log(bot, f"🚫 Usuario <code>{user_id}</code> expulsado tras 60 min.")
                except Exception as e:
                    print(f"Error al expulsar usuario: {e}", flush=True)

                to_remove.append(user_id_str)

        except Exception as e:
            print(f"Error comprobando usuario {user_id}: {e}", flush=True)

    for uid in to_remove:
        if uid in state["pending_users"]:
            del state["pending_users"][uid]
        if uid in state.get("processed_welcome_users", {}):
            del state["processed_welcome_users"][uid]

    welcomes = state.get("pending_welcomes", {})
    welcomes_to_remove = []

    for msg_id_str, wdata in list(welcomes.items()):
        msg_id = wdata["msg_id"]
        sent_at = wdata["sent_at"]
        if now - sent_at >= 120:
            try:
                await bot.delete_message(chat_id=group_target["chat_id"], message_id=msg_id)
            except Exception as e:
                print(f"Error borrando bienvenida {msg_id}: {e}", flush=True)
            welcomes_to_remove.append(msg_id_str)

    for wid in welcomes_to_remove:
        if wid in state["pending_welcomes"]:
            del state["pending_welcomes"][wid]

    return state_msg_id

# ==========================================================
# FUNCIONES YOUTUBE
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

    try:
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        r = requests.get(rss_url, timeout=15)
        if r.status_code == 200:
            found_vids = re.findall(r'<yt:videoId>(.*?)</yt:videoId>', r.text)
            for v in found_vids[:5]:
                if v not in vids:
                    vids.append(v)
    except Exception:
        pass

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
    except Exception:
        pass

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

    broadcast_content = snippet.get("liveBroadcastContent", "")
    has_ended = "actualEndTime" in live
    is_live = (broadcast_content == "live") and not has_ended

    return {
        "vid": video_id,
        "title": snippet.get("title", ""),
        "thumb": thumb_url,
        "link": f"https://www.youtube.com/watch?v={video_id}",
        "is_live": is_live,
        "viewers": live.get("concurrentViewers"),
        "views": stats.get("viewCount"),
        "start": live.get("actualStartTime") or snippet.get("publishedAt")
    }

def is_video_too_old(info, max_hours=48):
    start_str = info.get("start")
    if not start_str:
        return False
    try:
        dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        age_hours = (now_utc - dt).total_seconds() / 3600.0
        return age_hours > max_hours
    except Exception:
        return False

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
        return f"🔴 <b>DIRECTO EN VIVO</b>\n✨ <b>{title}</b>\n{v_line}🕒 {iso_to_local(info['start'])}\n👉 {info['link']}"
    
    v_line = f"👀 {info['views']} views\n" if info['views'] else ""
    return f"🎥 <b>NUEVO VÍDEO</b>\n✨ <b>{title}</b>\n{v_line}🕒 {iso_to_local(info['start'])}\n👉 {info['link']}"

async def send_post(bot, target, info, kind):
    cap = format_caption(info, kind)
    kwargs = {"parse_mode": "HTML"}
    if target.get("thread_id") is not None:
        kwargs["message_thread_id"] = target["thread_id"]

    if info.get('thumb'):
        try:
            r = requests.get(info['thumb'], timeout=20)
            msg = await bot.send_photo(chat_id=target["chat_id"], photo=r.content, caption=cap, **kwargs)
            return msg.message_id
        except Exception as e:
            print(f"Error enviando foto a {target['chat_id']}: {e}", flush=True)

    msg = await bot.send_message(chat_id=target["chat_id"], text=cap, **kwargs)
    return msg.message_id

async def update_msg(bot, target, mid, info, kind):
    cap = format_caption(info, kind)
    try:
        if info.get('thumb'):
            r = requests.get(info['thumb'], timeout=20)
            media = InputMediaPhoto(media=io.BytesIO(r.content), caption=cap, parse_mode="HTML")
            await bot.edit_message_media(chat_id=target["chat_id"], message_id=mid, media=media)
            return True
        else:
            await bot.edit_message_text(chat_id=target["chat_id"], message_id=mid, text=cap, parse_mode="HTML")
            return True
    except Exception as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str:
            return True
        try: 
            await bot.edit_message_caption(chat_id=target["chat_id"], message_id=mid, caption=cap, parse_mode="HTML")
            return True
        except Exception:
            try:
                await bot.edit_message_text(chat_id=target["chat_id"], message_id=mid, text=cap, parse_mode="HTML")
                return True
            except Exception:
                return False

async def process_channel_videos(bot, target, channel_id, state, log_target, state_msg_id):
    if not target or not channel_id:
        return state_msg_id

    vids = get_recent_video_ids(channel_id)
    target_key_label = target.get('key', 'general')
    target_prefix = f"{target['chat_id']}_{target.get('thread_id')}_"

    for key, status in list(state.get("vid_status", {}).items()):
        if status == "live" and key.startswith(target_prefix):
            vid_from_key = key[len(target_prefix):]
            if vid_from_key not in vids:
                vids.append(vid_from_key)

    if vids:
        for vid in vids:
            info = yt_video_info(vid)
            if not info: continue

            kind = "live" if info["is_live"] else "video"
            key = f"{target_prefix}{vid}"
            mid = state["msg_ids"].get(key)

            if BASELINE_ONLY:
                if not mid:
                    state["msg_ids"][key] = -1
                    state["vid_status"][key] = kind
                    state_msg_id = await save_state_to_telegram(bot, log_target, state, state_msg_id)
                continue

            if not mid:
                # Si el vídeo no está en la BD y tiene más de 48 horas (y NO está en directo), no se publica
                if is_video_too_old(info, max_hours=48) and not info["is_live"]:
                    state["msg_ids"][key] = -1
                    state["vid_status"][key] = kind
                    state_msg_id = await save_state_to_telegram(bot, log_target, state, state_msg_id)
                    continue

                mid = await send_post(bot, target, info, kind)
                if mid:
                    state["msg_ids"][key] = mid
                    state["vid_status"][key] = kind
                    state_msg_id = await save_state_to_telegram(bot, log_target, state, state_msg_id)
                    await send_log(bot, f"📢 [{target_key_label}] Alerta publicada: <b>{info['title']}</b> ({kind.upper()}).")

            elif mid != -1:
                old_kind = state["vid_status"].get(key)
                if old_kind != kind:
                    if await update_msg(bot, target, mid, info, kind):
                        state["msg_ids"][key] = mid
                        state["vid_status"][key] = kind
                        state_msg_id = await save_state_to_telegram(bot, log_target, state, state_msg_id)
                        await send_log(bot, f"🔄 [{target_key_label}] Estado actualizado: <b>{info['title']}</b> pasó de {old_kind} a {kind}.")

    return state_msg_id

async def process_channel_posts(bot, target, channel_id, state, log_target, state_msg_id):
    if not target or not channel_id:
        return state_msg_id

    posts = get_recent_community_posts(channel_id)
    target_key_label = target.get('key', 'general')
    seen_posts = state.setdefault("seen_posts", [])
    msg_ids_posts = state.setdefault("msg_ids_posts", {})

    # Si es la primera ejecución o no hay registros, registramos lo existente sin notificar
    is_initial_run = (len(seen_posts) == 0 and len(msg_ids_posts) == 0)

    if posts:
        for post in reversed(posts):
            post_id = post["vid"]
            key = f"{target['chat_id']}_{target.get('thread_id')}_{post_id}"

            is_already_seen = (
                post_id in seen_posts or
                key in msg_ids_posts or
                any(post_id in k for k in msg_ids_posts)
            )

            if BASELINE_ONLY or is_initial_run:
                if not is_already_seen:
                    msg_ids_posts[key] = -1
                    if post_id not in seen_posts:
                        seen_posts.append(post_id)
                    state_msg_id = await save_state_to_telegram(bot, log_target, state, state_msg_id)
            else:
                if not is_already_seen:
                    mid = await send_post(bot, target, post, "post")
                    if mid:
                        msg_ids_posts[key] = mid
                        if post_id not in seen_posts:
                            seen_posts.append(post_id)
                        state_msg_id = await save_state_to_telegram(bot, log_target, state, state_msg_id)
                        await send_log(bot, f"💬 [{target_key_label}] Nueva publicación de comunidad enviada.")

    return state_msg_id

# ==========================================================
# BUCLE PRINCIPAL ASÍNCRONO
# ==========================================================
async def main_loop():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing env var: TELEGRAM_TOKEN")

    async with Bot(token=TELEGRAM_TOKEN) as bot:
        log_target = parse_target(LOG_CHAT_ID_RAW, "log_channel")
        state, state_msg_id = await load_state_from_telegram(bot, log_target)

        target_ch1_vids = parse_target(CHAT_ID_GROUP_RAW, "ch1_vids")        # Topic 5621
        target_ch1_posts = parse_target(CHAT_ID_POSTS_RAW, "ch1_posts")      # Topic 5801
        target_ch2_vids = parse_target(CHAT_ID_GROUP_DIRECTO_RAW, "ch2_vids") # Topic 5622
        welcome_thread_id = parse_thread_id(WELCOME_THREAD_ID_RAW)

        print("🚀 Bot activo y escuchando...", flush=True)

        last_yt_check = 0

        while True:
            try:
                if target_ch1_vids:
                    state_msg_id = await process_moderation(bot, target_ch1_vids, state, log_target, state_msg_id, welcome_thread_id=welcome_thread_id)

                now = time.time()

                if now - last_yt_check >= 120:
                    last_yt_check = now

                    try:
                        if CHANNEL_ID and target_ch1_vids:
                            state_msg_id = await process_channel_videos(bot, target_ch1_vids, CHANNEL_ID, state, log_target, state_msg_id)
                        
                        if CHANNEL_ID and target_ch1_posts:
                            state_msg_id = await process_channel_posts(bot, target_ch1_posts, CHANNEL_ID, state, log_target, state_msg_id)
                        
                        if CHANNEL_ID_DIRECTO and target_ch2_vids:
                            state_msg_id = await process_channel_videos(bot, target_ch2_vids, CHANNEL_ID_DIRECTO, state, log_target, state_msg_id)

                    except Exception as yt_err:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Error API YouTube: {yt_err}", flush=True)

                state_msg_id = await save_state_to_telegram(bot, log_target, state, state_msg_id)

            except Exception as e:
                print(f"Error en bucle principal: {e}", flush=True)

            await asyncio.sleep(2)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    asyncio.run(main_loop())
