import os, re, html, json, asyncio, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, ChatMemberHandler

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
VALUES_URL = os.getenv("VALUE_URL", "https://bloxfruitsvalues.com/values").strip()
STOCK_URL = os.getenv("STOCK_URL", "https://bloxfruitsvalues.com/stock").strip()
DB_PATH = Path(os.getenv("DB_PATH", "data.json"))
CHECK_INTERVAL = max(900, int(os.getenv("CHECK_INTERVAL_SECONDS", "3600")))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bloxpulse")
S = requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
                  "Accept":"text/html,application/xhtml+xml", "Accept-Language":"en-US,en;q=0.9"})
DB = {"users": {}, "chats": {}, "values": {}, "stock": {}}
IMAGES = {}
FRUITS = ["Rocket","Spin","Blade","Chop","Spring","Bomb","Smoke","Spike","Flame","Falcon","Eagle","Ice","Sand","Dark","Diamond","Light","Rubber","Barrier","Ghost","Magma","Quake","Buddha","Love","Creation","Spider","Sound","Phoenix","Portal","Rumble","Lightning","Pain","Blizzard","Gravity","Mammoth","T-Rex","Dough","Shadow","Venom","Control","Spirit","Dragon","West Dragon","East Dragon","Leopard","Tiger","Kitsune","Yeti","Gas","Magnet"]
LOOKUP = {x.lower():x for x in FRUITS}
LOOKUP["trex"] = "T-Rex"

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def load_db():
    global DB
    try:
        d=json.loads(DB_PATH.read_text(encoding="utf-8"))
        if isinstance(d,dict):
            for k in DB:
                if isinstance(d.get(k),dict): DB[k]=d[k]
    except FileNotFoundError: pass
    except Exception: log.exception("Could not read data.json; keeping in-memory data")

def save_db():
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    p=DB_PATH.with_suffix(".tmp")
    p.write_text(json.dumps(DB,ensure_ascii=False,indent=2),encoding="utf-8")
    p.replace(DB_PATH)

def norm(x): return " ".join(str(x or "").replace("\xa0"," ").split())

def fruit_name(text):
    t=norm(text).lower()
    for k in sorted(LOOKUP,key=len,reverse=True):
        if re.search(r"(?<![a-z])"+re.escape(k)+r"(?![a-z])",t): return LOOKUP[k]
    return None

def soup_for(url):
    r=S.get(url,timeout=25); r.raise_for_status()
    return BeautifulSoup(r.text,"html.parser")

def parse_num(s):
    m=re.search(r"(?<![A-Za-z])(\d+(?:\.\d+)?)\s*([KMBT])\b",str(s),re.I)
    if not m: return None
    return float(m.group(1))*{"K":1e3,"M":1e6,"B":1e9,"T":1e12}[m.group(2).upper()]

def fmt_num(n):
    for unit,scale in (("T",1e12),("B",1e9),("M",1e6),("K",1e3)):
        if n>=scale: return f"{n/scale:g}{unit}"
    return f"{n:g}"

def image_of(node,base):
    if not node:return ""
    im=node.find("img")
    if not im:return ""
    src=im.get("src") or im.get("data-src") or im.get("data-lazy-src") or ""
    if not src and im.get("srcset"): src=im["srcset"].split(",")[0].strip().split()[0]
    return urljoin(base,src) if src and not src.startswith("data:") else ""

def card_of(node):
    cur=node
    for _ in range(5):
        if cur.parent is None: break
        cur=cur.parent
        if len(norm(cur.get_text(" ",strip=True)))<=900: return cur
    return node.parent or node

def fetch_values():
    sp=soup_for(VALUES_URL)
    values={}; imgs={}
    # The page exposes one heading per item. Extract only that item's card.
    for h in sp.find_all(["h2","h3","h4"]):
        name=fruit_name(h.get_text(" ",strip=True))
        if not name or name in values: continue
        card=card_of(h); text=norm(card.get_text(" ",strip=True))
        if len(text)>900: continue
        # In the requested site's cards the trade value appears before Beli metadata.
        tail=text[text.lower().find(name.lower())+len(name):]
        m=re.search(r"\b(\d+(?:\.\d+)?\s*[KMBT])\b",tail,re.I)
        if not m: continue
        n=parse_num(m.group(1))
        if not n or n<=0: continue
        values[name]=fmt_num(n)
        img=image_of(card,VALUES_URL)
        if img: imgs[name]=img
    if len(values)<5: raise RuntimeError(f"Parsed only {len(values)} fruit values from {VALUES_URL}; page layout may have changed")
    IMAGES.update(imgs)
    log.info("Parsed %d values from %s",len(values),VALUES_URL)
    return dict(sorted(values.items(),key=lambda x:parse_num(x[1]) or 0,reverse=True))

def fetch_stock():
    sp=soup_for(STOCK_URL)
    result={}
    for title,aliases in (("استاک معمولی",{"normal stock"}),("استاک میراژ",{"mirage stock"})):
        h=next((x for x in sp.find_all(["h2","h3"]) if norm(x.get_text(" ",strip=True)).lower() in aliases),None)
        if not h: continue
        # Scope to a section/container and collect fruit cards from headings.
        container=h.parent
        for _ in range(5):
            if container and len(norm(container.get_text(" ",strip=True)))>100: break
            container=container.parent if container else None
        if not container: continue
        items=[]; seen=set()
        for fh in container.find_all(["h3","h4"]):
            name=fruit_name(fh.get_text(" ",strip=True))
            if not name or name in seen: continue
            card=card_of(fh); txt=norm(card.get_text(" ",strip=True))
            if len(txt)>900: continue
            im=image_of(card,STOCK_URL) or IMAGES.get(name,"")
            vm=re.search(r"\bValue\s*(\d+(?:\.\d+)?\s*[KMBT])",txt,re.I)
            bm=re.search(r"\bBeli\s*([\d,]+)",txt,re.I)
            rm=re.search(r"\bRobux\s*([\d,]+)",txt,re.I)
            item={"name":name,"image":im}
            if vm:item["value"]=vm.group(1).replace(" ","")
            if bm:item["beli"]=bm.group(1)
            if rm:item["robux"]=rm.group(1)
            items.append(item);seen.add(name)
        if items: result[title]=items
    if not result: raise RuntimeError(f"No stock cards parsed from {STOCK_URL}")
    log.info("Stock source returned %s", {k:len(v) for k,v in result.items()})
    return result

def keyboard(saved):
    rows=[[KeyboardButton("📦 استاک فعلی"),KeyboardButton("💎 ولیوها")],
          [KeyboardButton("🔔 اعلان‌ها"),KeyboardButton("ℹ️ راهنما")]]
    if not saved: rows.insert(1,[KeyboardButton("📱 ثبت شماره",request_contact=True)])
    return ReplyKeyboardMarkup(rows,resize_keyboard=True)

def rec_for(user):
    rec=DB["users"].setdefault(str(user.id),{"id":user.id,"phone":"","phone_verified":False,"created_at":now(),"alerts":True})
    rec.update({"id":user.id,"username":user.username or "","first_name":user.first_name or ""})
    rec.setdefault("phone","");rec.setdefault("phone_verified",False);rec.setdefault("alerts",True);rec.setdefault("created_at",now())
    return rec

async def start(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    u=update.effective_user
    if not u:return
    r=rec_for(u);save_db()
    saved=bool(r.get("phone") and r.get("phone_verified"))
    msg=(f"سلام {html.escape(u.first_name or '')}!\nخوش برگشتی 🌟\nشماره‌ات قبلاً ثبت شده." if saved else
         f"سلام {html.escape(u.first_name or '')}!\nبرای ثبت شماره، دکمه «📱 ثبت شماره» را بزن؛ فقط یک بار لازم است.")
    await update.effective_message.reply_text(msg,reply_markup=keyboard(saved))

async def contact_handler(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    m=update.effective_message;u=update.effective_user;c=m.contact if m else None
    if not u or not c:return
    if c.user_id!=u.id:
        await m.reply_text("لطفاً شمارهٔ خودت را با دکمه رسمی تلگرام بفرست.");return
    r=rec_for(u)
    if r.get("phone_verified") and r.get("phone")==c.phone_number:
        await m.reply_text("✅ شماره‌ات قبلاً ثبت شده.",reply_markup=keyboard(True));return
    r.update({"phone":c.phone_number,"phone_verified":True,"phone_updated_at":now()});save_db()
    await m.reply_text("✅ شماره ثبت شد؛ لازم نیست دوباره بفرستی.",reply_markup=keyboard(True))
    note=("📱 <b>شماره کاربر ثبت شد</b>\n"
          f"نام: {html.escape(u.full_name or '-')}\nآیدی: <code>{u.id}</code>\n"
          f"شماره: <code>{html.escape(c.phone_number)}</code>\nزمان: {now()}\n"
          "<i>این تأیید پیامکی نیست؛ کاربر contact خودش را فرستاده.</i>")
    for aid in ADMIN_IDS:
        try: await ctx.bot.send_message(aid,note,parse_mode="HTML")
        except Exception:log.exception("Admin notification failed")

async def send_items(update,items,title):
    m=update.effective_message
    await m.reply_text(title,parse_mode="HTML",disable_web_page_preview=True)
    media=[]
    for it in items:
        name=it["name"]; cap=f"🍈 <b>{html.escape(name)}</b>"
        if it.get("value"):cap+=f"\n💎 ولیو: <b>{html.escape(str(it['value']))}</b>"
        if it.get("beli"):cap+=f"\n💰 Beli: {html.escape(str(it['beli']))}"
        if it.get("robux"):cap+=f"\n💎 Robux: {html.escape(str(it['robux']))}"
        img=it.get("image") or IMAGES.get(name)
        if img:media.append(InputMediaPhoto(media=img,caption=cap,parse_mode="HTML"))
        else:await m.reply_text(cap,parse_mode="HTML")
    for i in range(0,len(media),10):
        batch=media[i:i+10]
        if len(batch)==1:await m.reply_photo(batch[0].media,caption=batch[0].caption,parse_mode="HTML")
        elif batch:await m.reply_media_group(batch)

async def stock_cmd(update,ctx):
    try:
        data=await asyncio.to_thread(fetch_stock)
        await update.effective_message.reply_text(
            f"📦 <b>استاک فعلی</b>\nمنبع: <a href=\"{STOCK_URL}\">BloxFruitsValues</a>\n"
            f"آخرین دریافت: {now()}\nریست معمولی هر ۴ ساعت و میراژ هر ۲ ساعت طبق UTC.",
            parse_mode="HTML",disable_web_page_preview=True)
        for title,items in data.items():
            await send_items(update,items,f"🍏 <b>{html.escape(title)}</b>")
    except Exception:
        log.exception("Stock request failed")
        await update.effective_message.reply_text("⚠️ سایت استاک الان پاسخ قابل‌خواندن نداد؛ خطای دقیق در لاگ هاست ثبت شد.")

async def values_cmd(update,ctx):
    try:
        data=await asyncio.to_thread(fetch_values)
        await update.effective_message.reply_text(
            f"💎 <b>ولیوهای میوه‌ها</b>\nمنبع: <a href=\"{VALUES_URL}\">BloxFruitsValues</a>\n"
            f"دریافت: {now()}\nاین عددها ارزش معاملاتی‌اند، نه قیمت خرید Beli/Robux.",
            parse_mode="HTML",disable_web_page_preview=True)
        items=[{"name":n,"value":v,"image":IMAGES.get(n,"")} for n,v in data.items()]
        await send_items(update,items,"🍈 <b>هر میوه با تصویر خودش</b>")
    except Exception:
        log.exception("Value request failed")
        await update.effective_message.reply_text("⚠️ سایت ولیوها الان پاسخ قابل‌خواندن نداد؛ خطای دقیق در لاگ هاست ثبت شد.")

async def text_handler(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    m=update.effective_message;u=update.effective_user
    if not m or not u or not m.text:return
    r=rec_for(u);save_db()
    if m.text=="📦 استاک فعلی":await stock_cmd(update,ctx)
    elif m.text=="💎 ولیوها":await values_cmd(update,ctx)
    elif m.text=="🔔 اعلان‌ها":
        r["alerts"]=not r.get("alerts",True);save_db()
        await m.reply_text("اعلان‌ها "+("روشن شد." if r["alerts"] else "خاموش شد."))
    elif m.text=="ℹ️ راهنما":await m.reply_text("📦 استاک فعلی: استاک معمولی و میراژ\n💎 ولیوها: ارزش معاملاتی میوه‌ها\nشماره فقط یک بار ثبت می‌شود.\nادمین: /status")

async def status(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ فقط ادمین.");return
    await update.effective_message.reply_text(f"🟢 فعال\nکاربران: {len(DB['users'])}\nآخرین زمان: {now()}")

async def chat_member(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    ch=update.effective_chat
    if ch and ch.type in ("group","supergroup"):
        DB["chats"][str(ch.id)]={"title":ch.title or "","type":ch.type};save_db()

async def periodic(app):
    await asyncio.sleep(20)
    while True:
        # Single instance is required; the host must not run two copies with the same token.
        try:
            vals=await asyncio.to_thread(fetch_values)
            if DB.get("values") and DB["values"]!=vals:
                for uid,r in DB["users"].items():
                    if r.get("alerts",True):
                        try:await app.bot.send_message(int(uid),"💎 ولیوها به‌روزرسانی شدند؛ برای دیدن فهرست /start را بزنید.")
                        except Exception:pass
            DB["values"]=vals;save_db()
        except asyncio.CancelledError:raise
        except Exception:log.exception("Periodic values check failed")
        try:
            stock=await asyncio.to_thread(fetch_stock)
            if DB.get("stock") and DB["stock"]!=stock:
                for uid,r in DB["users"].items():
                    if r.get("alerts",True):
                        try:await app.bot.send_message(int(uid),"📦 استاک تغییر کرده؛ «استاک فعلی» را بزنید.")
                        except Exception:pass
            DB["stock"]=stock;save_db()
        except asyncio.CancelledError:raise
        except Exception:log.exception("Periodic stock check failed")
        await asyncio.sleep(CHECK_INTERVAL)

async def post_init(app):
    load_db()
    app.bot_data["periodic_task"]=asyncio.create_task(periodic(app))

async def post_shutdown(app):
    t=app.bot_data.get("periodic_task")
    if t:
        t.cancel()
        try:await t
        except asyncio.CancelledError:pass

def main():
    if not TOKEN:raise RuntimeError("BOT_TOKEN is not configured in host environment")
    load_db()
    app=Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("help",start))
    app.add_handler(CommandHandler("status",status))
    app.add_handler(MessageHandler(filters.CONTACT,contact_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    app.add_handler(ChatMemberHandler(chat_member,ChatMemberHandler.MY_CHAT_MEMBER))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":main()
