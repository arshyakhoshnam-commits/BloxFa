#!/usr/bin/env python3
"""
BloxPulse — Telegram bot for Blox Fruits Stock & Values
Uses bloxfruitsvalues.com tRPC API + full catalog (~92 items)
Mini App: http://scriptx.ir/BloxTrading/
Python 3.11 / 3.12 · python-telegram-bot 21.x
"""

import os
import re
import html
import json
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ChatMemberHandler,
)
from telegram.error import Conflict

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
VALUES_URL = os.getenv("VALUE_URL", "https://bloxfruitsvalues.com/values").strip()
STOCK_URL = os.getenv("STOCK_URL", "https://bloxfruitsvalues.com/stock").strip()
API_BASE = "https://bloxfruitsvalues.com/api/ws"
# Mini App / Web site
MINI_APP_URL = os.getenv("MINI_APP_URL", "http://scriptx.ir/BloxTrading/").strip()
DB_PATH = Path(os.getenv("DB_PATH", "data.json"))
CHECK_INTERVAL = max(900, int(os.getenv("CHECK_INTERVAL_SECONDS", "3600")))
SEED_PATH = Path(__file__).with_name("values_seed.json")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bloxpulse")

S = requests.Session()
S.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "trpc-accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://bloxfruitsvalues.com",
        "Referer": "https://bloxfruitsvalues.com/stock",
    }
)

DB: dict[str, Any] = {"users": {}, "chats": {}, "values": {}, "stock": {}, "meta": {}}
IMAGES: dict[str, str] = {}

FRUITS = [
    "Rocket", "Spin", "Blade", "Chop", "Spring", "Bomb", "Smoke", "Spike",
    "Flame", "Falcon", "Eagle", "Ice", "Sand", "Dark", "Diamond", "Light",
    "Rubber", "Barrier", "Ghost", "Magma", "Quake", "Buddha", "Love",
    "Creation", "Spider", "Sound", "Phoenix", "Portal", "Rumble", "Lightning",
    "Pain", "Blizzard", "Gravity", "Mammoth", "T-Rex", "Dough", "Shadow",
    "Venom", "Control", "Spirit", "Dragon", "West Dragon", "East Dragon",
    "Leopard", "Tiger", "Kitsune", "Yeti", "Gas", "Magnet",
]
LOOKUP = {x.lower(): x for x in FRUITS}
LOOKUP.update({"trex": "T-Rex", "t-rex": "T-Rex", "west dragon": "West Dragon", "east dragon": "East Dragon"})

# Base values from seed (physical trade value)
BASE_VALUES: dict[str, float] = {}
try:
    if SEED_PATH.is_file():
        BASE_VALUES = {k: float(v) for k, v in json.loads(SEED_PATH.read_text(encoding="utf-8")).items()}
except Exception:
    log.warning("Could not load values_seed.json")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def load_db() -> None:
    global DB
    try:
        raw = json.loads(DB_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k in DB:
                if isinstance(raw.get(k), dict):
                    DB[k] = raw[k]
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("Could not read data.json")


def save_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(DB, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DB_PATH)


def norm(x: Any) -> str:
    return " ".join(str(x or "").replace("\xa0", " ").split())


def fruit_name(text: str) -> str | None:
    t = norm(text).lower()
    for k in sorted(LOOKUP, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", t):
            return LOOKUP[k]
    return None


def parse_num(s: Any) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"(?<![A-Za-z])(\d+(?:\.\d+)?)\s*([KMBT])?\b", str(s), re.I)
    if not m:
        return None
    n = float(m.group(1))
    mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get((m.group(2) or "").upper(), 1)
    return n * mult


def fmt_num(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit, scale in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= scale:
            v = n / scale
            return f"{v:g}{unit}" if v >= 10 or v == int(v) else f"{v:.1f}{unit}".replace(".0", "")
    return f"{n:g}"


def trpc_get(procedure: str, input_payload: dict | None = None) -> Any:
    if input_payload is None:
        input_payload = {"0": {"json": None}}
    url = f"{API_BASE}/{procedure}"
    params = {"batch": "1", "input": json.dumps(input_payload, separators=(",", ":"))}
    r = S.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Empty tRPC response for {procedure}")
    item = data[0]
    if "error" in item:
        msg = item["error"].get("json", {}).get("message", str(item["error"]))
        raise RuntimeError(f"tRPC error on {procedure}: {msg}")
    return item["result"]["data"]["json"]


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------
def fetch_stock() -> dict[str, list[dict]]:
    try:
        payload = trpc_get("stock.get")
    except Exception as e:
        log.exception("Stock API request failed")
        raise RuntimeError(f"Stock API failed: {e}") from e

    result: dict[str, list[dict]] = {}
    for key, title in (("normal", "استاک معمولی"), ("mirage", "استاک میراژ")):
        fruits = (payload.get(key) or {}).get("fruits") or []
        items = []
        for f in fruits:
            name = fruit_name(f.get("name") or "") or f.get("name") or "Unknown"
            img = f.get("image") or ""
            if img:
                IMAGES[name] = img
            bp, rp = f.get("beliPrice"), f.get("robuxPrice")
            items.append({
                "name": name,
                "image": img,
                "rarity": f.get("rarity") or "",
                "type": f.get("fruitType") or "",
                "beli": f"{bp:,}" if bp is not None else "",
                "robux": f"{rp:,}" if rp is not None else "",
                "value": fmt_num(f.get("regularValue")),
                "demand": f.get("regularDemand"),
            })
        if items:
            result[title] = items

    if not result:
        raise RuntimeError("No stock cards returned by API")

    meta = {}
    for key in ("normal", "mirage"):
        win = (payload.get(key) or {}).get("window") or {}
        if win.get("resetsAt"):
            meta[f"{key}_resets_at"] = win["resetsAt"]
    DB["meta"].update(meta)
    log.info("Stock OK — normal=%d mirage=%d", len(result.get("استاک معمولی", [])), len(result.get("استاک میراژ", [])))
    return result


def fetch_values() -> dict[str, str]:
    """
    Full catalog: start from seed (~85+ items), override with live values from stock API.
    Returns name -> formatted physical value string.
    """
    values: dict[str, float] = dict(BASE_VALUES)

    try:
        payload = trpc_get("stock.get")
        for section_key in ("normal", "mirage"):
            for f in (payload.get(section_key) or {}).get("fruits") or []:
                name = fruit_name(f.get("name") or "") or f.get("name")
                if not name:
                    continue
                val = f.get("regularValue")
                if val is not None and val > 0:
                    values[name] = float(val)
                if f.get("image"):
                    IMAGES[name] = f["image"]
        for hist_list in (payload.get("history") or {}).values():
            if not isinstance(hist_list, list):
                continue
            for entry in hist_list:
                for f in entry.get("fruits") or []:
                    name = fruit_name(f.get("name") or "") or f.get("name")
                    if name and f.get("value") and name not in values:
                        values[name] = float(f["value"])
                    if f.get("image"):
                        IMAGES[name] = f["image"]
    except Exception as e:
        log.warning("Live value overlay failed: %s", e)

    # Keep previously stored if still missing
    for k, v in (DB.get("values") or {}).items():
        n = parse_num(v)
        if k not in values and n:
            values[k] = n

    if len(values) < 5:
        raise RuntimeError(f"Only {len(values)} values available")

    formatted = {k: fmt_num(v) for k, v in values.items()}
    log.info("Values ready: %d items", len(formatted))
    return dict(sorted(formatted.items(), key=lambda x: parse_num(x[1]) or 0, reverse=True))


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def keyboard(saved: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("📦 استاک فعلی"), KeyboardButton("💎 ولیوها")],
        [KeyboardButton("🌐 سایت / مینی‌اپ"), KeyboardButton("🔔 اعلان‌ها")],
        [KeyboardButton("ℹ️ راهنما")],
    ]
    if not saved:
        rows.insert(1, [KeyboardButton("📱 ثبت شماره", request_contact=True)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def mini_app_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 باز کردن BloxTrading", web_app=WebAppInfo(url=MINI_APP_URL))],
        [InlineKeyboardButton("🔗 باز کردن در مرورگر", url=MINI_APP_URL)],
    ])


def rec_for(user) -> dict:
    rec = DB["users"].setdefault(
        str(user.id),
        {"id": user.id, "phone": "", "phone_verified": False, "created_at": now(), "alerts": True},
    )
    rec.update({"id": user.id, "username": user.username or "", "first_name": user.first_name or ""})
    rec.setdefault("phone", "")
    rec.setdefault("phone_verified", False)
    rec.setdefault("alerts", True)
    rec.setdefault("created_at", now())
    return rec


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    r = rec_for(u)
    save_db()
    saved = bool(r.get("phone") and r.get("phone_verified"))
    msg = (
        f"سلام {html.escape(u.first_name or '')}!\nخوش برگشتی 🌟\nشماره‌ات قبلاً ثبت شده."
        if saved
        else f"سلام {html.escape(u.first_name or '')}!\nبرای ثبت شماره دکمه «📱 ثبت شماره» را بزن."
    )
    await update.effective_message.reply_text(msg, reply_markup=keyboard(saved))
    await update.effective_message.reply_text(
        "🌐 سایت و مینی‌اپ BloxTrading را از اینجا باز کن:",
        reply_markup=mini_app_markup(),
    )


async def contact_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    m = update.effective_message
    u = update.effective_user
    c = m.contact if m else None
    if not u or not c:
        return
    if c.user_id != u.id:
        await m.reply_text("لطفاً شمارهٔ خودت را با دکمه رسمی تلگرام بفرست.")
        return
    r = rec_for(u)
    if r.get("phone_verified") and r.get("phone") == c.phone_number:
        await m.reply_text("✅ شماره‌ات قبلاً ثبت شده.", reply_markup=keyboard(True))
        return
    r.update({"phone": c.phone_number, "phone_verified": True, "phone_updated_at": now()})
    save_db()
    await m.reply_text("✅ شماره ثبت شد.", reply_markup=keyboard(True))
    note = (
        f"📱 <b>شماره ثبت شد</b>\nنام: {html.escape(u.full_name or '-')}\n"
        f"آیدی: <code>{u.id}</code>\nشماره: <code>{html.escape(c.phone_number)}</code>\nزمان: {now()}"
    )
    for aid in ADMIN_IDS:
        try:
            await ctx.bot.send_message(aid, note, parse_mode="HTML")
        except Exception:
            log.exception("Admin notify failed")


async def stock_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = await asyncio.to_thread(fetch_stock)
        meta = DB.get("meta") or {}
        extra = ""
        if meta.get("normal_resets_at"):
            try:
                ts = int(meta["normal_resets_at"]) / 1000
                extra = f"\n⏱ ریست بعدی (تقریبی): {datetime.fromtimestamp(ts, timezone.utc).strftime('%H:%M UTC')}"
            except Exception:
                pass
        await update.effective_message.reply_text(
            f"📦 <b>استاک فعلی</b>\nمنبع: <a href=\"{STOCK_URL}\">BloxFruitsValues</a>\n"
            f"آخرین دریافت: {now()}{extra}\n"
            "ریست معمولی هر ۴ ساعت · میراژ هر ۲ ساعت (UTC).",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        for title, items in data.items():
            lines = [f"🍏 <b>{html.escape(title)}</b>"]
            for it in items:
                line = f"• <b>{html.escape(it['name'])}</b>"
                if it.get("rarity"):
                    line += f" · {html.escape(it['rarity'])}"
                if it.get("value"):
                    line += f"\n  💎 {html.escape(str(it['value']))}"
                if it.get("beli"):
                    line += f" · 💰 {html.escape(str(it['beli']))}"
                if it.get("robux"):
                    line += f" · 💎R {html.escape(str(it['robux']))}"
                lines.append(line)
            await update.effective_message.reply_text(
                "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
            )
        DB["stock"] = data
        save_db()
    except Exception:
        log.exception("Stock request failed")
        await update.effective_message.reply_text(
            "⚠️ دریافت استاک ناموفق بود. چند دقیقه بعد دوباره امتحان کن."
        )


async def values_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = await asyncio.to_thread(fetch_values)
        # Send as text chunks (Telegram limit ~4096)
        header = (
            f"💎 <b>ولیوهای کامل میوه‌ها</b>\n"
            f"تعداد: {len(data)} آیتم\n"
            f"زمان: {now()}\n"
            f"منبع پایه + به‌روزرسانی زنده از استاک\n"
            f"<i>ارزش معاملاتی فیزیکی (نه Beli/Robux)</i>\n\n"
        )
        lines = [f"{i+1}. <b>{html.escape(n)}</b> — {html.escape(v)}" for i, (n, v) in enumerate(data.items())]
        chunks: list[str] = []
        current = header
        for line in lines:
            if len(current) + len(line) + 1 > 3800:
                chunks.append(current)
                current = line + "\n"
            else:
                current += line + "\n"
        if current.strip():
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            await update.effective_message.reply_text(
                chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=mini_app_markup() if i == len(chunks) - 1 else None,
            )
        DB["values"] = data
        save_db()
    except Exception:
        log.exception("Value request failed")
        await update.effective_message.reply_text(
            "⚠️ دریافت ولیوها ناموفق بود. چند دقیقه بعد دوباره امتحان کن."
        )


async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    m = update.effective_message
    u = update.effective_user
    if not m or not u or not m.text:
        return
    r = rec_for(u)
    save_db()
    t = m.text
    if t == "📦 استاک فعلی":
        await stock_cmd(update, ctx)
    elif t == "💎 ولیوها":
        await values_cmd(update, ctx)
    elif t == "🌐 سایت / مینی‌اپ":
        await m.reply_text(
            "سایت و مینی‌اپ BloxTrading:\n"
            f"🔗 {MINI_APP_URL}\n\n"
            "از دکمه زیر هم می‌تونی مستقیم باز کنی:",
            reply_markup=mini_app_markup(),
        )
    elif t == "🔔 اعلان‌ها":
        r["alerts"] = not r.get("alerts", True)
        save_db()
        await m.reply_text("اعلان‌ها " + ("روشن شد ✅" if r["alerts"] else "خاموش شد 🔕"))
    elif t == "ℹ️ راهنما":
        await m.reply_text(
            "📦 استاک فعلی — استاک معمولی و میراژ (متن + زمان)\n"
            "💎 ولیوها — لیست کامل ارزش معاملاتی (~۹۰ آیتم)\n"
            "🌐 سایت / مینی‌اپ — باز کردن BloxTrading\n"
            "شماره فقط یک بار ثبت می‌شود.\n"
            "ادمین: /status"
        )


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ فقط ادمین.")
        return
    await update.effective_message.reply_text(
        f"🟢 فعال\nکاربران: {len(DB['users'])}\nولیوها: {len(DB.get('values') or {})}\nزمان: {now()}"
    )


async def chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ch = update.effective_chat
    if ch and ch.type in ("group", "supergroup"):
        DB["chats"][str(ch.id)] = {"title": ch.title or "", "type": ch.type}
        save_db()


async def periodic(app: Application) -> None:
    await asyncio.sleep(20)
    while True:
        try:
            vals = await asyncio.to_thread(fetch_values)
            if DB.get("values") and DB["values"] != vals:
                for uid, r in list(DB["users"].items()):
                    if r.get("alerts", True):
                        try:
                            await app.bot.send_message(int(uid), "💎 ولیوها به‌روزرسانی شدند.")
                        except Exception:
                            pass
            DB["values"] = vals
            save_db()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Periodic values failed")

        try:
            stock = await asyncio.to_thread(fetch_stock)
            if DB.get("stock") and DB["stock"] != stock:
                for uid, r in list(DB["users"].items()):
                    if r.get("alerts", True):
                        try:
                            await app.bot.send_message(int(uid), "📦 استاک تغییر کرده.")
                        except Exception:
                            pass
            DB["stock"] = stock
            save_db()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Periodic stock failed")

        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(app: Application) -> None:
    load_db()
    app.bot_data["periodic_task"] = asyncio.create_task(periodic(app))


async def post_shutdown(app: Application) -> None:
    t = app.bot_data.get("periodic_task")
    if t:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


def main() -> None:
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured in environment")
    load_db()
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(ChatMemberHandler(chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    log.info("Starting BloxPulse (Mini App: %s)", MINI_APP_URL)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Conflict:
        log.error(
            "Conflict: another getUpdates is running. "
            "Stop the other instance and keep only one."
        )
        raise


if __name__ == "__main__":
    main()
