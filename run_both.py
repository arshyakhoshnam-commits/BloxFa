#!/usr/bin/env python3
"""
Run Telegram + Rubika BloxPulse on a single host process.
FadeHost free tier: set start command to: python run_both.py

Env:
  BOT_TOKEN       — Telegram
  RUBIKA_TOKEN    — Rubika (optional; if missing, only Telegram runs)
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bloxpulse.runner")


def run_telegram() -> None:
    """Block with Telegram polling (main thread)."""
    # Import here so Rubika can start even if telegram import fails late
    import bot as telegram_bot

    log.info("Telegram bot starting…")
    telegram_bot.main()


def run_rubika() -> None:
    token = os.getenv("RUBIKA_TOKEN", "").strip()
    if not token:
        log.warning("RUBIKA_TOKEN empty — Rubika bot skipped")
        return

    try:
        from rubka import Robot, Message
    except ImportError:
        log.error("rubka not installed — pip install rubka")
        return

    from core import (
        fetch_stock,
        fetch_values,
        format_stock_message,
        format_values_message,
        now,
    )

    bot = Robot(token)

    @bot.on_message(commands=["start", "help"])
    async def start(bot: Robot, message: Message):
        await message.reply(
            "سلام 👋 BloxPulse روبیکا\n\n"
            "• استاک / stock\n"
            "• ولیو / values\n"
            "• راهنما / help\n\n"
            f"{now()}"
        )

    @bot.on_message(commands=["stock", "استاک"])
    async def stock_cmd(bot: Robot, message: Message):
        try:
            data = fetch_stock()
            for msg in format_stock_message(data):
                await message.reply(msg)
                time.sleep(0.25)
        except Exception:
            log.exception("rubika stock")
            await message.reply("⚠️ استاک الان در دسترس نیست.")

    @bot.on_message(commands=["values", "value", "ولیو", "ولیوها"])
    async def values_cmd(bot: Robot, message: Message):
        try:
            data = fetch_values()
            for msg in format_values_message(data):
                await message.reply(msg)
                time.sleep(0.3)
        except Exception:
            log.exception("rubika values")
            await message.reply("⚠️ ولیو الان در دسترس نیست.")

    @bot.on_message()
    async def fallback(bot: Robot, message: Message):
        t = (message.text or "").strip()
        if t in ("استاک", "📦 استاک", "stock"):
            await stock_cmd(bot, message)
        elif t in ("ولیو", "ولیوها", "💎 ولیو", "values"):
            await values_cmd(bot, message)
        elif t in ("راهنما", "help", "/start"):
            await start(bot, message)

    log.info("Rubika bot starting…")
    bot.run()


def main() -> None:
    tg = os.getenv("BOT_TOKEN", "").strip()
    rb = os.getenv("RUBIKA_TOKEN", "").strip()

    if not tg and not rb:
        log.error("Set BOT_TOKEN and/or RUBIKA_TOKEN")
        sys.exit(1)

    # Rubika in background thread (its own loop)
    if rb:
        t = threading.Thread(target=run_rubika, name="rubika", daemon=True)
        t.start()
        log.info("Rubika thread launched")
        time.sleep(1.5)

    if tg:
        # Telegram blocks main thread (python-telegram-bot design)
        run_telegram()
    else:
        # Only Rubika — keep process alive
        log.info("Only Rubika — waiting")
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()

