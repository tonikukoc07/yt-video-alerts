import os
import re
import time
import feedparser
from telegram import Bot

# ====== ENV ======
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

# Para GitHub Actions (run + exit). Si lo quieres en modo "bucle" en un server, ponlo a True.
RUN_ONCE = os.environ.get("RUN_ONCE", "true").lower() == "true"

# Solo se usa si RUN_ONCE = false
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))

RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# Estado persistente (se cachea en Actions)
LAST_FILE = "last_video.txt"


def must_env(name, value):
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


def load_last_video():
    try:
        with open(LAST_FILE, "r", encoding="utf-8") as f:
            v = f.read().strip()
            return v if v else None
    except FileNotFoundError:
        return None


def save_last_video(video_id: str):
    with open(LAST_FILE, "w", encoding="utf-8") as f:
        f.write(video_id)


def is_live_entry(entry) -> bool:
    """
    Detecta si es DIRECTO a partir del RSS.
    YouTube suele meter 'live' en yt:broadcast o en campos similares,
    pero no siempre viene igual. Hacemos heurística.
    """
    # 1) Algunos feeds incluyen yt_broadcast o media_* con 'live'
    for key in ["yt_broadcast", "broadcast", "media_status", "status"]:
        val = getattr(entry, key, None)
        if isinstance(val, str) and "live" in val.lower():
            return True

    # 2) Buscar en tags/links
    # A veces "live" aparece en href o en otras partes del objeto.
    raw = str(entry).lower()
    if "live" in raw and ("yt:live" in raw or "broadcast" in raw):
        return True

    # 3) fallback: si el link lleva /live o algo similar
    link = getattr(entry, "link", "") or ""
    if "/live" in link:
        return True

    return False


def extract_video_id(entry):
    # feedparser suele poner yt_videoid
    vid = getattr(entry, "yt_videoid", None)
    if vid:
        return vid

    # fallback: extraer v= de la URL
    link = getattr(entry, "link", "") or ""
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", link)
    return m.group(1) if m else None


def extract_thumbnail(entry):
    """
    YouTube RSS suele traer media_thumbnail con URL.
    """
    thumb = None

    media_thumbnail = getattr(entry, "media_thumbnail", None)
    if isinstance(media_thumbnail, list) and media_thumbnail:
        thumb = media_thumbnail[0].get("url")

    if not thumb:
        # fallback: si tenemos video_id, usamos imagen estándar
        vid = extract_video_id(entry)
        if vid:
            thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    return thumb


def fetch_latest():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return None

    e = feed.entries[0]
    vid = extract_video_id(e)
    title = getattr(e, "title", "") or ""
    link = getattr(e, "link", "") or ""
    thumb = extract_thumbnail(e)
    live = is_live_entry(e)

    return vid, title, link, live, thumb


def build_message(title: str, link: str, live: bool) -> str:
    if live:
        header = "🔴 **DIRECTO**"
    else:
        header = "🎥 **NUEVO VÍDEO**"

    # OJO: no usamos parse_mode, así que ** no se interpreta.
    # Si quieres negritas, habría que parse_mode="Markdown" y escapar.
    # Para evitar errores, lo dejamos en texto plano.
    header = "🔴 DIRECTO" if live else "🎥 NUEVO VÍDEO"

    msg = (
        f"{header}\n"
        f"✨ {title}\n\n"
        f"👉 {link}"
    )
    return msg


def send_alert(bot: Bot, chat_id: int, title: str, link: str, live: bool, thumb: str):
    msg = build_message(title, link, live)

    # Intentamos mandar con foto (miniatura). Si falla, mandamos texto.
    try:
        if thumb:
            bot.send_photo(chat_id=chat_id, photo=thumb, caption=msg, parse_mode=None)
        else:
            bot.send_message(chat_id=chat_id, text=msg, parse_mode=None)
    except Exception as e:
        print("Photo send failed, fallback to text. Error:", repr(e))
        bot.send_message(chat_id=chat_id, text=msg, parse_mode=None)


def run_once():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    last_sent = load_last_video()
    latest = fetch_latest()

    print("RSS:", RSS_URL)
    print("Last sent:", last_sent)

    if not latest:
        print("No entries found.")
        return

    vid, title, link, live, thumb = latest
    print("Latest vid:", vid)

    if not vid:
        print("No video id detected, skipping.")
        return

    if vid == last_sent:
        print("No new video. Skipping.")
        return

    send_alert(bot, chat_id, title, link, live, thumb)
    save_last_video(vid)
    print("Sent and saved:", vid)


def loop_forever():
    # Por si algún día lo quieres en un server 24/7
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("CHAT_ID", CHAT_ID)
    must_env("CHANNEL_ID", CHANNEL_ID)

    bot = Bot(token=TELEGRAM_TOKEN)
    chat_id = int(CHAT_ID)

    # Si es la primera vez, inicializamos sin avisar (opcional)
    last_sent = load_last_video()
    first_run = last_sent is None

    print("Bot started. Polling RSS:", RSS_URL)

    while True:
        try:
            latest = fetch_latest()
            if latest:
                vid, title, link, live, thumb = latest
                if not vid:
                    time.sleep(POLL_SECONDS)
                    continue

                if first_run:
                    save_last_video(vid)
                    last_sent = vid
                    first_run = False
                    print("Initialized last_sent =", last_sent)
                else:
                    if vid != last_sent:
                        send_alert(bot, chat_id
