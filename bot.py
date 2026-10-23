import os, re, json, logging, asyncio
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
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
        if name and name.lower() not in ("values", "value list", "blox fruits values"):
            # Keep first/smallest matching container; avoid overwriting with parent duplicates.
            items[name] = m.group(1).replace(" ", "").upper()
    if len(items) < 15:
        raise ValueError(f"فقط {len(items)} آیتم از منبع ولیو خوانده شد؛ برای جلوگیری از ارسال داده ناقص، انتشار لغو شد.")
    return items

async def send_to_chat(context, chat_id, text):
    try:
        await context.bot.send_message(chat_id=int(chat_id), text=text, disable_web_page_preview=True)
        return True
    except Exception as exc:
        log.warning("Send failed to %s: %s", chat_id, exc)
        return False

async def broadcast(context, text, include_users=True, include_groups=True):
    targets = set()
    if include_users:
        targets.update(str(uid) for uid, info in DB["users"].items() if info.get("verified") and info.get("subscribed", True))
    if include_groups:
        targets.update(str(cid) for cid, info in DB["chats"].items() if info.get("enabled", True))
    sent = failed = 0
    for target in targets:
        if await send_to_chat(context, target, text):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.04)
    log.info("Broadcast complete: sent=%s failed=%s", sent, failed)

def format_stock(stock):
    lines = ["📦 استاک جدید Blox Fruits", f"🕒 بررسی‌شده در: {now_text()}", ""]
    for section, fruits in stock.items():
        lines.append(f"🔹 {section}")
        lines.extend(f"• {name}" for name in fruits)
        lines.append("")
    lines.append("منبع: صفحه عمومی استاک جامعه")
    return "\n".join(lines).strip()

def format_value_changes(changes):
    lines = ["💎 تغییرات ولیوی Blox Fruits", f"🕒 بررسی‌شده در: {now_text()}", ""]
    for name, (old, new) in changes[:60]:
        lines.append(f"• {name}: {old or 'جدید'} ➜ {new}")
    if len(changes) > 60:
        lines.append(f"\nو {len(changes)-60} تغییر دیگر…")
    lines += ["", f"فهرست منبع: {VALUE_URL}", "⚠️ ولیوها تخمینی و جامعه‌محورند؛ قیمت رسمی Roblox نیستند."]
    return "\n".join(lines)

async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    # Stock: publish only if contents changed; the first successful run establishes baseline.
    try:
        stock = await asyncio.to_thread(fetch_stock)
        if stock != DB.get("stock"):
            DB["stock"] = stock
            save_db()
            if context.application.bot_data.get("stock_initialized"):
                await broadcast(context, format_stock(stock))
            else:
                context.application.bot_data["stock_initialized"] = True
                log.info("Initial stock baseline saved; no broadcast")
    except Exception:
        log.exception("Stock fetch failed; previous data retained")

    # Values: poll each cycle; only send changes, reject obviously incomplete fetches.
    try:
        values = await asyncio.to_thread(fetch_values)
        previous = DB.get("values", {})
        changes = [(name, (previous.get(name), value)) for name, value in values.items()
                   if name in previous and previous[name] != value]
        is_baseline = not bool(previous)
        DB["values"] = values
        save_db()
        if changes and not is_baseline:
            await broadcast(context, format_value_changes(changes))
        elif is_baseline:
            log.info("Initial values baseline saved; no broadcast")
    except Exception:
        log.exception("Values fetch failed; previous data retained")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        DB["chats"][str(chat.id)] = {"title": chat.title or "گروه", "enabled": True}
        save_db()
        if update.message:
            await update.message.reply_text("✅ BloxPulse فارسی به این گروه اضافه شد.\nبرای دریافت اعلان‌ها، بات باید اجازه ارسال پیام داشته باشد.")
        return
    DB["users"].setdefault(str(user.id), {"name": user.full_name, "verified": False, "subscribed": True})
    save_db()
    await update.message.reply_text(
        "سلام! 👋 به BloxPulse فارسی خوش اومدی.\n\nبرای دریافت اعلان‌های شخصی، ابتدا شماره خودت را با دکمه «تأیید شماره» به اشتراک بگذار. شماره فقط از طریق دکمه تماس تلگرام پذیرفته می‌شود.",
        reply_markup=home_keyboard()
    )

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    user = update.effective_user
    if not contact or contact.user_id != user.id:
        await update.message.reply_text("❌ لطفاً فقط شماره خودت را با دکمه داخل بات به اشتراک بگذار.")
        return
    info = DB["users"].setdefault(str(user.id), {"name": user.full_name, "subscribed": True})
    info["verified"] = True
    info["phone"] = contact.phone_number
    info["name"] = user.full_name
    save_db()
    await update.message.reply_text("✅ شماره‌ات تأیید شد. اعلان‌های شخصی برایت فعال است.", reply_markup=home_keyboard())

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "📦 استاک فعلی":
        try:
            stock = await asyncio.to_thread(fetch_stock)
            await update.message.reply_text(format_stock(stock))
        except Exception:
            await update.message.reply_text("الان نتونستم استاک را از منبع بخونم. کمی بعد دوباره امتحان کن.")
    elif text == "💎 ولیوها":
        values = DB.get("values", {})
        if not values:
            await update.message.reply_text("هنوز داده ولیو با موفقیت دریافت نشده. مدیر باید منبع را بررسی کند.")
        else:
            ordered = sorted(values.items(), key=lambda x: parse_value(x[1]), reverse=True)
            lines = ["💎 ولیوهای ثبت‌شده", f"🕒 آخرین بررسی: {now_text()}", ""]
            lines += [f"{i}. {name}: {val}" for i, (name, val) in enumerate(ordered[:70], 1)]
            lines += ["", f"منبع: {VALUE_URL}", "⚠️ مقادیر تخمینی جامعه هستند."]
            await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)
    elif text == "🔔 اعلان‌ها":
        info = DB["users"].get(str(update.effective_user.id), {})
        info["subscribed"] = not info.get("subscribed", True)
        DB["users"][str(update.effective_user.id)] = info
        save_db()
        await update.message.reply_text("🔔 اعلان‌ها فعال شد." if info["subscribed"] else "🔕 اعلان‌ها غیرفعال شد.")
    elif text == "ℹ️ راهنما":
        await update.message.reply_text("این بات استاک و تغییرات ولیوی عمومی Blox Fruits را بررسی می‌کند.\nبرای دریافت اعلان خصوصی، شماره خودت را تأیید کن. /start")
    else:
        await update.message.reply_text("از دکمه‌های منو استفاده کن یا /start را بزن.", reply_markup=home_keyboard())

def parse_value(value):
    m = re.match(r"([\d.]+)([KMBT]?)", str(value), re.I)
    if not m: return 0
    return float(m.group(1)) * {"":1, "K":1e3, "M":1e6, "B":1e9, "T":1e12}.get(m.group(2).upper(), 1)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"شناسه عددی شما: `{update.effective_user.id}`", parse_mode="Markdown")

async def admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text(
        f"وضعیت BloxPulse\nکاربران: {len(DB['users'])}\nکاربران تأییدشده: {sum(1 for x in DB['users'].values() if x.get('verified'))}\nگروه‌ها/کانال‌ها: {len(DB['chats'])}\nآیتم‌های ولیو: {len(DB.get('values', {}))}\nفاصله بررسی: {CHECK_INTERVAL} ثانیه"
    )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    message = " ".join(context.args).strip()
    if not message:
        await update.message.reply_text("استفاده: /broadcast متن پیام")
        return
    await broadcast(context, "📣 پیام مدیر\n\n" + message)

async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        member = update.my_chat_member.new_chat_member
        if member.status in ("member", "administrator"):
            DB["chats"][str(chat.id)] = {"title": chat.title or "گروه", "enabled": True}
        elif member.status in ("left", "kicked"):
            DB["chats"].pop(str(chat.id), None)
        save_db()

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Set it in the host environment variables.")
    load_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", myid))
    app.add_handler(CommandHandler("status", admin_status))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.StatusUpdate.MY_CHAT_MEMBER, chat_member_update))
    app.job_queue.run_repeating(scheduled_check, interval=CHECK_INTERVAL, first=10)
    log.info("BloxPulse Persian is starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
