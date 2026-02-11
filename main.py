import os
import json
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests
from telegram import Bot


# ====== ENV ======
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")              # canal o grupo (id numérico)
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")        # canal de YouTube (UCxxxx)

SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "15")) # cuantos entries del RSS revisar
TZ_NAME = os.environ.get("TZ", "Europe/Madrid")
PIN_LATEST = os.environ.get("PIN_LATEST", "0") == "1"

STATE_FILE = "state.json"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"


def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


# ====== STATE ======
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


# ====== HELPERS ======
def safe_text(s: str) -> str:
    return (s or "").replace("\u0000", "").strip()


def dt_from_entry(entry) -> datetime | None:
    # feedparser suele dar published_parsed / updated_parsed (time.struct_time)
    for attr in ("updated_parsed", "published_parsed"):
        st = getattr(entry, attr, None)
        if st:
            return datetime(*st[:6], tzinfo=timezone.utc)
    return None


def fmt_local(dt_utc: datetime | None, tz_name: str) -> str:
    if not dt_utc:
        return ""
    tz = ZoneInfo(tz_name)
    return dt_utc.astimezone(tz).strftime("%d/%m %H:%M")


def extract_video_id(entry):
    return getattr(entry, "yt_videoid", None) or getattr(entry, "yt_videoId", None)


def extract_thumb(entry) -> str | None:
    try:
        if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            return entry.media_thumbnail[0].get("url")
    except Exception:
        pass
    return None


# ====== YouTube watch page parsing (sin API key) ======
def _extract_json_object_after(html: str, marker: str) -> dict | None:
    """
    Extrae el JSON del tipo: marker = { ... };
    usando balanceo de llaves (robusto contra saltos de línea).
    """
    idx = html.find(marker)
    if idx == -1:
        return None

    # busca la primera '{' después del marker
    start = html.find("{", idx)
    if start == -1:
        return None

    # balanceo de llaves respetando strings
    depth = 0
    in_str = False
    esc = False

    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = html[start : i + 1]
                    try:
                        return json.loads(chunk)
                    except Exception:
                        return None
    return None


def fetch_watch_details(video_id: str) -> dict:
    """
    Devuelve:
      - is_live_now (bool)  -> SOLO true si está emitiendo en este instante
      - view_count (int|None)
      - concurrent (int|None) (si aparece)
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
    }
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    html = r.text

    # El player response suele venir como: var ytInitialPlayerResponse = {...};
    pr = _extract_json_object_after(html, "ytInitialPlayerResponse")
    if not pr:
        # fallback por si cambia formato
        pr = _extract_json_object_after(html, "var ytInitialPlayerResponse")
    if not pr:
        return {"is_live_now": False, "view_count": None, "concurrent": None}

    # 1) isLiveNow (la señal buena)
    is_live_now = False
    concurrent = None

    try:
        micro = pr.get("microformat", {}).get("playerMicroformatRenderer", {})
        live_details = micro.get("liveBroadcastDetails", {}) or {}
        # clave importante:
        is_live_now = bool(live_details.get("isLiveNow", False))
        # a veces aparece concurrentViewerCount
        cv = live_details.get("concurrentViewerCount")
        if isinstance(cv, str) and cv.isdigit():
            concurrent = int(cv)
        elif isinstance(cv, int):
            concurrent = cv
    except Exception:
        pass

    # 2) viewCount (para VOD o incluso live)
    view_count = None
    try:
        vc = pr.get("videoDetails", {}).get("viewCount")
        if isinstance(vc, str) and vc.isdigit():
            view_count = int(vc)
        elif isinstance(vc, int):
            view_count = vc
    except Exception:
        pass

    return {"is_live_now": is_live_now, "view_count": view_count, "concurrent": concurrent}


# ====== RSS ======
def fetch_items(limit: int):
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return []

    items = []
    for e in feed.entries[: max(1, limit)]:
        vid = extract_video_id(e)
        if not vid:
            continue

        title = safe_text(getattr(e, "title", ""))
        link = safe_text(getattr(e, "link", "")) or f"https://www.youtube.com/watch?v={vid}"
        thumb = extract_thumb(e)
        dt_utc = dt_from_entry(e)

        # Enriquecemos con watch page para saber si está LIVE AHORA
        details = fetch_watch_details(vid)

        items.append(
            {
                "vid": vid,
                "title": title,
                "link": link,
                "thumb": thumb,
                "dt_utc": dt_utc,
                "is_live_now": details["is_live_now"],
                "view_count": details["view_count"],
                "concurrent": details["concurrent"],
            }
        )
        # pequeño respiro para no pegar demasiado a YouTube
        time.sleep(0.3)

    return items


# ====== Telegram ======
def send_post(bot: Bot, chat_id: int, item: dict) -> int | None:
    """
    Publica y devuelve message_id.
    """
    local_time = fmt_local(item.get("dt_utc"), TZ_NAME)
    is_live = item["is_live_now"]

    header = "🔴 DIRECTO" if is_live else "🎥 NUEVO VÍDEO"

    lines = [header, f"✨ {item['title']}"]

    # viewers / views
    if is_live and item.get("concurrent") is not None:
        lines.append(f"👥 {item['concurrent']} viendo ahora")
    elif (not is_live) and item.get("view_count") is not None:
        lines.append(f"👀 {item['view_count']} views")

    if local_time:
        lines.append(f"🕒 {local_time}")

    lines.append(f"👉 {item['link']}")
    caption = "\n".join(lines)

    # intentar foto (miniatura)
    if item.get("thumb"):
        try:
            r = requests.get(item["thumb"], timeout=15)
            r.raise_for_status()
            msg = bot.send_photo(chat_id=chat_id, photo=r.content, caption=caption)
            return getattr(msg, "message_id", None)
        except Exception as e:
            print("send_photo failed, fallback to text:", repr(e))

    msg = bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    return getattr(msg, "message_id", None)


def pin_message(bot: Bot, chat_id: int, message_id: int):
    # En canales: pin_chat_message funciona si el bot es admin con permiso de fijar
    bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)


def unpin_message(bot: Bot, chat_id: int, message_id: int):
    # Telegram permite “unpin” por id en algunos casos; si falla, lo ignoramos
    try:
        bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        try:
            bot.unpin_chat_message(chat_id=chat_id)  # fallback: unpin el actual
        except Exception:
            pass


# ====== LOGIC ======
def pick_priority_item(items: list[dict]) -> dict | None:
    """
    Prioridad B:
      - si hay directo activo -> escoger el directo (is_live_now)
      - si no -> último vídeo (primer entry del RSS que ya viene ordenado)
    """
    if not items:
        return None

    live_items = [x for x in items if x.get("is_live_now")]
    if live_items:
        # si hubiera más de uno, escogemos el más reciente por dt_utc
        live_items.sort(key=lambda x: x.get("dt_utc") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return live_items[0]

    return items[0]


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    state = load_state()
    last_notified_key = state.get("last_notified_key")  # evita duplicados
    last_pinned_mid = state.get("last_pinned_message_id")

    print("Loaded state:", state)

    items = fetch_items(SCAN_LIMIT)
    if not items:
        print("No RSS entries.")
        return

    target = pick_priority_item(items)
    if not target:
        print("No target item.")
        return

    # clave única según el “estado real”
    kind = "live" if target["is_live_now"] else "video"
    notify_key = f"{kind}:{target['vid']}"

    print("Target:", notify_key, target["title"], target["link"])

    # Primer run sin estado: inicializa y NO avisa (para no spamear con lo viejo)
    if not last_notified_key:
        state["last_notified_key"] = notify_key
        save_state(state)
        print("Initialized state (no notification):", notify_key)
        return

    # Evitar duplicados (incluye manual runs repetidos)
    if notify_key == last_notified_key:
        print("Already notified:", notify_key)
        return

    # publicar
    mid = send_post(bot, chat_id, target)
    print("Posted message_id:", mid)

    # pin (si está activado)
    if PIN_LATEST and mid:
        # des-fija el anterior (si existe)
        if last_pinned_mid and last_pinned_mid != mid:
            unpin_message(bot, chat_id, int(last_pinned_mid))
        pin_message(bot, chat_id, int(mid))
        state["last_pinned_message_id"] = int(mid)

    # guardar estado
    state["last_notified_key"] = notify_key
    save_state(state)
    print("Updated state:", state)


def main():
    run_once()


if __name__ == "__main__":
    main()
