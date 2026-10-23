# Self-bootstrap: install missing Python dependencies on first start when the host allows pip/network access.
import importlib.util
import subprocess
import sys

def _ensure_dependencies():
    required = {
        "requests": "requests>=2.32.3,<3",
        "bs4": "beautifulsoup4>=4.12.3,<5",
        "telegram": "python-telegram-bot==21.10",
    }
    missing = [package for module, package in required.items() if importlib.util.find_spec(module) is None]
    if not missing:
        return
    print("[BloxPulse] Installing missing dependencies automatically:", ", ".join(missing), flush=True)
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", *missing])
    except Exception as exc:
        print("[BloxPulse] Automatic dependency installation failed.", flush=True)
        print("[BloxPulse] Host must allow Python pip installs and outbound package downloads: " + str(exc), flush=True)
        raise

_ensure_dependencies()

import os
import re
import json
import html
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatMemberHandler,
    ContextTypes, filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(v.strip()) for v in os.getenv("ADMIN_IDS", "").split(",") if v.strip().isdigit()}
VALUE_URLS = [v.strip() for v in os.getenv(
    "VALUE_URLS", "https://bloxfruit.io/values/,https://bloxfruitsvalues.gg/values"
).split(",") if v.strip()]
STOCK_URLS = [v.strip() for v in os.getenv(
    "STOCK_URLS", "https://blox-fruitswiki.com/wiki/stock/,https://bloxfruitswiki.org/wiki/stock/"
).split(",") if v.strip()]
CHECK_INTERVAL = max(300, int(os.getenv("CHECK_INTERVAL_SECONDS", "3600")))
DB_PATH = Path(os.getenv("DB_PATH", "data.json"))

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bloxpulse")

DB = {"users": {}, "chats": {}, "values": {}, "stock": {}}
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

FRUITS = [
    "Rocket", "Spin", "Blade", "Chop", "Spring", "Bomb", "Smoke", "Spike",
    "Flame", "Falcon", "Eagle", "Ice", "Sand", "Dark", "Diamond", "Light",
    "Rubber", "Barrier", "Ghost", "Magma", "Quake", "Buddha", "Love",
    "Creation", "Spider", "Sound", "Phoenix", "Portal", "Rumble", "Lightning",
    "Pain", "Blizzard", "Gravity", "Mammoth", "T-Rex", "Dough", "Shadow",
    "Venom", "Control", "Spirit", "Dragon", "West Dragon", "East Dragon",
    "Leopard", "Tiger", "Kitsune", "Yeti", "Gas", "Magnet",
]
FRUIT_LOOKUP = {x.lower(): x for x in FRUITS}
FRUIT_LOOKUP["trex"] = "T-Rex"


def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def load_db():
    """Load existing data without discarding older user records."""
    global DB
    try:
        if DB_PATH.exists():
            loaded = json.loads(DB_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for key in ("users", "chats", "values", "stock"):
                    if isinstance(loaded.get(key), dict):
                        DB[key] = loaded[key]
    except Exception:
        log.exception("Failed to load data.json; in-memory fallback is active")


def save_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = DB_PATH.with_suffix(DB_PATH.suffix + ".tmp")
    temp.write_text(json.dumps(DB, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(DB_PATH)


def norm(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())


def get_soup(url):
    response = session.get(url, timeout=25)
    response.raise_for_status()
    if not response.text.strip():
        raise ValueError("منبع صفحه خالی برگرداند.")
    return BeautifulSoup(response.text, "html.parser")


def home_keyboard(phone_saved=False):
    rows = [
        [KeyboardButton("📦 استاک فعلی"), KeyboardButton("💎 ولیوها")],
        [KeyboardButton("🔔 اعلان‌ها"), KeyboardButton("ℹ️ راهنما")],
    ]
    if not phone_saved:
        rows.insert(1, [KeyboardButton("📱 ثبت شماره", request_contact=True)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def user_record(user):
    users = DB.setdefault("users", {})
    key = str(user.id)
    rec = users.setdefault(key, {
        "id": user.id,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "phone": "",
        "phone_verified": False,
        "created_at": now_text(),
        "alerts": True,
    })
    # Keep phone/creation date/alert preference from existing record.
    rec["id"] = user.id
    rec["username"] = user.username or ""
    rec["first_name"] = user.first_name or ""
    rec.setdefault("phone", "")
    rec.setdefault("phone_verified", False)
    rec.setdefault("created_at", now_text())
    rec.setdefault("alerts", True)
    return rec


def fruit_in(text):
    low = norm(text).lower()
    # Longest first to correctly match West/East Dragon.
    for key in sorted(FRUIT_LOOKUP, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(key) + r"(?![a-z])", low):
            return FRUIT_LOOKUP[key]
    return None


def parse_stock_section(soup, wanted):
    headings = soup.find_all(["h1", "h2", "h3", "h4"])
    heading = None
    for item in headings:
        title = norm(item.get_text(" ", strip=True)).lower()
        if any(re.fullmatch(p, title) for p in wanted):
            heading = item
            break
    if heading is None:
        return []

    found = []
    seen = set()
    # Walk following nodes until a sibling stock/history heading appears.
    for node in heading.next_elements:
        if getattr(node, "name", None) in ("h1", "h2", "h3", "h4"):
            title = norm(node.get_text(" ", strip=True)).lower()
            if any(token in title for token in (
                "current stock", "current mirage stock", "last stock",
                "last mirage", "history", "reset times",
            )):
                break
        name = None
        if getattr(node, "name", None) == "img":
            name = fruit_in(node.get("alt", ""))
        elif getattr(node, "name", None) in ("a", "li", "article", "strong", "b"):
            name = fruit_in(node.get_text(" ", strip=True))
        if name and name not in seen:
            found.append(name)
            seen.add(name)

    if not found:
        parent = heading.find_parent("section") or heading.parent
        for node in parent.find_all(["img", "a", "li", "article", "strong", "b"]):
            text = node.get("alt", "") if node.name == "img" else node.get_text(" ", strip=True)
            name = fruit_in(text)
            if name and name not in seen:
                found.append(name)
                seen.add(name)
    return found


def fetch_stock():
    errors = []
    for url in STOCK_URLS:
        try:
            soup = get_soup(url)
            normal = parse_stock_section(soup, [
                r"current stock", r"normal stock", r"current normal stock",
            ])
            mirage = parse_stock_section(soup, [
                r"current mirage stock", r"mirage stock", r"advanced stock",
            ])
            result = {}
            if normal:
                result["استاک معمولی"] = normal
            if mirage:
                result["استاک میراژ"] = mirage
            if result:
                log.info("Stock fetched from %s: %s", url, {k: len(v) for k, v in result.items()})
                return result
            errors.append(f"{url}: ساختار استاک پیدا نشد")
        except Exception as exc:
            log.warning("Stock source failed (%s): %s", url, exc)
            errors.append(f"{url}: {exc}")
    raise RuntimeError("منابع استاک ناموفق بودند: " + " | ".join(errors))


def parse_number(text):
    """Convert 5K/2.5M/1.2B or full comma-separated values to a number."""
    match = re.search(r"(?<![A-Za-z])(\d[\d,.]*)(?:\s*([KMBT]))?(?![A-Za-z])", str(text), re.I)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (match.group(2) or "").upper()
    return number * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[suffix]


def display_number(number):
    if number >= 1e12:
        return f"{number / 1e12:g}T"
    if number >= 1e9:
        return f"{number / 1e9:g}B"
    if number >= 1e6:
        return f"{number / 1e6:g}M"
    if number >= 1e3:
        return f"{number / 1e3:g}K"
    return f"{number:g}"


def fetch_values():
    errors = []
    for url in VALUE_URLS:
        try:
            soup = get_soup(url)
            values = {}

            # A fruit card/row should contain a recognizable fruit and at least one value.
            # Prefer the page's "trade value" label when present, avoiding Beli/Robux prices.
            for node in soup.find_all(["article", "tr", "li", "section"]):
                text = norm(node.get_text(" ", strip=True))
                if not text or len(text) > 1200:
                    continue
                fruit = fruit_in(text[:250])
                if not fruit:
                    # Try headings and image alt labels inside the card.
                    for tag in node.find_all(["h1", "h2", "h3", "h4", "img", "a"], limit=8):
                        candidate = tag.get("alt", "") if tag.name == "img" else tag.get_text(" ", strip=True)
                        fruit = fruit_in(candidate)
                        if fruit:
                            break
                if not fruit:
                    continue

                # Extract a value close to trade/value label. If the page's accessible text
                # is structured differently, fall back to the first large number.
                value_text = ""
                patterns = [
                    r"(?:regular\s+trade\s+value|trade\s+value|physical\s+value|value)\s*[:\-]?\s*([\d,.]+\s*[KMBT]?)",
                    r"([\d,.]+\s*[KMBT])",
                    r"\b(\d{1,3}(?:,\d{3}){1,4})\b",
                ]
                # Try smaller descendant blocks first so unrelated card values aren't mixed.
                descendants = [node] + node.find_all(["p", "div", "span", "strong", "b", "h3", "h4"], limit=80)
                for part in descendants:
                    part_text = norm(part.get_text(" ", strip=True))
                    if not part_text:
                        continue
                    label_match = re.search(patterns[0], part_text, re.I)
                    if label_match:
                        value_text = label_match.group(1)
                        break
                if not value_text:
                    for pattern in patterns[1:]:
                        match = re.search(pattern, text, re.I)
                        if match:
                            value_text = match.group(1)
                            break
                number = parse_number(value_text) if value_text else None
                if number is None or number <= 0:
                    continue
                # Keep the first valid card entry, unless a more precise later row appears.
                values.setdefault(fruit, display_number(number))

            if len(values) >= 5:
                log.info("Extracted %d value rows from %s", len(values), url)
                return dict(sorted(values.items(), key=lambda item: parse_number(item[1]) or 0, reverse=True))
            errors.append(f"{url}: فقط {len(values)} مورد قابل تشخیص بود")
        except Exception as exc:
            log.warning("Value source failed (%s): %s", url, exc)
            errors.append(f"{url}: {exc}")
    raise RuntimeError("منابع ولیو ناموفق بودند: " + " | ".join(errors))


def values_text(values, limit=70):
    return "\n".join(
        f"• {html.escape(str(name))} — <b>{html.escape(str(value))}</b>"
        for name, value in list(values.items())[:limit]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    rec = user_record(user)
    save_db()
    if rec.get("phone") and rec.get("phone_verified"):
        text = f"سلام {html.escape(user.first_name or '')}!\nخوش برگشتی 🌟\nشماره‌ات قبلاً ثبت شده و نیازی به تأیید دوباره نیست."
        keyboard = home_keyboard(phone_saved=True)
    else:
        text = f"سلام {html.escape(user.first_name or '')}!\nبه بات فارسی استاک و ولیو خوش اومدی.\nبرای ثبت شماره، دکمه «📱 ثبت شماره» را بزن؛ فقط یک بار لازم است."
        keyboard = home_keyboard(phone_saved=False)
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "راهنما:\n📦 استاک فعلی: نمایش استاک معمولی و میراژ\n"
        "💎 ولیوها: نمایش مقادیر میوه‌ها\n🔔 اعلان‌ها: روشن/خاموش کردن اعلان‌ها\n"
        "📱 ثبت شماره: فقط شمارهٔ خودت را با دکمهٔ رسمی تلگرام ارسال کن.\n"
        "دستورهای ادمین: /members و /status"
    )


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    sender = update.effective_user
    contact = message.contact if message else None
    if not sender or not contact:
        return
    if contact.user_id != sender.id:
        await message.reply_text("لطفاً شمارهٔ خودت را با دکمهٔ «ثبت شماره» ارسال کن.")
        return

    rec = user_record(sender)
    # Idempotent: if the same contact is already saved, don't repeat admin notification.
    already_saved = bool(rec.get("phone") and rec.get("phone_verified"))
    same_phone = rec.get("phone") == contact.phone_number
    if already_saved and same_phone:
        save_db()
        await message.reply_text("✅ شماره‌ات قبلاً ثبت شده؛ نیازی به تأیید دوباره نیست.", reply_markup=home_keyboard(phone_saved=True))
        return

    rec.update({
        "phone": contact.phone_number,
        "phone_verified": True,
        "phone_updated_at": now_text(),
    })
    save_db()
    await message.reply_text("✅ شماره ثبت شد؛ از این به بعد لازم نیست دوباره بفرستی.", reply_markup=home_keyboard(phone_saved=True))

    admin_text = (
        "📱 <b>شماره کاربر ثبت شد</b>\n"
        f"نام: {html.escape(sender.full_name or '-')}\n"
        f"نام کاربری: @{html.escape(sender.username) if sender.username else 'ندارد'}\n"
        f"آیدی عددی: <code>{sender.id}</code>\n"
        f"شماره: <code>{html.escape(contact.phone_number)}</code>\n"
        f"زمان: {now_text()}\n"
        "<i>این یعنی کاربر contact خودش را از تلگرام فرستاده؛ تأیید پیامکی نیست.</i>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, admin_text, parse_mode="HTML")
        except Exception:
            log.exception("Could not notify admin %s", admin_id)


async def send_stock(update, context):
    try:
        stock = await asyncio.to_thread(fetch_stock)
        sections = [
            f"<b>{html.escape(title)}</b>\n" + "\n".join("• " + html.escape(name) for name in fruits)
            for title, fruits in stock.items()
        ]
        await update.effective_message.reply_text(
            "📦 <b>استاک فعلی</b>\n\n" + "\n\n".join(sections),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Stock request failed")
        await update.effective_message.reply_text(
            "⚠️ منبع استاک فعلاً پاسخ درست نداد. خطای دقیق در لاگ هاست ثبت شده؛ کمی بعد دوباره امتحان کن."
        )


async def send_values(update, context):
    try:
        values = await asyncio.to_thread(fetch_values)
        await update.effective_message.reply_text(
            "💎 <b>ولیوها (از بیشترین به کمترین)</b>\n\n" + values_text(values),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Values request failed")
        await update.effective_message.reply_text(
            "⚠️ منبع ولیو فعلاً پاسخ قابل‌خواندن نداد. خطای دقیق در لاگ هاست ثبت شده؛ کمی بعد دوباره امتحان کن."
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    if not message or not user or not message.text:
        return
    rec = user_record(user)
    save_db()
    if message.text == "📦 استاک فعلی":
        await send_stock(update, context)
    elif message.text == "💎 ولیوها":
        await send_values(update, context)
    elif message.text == "🔔 اعلان‌ها":
        rec["alerts"] = not rec.get("alerts", True)
        save_db()
        await message.reply_text("اعلان‌ها " + ("روشن شد." if rec["alerts"] else "خاموش شد."))
    elif message.text == "ℹ️ راهنما":
        await help_cmd(update, context)


def make_members_html():
    users = sorted(DB.get("users", {}).values(), key=lambda item: int(item.get("id", 0)))
    rows = []
    for index, user in enumerate(users, 1):
        username = "@" + user.get("username", "") if user.get("username") else "ندارد"
        cells = [
            index, user.get("first_name", ""), username, user.get("id", ""),
            user.get("phone") or "ندارد",
            "ثبت‌شده از تلگرام" if user.get("phone_verified") else "ثبت نشده",
            user.get("created_at", ""),
        ]
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells) + "</tr>")
    body = "\n".join(rows) or '<tr><td colspan="7">هنوز کاربری ثبت نشده است.</td></tr>'
    return f"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>اعضای BloxPulse</title><style>
body{{font-family:Tahoma,Arial;background:#0e1420;color:#edf2f7;margin:0;padding:24px}}
.wrap{{max-width:1200px;margin:auto}}.count{{background:#192538;padding:16px;border-radius:12px;margin:16px 0}}
.tablewrap{{overflow:auto;background:#141e2d;border-radius:12px}}table{{border-collapse:collapse;width:100%;min-width:850px}}
th,td{{padding:12px;border-bottom:1px solid #2b394c;text-align:right;white-space:nowrap}}th{{background:#20324a}}
input{{padding:10px;border-radius:8px;border:1px solid #394b62;background:#111b29;color:white;width:min(360px,90%)}}
small{{color:#aab7c8}}</style></head><body><div class="wrap"><h1>اعضای BloxPulse فارسی</h1>
<div class="count">تعداد کل کاربران: <b>{len(users)}</b></div>
<input id="q" placeholder="جستجو در نام، آیدی، شماره..." oninput="filterRows()">
<p><small>این فایل حاوی اطلاعات خصوصی است؛ عمومی منتشرش نکن.</small></p>
<div class="tablewrap"><table><thead><tr><th>ردیف</th><th>نام</th><th>نام کاربری</th><th>آیدی</th><th>شماره</th><th>وضعیت شماره</th><th>تاریخ ثبت</th></tr></thead>
<tbody id="rows">{body}</tbody></table></div></div>
<script>function filterRows(){{const q=document.getElementById('q').value.toLowerCase();document.querySelectorAll('#rows tr').forEach(r=>r.style.display=r.innerText.toLowerCase().includes(q)?'':'none')}}</script>
</body></html>"""


async def members_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ این دستور فقط برای ادمین است.")
        return
    path = Path("members.html")
    path.write_text(make_members_html(), encoding="utf-8")
    with path.open("rb") as file_obj:
        await update.effective_message.reply_document(
            document=file_obj,
            filename="members.html",
            caption=f"فهرست اعضا — تعداد کل: {len(DB.get('users', {}))}\nاین فایل را عمومی نکن.",
        )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ این دستور فقط برای ادمین است.")
        return
    await update.effective_message.reply_text(
        f"🟢 بات فعال است.\nکاربران: {len(DB.get('users', {}))}\n"
        f"گروه‌ها: {len(DB.get('chats', {}))}\nفاصله بررسی: {CHECK_INTERVAL} ثانیه"
    )


async def announce_changes(app, key, new_data, formatter):
    old_data = DB.get(key, {})
    # First successful poll stores baseline; don't spam users at first startup.
    if old_data and old_data != new_data:
        text = formatter(new_data)
        for user_id, record in list(DB.get("users", {}).items()):
            if record.get("alerts", True):
                try:
                    await app.bot.send_message(int(user_id), text, parse_mode="HTML")
                except Exception as exc:
                    log.debug("Could not notify user %s: %s", user_id, exc)
        for chat_id in list(DB.get("chats", {})):
            try:
                await app.bot.send_message(int(chat_id), text, parse_mode="HTML")
            except Exception as exc:
                log.debug("Could not notify chat %s: %s", chat_id, exc)
    DB[key] = new_data
    save_db()


async def periodic_checks(app):
    await asyncio.sleep(10)
    while True:
        try:
            data = await asyncio.to_thread(fetch_stock)
            await announce_changes(
                app, "stock", data,
                lambda d: "📦 <b>تغییر استاک</b>\n\n" + "\n\n".join(
                    f"<b>{html.escape(k)}</b>\n" + "\n".join("• " + html.escape(v) for v in vals)
                    for k, vals in d.items()
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Periodic stock check failed")
        try:
            data = await asyncio.to_thread(fetch_values)
            await announce_changes(app, "values", data, lambda d: "💎 <b>تغییر ولیوها</b>\n\n" + values_text(d))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Periodic value check failed")
        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(app):
    load_db()
    app.bot_data["periodic_task"] = asyncio.create_task(periodic_checks(app))


async def post_shutdown(app):
    task = app.bot_data.get("periodic_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        DB.setdefault("chats", {})[str(chat.id)] = {"title": chat.title or "", "type": chat.type}
        save_db()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN در Environment Variables هاست تنظیم نشده است.")
    load_db()
    app = (
        Application.builder().token(BOT_TOKEN)
        .post_init(post_init).post_shutdown(post_shutdown).build()
    )
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
