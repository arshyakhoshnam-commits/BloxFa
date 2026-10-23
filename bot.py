import os, re, json, html, logging, asyncio
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ChatMemberHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
VALUE_URLS = [x.strip() for x in os.getenv("VALUE_URLS", os.getenv("VALUE_URL", "https://bloxfruitsvalues.net/values")).split(",") if x.strip()]
STOCK_URLS = [x.strip() for x in os.getenv("STOCK_URLS", "https://blox-fruitswiki.com/wiki/stock/,https://blox-fruits-wiki.com/wiki/stock/").split(",") if x.strip()]
CHECK_INTERVAL = max(300, int(os.getenv("CHECK_INTERVAL_SECONDS", "3600")))
DB_PATH = Path(os.getenv("DB_PATH", "data.json"))
logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bloxpulse")
DB = {"users": {}, "chats": {}, "values": {}, "stock": {}}
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"})

FRUITS = ["Rocket", "Spin", "Chop", "Spring", "Bomb", "Smoke", "Spike", "Flame", "Falcon", "Eagle", "Ice", "Sand", "Dark", "Diamond", "Light", "Rubber", "Barrier", "Ghost", "Magma", "Quake", "Buddha", "Love", "Creation", "Spider", "Sound", "Phoenix", "Portal", "Rumble", "Pain", "Blizzard", "Gravity", "Mammoth", "T-Rex", "Dough", "Shadow", "Venom", "Control", "Spirit", "Dragon", "Leopard", "Kitsune", "Yeti", "Gas", "Tiger", "West Dragon", "East Dragon"]

def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def load_db():
    global DB
    try:
        if DB_PATH.exists():
            data = json.loads(DB_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in ("users", "chats", "values", "stock"):
                    if isinstance(data.get(key), dict): DB[key] = data[key]
    except Exception:
        log.exception("Could not load data.json; using in-memory database")

def save_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(DB_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(DB, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DB_PATH)

def home_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📦 استاک فعلی"), KeyboardButton("💎 ولیوها")],
        [KeyboardButton("🔔 اعلان‌ها"), KeyboardButton("📱 ارسال شماره", request_contact=True)],
        [KeyboardButton("ℹ️ راهنما")]
    ], resize_keyboard=True)

def user_record(user):
    rec = DB.setdefault("users", {}).setdefault(str(user.id), {
        "id": user.id, "username": user.username or "", "first_name": user.first_name or "",
        "phone": "", "phone_verified": False, "created_at": now_text(), "alerts": True
    })
    rec.update({"id": user.id, "username": user.username or "", "first_name": user.first_name or ""})
    return rec

def get_soup(url, timeout=25):
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")

def norm(s):
    return " ".join((s or "").replace("\\xa0", " ").split())

def fruit_match(text):
    low = norm(text).lower()
    for fruit in sorted(FRUITS, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(fruit.lower()) + r"(?![a-z])", low):
            return fruit
    return None

def section_fruits(soup, patterns):
    headings = soup.find_all(["h1", "h2", "h3", "h4"])
    heading = next((h for h in headings if any(re.search(p, norm(h.get_text(" ", strip=True)).lower()) for p in patterns)), None)
    if not heading:
        return []
    # Walk following siblings until the next stock heading; prevents mixing "Last Stock".
    candidates = []
    for sibling in heading.next_elements:
        if sibling is heading:
            continue
        if getattr(sibling, "name", None) in ("h1", "h2", "h3", "h4"):
            title = norm(sibling.get_text(" ", strip=True)).lower()
            if any(x in title for x in ("current stock", "current mirage stock", "last stock", "last mirage", "history", "reset times")):
                break
        if getattr(sibling, "name", None) in ("a", "strong", "b", "li", "article", "img"):
            text = sibling.get("alt", "") if sibling.name == "img" else sibling.get_text(" ", strip=True)
            fruit = fruit_match(text)
            if fruit and fruit not in candidates:
                candidates.append(fruit)
    if not candidates:
        parent = heading.find_parent("section") or heading.parent
        for node in parent.find_all(["a", "strong", "b", "li", "article", "img"]):
            text = node.get("alt", "") if node.name == "img" else node.get_text(" ", strip=True)
            fruit = fruit_match(text)
            if fruit and fruit not in candidates:
                candidates.append(fruit)
    return candidates

def fetch_stock():
    errors = []
    for url in STOCK_URLS:
        try:
            soup = get_soup(url)
            normal = section_fruits(soup, [r"^current stock$", r"^normal stock$", r"^current normal stock$"])
            mirage = section_fruits(soup, [r"^current mirage stock$", r"^mirage stock$", r"^advanced stock$"])
            result = {}
            if normal: result["استاک معمولی"] = normal
            if mirage: result["استاک میراژ"] = mirage
            if result:
                log.info("Stock parsed from %s: %s", url, {k: len(v) for k, v in result.items()})
                return result
            errors.append(f"{url}: stock headings/cards not recognized")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            log.warning("Stock source failed (%s): %s", url, exc)
    raise RuntimeError("تمام منابع استاک ناموفق بودند: " + " | ".join(errors))

def value_number(value):
    m = re.search(r"([\\d,.]+)\\s*([KMBT]?)", str(value), re.I)
    if not m: return 0
    try: n = float(m.group(1).replace(",", ""))
    except ValueError: return 0
    return n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(m.group(2).upper(), 1)

def fetch_values():
    errors = []
    for url in VALUE_URLS:
        try:
            soup = get_soup(url)
            values = {}
            for node in soup.find_all(["article", "tr", "li"]):
                text = norm(node.get_text(" ", strip=True))
                if not text or len(text) > 350: continue
                matches = list(re.finditer(r"([\\d,.]+\\s*[KMBT])\\b", text, re.I))
                if not matches: continue
                # Usually the trade value is the first compact K/M/B value in a card.
                raw = matches[0].group(1).replace(" ", "").upper()
                name = ""
                for tag in node.find_all(["h2", "h3", "h4", "strong", "a", "td"], limit=8):
                    candidate = norm(tag.get_text(" ", strip=True))
                    if candidate and len(candidate) <= 60 and not re.search(r"\\b(value|demand|regular|permanent|beli|robux|price)\\b", candidate, re.I) and not re.fullmatch(r"[\\d,.]+\\s*[KMBT]?", candidate, re.I):
                        name = candidate
                        break
                if not name:
                    name = fruit_match(text) or ""
                if name: values[name] = raw
            if values:
                log.info("Parsed %s value rows from %s", len(values), url)
                return dict(sorted(values.items(), key=lambda kv: value_number(kv[1]), reverse=True))
            errors.append(f"{url}: no values parsed")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            log.warning("Value source failed (%s): %s", url, exc)
    raise RuntimeError("تمام منابع ولیو ناموفق بودند: " + " | ".join(errors))

def values_text(values, limit=70):
    return "\\n".join(f"• {html.escape(str(k))} — <b>{html.escape(str(v))}</b>" for k, v in list(values.items())[:limit])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        user_record(user); save_db()
    name = html.escape(user.first_name if user else "")
    await update.effective_message.reply_text(f"سلام {name}!\\nبه بات فارسی استاک و ولیو خوش آمدی.\\nبرای ثبت شماره، دکمه «📱 ارسال شماره» را بزن.", reply_markup=home_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("راهنما:\\n📦 استاک فعلی\\n💎 ولیوها\\n🔔 روشن/خاموش کردن اعلان‌ها\\n📱 ارسال شماره خودت از طریق تلگرام\\nادمین: /members و /status")

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, sender = update.effective_message, update.effective_user
    contact = msg.contact if msg else None
    if not sender or not contact: return
    if contact.user_id != sender.id:
        await msg.reply_text("لطفاً شمارهٔ خودت را با دکمهٔ تلگرام ارسال کن."); return
    rec = user_record(sender)
    rec.update({"phone": contact.phone_number, "phone_verified": True, "phone_updated_at": now_text()})
    save_db()
    await msg.reply_text("✅ شماره از طریق تلگرام ثبت شد.", reply_markup=home_keyboard())
    note = ("📱 <b>شماره کاربر ثبت شد</b>\\n"
            f"نام: {html.escape(sender.full_name or '-')}\\n"
            f"نام کاربری: @{html.escape(sender.username) if sender.username else 'ندارد'}\\n"
            f"آیدی عددی: <code>{sender.id}</code>\\nشماره: <code>{html.escape(contact.phone_number)}</code>\\nزمان: {now_text()}\\n"
            "<i>این تأیید پیامکی نیست؛ کاربر contact خودش را از تلگرام ارسال کرده است.</i>")
    for aid in ADMIN_IDS:
        try: await context.bot.send_message(aid, note, parse_mode="HTML")
        except Exception: log.exception("Failed to notify admin %s", aid)

async def send_stock(update, context):
    try:
        stock = await asyncio.to_thread(fetch_stock)
        body = "\\n\\n".join(f"<b>{html.escape(k)}</b>\\n" + "\\n".join("• " + html.escape(x) for x in v) for k, v in stock.items())
        await update.effective_message.reply_text("📦 <b>استاک فعلی</b>\\n\\n" + body, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        log.warning("Stock fetch failed: %s", exc)
        await update.effective_message.reply_text("فعلاً دریافت استاک ممکن نشد؛ کمی بعد دوباره امتحان کن.")

async def send_values(update, context):
    try:
        vals = await asyncio.to_thread(fetch_values)
        await update.effective_message.reply_text("💎 <b>ولیوها (از بیشترین به کمترین)</b>\\n\\n" + values_text(vals), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        log.warning("Value fetch failed: %s", exc)
        await update.effective_message.reply_text("فعلاً دریافت ولیوها ممکن نشد؛ منبع ممکن است تغییر کرده یا دسترسی بات را بسته باشد.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or not msg.text: return
    rec = user_record(user); save_db()
    if msg.text == "📦 استاک فعلی": await send_stock(update, context)
    elif msg.text == "💎 ولیوها": await send_values(update, context)
    elif msg.text == "🔔 اعلان‌ها":
        rec["alerts"] = not rec.get("alerts", True); save_db()
        await msg.reply_text("اعلان‌ها " + ("روشن شد." if rec["alerts"] else "خاموش شد."))
    elif msg.text == "ℹ️ راهنما": await help_cmd(update, context)

def make_members_html():
    users = sorted(DB.get("users", {}).values(), key=lambda u: int(u.get("id", 0)))
    rows = []
    for i, u in enumerate(users, 1):
        username = "@" + u.get("username", "") if u.get("username") else "ندارد"
        cells = [i, u.get("first_name", ""), username, u.get("id", ""), u.get("phone") or "ندارد",
                 "ثبت‌شده از تلگرام" if u.get("phone_verified") else "ثبت نشده", u.get("created_at", "")]
        rows.append("<tr>" + "".join("<td>" + html.escape(str(c)) + "</td>" for c in cells) + "</tr>")
    table = "\\n".join(rows) or '<tr><td colspan="7">هنوز کاربری ثبت نشده است.</td></tr>'
    return f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>اعضای BloxPulse</title>
<style>body{{font-family:Tahoma,Arial;background:#0e1420;color:#edf2f7;margin:0;padding:24px}}.wrap{{max-width:1200px;margin:auto}}h1{{font-size:25px}}.count{{background:#192538;padding:16px;border-radius:12px;margin:16px 0}}.tablewrap{{overflow:auto;background:#141e2d;border-radius:12px}}table{{border-collapse:collapse;width:100%;min-width:850px}}th,td{{padding:12px;border-bottom:1px solid #2b394c;text-align:right;white-space:nowrap}}th{{background:#20324a}}tr:hover{{background:#1b2a3d}}input{{padding:10px;border-radius:8px;border:1px solid #394b62;background:#111b29;color:white;width:min(360px,90%)}}small{{color:#aab7c8}}</style></head><body><div class="wrap"><h1>اعضای BloxPulse فارسی</h1><div class="count">تعداد کل کاربران: <b>{len(users)}</b></div><input id="q" placeholder="جستجو در نام، آیدی، شماره..." oninput="filterRows()"><p><small>این فایل شامل اطلاعات خصوصی کاربران است؛ آن را عمومی منتشر نکنید.</small></p><div class="tablewrap"><table><thead><tr><th>ردیف</th><th>نام</th><th>نام کاربری</th><th>آیدی عددی</th><th>شماره تلفن</th><th>وضعیت شماره</th><th>تاریخ ثبت</th></tr></thead><tbody id="rows">{table}</tbody></table></div></div><script>function filterRows(){{let q=document.getElementById('q').value.toLowerCase();document.querySelectorAll('#rows tr').forEach(r=>r.style.display=r.innerText.toLowerCase().includes(q)?'':'none')}}</script></body></html>'''

async def members_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ این دستور فقط برای ادمین است."); return
    path = Path("members.html")
    path.write_text(make_members_html(), encoding="utf-8")
    with path.open("rb") as f:
        await update.effective_message.reply_document(document=f, filename="members.html", caption=f"📋 فهرست اعضا — تعداد کل: {len(DB.get('users', {}))}\\nاین فایل را عمومی منتشر نکن.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ این دستور فقط برای ادمین است."); return
    await update.effective_message.reply_text(f"🟢 بات فعال است.\\nکاربران: {len(DB.get('users', {}))}\\nگروه‌ها: {len(DB.get('chats', {}))}\\nفاصله بررسی: {CHECK_INTERVAL} ثانیه")

async def announce_changes(app, key, new_data, formatter):
    old = DB.get(key, {})
    if old and old != new_data:
        message = formatter(new_data)
        for uid, rec in list(DB.get("users", {}).items()):
            if rec.get("alerts", True):
                try: await app.bot.send_message(int(uid), message, parse_mode="HTML")
                except Exception as exc: log.debug("Notify user %s failed: %s", uid, exc)
        for cid in list(DB.get("chats", {})):
            try: await app.bot.send_message(int(cid), message, parse_mode="HTML")
            except Exception as exc: log.debug("Notify chat %s failed: %s", cid, exc)
    DB[key] = new_data
    save_db()

async def periodic_checks(app):
    await asyncio.sleep(10)
    while True:
        try:
            data = await asyncio.to_thread(fetch_stock)
            await announce_changes(app, "stock", data, lambda d: "📦 <b>تغییر استاک</b>\\n\\n" + "\\n\\n".join("<b>"+html.escape(k)+"</b>\\n"+"\\n".join("• "+html.escape(x) for x in v) for k,v in d.items()))
        except asyncio.CancelledError: raise
        except Exception: log.exception("Stock check failed")
        try:
            data = await asyncio.to_thread(fetch_values)
            await announce_changes(app, "values", data, lambda d: "💎 <b>تغییر ولیوها</b>\\n\\n" + values_text(d))
        except asyncio.CancelledError: raise
        except Exception: log.exception("Value check failed")
        await asyncio.sleep(CHECK_INTERVAL)

async def post_init(app):
    load_db()
    app.bot_data["periodic_task"] = asyncio.create_task(periodic_checks(app))

async def post_shutdown(app):
    task = app.bot_data.get("periodic_task")
    if task:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        DB.setdefault("chats", {})[str(chat.id)] = {"title": chat.title or "", "type": chat.type}
        save_db()

def main():
    if not BOT_TOKEN:
        raise RuntimeError("متغیر BOT_TOKEN در Environment Variables هاست تنظیم نشده است.")
    load_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("members", members_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
