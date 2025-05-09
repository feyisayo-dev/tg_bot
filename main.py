import pandas as pd
import time
import logging
import os
import asyncio
from urllib.parse import quote
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    CallbackContext,
)
from utils import (
    get_video_info,
    get_file_size,
    get_duration,
    download,
    get_video_formats,
)
from datetime import datetime, timedelta
import json
import sqlite3
import uuid  # To generate unique IDs
from telegram.helpers import escape_markdown

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")


# Function to initialize the database
def init_db():
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,
            url TEXT
        )
    """
    )
    conn.commit()
    conn.close()


# Function to store URL and generate an ID
def store_video_url(url):
    video_id = str(uuid.uuid4())[:8]  # Generate short unique ID (8 chars)
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO videos (id, url) VALUES (?, ?)", (video_id, url))
    conn.commit()
    conn.close()
    return video_id  # Return the unique ID


# Function to get the URL from the ID
def get_video_url(video_id):
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("SELECT url FROM videos WHERE id = ?", (video_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None  # Return URL if found


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DOWNLOAD_DIR = "downloads"
TELEGRAM_MAX_SIZE = 2000 * 1024 * 1024

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

user_ids = {}
user_count = 0

os.makedirs("logs", exist_ok=True)
log_file_path = "logs/upload_log.xlsx"
queue = asyncio.Queue()
queue_positions = {}  # Dictionary to track queue positions
if not os.path.exists(log_file_path):
    df_log = pd.DataFrame(
        columns=[
            "User",
            "File Size",
            "Download Time",
            "Upload Time",
            "Total Time",
            "Speed",
        ]
    )
    df_log.to_excel(log_file_path, index=False)
else:
    df_log = pd.read_excel(log_file_path)

def suggest_clean_url(url):
    if any(site in url for site in ["faphouse.com"]):
        base_url = url.split("?")[0].split("&")[0]
        return f"👀 Heads up! For smoother downloads, use a clean URL like:\n\n{base_url}"
    return ""


def format_time(seconds):
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{int(hrs)} hr{'s' if hrs > 1 else ''} {int(mins)} min{'s' if mins > 1 else ''} {int(secs)} sec{'s' if secs > 1 else ''}"
    elif mins > 0:
        return f"{int(mins)} min{'s' if mins > 1 else ''} {int(secs)} sec{'s' if secs > 1 else ''}"
    else:
        return f"{int(secs)} sec{'s' if secs > 1 else ''}"


async def start(update: Update, context: CallbackContext) -> None:
    chat_type = update.effective_chat.type

    if chat_type in ["group", "supergroup"]:
        await update.message.reply_text(
            "👋 Hello Group! Use /download to start downloading videos."
        )
        return

    global user_count
    user = update.message.from_user
    username = user.username or "unknown_user"

    if username not in user_ids:
        user_ids[username] = user_count
        user_count += 1
        with open("logs/user_log_download_bot.txt", "a") as log_file_user:
            log_file_user.write(
                f"{datetime.now()} - User: {username}  User Count: {user_count},\n"
            )

    logger.info(f"User {username} (ID: {user_ids[username]}) started the bot")

    keyboard = [
        [
            InlineKeyboardButton("Download", callback_data="download"),
            InlineKeyboardButton(
                "Open Private Browser",
                web_app=WebAppInfo(
                    url="https://offeyicial.pythonanywhere.com/?mode=incognito"
                ),
            ),
            InlineKeyboardButton(
                "Open Normal Browser",
                web_app=WebAppInfo(
                    url="https://offeyicial.pythonanywhere.com/?mode=normal"
                ),
            ),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome! Choose an option:", reply_markup=reply_markup
    )


async def about(update: Update, context: CallbackContext) -> None:
    about_text = (
        "*MediaMate* by OFFEYICIAL\n\n"
        "*MediaMate* is a versatile Telegram bot for downloading videos and audio from various online sources. "
        "Easily download and save your favorite media with real-time progress updates. The bot also supports opening websites "
        "in private or normal browsers directly from Telegram.\n\n"
        "*Features:*\n"
        "- Download media with progress updates.\n"
        "- Receive files or videos directly in chat.\n"
        "- Open websites in incognito or standard mode.\n\n"
        "Enjoy seamless media management with *MediaMate*! For more information, visit our website."
    )
    await update.message.reply_text(about_text, parse_mode="Markdown")


async def donate(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text(
        "If you like this bot and want to support its development, you can donate to us at: https://patreon.com/offeyicial/donate"
    )


async def send_video_info_message(
    context, chat_id, file_size_mb, duration_hms, estimated_time_hms, reply_to_msg_id=None
):
    message_text = (
        f"📂 Video Information:\n"
        f"File Size: {file_size_mb:.2f} MB\n"
        f"⏱ Duration: {duration_hms}\n"
        f"⌛ Estimated Time to Receive: {estimated_time_hms}"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=message_text,
        reply_to_message_id=reply_to_msg_id  # 👈 this is what makes it a reply
    )


async def send_delay_message(context, chat_id):
    message_text = (
        "⚠️ Please wait, we are sorry for the delay. "
        "It is from our side, not yours, our dear user. 🙏"
    )
    await context.bot.send_message(chat_id=chat_id, text=message_text)


processing_event = asyncio.Event()


async def send_processing_message(context, chat_id):
    processing_message = None
    while not processing_event.is_set():
        new_message = await context.bot.send_message(
            chat_id=chat_id, text="Processing..."
        )
        if processing_message:
            await processing_message.delete()
        processing_message = new_message
        await asyncio.sleep(5)
    if processing_message:
        await processing_message.delete()


async def button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "download":
        await query.message.reply_text(
            "Send me a video or audio link, and I will try to download it."
        )


async def handle_download_logic(chat_id, url, context, selected_format=None, reply_to_msg_id=None):
    try:
        sanitized_info = get_video_info(url)
        file_size = get_file_size(sanitized_info) or 0
        file_size_mb = file_size / (1024 * 1024)
        duration_hms = format_time(get_duration(sanitized_info))

        # # Send video info
        # await send_video_info_message(
        #     context, chat_id, file_size_mb, duration_hms, "Calculating...", reply_to_msg_id
        # )

        quality_options = get_video_formats(url)
        thumbnail_url = sanitized_info.get("thumbnail")

        # ========== DEFAULT DOWNLOAD IF NO FORMATS ========== #
        if not quality_options:
            await context.bot.send_message(
                chat_id, "⚠️ No available formats found, downloading the default video..."
            )

            # 📌 Pin Downloading...
            pin_msg = await context.bot.send_message(chat_id, "📥 Downloading video... Please wait.")
            await context.bot.pin_chat_message(chat_id, pin_msg.message_id)

            file_paths = download(url, None)

            # ✅ Unpin Downloading...
            try:
                await context.bot.unpin_chat_message(chat_id, pin_msg.message_id)
                await pin_msg.delete()
            except Exception as e:
                logger.warning(f"Couldn't unpin/delete downloading message: {e}")

            # 📌 Pin Sending...
            send_pin_msg = await context.bot.send_message(chat_id, "📤 Sending video... Please wait.")
            await context.bot.pin_chat_message(chat_id, send_pin_msg.message_id)

            for file_path in file_paths:
                try:
                    with open(file_path, "rb") as file:
                        await context.bot.send_video(
                            chat_id=chat_id,
                            video=file,
                            supports_streaming=True,
                            reply_to_message_id=reply_to_msg_id
                        )
                    os.remove(file_path)
                except Exception as e:
                    logger.exception(f"Error sending file {file_path}: {e}")
                    await context.bot.send_message(chat_id, f"⚠️ Error sending the video.\n\n`{str(e)}`", parse_mode="Markdown")

            # ✅ Unpin Sending...
            try:
                await context.bot.unpin_chat_message(chat_id, send_pin_msg.message_id)
                await send_pin_msg.delete()
            except Exception as e:
                logger.warning(f"Couldn't unpin/delete sending message: {e}")

            return

        # ========== IF USER NEEDS TO SELECT FORMAT ========== #
        if selected_format is None:
            video_id = store_video_url(url)
            keyboard = [
                [
                    InlineKeyboardButton(
                        f"{q['label']} - {((q.get('filesize') or 0) / (1024 * 1024)):.2f} MB",
                        callback_data=json.dumps({"video_id": video_id, "format_id": q["format_id"]}),
                    )
                ]
                for q in quality_options
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if thumbnail_url:
                caption_text = (
                    f"📥 *Select the quality you want:*\n\n"
                    f"🎥 *Title:* {escape_markdown(sanitized_info.get('title', 'Unknown'), version=2)}\n"
                    f"⏱ *Duration:* {escape_markdown(duration_hms, version=2)}"
                )
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=thumbnail_url,
                    caption=caption_text,
                    reply_markup=reply_markup,
                    parse_mode="MarkdownV2",
                    reply_to_message_id=reply_to_msg_id,
                )
            else:
                await context.bot.send_message(
                    chat_id, "📥 Select the quality you want:", reply_markup=reply_markup
                )
            return

        # ========== FORMAT WAS SELECTED, START DOWNLOAD ========== #
        pin_msg = await context.bot.send_message(chat_id, "📥 Downloading video... Please wait.")
        await context.bot.pin_chat_message(chat_id, pin_msg.message_id)

        file_paths = download(url, selected_format)

        try:
            await context.bot.unpin_chat_message(chat_id, pin_msg.message_id)
            await pin_msg.delete()
        except Exception as e:
            logger.warning(f"Couldn't unpin/delete downloading message: {e}")

        send_pin_msg = await context.bot.send_message(chat_id, "📤 Sending video... Please wait.")
        await context.bot.pin_chat_message(chat_id, send_pin_msg.message_id)

        title = sanitized_info.get("title", "Untitled")
        words = title.split()
        short_title = " ".join(words[:10]) + "..." if len(words) > 10 else title
        caption = f"{short_title} downloaded by @offeyicialBot"

        for file_path in file_paths:
            try:
                with open(file_path, "rb") as file:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=file,
                        supports_streaming=True,
                        caption=caption,
                        reply_to_message_id=reply_to_msg_id
                    )
                os.remove(file_path)
            except Exception as e:
                logger.exception(f"Error sending file {file_path}: {e}")
                await context.bot.send_message(chat_id, f"⚠️ Error sending the video.\n\n`{str(e)}`", parse_mode="Markdown")
        try:
            await context.bot.unpin_chat_message(chat_id, send_pin_msg.message_id)
            await send_pin_msg.delete()
        except Exception as e:
            logger.warning(f"Couldn't unpin/delete sending message: {e}")

        await context.bot.send_message(chat_id, "✅ Download complete! 🎥")

    except Exception as e:
        error_text = str(e)
        if "HTTP Error 423" in error_text:
            message = "🚫 This video is locked or unavailable in your region."
        elif "HTTP Error 404" in error_text:
            message = "❌ The video link is invalid or has been removed."
        elif "HTTP Error 403" in error_text:
            message = "🚫 Access denied. The site may require login or region access."
        else:
            message = f"⚠️ Download failed: `{error_text}`"

        logger.exception(f"Error during download: {message}")
        await context.bot.send_message(chat_id, message, parse_mode="Markdown")

async def download_media(update: Update, context: CallbackContext, override_url=None, reply_to_msg_id=None) -> None:
    chat_id = update.effective_chat.id
    url = override_url or update.message.text.strip()

    # Validate the URL
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("⚠️ Please send a valid URL")
        return

    # Suggest a clean URL if applicable
    cleaning_tip = suggest_clean_url(url)
    if cleaning_tip:
        await context.bot.send_message(chat_id, cleaning_tip)

    # Handle the download logic
    await handle_download_logic(
    chat_id, url, context, 
    reply_to_msg_id=reply_to_msg_id or update.message.message_id
    )




async def download_command(update: Update, context: CallbackContext) -> None:
    if context.args:
        url = context.args[0].strip()
        await download_media(update, context, override_url=url, reply_to_msg_id=update.message.message_id)
    else:
        await update.message.reply_text("⚠️ Usage: /download <URL>")


async def quality_selection(update: Update, context: CallbackContext) -> None:
    query = update.callback_query

    try:
        # Decode JSON data safely
        data = json.loads(query.data)
        video_id = data["video_id"]
        selected_format = data["format_id"]
    except (json.JSONDecodeError, KeyError):
        await query.answer("⚠️ Invalid selection.", show_alert=True)
        return

    await query.answer()  # Acknowledge the callback

    chat_id = query.message.chat_id
    user_id = query.from_user.id
    url = get_video_url(video_id)  # Retrieve the original URL from the DB

    if not url:
        await context.bot.send_message(chat_id, "⚠️ Error: Video not found.")
        return

    # Add the user request to the queue
    reply_to_msg_id = update.callback_query.message.message_id
    # Fetch available formats again to get file size
    formats = get_video_formats(url)
    chosen_format = next((f for f in formats if f["format_id"] == selected_format), None)

    if chosen_format:
        size_bytes = chosen_format.get("filesize") or 0
        size_mb = size_bytes / (1024 * 1024)

        if size_mb > 50:
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔒 This format is larger than 50MB and only available to Premium users.\nUpgrade to download. /upgrade",
                reply_to_message_id=reply_to_msg_id
            )
            return

    # Proceed to queue if size is fine
    await queue.put((chat_id, user_id, url, selected_format, reply_to_msg_id))


    queue_positions[chat_id] = queue.qsize()  # Assign a unique position

    # Notify the user of their queue position
    queue_position = queue_positions[chat_id]
    await context.bot.send_message(
        chat_id,
        f"📥 Your request has been added to the queue. Your position: {queue_position}. Please wait..."
    )


async def help_command(update: Update, context: CallbackContext) -> None:
    help_message = (
        "*📖 How to Use MediaMate Bot*\n\n"
        "This bot lets you download videos and audio from various websites.\n\n"
        "*✅ Commands Available:*\n"
        "/start - Main menu with download options and browsers\n"
        "/about - Information about the bot and its features\n"
        "/donate - Support development and maintenance\n"
        "/download <url> - Instantly download media using a direct URL\n"
        "/help - Show this help message\n\n"
        "*🎯 To Download Media:*\n"
        "Just send a direct video or audio link in the chat. The bot will analyze it and let you choose the quality to download.\n\n"
        "*💡 You can also use:*\n"
        "`/download <video-url> @offeyicialBot`\n"
        "✅ Works in group chats or forwarded messages!\n\n"
        "*📌 Tip for Long URLs:*\n"
        "Remove tracking parameters (after `?` or `&`) for better results.\n"
        "Example:\n"
        "`https://website.com/videos/abc123?utm_source=xyz` → `https://website.com/videos/abc123`\n\n"
        "Enjoy fast, high-quality downloads with *MediaMate*! 😎"
    )
    await update.message.reply_text(help_message, parse_mode="Markdown")


async def send_logs_to_owner(context: CallbackContext) -> None:
    owner_id = int(os.getenv("OWNER_ID"))

    # Send videos.db
    if os.path.exists("videos.db"):
        with open("videos.db", "rb") as db_file:
            await context.bot.send_document(chat_id=owner_id, document=db_file, caption="📄 videos.db")

    # Send logs/user_log_download_bot.txt
    log_path = "logs/user_log_download_bot.txt"
    if os.path.exists(log_path):
        with open(log_path, "rb") as log_file:
            await context.bot.send_document(chat_id=owner_id, document=log_file, caption="📄 User Log File")


async def send_data_command(update: Update, context: CallbackContext) -> None:
    owner_id = int(os.getenv("OWNER_ID"))
    if update.effective_user.id == owner_id:
        await send_logs_to_owner(context)
        await update.message.reply_text("✅ Sent files to your DM.")
    else:
        await update.message.reply_text("🚫 You are not authorized to use this command.")


async def error_handler(update: object, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if update and isinstance(update, Update):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="An error occurred while processing your request.",
        )


async def process_queue(context: CallbackContext):
    while True:
        chat_id, user_id, url, selected_format, reply_to_msg_id = await queue.get()

        try:
            await handle_download_logic(chat_id, url, context, selected_format, reply_to_msg_id)
        finally:
            queue.task_done()
            if chat_id in queue_positions:
                del queue_positions[chat_id]

async def upgrade(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text(
        "🙌 *Thanks for your support!*\n\n"
        "We’re working on a feature to let you download videos over 50MB.\n"
        "It’s not ready *just yet*, but hang tight — it's coming soon.\n\n"
        "For now, enjoy downloading smaller files and stick with us while we build more for you. 💪",
        parse_mode="Markdown"
    )



async def run_bot():
    init_db()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(300)
        .connect_timeout(300)
        .build()
    )
    await app.bot.delete_webhook(drop_pending_updates=True)  # 🧨 Required for polling
    asyncio.create_task(process_queue(app))
    app.add_handler(
        CommandHandler(
            "start", start, filters=filters.ChatType.GROUPS | filters.ChatType.PRIVATE
        )
    )
    app.add_handler(
        CommandHandler(
            "about", about, filters=filters.ChatType.GROUPS | filters.ChatType.PRIVATE
        )
    )
    app.add_handler(
        CommandHandler(
            "help",
            help_command,
            filters=filters.ChatType.GROUPS | filters.ChatType.PRIVATE,
        )
    )
    app.add_handler(
        CommandHandler(
            "upgrade",
            upgrade,
            filters=filters.ChatType.PRIVATE | filters.ChatType.GROUPS,
        )
    )

    app.add_handler(
        CommandHandler(
            "donate", donate, filters=filters.ChatType.GROUPS | filters.ChatType.PRIVATE
        )
    )

    app.add_handler(
        CommandHandler(
            "download",
            download_command,
            filters=filters.ChatType.GROUPS | filters.ChatType.PRIVATE,
        )
    )

    app.add_handler(CallbackQueryHandler(quality_selection))
    app.add_handler(CommandHandler("sendfiles", send_data_command))

    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, download_media)
    )
    app.add_error_handler(error_handler)

    app.run_polling()


if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.get_event_loop().run_until_complete(run_bot())