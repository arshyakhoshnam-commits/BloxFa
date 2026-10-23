import os, re, json, logging, asyncio
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatMemberHandler,
    ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
VALUE_URL = os.getenv("VALUE_URL", "https://bloxfruitsvalues.net/values")
STOCK_URL = os.getenv("STOCK_URL", "https://fruityblox.com/stock")
CHECK_INTERVAL = max(300, int(os.getenv("CHECK_INTERVAL_SECONDS", "3600")))
DB_PATH = Path(os.getenv("DB_PATH", "data.json"))
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("bloxpulse")

DB = {"users": {}, "chats": {}, "values": {}, "stock": {}}
session = requests.Session()
session.headers.update({"User-Agent": "BloxPulseFa/1.0 (community stock/value notifier)"})

def load_db():
    global DB
    try:
        if DB_PATH.exists():
            loaded = json.loads(DB_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                DB.update(loaded)
    except Exception:
        log.exception("Could not read database; starting with in-memory defaults")

def save_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(DB, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DB_PATH)

def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def home_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📦 استاک فعلی"), KeyboardButton("💎 ولیوها")],
        [KeyboardButton("🔔 اعلان‌ها"), KeyboardButton("📱 تأیید شماره", request_contact=True)],
        [KeyboardButton("ℹ️ راهنما")]
    ], resize_keyboard=True)

def fetch_stock():
    """Community source adapter. Returns Normal/Mirage sections when source markup is available."""
    response = session.get(STOCK_URL, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    result = {}
    for h in soup.find_all(["h2", "h3"]):
        title = h.get_text(" ", strip=True).lower()
        if title not in ("normal", "mirage", "normal stock", "mirage stock"):
            continue
        section = h.find_parent("section") or h.parent
        fruits = []
        for item in section.find_all(["h3", "h4", "li"]):
            name = item.get_text(" ", strip=True)
            if name and name.lower() not in ("normal", "mirage", "normal stock", "mirage stock") and len(name) < 80:
                if name not in fruits:
                    fruits.append(name)
        key = "استاک معمولی" if "normal" in title else "استاک میراژ"
        if fruits:
            result[key] = fruits
    if not result:
        raise ValueError("ساختار صفحه استاک قابل تشخیص نیست؛ منبع ممکن است تغییر کرده باشد.")
    return result

def fetch_values():
    """Best-effort HTML parser for public community values; not an official Roblox feed."""
    response = session.get(VALUE_URL, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    items = {}
    # First pass: inspect repeated cards/articles/list items for item name and value text.
    for node in soup.find_all(["article", "li", "tr", "div"]):
        text = " ".join(node.get_text(" ", strip=True).split())
        if not text or len(text) > 500:
            continue
        m = re.search(r'(?<![\w.])(?:Value\s*)?([0-9]+(?:\.[0-9]+)?\s*[KMBT])\b', text, re.I)
        if not m:
            continue
        name = None
        for tag in node.find_all(["h2", "h3", "h4", "strong", "a"], limit=4):
            candidate = " ".join(tag.get_text(" ", strip=True).split())
            if candidate and len(candidate) < 70 and not re.search(r'\b(value|regular|permanent|demand)\b', candidate, re.I):
                name = candidate
                break
