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
    app.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))
    app.job_queue.run_repeating(scheduled_check, interval=CHECK_INTERVAL, first=10)
    log.info("BloxPulse Persian is starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
