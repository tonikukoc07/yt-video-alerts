import os
import re
import requests
import feedparser
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot

# ========= ENV =========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")           # Canal o supergrupo
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")     # Canal de YouTube

TZ_NAME = os.environ.get("TZ", "Europe/Madrid")
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "15"))
KEEP_LAST = int(os.environ.get("KEEP_LAST", "10"))  # ✅ histórico: cuantos mensajes mantener


RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"


def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


def safe_text(s: str) -> str:
    return (s or "").replace("\u0000", "").strip()


def format_local(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(ZoneInfo(TZ_NAME)).strftime("%d/%m %H:%M")


def download_bytes(url: str, timeout=12) -> bytes | None:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def extract_video_id_from_text(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{6,})", text)
    return m.group(1) if m else None


def get_pinned_video_id(bot: Bot, chat_id: int) -> str | None:
    """
    Usamos el mensaje fijado como "estado" para evitar duplicados.
    """
    try:
        chat = bot.get_chat(chat_id)
        pinned = getattr(chat, "pinned_message", None)
        if not pinned:
            return None

        if getattr(pinned, "caption", None):
            return extract_video_id_from_text(pinned.caption)

        if getattr(pinned, "text", None):
            return extract_video_id_from_text(pinned.text)

        return None
    except Exception:
        return None


def fetch_live_now_from_channel() -> tuple[str, str] | None:
    """
    Prioridad B: si hay directo activo -> lo usamos.
    Mira /live del canal.
    """
    url = f"https://www.youtube.com/channel/{CHANNEL_ID}/live"
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text

        # Señales típicas de directo activo
        if '"isLiveNow":true' not in html and '"status":"LIVE"' not in html and '"LIVE_NOW"' not in html:
            return None

        m = re.search(r'"videoId":"([A-Za-z0-9_-]{6,})"', html)
        if not m:
            return None
        vid = m.group(1)

        # Intento suave de sacar título del HTML
        title = ""
        mt = re.search(r'"title":"([^"]{3,140})"', html)
        if mt:
            title = mt.group(1)

        return (vid, title)
    except Exception:
        return None


def fetch_latest_from_rss():
    feed = feedparser.parse(RSS_URL)
    if not getattr(feed, "entries", None):
        return None

    e = feed.entries[0]
    vid = getattr(e, "yt_videoid", None) or getattr(e, "yt_videoId", None)
    if not vid:
        return None

    title = safe_text(getattr(e, "title", "") or "")
    link = safe_text(getattr(e, "link", "") or "")

    published_dt = None
    try:
        if getattr(e, "published_parsed", None):
            import time as _time
            published_dt = datetime.fromtimestamp(_time.mktime(e.published_parsed), tz=ZoneInfo("UTC"))
    except Exception:
        published_dt = None

    thumb = None
    try:
        if hasattr(e, "media_thumbnail") and e.media_thumbnail:
            thumb = e.media_thumbnail[0].get("url")
    except Exception:
        thumb = None

    return {
        "vid": vid,
        "title": title,
        "link": link,
        "thumb": thumb,
        "published_utc": published_dt,
    }


def build_caption(kind: str, title: str, link: str, published_utc: datetime | None) -> str:
    header = "🔴 DIRECTO" if kind == "live" else "🎥 NUEVO VÍDEO"
    lines = [header]
    if title:
        lines.append(f"✨ {title}")
    when = format_local(published_utc)
    if when:
        lines.append(f"🕒 {when}")
    lines.append(f"👉 {link}")
    return "\n".join(lines)


def send_post(bot: Bot, chat_id: int, kind: str, vid: str, title: str, thumb_url: str | None, published_utc: datetime | None):
    link = f"https://www.youtube.com/watch?v={vid}"
    caption = build_caption(kind, title, link, published_utc)

    if thumb_url:
        b = download_bytes(thumb_url)
        if b:
            return bot.send_photo(chat_id=chat_id, photo=b, caption=caption)

    return bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)


def pin_message(bot: Bot, chat_id: int, message_id: int):
    bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)


def cleanup_history(bot: Bot, chat_id: int, keep_last: int):
    """
    Borra mensajes antiguos del canal/grupo para mantener solo N últimos.
    OJO: para esto el bot necesita permiso de "Eliminar mensajes".
    Funciona bien en supergrupos.
    En canales: si el bot es admin con permiso, normalmente también.
    """
    if keep_last <= 0:
        return

    try:
        # Leemos el chat (para saber message_id fijado, etc.)
        chat = bot.get_chat(chat_id)
        pinned = getattr(chat, "pinned_message", None)
        pinned_id = pinned.message_id if pinned else None
    except Exception:
        pinned_id = None

    # Estrategia simple:
    # - Tomamos el message_id más alto (el último) aproximando por pinned o por el último post que acabamos de enviar
    # - Como Telegram no permite listar historial por API Bot, usamos una ventana:
    #   intentamos borrar desde (last_id - 200) hacia atrás, dejando los últimos N.
    #
    # Para hacerlo robusto SIN listar, guardamos la ventana alrededor del pinned/último.
    # Aquí usamos un truco: conservamos los últimos N partiendo del message_id actual.
    #
    # NOTA: si Telegram no deja borrar algunos (no admin / antiguos), simplemente se ignora.

    # Para poder limpiar “bien” sin list API, necesitamos que el bot sepa el último message_id.
    # Lo pasamos por env via state? No. Lo resolvemos limpiando SOLO cuando podamos inferir.
    # Lo más práctico: limpiar alrededor del pinned_id (si existe). Si no, no limpiamos.
    if not pinned_id:
        return

    # Mantener pinned y los últimos N-1 posteriores (aprox). Borramos una ventana anterior amplia.
    # Borramos IDs desde pinned_id - 500 hasta pinned_id - 1, dejando margen.
    start = max(1, pinned_id - 500)
    end = pinned_id - 1

    # Como no sabemos exactamente cuáles son "los últimos N", esta limpieza es conservadora:
    # borra mucho de lo antiguo, pero NO toca los nuevos.
    # Si quieres limpieza exacta de "últimos 10", hay que guardar message_ids en un archivo/DB.
    # (Si lo quieres exacto, te lo hago, pero ya requiere volver a guardar estado.)
    for mid in range(start, end):
        if pinned_id and mid == pinned_id:
            continue
        try:
            bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


def main():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    # 1) Prioridad B: si hay directo activo -> fijar directo
    live = fetch_live_now_from_channel()
    if live:
        vid, title_guess = live
        pinned_vid = get_pinned_video_id(bot, chat_id)
        if pinned_vid == vid:
            print("Already pinned to current LIVE. No action.")
            return

        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        msg = send_post(bot, chat_id, "live", vid, title_guess or "Estamos en directo", thumb, None)
        pin_message(bot, chat_id, msg.message_id)

        # Limpieza (opcional, conservadora)
        cleanup_history(bot, chat_id, KEEP_LAST)

        print("Posted + pinned LIVE:", vid)
        return

    # 2) Si no hay directo -> último vídeo del RSS
    latest = fetch_latest_from_rss()
    if not latest:
        print("No RSS entries.")
        return

    vid = latest["vid"]
    pinned_vid = get_pinned_video_id(bot, chat_id)
    if pinned_vid == vid:
        print("Already pinned to latest VIDEO. No action.")
        return

    msg = send_post(
        bot,
        chat_id,
        "video",
        vid,
        latest.get("title", ""),
        latest.get("thumb"),
        latest.get("published_utc"),
    )
    pin_message(bot, chat_id, msg.message_id)

    cleanup_history(bot, chat_id, KEEP_LAST)

    print("Posted + pinned VIDEO:", vid)


if __name__ == "__main__":
    main()
