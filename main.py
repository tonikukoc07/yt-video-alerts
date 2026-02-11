import os
import json
import time
import re
import feedparser
import requests
from datetime import datetime
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

# Opcionales
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "0"))  # si lo ejecutas en server
PIN_LATEST = os.environ.get("PIN_LATEST", "0") == "1"    # fija el último aviso
TZ = os.environ.get("TZ", "Europe/Madrid")               # solo para mostrar hora bonita

STATE_FILE = "state.json"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# --- utils ---

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
    return (s or "").replace("\u0000", "")

def fmt_local(dt_struct) -> str:
    # dt_struct: time.struct_time (feedparser)
    try:
        # GitHub Actions suele ir en UTC; para mostrar hora local "bonita" sin librerías extra:
        # asumimos que dt_struct ya es UTC y lo mostramos como "DD/MM HH:MM"
        dt = datetime(*dt_struct[:6])
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return ""

# --- YouTube helpers ---

def is_video_live_now(video_id: str) -> bool:
    """
    Sin API: miramos el HTML del watch page buscando flags típicos.
    Si YouTube cambia el HTML, puede fallar (pero suele funcionar).
    """
    if not video_id:
        return False
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text

        needles_true = [
            '"isLiveNow":true',
            '"isLiveContent":true',
            '"LIVE_NOW"',
            '"status":"LIVE"',
        ]
        # Para evitar falsos positivos cuando ya terminó:
        needles_false = [
            '"isLiveNow":false',
            '"status":"ENDED"',
            '"LIVE_STREAM_OFFLINE"',
        ]

        live_true = any(n in html for n in needles_true)
        live_false = any(n in html for n in needles_false)

        return live_true and not live_false
    except Exception:
        return False

def fetch_entries(limit=15):
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return []
    return feed.entries[:limit]

def parse_entry(e):
    vid = getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None)
    title = safe_text(getattr(e, "title", "") or "")
    link = safe_text(getattr(e, "link", "") or "")
    published = getattr(e, "published_parsed", None)  # time.struct_time
    updated = getattr(e, "updated_parsed", None)

    thumb = None
    try:
        if hasattr(e, "media_thumbnail") and e.media_thumbnail:
            thumb = e.media_thumbnail[0].get("url")
    except Exception:
        thumb = None

    # views (RSS lo trae a veces en media:community/media:statistics pero feedparser lo mete raro)
    views = None
    try:
        # buscar algo tipo 'views="123"'
        raw = str(e)
        m = re.search(r'views["\']\s*[:=]\s*["\']?(\d+)', raw)
        if m:
            views = int(m.group(1))
    except Exception:
        views = None

    return {
        "vid": vid,
        "title": title,
        "link": link,
        "thumb": thumb,
        "published": published,
        "updated": updated,
        "views": views,
    }

# --- Telegram helpers ---

def build_caption(kind: str, item: dict) -> str:
    # kind: "live" o "video"
    header = "🔴 DIRECTO" if kind == "live" else "🎥 NUEVO VÍDEO"
    title = item["title"]
    link = item["link"]
    views = item.get("views")
    when = fmt_local(item["published"]) if item.get("published") else ""

    lines = [header, f"✨ {title}"]
    if views is not None:
        lines.append(f"👀 {views} views")
    if when:
        lines.append(f"🕒 {when}")
    lines.append(f"👉 {link}")
    return "\n".join(lines)

def send_photo_or_text(bot: Bot, chat_id: int, caption: str, thumb_url: str | None):
    if thumb_url:
        try:
            r = requests.get(thumb_url, timeout=10)
            r.raise_for_status()
            msg = bot.send_photo(chat_id=chat_id, photo=r.content, caption=caption)
            return msg
        except Exception as e:
            print("send_photo failed, fallback to text:", repr(e))

    msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    return msg

def pin_message(bot: Bot, chat_id: int, message_id: int):
    try:
        # despinneamos el anterior (si existe) para que quede limpio
        bot.unpin_all_chat_messages(chat_id=chat_id)
    except Exception as e:
        print("unpin_all failed (maybe not allowed):", repr(e))

    try:
        bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except Exception as e:
        print("pin failed:", repr(e))

def try_edit_message(bot: Bot, chat_id: int, message_id: int, new_caption: str):
    """
    Si el mensaje fijado era una foto, editamos caption.
    Si era texto, editamos text.
    No sabemos el tipo aquí, así que probamos caption y luego text.
    """
    try:
        bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=new_caption)
        return True
    except Exception:
        pass

    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=new_caption, parse_mode=None)
        return True
    except Exception as e:
        print("edit failed:", repr(e))
        return False

# --- Main logic (Regla B) ---

def choose_target(entries):
    """
    Regla B:
    - si hay live activo -> el live más reciente por published
    - si no -> el vídeo más reciente por published (entry más reciente)
    """
    parsed = [parse_entry(e) for e in entries]
    parsed = [p for p in parsed if p.get("vid")]

    # Orden por published (si falta, usamos updated)
    def key_dt(p):
        return p.get("published") or p.get("updated") or time.gmtime(0)

    # Detectar lives activos
    lives = []
    for p in parsed:
        if is_video_live_now(p["vid"]):
            lives.append(p)

    if lives:
        lives.sort(key=key_dt, reverse=True)
        return "live", lives[0]

    parsed.sort(key=key_dt, reverse=True)
    return "video", parsed[0] if parsed else (None, None)

def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    last_notified_vid = state.get("last_notified_vid")           # último vid avisado (sea live o video)
    pinned_message_id = state.get("pinned_message_id")           # msg id fijado (si PIN_LATEST)
    pinned_vid = state.get("pinned_vid")                         # vid fijado
    pinned_kind = state.get("pinned_kind")                       # "live" o "video"

    entries = fetch_entries(limit=15)
    if not entries:
        print("No entries in RSS.")
        return

    kind, item = choose_target(entries)
    if not item:
        print("No valid item.")
        return

    vid = item["vid"]
    caption = build_caption(kind, item)

    print("Chosen:", kind, vid, item["title"])

    # 1) Si no hay estado aún, inicializamos sin avisar para no spamear antiguo
    if not last_notified_vid:
        state["last_notified_vid"] = vid
        # (no fijamos nada en el primer run, para evitar “pin” de algo viejo)
        save_state(state)
        print("Initialized state (no notification). last_notified_vid =", vid)
        return

    # 2) Caso ESPECIAL: antes estaba fijado como LIVE y ahora ese MISMO vid ya no está live
    # -> “se terminó el directo” => actualizamos el mensaje fijado a VIDEO (sin duplicar)
    if pinned_vid and pinned_kind == "live" and pinned_vid == vid and kind == "video":
        if pinned_message_id:
            ok = try_edit_message(bot, chat_id, pinned_message_id, caption)
            if ok:
                state["pinned_kind"] = "video"
                state["last_notified_vid"] = vid  # sigue siendo el mismo, pero actualizado
                save_state(state)
                print("Updated pinned live -> video (edited).")
                return
        # Si no se puede editar, hacemos fallback: enviamos uno nuevo y lo fijamos
        msg = send_photo_or_text(bot, chat_id, caption, item.get("thumb"))
        if PIN_LATEST:
            pin_message(bot, chat_id, msg.message_id)
            state["pinned_message_id"] = msg.message_id
            state["pinned_vid"] = vid
            state["pinned_kind"] = "video"
        state["last_notified_vid"] = vid
        save_state(state)
        print("Fallback: posted new video and pinned.")
        return

    # 3) Si el vid NO cambió y no hay transición, no hacemos nada
    if vid == last_notified_vid:
        print("No change. Skipping.")
        return

    # 4) Aviso normal (nuevo live o nuevo video)
    msg = send_photo_or_text(bot, chat_id, caption, item.get("thumb"))

    if PIN_LATEST:
        pin_message(bot, chat_id, msg.message_id)
        state["pinned_message_id"] = msg.message_id
        state["pinned_vid"] = vid
        state["pinned_kind"] = kind

    state["last_notified_vid"] = vid
    save_state(state)
    print("Notified and updated state. last_notified_vid =", vid)

def main():
    run_once()

    # modo “server” opcional
    if POLL_SECONDS and POLL_SECONDS > 0:
        while True:
            time.sleep(POLL_SECONDS)
            run_once()

if __name__ == "__main__":
    main()
