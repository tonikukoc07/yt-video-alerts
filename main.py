import os
import json
import time
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import requests
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

# En Actions no hace falta loop, cron manda. Si lo usas en server, puedes activar loop.
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "0"))

STATE_FILE = "state.json"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# Cuántas entradas revisar del RSS (para pillar directos aunque no sean el entry[0])
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "15"))

# Pin del último aviso (opcional). Requiere permiso de "Pin messages" en el chat/canal.
PIN_LATEST = os.environ.get("PIN_LATEST", "0") == "1"

# Zona horaria para mostrar la hora local
TZ = os.environ.get("TZ", "Europe/Madrid")


def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


def safe_text(s: str) -> str:
    # No usamos parse_mode para evitar líos con entidades
    return (s or "").replace("\u0000", "").strip()


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen_ids": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "seen_ids" not in data or not isinstance(data["seen_ids"], list):
                data["seen_ids"] = []
            return data
    except Exception:
        return {"seen_ids": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def parse_entry(e):
    # video id
    vid = getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None)

    title = safe_text(getattr(e, "title", "") or "")
    link = safe_text(getattr(e, "link", "") or "")

    # published/updated
    published_struct = getattr(e, "published_parsed", None)
    updated_struct = getattr(e, "updated_parsed", None)

    def to_ts(st):
        if not st:
            return 0
        try:
            return int(time.mktime(st))
        except Exception:
            return 0

    published_ts = to_ts(published_struct)
    updated_ts = to_ts(updated_struct)

    # thumbnail
    thumb = None
    try:
        if hasattr(e, "media_thumbnail") and e.media_thumbnail:
            thumb = e.media_thumbnail[0].get("url")
    except Exception:
        thumb = None

    # views (si viene)
    views = None
    try:
        # feedparser suele mapear <media:statistics views="...">
        if hasattr(e, "media_statistics") and e.media_statistics:
            v = e.media_statistics[0].get("views")
            if v is not None:
                views = int(v)
    except Exception:
        views = None

    # “live” (cuando RSS lo trae)
    live_flag = None
    for key in ("yt_livebroadcastcontent", "yt_liveBroadcastContent", "ytc_livebroadcastcontent"):
        live_flag = getattr(e, key, None)
        if live_flag:
            break
    is_live = str(live_flag).lower() == "live"

    return {
        "vid": vid,
        "title": title,
        "link": link,
        "thumb": thumb,
        "views": views,
        "published_ts": published_ts,
        "updated_ts": updated_ts,
        "is_live": is_live,
    }


def is_live_by_html(video_url: str) -> bool:
    """
    Cuando el RSS no dice 'live', miramos el HTML del watch.
    Es bastante fiable para directos, y solo lo hacemos si hace falta.
    """
    try:
        r = requests.get(video_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text

        # Señales típicas en el HTML/JSON embebido
        if '"isLiveContent":true' in html:
            return True
        if '"isLiveNow":true' in html:
            return True
        if '"liveBroadcastDetails"' in html:
            return True
        if '"badge":"LIVE"' in html or '"label":"LIVE"' in html:
            return True

        return False
    except Exception:
        return False


def fmt_local_time(ts: int) -> str:
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts, tz=ZoneInfo(TZ))
    return dt.strftime("%d/%m %H:%M")


def build_caption(item: dict) -> str:
    is_live = item["is_live"]
    title = item["title"]
    link = item["link"]
    views = item.get("views")
    when = fmt_local_time(item.get("published_ts") or item.get("updated_ts") or 0)

    header = "🔴 DIRECTO" if is_live else "🎥 NUEVO VÍDEO"
    lines = [
        header,
        f"✨ {title}",
    ]
    if isinstance(views, int):
        lines.append(f"👀 {views} views")
    if when:
        lines.append(f"🕒 {when}")
    lines.append(f"👉 {link}")
    return "\n".join(lines)


def send_to_telegram(bot: Bot, chat_id: int, item: dict) -> int:
    """
    Devuelve message_id del mensaje enviado (para pin opcional).
    """
    caption = build_caption(item)
    thumb = item.get("thumb")

    # Intentar enviar con miniatura (descargada)
    if thumb:
        try:
            r = requests.get(thumb, timeout=10)
            r.raise_for_status()
            msg = bot.send_photo(chat_id=chat_id, photo=r.content, caption=caption)
            return msg.message_id
        except Exception as e:
            print("send_photo failed, fallback to text. Error:", repr(e))

    msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    return msg.message_id


def fetch_feed_items():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return []
    items = []
    for e in feed.entries[:SCAN_LIMIT]:
        item = parse_entry(e)
        if item.get("vid"):
            items.append(item)

    # Orden: más reciente primero (por updated, si no published)
    items.sort(key=lambda x: (x.get("updated_ts", 0) or 0, x.get("published_ts", 0) or 0), reverse=True)
    return items


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    seen = state.get("seen_ids", [])
    seen_set = set(seen)

    items = fetch_feed_items()
    if not items:
        print("No entries in RSS.")
        return

    # 1) Primer run: inicializa con lo que haya ahora y NO avises (evita spam histórico)
    if not seen:
        # Guardamos varios para cubrir “directos programados” y evitar notificar algo viejo
        seed = [it["vid"] for it in items[:5] if it.get("vid")]
        state["seen_ids"] = seed
        save_state(state)
        print("Initialized state with:", seed, "(no notification)")
        return

    # 2) Buscar el primer video no visto aún (en top SCAN_LIMIT)
    candidate = None
    for it in items:
        if it["vid"] not in seen_set:
            candidate = it
            break

    if not candidate:
        print("No new video/live found in top", SCAN_LIMIT)
        return

    # 3) Detección live mejorada (si RSS no lo dice, miramos HTML)
    if not candidate["is_live"] and candidate.get("link"):
        if is_live_by_html(candidate["link"]):
            candidate["is_live"] = True

    # 4) Enviar
    message_id = send_to_telegram(bot, chat_id, candidate)
    print("Notified:", candidate["vid"], candidate["title"])

    # 5) Pin opcional
    if PIN_LATEST:
        try:
            bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
            print("Pinned message:", message_id)
        except Exception as e:
            print("Pin failed:", repr(e))

    # 6) Guardar estado (evitar duplicados)
    # Mantenemos una lista acotada para que no crezca infinito
    seen.insert(0, candidate["vid"])
    seen = list(dict.fromkeys(seen))  # unique, mantiene orden
    seen = seen[:50]
    state["seen_ids"] = seen
    save_state(state)
    print("State updated. seen_ids size =", len(seen))


def main():
    run_once()

    if POLL_SECONDS and POLL_SECONDS > 0:
        while True:
            time.sleep(POLL_SECONDS)
            run_once()


if __name__ == "__main__":
    main()
