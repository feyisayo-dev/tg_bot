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


async def handle_download_logic(chat_id, url, context, selected_format=None, reply_to_message_id=None):
    try:
        sanitized_info = get_video_info(url)
        file_size = get_file_size(sanitized_info) or 0
        file_size_mb = file_size / (1024 * 1024)
        duration_hms = format_time(get_duration(sanitized_info))

        # Send video info to the user
        await send_video_info_message(
            context, chat_id, file_size_mb, duration_hms, "Calculating...", reply_to_message_id
        )

        quality_options = get_video_formats(url)
        thumbnail_url = sanitized_info.get("thumbnail")

        # Pin "Downloading, please wait..." message
        downloading_message = await context.bot.send_message(
            chat_id, "📥 Downloading, please wait..."
        )
        await context.bot.pin_chat_message(chat_id, downloading_message.message_id)

        # If no formats are available, download the default video
        if not quality_options:
            file_paths = download(url, None)

            # Unpin the "Downloading" message
            await context.bot.unpin_chat_message(chat_id, downloading_message.message_id)
            await downloading_message.delete()

            # Pin "Sending, please wait..." message
            sending_message = await context.bot.send_message(
                chat_id, "📤 Sending, please wait..."
            )
            await context.bot.pin_chat_message(chat_id, sending_message.message_id)

            # Send the downloaded video(s)
            for file_path in file_paths:
                try:
                    with open(file_path, "rb") as file:
                        await context.bot.send_video(
                            chat_id=chat_id,
                            video=file,
                            supports_streaming=True,
                            reply_to_message_id=reply_to_message_id,  # ✅ Replies to the original message
                        )
                    os.remove(file_path)
                except Exception as e:
                    logger.exception(f"Error sending file {file_path}: {e}")
                    await context.bot.send_message(
                        chat_id, "⚠️ Error sending the video.", reply_to_message_id=reply_to_message_id
                    )

            # Unpin the "Sending" message
            await context.bot.unpin_chat_message(chat_id, sending_message.message_id)
            await sending_message.delete()
            return

        # If formats exist and no specific format is selected, show the keyboard
        if selected_format is None:
            video_id = store_video_url(url)
            keyboard = [
                [
                    InlineKeyboardButton(
                        f"{q['resolution']} - {((q.get('filesize') or 0) / (1024 * 1024)):.2f} MB",
                        callback_data=json.dumps(
                            {"video_id": video_id, "format_id": q["format_id"]}
                        ),
                    )
                ]
                for q in quality_options
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if thumbnail_url:
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
                    reply_to_message_id=reply_to_message_id,
                )
            else:
                await context.bot.send_message(
                    chat_id,
                    "📥 Select the quality you want:",
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                )

            # Unpin the "Downloading" message
            await context.bot.unpin_chat_message(chat_id, downloading_message.message_id)
            await downloading_message.delete()
            return

        # If a specific format is selected, download it
        file_paths = download(url, selected_format)

        # Unpin the "Downloading" message
        await context.bot.unpin_chat_message(chat_id, downloading_message.message_id)
        await downloading_message.delete()

        # Pin "Sending, please wait..." message
        sending_message = await context.bot.send_message(
            chat_id, "📤 Sending, please wait..."
        )
        await context.bot.pin_chat_message(chat_id, sending_message.message_id)

        # Send the downloaded video(s)
        for file_path in file_paths:
            try:
                with open(file_path, "rb") as file:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=file,
                        supports_streaming=True,
                        reply_to_message_id=reply_to_message_id,  # ✅ Replies to the original message
                    )
                os.remove(file_path)
            except Exception as e:
                logger.exception(f"Error sending file {file_path}: {e}")
                await context.bot.send_message(
                    chat_id, "⚠️ Error sending the video.", reply_to_message_id=reply_to_message_id
                )

        # Unpin the "Sending" message
        await context.bot.unpin_chat_message(chat_id, sending_message.message_id)
        await sending_message.delete()

        await context.bot.send_message(
            chat_id, "✅ Download complete! 🎥", reply_to_message_id=reply_to_message_id
        )

    except Exception as e:
        logger.exception(f"Error during download: {e}")
        await context.bot.send_message(
            chat_id, "⚠️ An error occurred while processing your request.", reply_to_message_id=reply_to_message_id
        )


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
        "*Help - List of Commands*\n\n"
        "/start - Show options to download or open browsers\n"
        "/about - Information about the bot\n"
        "/donate - Information on how to donate\n"
        "/help - Show this help message\n\n"
        "To download media, send a video or audio link.\n\n"
        "*Tip for sites with long URLs like adult content sites or any other.*\n"
        "For smoother downloads, it's recommended to only use the base URL and remove any parameters after `?` or `&` in the link.\n"
        "Example:\n"
        "https://website.com/videos/abcc\n\n"
        "Avoid using links with tracking parameters like `utm_content=...&ref=...`."
    )
    await update.message.reply_text(help_message, parse_mode="Markdown")


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
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.get_event_loop().run_until_complete(run_bot())
