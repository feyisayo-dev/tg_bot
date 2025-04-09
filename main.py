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
    context, chat_id, file_size_mb, duration_hms, estimated_time_hms
):
    message_text = (
        f"📂 Video Information:\n"
        f"File Size: {file_size_mb:.2f} MB\n"
        f"⏱ Duration: {duration_hms}\n"
        f"⌛ Estimated Time to Receive: {estimated_time_hms}"
    )
    await context.bot.send_message(chat_id=chat_id, text=message_text)


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


async def download_media(
    update: Update, context: CallbackContext, override_url=None
) -> None:
    chat_id = update.effective_chat.id
    url = override_url or update.message.text.strip()

    download_start_time = time.time()

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("⚠️ Please send a valid URL")
        return

    try:
        sanitized_info = get_video_info(url)
        file_size = get_file_size(sanitized_info) or 0
        file_size_mb = file_size / (1024 * 1024)
        duration_hms = format_time(get_duration(sanitized_info))

        await send_video_info_message(
            context, chat_id, file_size_mb, duration_hms, "Calculating..."
        )

        quality_options = get_video_formats(url)
        thumbnail_url = sanitized_info.get("thumbnail")

        if not quality_options:
            await update.message.reply_text(
                "⚠️ No available formats found, downloading the default video...",
                reply_to_message_id=update.message.message_id
            )
            processing_task = asyncio.create_task(
                send_processing_message(context, chat_id)
            )

            try:
                file_paths = download(url, None)
                processing_task.cancel()
                try:
                    await processing_task
                except asyncio.CancelledError:
                    pass

                for file_path in file_paths:
                    try:
                        with open(file_path, 'rb') as file:
                            await context.bot.send_video(
                                chat_id=chat_id,
                                video=file,
                                supports_streaming=True,
                                reply_to_message_id=update.message.message_id
                            )
                        os.remove(file_path)
                    except Exception as e:
                        logger.exception(f"Error sending file {file_path}: {e}")

                await update.message.reply_text(
                    "✅ Your download is complete! 🎥",
                    reply_to_message_id=update.message.message_id
                )

            except Exception as e:
                await update.message.reply_text(
                    f"⚠️ Download failed: {e}",
                    reply_to_message_id=update.message.message_id
                )
                logger.exception(f"Error downloading default video: {e}")
            return

        # If formats exist, show selection
        context.user_data["url"] = url
        context.user_data["sanitized_info"] = sanitized_info

        video_id = store_video_url(url)

        keyboard = [
            [
                InlineKeyboardButton(
                    f"{q['resolution']} - {((q.get('filesize') or 0) / (1024 * 1024)):.2f} MB",
                    callback_data=json.dumps(
                        {"video_id": video_id, "format_id": q["format_id"]}
                    )
                )
            ] for q in quality_options
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if thumbnail_url:
            from telegram.helpers import escape_markdown

            caption_text = (
                f"📥 *Select the quality you want:*\n\n"
                f"🎥 *Title:* {escape_markdown(sanitized_info.get('title', 'Unknown'), version=2)}\n"
                f"📂 *File Size:* {escape_markdown(f'{file_size_mb:.2f}', version=2)} MB\n"
                f"⏱ *Duration:* {escape_markdown(duration_hms, version=2)}"
            )

            await context.bot.send_photo(
                chat_id=chat_id,
                photo=thumbnail_url,
                caption=caption_text,
                reply_markup=reply_markup,
                parse_mode="MarkdownV2",
                reply_to_message_id=update.message.message_id,
            )
        else:
            await update.message.reply_text(
                "📥 Select the quality you want:",
                reply_markup=reply_markup,
                reply_to_message_id=update.message.message_id,
            )

    except Exception as e:
        await update.message.reply_text(
            f"⚠️ An error occurred while fetching video details: {e}"
        )
        logger.exception(f"Error fetching video details: {e}")

async def download_command(update: Update, context: CallbackContext) -> None:
    if context.args:
        url = context.args[0].strip()
        await download_media(update, context, override_url=url)
    else:
        await update.message.reply_text("⚠️ Usage: /download <URL>")


async def quality_selection(update: Update, context: CallbackContext) -> None:
    """Handles the user’s quality selection from the inline keyboard."""
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
    url = get_video_url(video_id)  # Retrieve the original URL from the DB

    if not url:
        await context.bot.send_message(chat_id, "⚠️ Error: Video not found.")
        return

    try:
        # Fetch video info & initiate download
        sanitized_info = get_video_info(url)
        pin_msg = await context.bot.send_message(
            chat_id, "📥 Downloading video... Please wait."
        )
        await context.bot.pin_chat_message(chat_id, pin_msg.message_id)

        file_paths = download(url, selected_format)

        if not file_paths:
            await context.bot.send_message(chat_id, "⚠️ Download failed.")
            return

        # Send the downloaded video(s) to the user
        for file_path in file_paths:
            try:
                with open(file_path, "rb") as file:
                    title = sanitized_info.get("title", "Unknown Video")
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=file,
                        supports_streaming=True,
                        caption=f"🎥 {title} downloaded by @OffeyicialBot",
                    )
                os.remove(file_path)  # Clean up the file after sending
            except Exception as e:
                logger.exception(f"Error sending file {file_path}: {e}")
                await context.bot.send_message(chat_id, "⚠️ Error sending the video.")

        await context.bot.send_message(chat_id, "✅ Download complete! 🎥")
        await context.bot.unpin_chat_message(chat_id, pin_msg.message_id)
        await pin_msg.delete()
    except Exception as e:
        logger.exception(f"Unexpected error while downloading: {e}")
        await context.bot.send_message(
            chat_id, "⚠️ An error occurred while downloading."
        )


async def help_command(update: Update, context: CallbackContext) -> None:
    help_message = (
        "*Help - List of Commands*\n\n"
        "/start - Show options to download or open browsers\n"
        "/about - Information about the bot\n"
        "/donate - Information on how to donate\n"
        "/help - Show this help message\n\n"
        "To download media, send a video or audio link."
    )
    await update.message.reply_text(help_message, parse_mode="Markdown")


async def error_handler(update: object, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if update and isinstance(update, Update):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="An error occurred while processing your request.",
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

    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, download_media)
    )
    app.add_error_handler(error_handler)

    app.run_polling()


if __name__ == "__main__":
    asyncio.run(run_bot())
