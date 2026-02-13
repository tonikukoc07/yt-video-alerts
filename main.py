import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from telegram import Bot

STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")        # YouTube channelId (UCxxxx)
YT_API_KEY = os.environ.get("YT_API_KEY", "")

SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "15"))
TZ_NAME = os.environ.get("TZ", "Europe/Madrid")
PIN_LATEST = os.environ.get("PIN_LATEST", "1") == "1"  # fija el elegido

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"


def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "notified": [],              # lista de videoIds ya avisados
            "pinned": {                  # info del mensaje fijado por el bot
                "video_id": None,
                "message_id": None,
                "was_live": False,
            }
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"notified": [], "pinned": {"video_id": None, "message_id": None, "was_live": False}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def yt_get(path, params):
    params = dict(params)
    params["key"] = YT_API_KEY
    r = requests.get(f"{YOUTUBE_API}/{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def get_active_live_video_id():
    """
    Devuelve el videoId del directo ACTIVO (si existe), si no None.
    """
    data = yt_get("search", {
        "part": "id",
        "channelId": CHANNEL_ID,
        "eventType": "live",
        "type": "video",
        "order": "date",
        "maxResults": 1,
    })
    items = data.get("items", [])
    if not items:
        return None
    return items[0]["id"]["videoId"]


def get_latest_video_ids(limit=15):
    """
    Devuelve los últimos N videoIds (incluye directos pasados, videos normales, etc.)
    """
    data = yt_get("search", {
        "part": "id",
        "channelId": CHANNEL_ID,
        "type": "video",
        "order": "date",
        "maxResults": min(limit, 50),
    })
    ids = []
    for it in data.get("items", []):
        try:
            ids.append(it["id"]["videoId"])
        except Exception:
            pass
    return ids


def get_videos_info(video_ids):
    """
    Devuelve dict: {videoId: info} usando videos.list (snippet + stats + liveStreamingDetails)
    """
    if not video_ids:
        return {}

    # YouTube permite hasta 50 IDs en videos.list
    joined = ",".join(video_ids[:50])
    data = yt_get("videos", {
        "part": "snippet,statistics,liveStreamingDetails",
        "id": joined,
        "maxResults": len(video_ids[:50]),
    })

    out = {}
    for it in data.get("items", []):
        vid = it.get("id")
        snippet = it.get("snippet", {})
        stats = it.get("statistics", {})
        live = it.get("liveStreamingDetails", {}) or {}

        title = snippet.get("title", "")
        link = f"https://www.youtube.com/watch?v={vid}"

        # Mejor miniatura disponible
        thumbs = snippet.get("thumbnails", {}) or {}
        thumb_url = None
        for k in ("maxres", "standard", "high", "medium", "default"):
            if k in thumbs and "url" in thumbs[k]:
                thumb_url = thumbs[k]["url"]
                break

        published_at = snippet.get("publishedAt")  # ISO
        view_count = stats.get("viewCount")        # string

        # LIVE REAL: actualStartTime existe y actualEndTime NO existe
        actual_start = live.get("actualStartTime")
        actual_end = live.get("actualEndTime")
        is_live_now = bool(actual_start) and not bool(actual_end)

        # viewers concurrentes (si YouTube lo da)
        concurrent = live.get("concurrentViewers")

        out[vid] = {
            "vid": vid,
            "title": title,
            "link": link,
            "thumb": thumb_url,
            "publishedAt": published_at,
            "views": view_count,
            "is_live_now": is_live_now,
            "concurrent": concurrent,
        }
    return out


def fmt_local_time(iso_utc, tz_name):
    if not iso_utc:
        return ""
    try:
        # Ej: 2026-02-12T15:41:00Z
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        local = dt.astimezone(ZoneInfo(tz_name))
        return local.strftime("%d/%m %H:%M")
    except Exception:
        return ""


def build_caption(info, tz_name):
    title = info["title"]
    link = info["link"]
    is_live = info["is_live_now"]

    header = "🔴 DIRECTO" if is_live else "🎥 NUEVO VÍDEO"

    # Views / viewers (si es live y concurrent viene)
    extra_lines = []
    if is_live and info.get("concurrent"):
        extra_lines.append(f"👀 {info['concurrent']} viewers")
    elif info.get("views"):
        extra_lines.append(f"👀 {info['views']} views")

    # Hora local (publicación o inicio)
    t = fmt_local_time(info.get("publishedAt"), tz_name)
    if t:
        extra_lines.append(f"🕒 {t}")

    lines = [header, f"✨ {title}"]
    lines.extend(extra_lines)
    lines.append(f"👉 {link}")
    return "\n".join(lines)


def send_new_post(bot: Bot, chat_id: int, info, tz_name):
    caption = build_caption(info, tz_name)

    # Si hay miniatura, lo mandamos como foto para que se vea “pro”
    if info.get("thumb"):
        try:
            r = requests.get(info["thumb"], timeout=20)
            r.raise_for_status()
            msg = bot.send_photo(chat_id=chat_id, photo=r.content, caption=caption)
            return msg.message_id
        except Exception as e:
            print("send_photo failed, fallback to text:", repr(e))

    msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    return msg.message_id


def edit_pinned_if_needed(bot: Bot, chat_id: int, state, info, tz_name):
    """
    Si el mensaje fijado existe y es el mismo vídeo:
      - si antes era live y ahora NO lo es -> editar caption a 🎥
      - si cambian viewers/views/hora -> opcional (aquí actualizamos caption siempre)
    """
    pinned = state.get("pinned", {}) or {}
    pinned_vid = pinned.get("video_id")
    pinned_mid = pinned.get("message_id")
    was_live = bool(pinned.get("was_live"))

    if not pinned_vid or not pinned_mid:
        return state

    if pinned_vid != info["vid"]:
        return state

    # Si era live y ya terminó -> cambiar icono (edit caption)
    # (También refrescamos caption por si cambian viewers/views)
    new_caption = build_caption(info, tz_name)

    try:
        bot.edit_message_caption(chat_id=chat_id, message_id=int(pinned_mid), caption=new_caption)
        pinned["was_live"] = bool(info["is_live_now"])
        state["pinned"] = pinned
    except Exception as e:
        # Si Telegram no deja editar (por ejemplo si no era photo), intentar edit text
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=int(pinned_mid), text=new_caption, parse_mode=None)
            pinned["was_live"] = bool(info["is_live_now"])
            state["pinned"] = pinned
        except Exception as e2:
            print("edit pinned failed:", repr(e), repr(e2))

    return state


def pin_message(bot: Bot, chat_id: int, message_id: int):
    try:
        bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except Exception as e:
        print("pin failed:", repr(e))


def main():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)
    must_env("YT_API_KEY", YT_API_KEY)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    notified = state.get("notified", []) or []
    # limita tamaño para que no crezca infinito
    notified = notified[-200:]

    # 1) Detectar directo ACTIVO (si existe)
    live_vid = get_active_live_video_id()

    # 2) Traer últimos N vídeos para no perder publicaciones rápidas (Parte 10 -> Parte 11)
    latest_ids = get_latest_video_ids(SCAN_LIMIT)

    # Montamos conjunto de IDs a consultar en videos.list
    ids_to_fetch = []
    if live_vid:
        ids_to_fetch.append(live_vid)
    ids_to_fetch.extend(latest_ids)

    # Dedup preservando orden
    seen = set()
    ids_to_fetch = [x for x in ids_to_fetch if x and (x not in seen and not seen.add(x))]

    info_map = get_videos_info(ids_to_fetch)

    # 3) Publicar pendientes (los que están en latest_ids y no se han avisado)
    #    en orden cronológico (del más antiguo al más nuevo dentro del scan)
    pending = [vid for vid in reversed(latest_ids) if vid in info_map and vid not in notified]

    for vid in pending:
        info = info_map[vid]
        mid = send_new_post(bot, chat_id, info, TZ_NAME)
        notified.append(vid)

        # Pin “pro” (B): si hay live activo, ese manda; si no, el último vídeo.
        # Aquí NO fijamos cada pending, solo fijamos al final el elegido.
        # (Así si salen 2 vídeos seguidos no estás repineando 2 veces en 1 run)
        save_state({**state, "notified": notified})

    # 4) Elegir qué debe estar fijado (B)
    #    - Si hay live activo -> fijar ese
    #    - Si no -> fijar el último vídeo real (latest_ids[0])
    pin_target_vid = None
    if live_vid and live_vid in info_map and info_map[live_vid]["is_live_now"]:
        pin_target_vid = live_vid
    elif latest_ids and latest_ids[0] in info_map:
        pin_target_vid = latest_ids[0]

    # 5) Si ya hay un pinned del mismo vídeo, pero cambió live->video, editar caption
    if pin_target_vid and pin_target_vid in info_map:
        state["notified"] = notified
        state = edit_pinned_if_needed(bot, chat_id, state, info_map[pin_target_vid], TZ_NAME)
        save_state(state)

    # 6) Si hay que cambiar el fijado a otro vídeo, publicar (si no existe) y fijar
    if PIN_LATEST and pin_target_vid and pin_target_vid in info_map:
        pinned = state.get("pinned", {}) or {}
        current_pinned_vid = pinned.get("video_id")

        if current_pinned_vid != pin_target_vid:
            # Si el vídeo ya fue notificado antes, NO lo volvemos a avisar;
            # solo lo volvemos a publicar si necesitamos un mensaje del bot para fijar.
            # (Telegram no puede fijar un mensaje que NO sea del bot si quieres luego editarlo)
            info = info_map[pin_target_vid]
            mid = send_new_post(bot, chat_id, info, TZ_NAME)

            pin_message(bot, chat_id, mid)

            state["pinned"] = {
                "video_id": pin_target_vid,
                "message_id": mid,
                "was_live": bool(info["is_live_now"]),
            }

            # OJO: para no duplicar “avisos”, no metemos aquí a notified si ya estaba.
            if pin_target_vid not in notified:
                notified.append(pin_target_vid)

            state["notified"] = notified[-200:]
            save_state(state)


if __name__ == "__main__":
    main()
