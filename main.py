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
