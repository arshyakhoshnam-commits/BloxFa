#!/usr/bin/env python3
"""
BloxPulse / BloxFa — Telegram bot for Blox Fruits Stock & Values
Fixed: uses official tRPC API of bloxfruitsvalues.com (stock.get)
Compatible with Python 3.11 / 3.12 and python-telegram-bot 21.x
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
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto
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
DB_PATH = Path(os.getenv("DB_PATH", "data.json"))
CHECK_INTERVAL = max(900, int(os.getenv("CHECK_INTERVAL_SECONDS", "3600")))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bloxpulse")

# Session with browser-like headers (required by the site)
S = requests.Session()
S.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
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

# Canonical fruit list (used for name normalisation)
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
LOOKUP["trex"] = "T-Rex"
LOOKUP["t-rex"] = "T-Rex"
LOOKUP["west dragon"] = "West Dragon"
LOOKUP["east dragon"] = "East Dragon"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
        log.exception("Could not read data.json; keeping in-memory data")


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
    mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(
        (m.group(2) or "").upper(), 1
    )
    return n * mult


def fmt_num(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit, scale in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= scale:
            return f"{n / scale:g}{unit}"
    return f"{n:g}"


def trpc_get(procedure: str, input_payload: dict | None = None) -> Any:
    """Call a tRPC procedure on bloxfruitsvalues.com."""
    if input_payload is None:
        input_payload = {"0": {"json": None}}
    url = f"{API_BASE}/{procedure}"
    params = {
        "batch": "1",
        "input": json.dumps(input_payload, separators=(",", ":")),
    }
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
# Data fetchers
# ---------------------------------------------------------------------------
def fetch_stock() -> dict[str, list[dict]]:
    """
    Fetch live Normal + Mirage stock from the official API.
    Returns: {"استاک معمولی": [...], "استاک میراژ": [...]}
    """
    try:
        payload = trpc_get("stock.get")
    except Exception as e:
        log.exception("Stock API request failed")
        raise RuntimeError(f"Stock API failed: {e}") from e

    result: dict[str, list[dict]] = {}
    mapping = (
        ("normal", "استاک معمولی"),
        ("mirage", "استاک میراژ"),
    )

    for key, title in mapping:
        section = payload.get(key) or {}
        fruits = section.get("fruits") or []
        items = []
        for f in fruits:
            name = fruit_name(f.get("name") or "") or f.get("name") or "Unknown"
            img = f.get("image") or ""
            if img:
                IMAGES[name] = img
            item = {
                "name": name,
                "image": img,
                "rarity": f.get("rarity") or "",
                "type": f.get("fruitType") or "",
                "beli": f"{f.get('beliPrice'):,}" if f.get("beliPrice") is not None else "",
                "robux": f"{f.get('robuxPrice'):,}" if f.get("robuxPrice") is not None else "",
                "value": fmt_num(f.get("regularValue")),
                "demand": f.get("regularDemand"),
            }
            items.append(item)
        if items:
            result[title] = items

    if not result:
        raise RuntimeError("No stock cards returned by API")

    # Store meta for countdowns
    meta = {}
    for key in ("normal", "mirage"):
        sec = payload.get(key) or {}
        win = sec.get("window") or {}
        if win.get("resetsAt"):
            meta[f"{key}_resets_at"] = win["resetsAt"]
    DB["meta"].update(meta)
    log.info(
        "Stock API OK — normal=%d mirage=%d",
        len(result.get("استاک معمولی", [])),
        len(result.get("استاک میراژ", [])),
    )
    return result


def fetch_values() -> dict[str, str]:
    """
    Build a values dict from the stock API (which embeds current values)
    plus any previously cached values. This avoids the broken HTML parser.
    """
    values: dict[str, str] = {}
    try:
        payload = trpc_get("stock.get")
        for section_key in ("normal", "mirage"):
            for f in (payload.get(section_key) or {}).get("fruits") or []:
                name = fruit_name(f.get("name") or "") or f.get("name")
                if not name:
                    continue
                val = f.get("regularValue")
                if val is not None and val > 0:
                    values[name] = fmt_num(val)
                img = f.get("image")
                if img:
                    IMAGES[name] = img
        # Also pull from history if present
        for hist_list in (payload.get("history") or {}).values():
            if not isinstance(hist_list, list):
                continue
            for entry in hist_list:
                for f in entry.get("fruits") or []:
                    name = fruit_name(f.get("name") or "") or f.get("name")
                    if name and name not in values and f.get("value"):
                        values[name] = fmt_num(f["value"])
                    if f.get("image"):
                        IMAGES[name] = f["image"]
    except Exception as e:
        log.warning("Values from stock API failed: %s", e)

    # Merge previously stored values so the list stays useful between stock rotations
    old = DB.get("values") or {}
    for k, v in old.items():
        if k not in values:
            values[k] = v

    if len(values) < 3:
        raise RuntimeError(
            f"Parsed only {len(values)} fruit values; API may be temporarily unavailable"
        )

    log.info("Parsed %d values", len(values))
    return dict(
        sorted(values.items(), key=lambda x: parse_num(x[1]) or 0, reverse=True)
    )


# ---------------------------------------------------------------------------
# Telegram UI
# ---------------------------------------------------------------------------
def keyboard(saved: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("📦 استاک فعلی"), KeyboardButton("💎 ولیوها")],
        [KeyboardButton("🔔 اعلان‌ها"), KeyboardButton("ℹ️ راهنما")],
    ]
    if not saved:
        rows.insert(1, [KeyboardButton("📱 ثبت شماره", request_contact=True)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def rec_for(user) -> dict:
    rec = DB["users"].setdefault(
        str(user.id),
        {
            "id": user.id,
            "phone": "",
            "phone_verified": False,
            "created_at": now(),
            "alerts": True,
        },
    )
    rec.update(
        {
            "id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
        }
    )
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
    if saved:
        msg = (
            f"سلام {html.escape(u.first_name or '')}!\n"
            "خوش برگشتی 🌟\nشماره‌ات قبلاً ثبت شده."
        )
    else:
        msg = (
            f"سلام {html.escape(u.first_name or '')}!\n"
            "برای ثبت شماره، دکمه «📱 ثبت شماره» را بزن؛ فقط یک بار لازم است."
        )
    await update.effective_message.reply_text(msg, reply_markup=keyboard(saved))


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
        await m.reply_text(
            "✅ شماره‌ات قبلاً ثبت شده.", reply_markup=keyboard(True)
        )
        return
    r.update(
        {
            "phone": c.phone_number,
            "phone_verified": True,
            "phone_updated_at": now(),
        }
    )
    save_db()
    await m.reply_text(
        "✅ شماره ثبت شد؛ لازم نیست دوباره بفرستی.",
        reply_markup=keyboard(True),
    )
    note = (
        "📱 <b>شماره کاربر ثبت شد</b>\n"
        f"نام: {html.escape(u.full_name or '-')}\n"
        f"آیدی: <code>{u.id}</code>\n"
        f"شماره: <code>{html.escape(c.phone_number)}</code>\n"
        f"زمان: {now()}\n"
        "<i>این تأیید پیامکی نیست؛ کاربر contact خودش را فرستاده.</i>"
    )
    for aid in ADMIN_IDS:
        try:
            await ctx.bot.send_message(aid, note, parse_mode="HTML")
        except Exception:
            log.exception("Admin notification failed")


async def send_items(update, items: list[dict], title: str) -> None:
    m = update.effective_message
    await m.reply_text(title, parse_mode="HTML", disable_web_page_preview=True)
    media = []
    for it in items:
        name = it["name"]
        cap = f"🍈 <b>{html.escape(name)}</b>"
        if it.get("rarity"):
            cap += f"  ·  {html.escape(str(it['rarity']))}"
        if it.get("value"):
            cap += f"\n💎 ولیو: <b>{html.escape(str(it['value']))}</b>"
        if it.get("beli"):
            cap += f"\n💰 Beli: {html.escape(str(it['beli']))}"
        if it.get("robux"):
            cap += f"\n💎 Robux: {html.escape(str(it['robux']))}"
        if it.get("demand") is not None:
            cap += f"\n📈 Demand: {it['demand']}/10"
        img = it.get("image") or IMAGES.get(name)
        if img:
            media.append(InputMediaPhoto(media=img, caption=cap, parse_mode="HTML"))
        else:
            await m.reply_text(cap, parse_mode="HTML")
    for i in range(0, len(media), 10):
        batch = media[i : i + 10]
        if len(batch) == 1:
            await m.reply_photo(
                batch[0].media, caption=batch[0].caption, parse_mode="HTML"
            )
        elif batch:
            await m.reply_media_group(batch)


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
            f"📦 <b>استاک فعلی</b>\n"
            f"منبع: <a href=\"{STOCK_URL}\">BloxFruitsValues</a>\n"
            f"آخرین دریافت: {now()}{extra}\n"
            "ریست معمولی هر ۴ ساعت و میراژ هر ۲ ساعت (UTC).",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        for title, items in data.items():
            await send_items(update, items, f"🍏 <b>{html.escape(title)}</b>")
        DB["stock"] = data
        save_db()
    except Exception:
        log.exception("Stock request failed")
        await update.effective_message.reply_text(
            "⚠️ سایت استاک الان پاسخ قابل‌خواندن نداد؛ خطای دقیق در لاگ هاست ثبت شد."
        )


async def values_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = await asyncio.to_thread(fetch_values)
        await update.effective_message.reply_text(
            f"💎 <b>ولیوهای میوه‌ها</b>\n"
            f"منبع: <a href=\"{VALUES_URL}\">BloxFruitsValues</a>\n"
            f"دریافت: {now()}\n"
            "این عددها ارزش معاملاتی‌اند، نه قیمت خرید Beli/Robux.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        items = [
            {"name": n, "value": v, "image": IMAGES.get(n, "")}
            for n, v in data.items()
        ]
        # Limit to top 30 to avoid flooding
        await send_items(update, items[:30], "🍈 <b>هر میوه با تصویر خودش</b>")
        DB["values"] = data
        save_db()
    except Exception:
        log.exception("Value request failed")
        await update.effective_message.reply_text(
            "⚠️ سایت ولیوها الان پاسخ قابل‌خواندن نداد؛ خطای دقیق در لاگ هاست ثبت شد."
        )


async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    m = update.effective_message
    u = update.effective_user
    if not m or not u or not m.text:
        return
    r = rec_for(u)
    save_db()
    if m.text == "📦 استاک فعلی":
        await stock_cmd(update, ctx)
    elif m.text == "💎 ولیوها":
        await values_cmd(update, ctx)
    elif m.text == "🔔 اعلان‌ها":
        r["alerts"] = not r.get("alerts", True)
        save_db()
        await m.reply_text(
            "اعلان‌ها " + ("روشن شد." if r["alerts"] else "خاموش شد.")
        )
    elif m.text == "ℹ️ راهنما":
        await m.reply_text(
            "📦 استاک فعلی: استاک معمولی و میراژ\n"
            "💎 ولیوها: ارزش معاملاتی میوه‌ها\n"
            "شماره فقط یک بار ثبت می‌شود.\n"
            "ادمین: /status"
        )


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ فقط ادمین.")
        return
    await update.effective_message.reply_text(
        f"🟢 فعال\nکاربران: {len(DB['users'])}\nآخرین زمان: {now()}"
    )


async def chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ch = update.effective_chat
    if ch and ch.type in ("group", "supergroup"):
        DB["chats"][str(ch.id)] = {"title": ch.title or "", "type": ch.type}
        save_db()


# ---------------------------------------------------------------------------
# Periodic background task
# ---------------------------------------------------------------------------
async def periodic(app: Application) -> None:
    await asyncio.sleep(20)
    while True:
        try:
            vals = await asyncio.to_thread(fetch_values)
            if DB.get("values") and DB["values"] != vals:
                for uid, r in DB["users"].items():
                    if r.get("alerts", True):
                        try:
                            await app.bot.send_message(
                                int(uid),
                                "💎 ولیوها به‌روزرسانی شدند؛ برای دیدن فهرست /start را بزنید.",
                            )
                        except Exception:
                            pass
            DB["values"] = vals
            save_db()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Periodic values check failed")

        try:
            stock = await asyncio.to_thread(fetch_stock)
            if DB.get("stock") and DB["stock"] != stock:
                for uid, r in DB["users"].items():
                    if r.get("alerts", True):
                        try:
                            await app.bot.send_message(
                                int(uid),
                                "📦 استاک تغییر کرده؛ «استاک فعلی» را بزنید.",
                            )
                        except Exception:
                            pass
            DB["stock"] = stock
            save_db()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Periodic stock check failed")

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
        raise RuntimeError(
            "BOT_TOKEN is not configured. Set it in the host environment variables."
        )
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
    app.add_handler(
        ChatMemberHandler(chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    log.info("Starting BloxPulse bot (single instance required)")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Conflict:
        log.error(
            "Conflict: another getUpdates is running with the same token. "
            "Stop the other instance (FadeHost or local Windows) and restart only one."
        )
        raise


if __name__ == "__main__":
    main()
