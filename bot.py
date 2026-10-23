import os, re, json, logging, asyncio, html
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ChatMemberHandler, ContextTypes, filters

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
session.headers.update({"User-Agent": "BloxPulseFa/1.1"})

def load_db():
    global DB
    try:
        if DB_PATH.exists():
            loaded = json.loads(DB_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict): DB.update(loaded)
    except Exception: log.exception("Could not read database")

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
        [KeyboardButton("🔔 اعلان‌ها"), KeyboardButton("📱 ارسال شماره", request_contact=True)],
        [KeyboardButton("ℹ️ راهنما")]
    ], resize_keyboard=True)

def user_record(user):
    rec = DB.setdefault("users", {}).setdefault(str(user.id), {
        "id": user.id, "username": user.username or "", "first_name": user.first_name or "",
        "phone": "", "phone_verified": False, "created_at": now_text(), "alerts": True
    })
    rec["username"], rec["first_name"] = user.username or "", user.first_name or ""
    return rec

def fetch_stock():
    r = session.get(STOCK_URL, timeout=20); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser"); result = {}
    for h in soup.find_all(["h2", "h3"]):
        title = h.get_text(" ", strip=True).lower()
        if title not in ("normal", "mirage", "normal stock", "mirage stock"): continue
        section = h.find_parent("section") or h.parent; fruits = []
        for item in section.find_all(["h3", "h4", "li"]):
            name = item.get_text(" ", strip=True)
            if name and name.lower() not in ("normal", "mirage", "normal stock", "mirage stock") and len(name) < 80 and name not in fruits:
                fruits.append(name)
        if fruits: result["استاک معمولی" if "normal" in title else "استاک میراژ"] = fruits
    if not result: raise ValueError("ساختار صفحه استاک قابل تشخیص نیست.")
    return result

def value_number(s):
    m = re.match(r"([\d.]+)\s*([KMBT]?)", str(s), re.I)
    if not m: return 0
    return float(m.group(1))*{"":1,"K":1e3,"M":1e6,"B":1e9,"T":1e12}.get(m.group(2).upper(),1)

def fetch_values():
    r = session.get(VALUE_URL, timeout=25); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser"); items = {}
    for node in soup.find_all(["article", "li", "tr", "div"]):
        text = " ".join(node.get_text(" ", strip=True).split())
        if not text or len(text) > 500: continue
        m = re.search(r"(?<![\w.])(?:Value\s*)?([0-9]+(?:\.[0-9]+)?\s*[KMBT])\b", text, re.I)
        if not m: continue
        name = None
        for tag in node.find_all(["h2","h3","h4","strong","a"], limit=4):
            candidate = " ".join(tag.get_text(" ", strip=True).split())
            if candidate and len(candidate)<70 and not re.search(r"\b(value|regular|permanent|demand)\b",candidate,re.I):
                name = candidate; break
        if name: items[name] = m.group(1).replace(" ","").upper()
    if not items: raise ValueError("از صفحه ولیو موردی استخراج نشد.")
    return dict(sorted(items.items(), key=lambda kv: value_number(kv[1]), reverse=True))

def values_text(values, limit=70):
    return "\n".join(f"• {html.escape(k)} — <b>{html.escape(str(v))}</b>" for k,v in list(values.items())[:limit])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user: user_record(user); save_db()
    await update.effective_message.reply_text(
        f"سلام {user.first_name if user else ''}!\nبه بات فارسی استاک و ولیو خوش آمدی.\nبرای ثبت شماره، دکمه «📱 ارسال شماره» را بزن.",
        reply_markup=home_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("راهنما: 📦 استاک فعلی، 💎 ولیوها، 🔔 اعلان‌ها، 📱 ارسال شماره. دستور ادمین /members فایل HTML اعضا را می‌فرستد.")

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, sender = update.effective_message, update.effective_user
    contact = msg.contact
    if not sender or not contact: return
    if contact.user_id != sender.id:
        await msg.reply_text("لطفاً شمارهٔ خودت را با دکمهٔ تلگرام ارسال کن."); return
    rec = user_record(sender)
    rec.update({"phone": contact.phone_number, "phone_verified": True, "phone_updated_at": now_text()})
    save_db()
    await msg.reply_text("✅ شماره‌ات خودکار ثبت و تأیید شد.", reply_markup=home_keyboard())
    note = ("📱 <b>شماره کاربر ثبت و تأیید شد</b>\n"
            f"نام: {html.escape(sender.full_name or '-')}\n"
            f"نام کاربری: @{html.escape(sender.username) if sender.username else 'ندارد'}\n"
            f"آیدی عددی: <code>{sender.id}</code>\nشماره: <code>{html.escape(contact.phone_number)}</code>\nزمان: {now_text()}")
    for aid in ADMIN_IDS:
        try: await context.bot.send_message(aid, note, parse_mode="HTML")
        except Exception: log.exception("Failed to notify admin %s", aid)

async def send_stock(update, context):
    try:
        stock = await asyncio.to_thread(fetch_stock)
        body = "\n\n".join(f"<b>{html.escape(k)}</b>\n" + "\n".join("• "+html.escape(x) for x in v) for k,v in stock.items())
        await update.effective_message.reply_text("📦 <b>استاک فعلی</b>\n\n"+body, parse_mode="HTML")
    except Exception as e:
        log.warning("stock fetch failed: %s", e)
        await update.effective_message.reply_text("فعلاً دریافت استاک ممکن نشد؛ کمی بعد دوباره امتحان کن.")

async def send_values(update, context):
    try:
        vals = await asyncio.to_thread(fetch_values)
        await update.effective_message.reply_text("💎 <b>ولیوها (از بیشترین به کمترین)</b>\n\n"+values_text(vals), parse_mode="HTML")
    except Exception as e:
        log.warning("values fetch failed: %s", e)
        await update.effective_message.reply_text("فعلاً دریافت ولیوها ممکن نشد.")

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
    for i,u in enumerate(users,1):
        username = "@"+u.get("username","") if u.get("username") else "ندارد"
        cells = [i,u.get("first_name",""),username,u.get("id",""),u.get("phone") or "ندارد",
                 "تأیید شده" if u.get("phone_verified") else "ثبت نشده",u.get("created_at","")]
        rows.append("<tr>"+"".join("<td>"+html.escape(str(c))+"</td>" for c in cells)+"</tr>")
    table = "\n".join(rows) or '<tr><td colspan="7">هنوز کاربری ثبت نشده است.</td></tr>'
    return f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>اعضای BloxPulse</title>
<style>body{{font-family:Tahoma,Arial;background:#0e1420;color:#edf2f7;margin:0;padding:24px}}.wrap{{max-width:1200px;margin:auto}}h1{{font-size:25px}}.count{{background:#192538;padding:16px;border-radius:12px;margin:16px 0}}.tablewrap{{overflow:auto;background:#141e2d;border-radius:12px}}table{{border-collapse:collapse;width:100%;min-width:850px}}th,td{{padding:12px;border-bottom:1px solid #2b394c;text-align:right;white-space:nowrap}}th{{background:#20324a}}tr:hover{{background:#1b2a3d}}input{{padding:10px;border-radius:8px;border:1px solid #394b62;background:#111b29;color:white;width:min(360px,90%)}}small{{color:#aab7c8}}</style></head><body><div class="wrap"><h1>اعضای BloxPulse فارسی</h1><div class="count">تعداد کل کاربران: <b>{len(users)}</b></div><input id="q" placeholder="جستجو در نام، آیدی، شماره..." oninput="filterRows()"><p><small>این فایل شامل اطلاعات خصوصی کاربران است؛ آن را عمومی منتشر نکنید.</small></p><div class="tablewrap"><table><thead><tr><th>ردیف</th><th>نام</th><th>نام کاربری</th><th>آیدی عددی</th><th>شماره تلفن</th><th>وضعیت شماره</th><th>تاریخ ثبت</th></tr></thead><tbody id="rows">{table}</tbody></table></div></div><script>function filterRows(){{let q=document.getElementById('q').value.toLowerCase();document.querySelectorAll('#rows tr').forEach(r=>r.style.display=r.innerText.toLowerCase().includes(q)?'':'none')}}</script></body></html>'''

async def members_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ این دستور فقط برای ادمین است."); return
    path = Path("members.html"); path.write_text(make_members_html(), encoding="utf-8")
    with path.open("rb") as f:
        await update.effective_message.reply_document(document=f, filename="members.html",
            caption=f"📋 فهرست اعضا — تعداد کل: {len(DB.get('users',{}))}\nاین فایل را عمومی منتشر نکن.")

async def announce_changes(app, key, new_data, formatter):
    old = DB.get(key, {})
    if old and old != new_data:
        message = formatter(new_data)
        for uid, rec in list(DB.get("users", {}).items()):
            if rec.get("alerts", True):
                try: await app.bot.send_message(int(uid), message, parse_mode="HTML")
                except Exception: pass
        for cid in list(DB.get("chats", {})):
            try: await app.bot.send_message(int(cid), message, parse_mode="HTML")
            except Exception: pass
    DB[key] = new_data; save_db()

async def periodic_checks(app):
    while True:
        try:
            data = await asyncio.to_thread(fetch_stock)
            await announce_changes(app,"stock",data,lambda d:"📦 <b>تغییر استاک</b>\n\n"+"\n\n".join("<b>"+html.escape(k)+"</b>\n"+"\n".join("• "+html.escape(x) for x in v) for k,v in d.items()))
        except Exception: log.exception("stock check failed")
        try:
            data = await asyncio.to_thread(fetch_values)
            await announce_changes(app,"values",data,lambda d:"💎 <b>تغییر ولیوها</b>\n\n"+values_text(d))
        except Exception: log.exception("values check failed")
        await asyncio.sleep(CHECK_INTERVAL)

async def post_init(app):
    load_db()
    app.create_task(periodic_checks(app))

async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat:
        DB.setdefault("chats",{})[str(chat.id)]={"title":chat.title or "", "type":chat.type}
        save_db()

def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN در Environment Variables تنظیم نشده است.")
    load_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("help",help_cmd))
    app.add_handler(CommandHandler("members",members_cmd))
    app.add_handler(MessageHandler(filters.CONTACT,handle_contact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))
    app.add_handler(ChatMemberHandler(chat_member_update,ChatMemberHandler.MY_CHAT_MEMBER))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__": main()
