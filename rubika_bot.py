#!/usr/bin/env python3
"""
BloxPulse Rubika — same stock/values as Telegram bot.
Token from Rubika BotFather → env RUBIKA_TOKEN
Can share data.json path with Telegram bot via DB_PATH.
Host free on FadeHost (second app needs paid OR same process).
"""
from __future__ import annotations

import logging
import os
import time

from core import fetch_stock, fetch_values, format_stock_message, format_values_message, now

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bloxpulse.rubika")

TOKEN = os.getenv("RUBIKA_TOKEN", "").strip()


def main() -> None:
    if not TOKEN:
        raise RuntimeError("RUBIKA_TOKEN not set")

    try:
        from rubka import Robot, Message
    except ImportError:
        log.error("Install: pip install rubka")
        raise

    bot = Robot(TOKEN)

    @bot.on_message(commands=["start", "help"])
    async def start(bot: Robot, message: Message):
        text = (
            "سلام 👋 BloxPulse روبیکا\n\n"
            "دستورها:\n"
            "• استاک / stock — استاک فعلی\n"
            "• ولیو / values — لیست ولیو\n"
            "• راهنما / help\n\n"
            f"زمان سرور: {now()}"
        )
        await message.reply(text)

    @bot.on_message(commands=["stock", "استاک"])
    async def stock_cmd(bot: Robot, message: Message):
        try:
            data = fetch_stock()
            for msg in format_stock_message(data):
                await message.reply(msg)
                time.sleep(0.3)
        except Exception:
            log.exception("stock failed")
            await message.reply("⚠️ دریافت استاک ناموفق بود. کمی بعد دوباره تلاش کن.")

    @bot.on_message(commands=["values", "value", "ولیو", "ولیوها"])
    async def values_cmd(bot: Robot, message: Message):
        try:
            data = fetch_values()
            for msg in format_values_message(data):
                await message.reply(msg)
                time.sleep(0.35)
        except Exception:
            log.exception("values failed")
            await message.reply("⚠️ دریافت ولیو ناموفق بود.")

    @bot.on_message()
    async def text_fallback(bot: Robot, message: Message):
        t = (message.text or "").strip()
        if t in ("استاک", "📦 استاک", "stock"):
            await stock_cmd(bot, message)
        elif t in ("ولیو", "ولیوها", "💎 ولیو", "values"):
            await values_cmd(bot, message)
        elif t in ("راهنما", "help"):
            await start(bot, message)

    log.info("Rubika BloxPulse starting")
    bot.run()


if __name__ == "__main__":
    main()
