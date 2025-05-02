import random
import re
from uuid import uuid4
import os
from datetime import datetime
from yt_dlp import YoutubeDL

def get_video_info(url):
    with YoutubeDL() as ydl:
        info = ydl.extract_info(url, download=False)
        sanitized_info = ydl.sanitize_info(info)
        thumbnail_url = info.get("thumbnail", "No thumbnail found")
        print(f"This is the sanitized info thumbnail {thumbnail_url}")
        return sanitized_info

def get_file_size(sanitized_info):
    default_size = 10 * 1024 * 1024  # 10MB in bytes
    if "filesize_approx" in sanitized_info:
        file_size = sanitized_info["filesize_approx"]
        if file_size is None:
            return default_size
        return file_size
    else:
        return default_size

def get_duration(sanitized_info):
    if "duration" in sanitized_info:
        duration = sanitized_info["duration"]
        if duration is not None:
            return duration
    return 600  # Default duration of 10 minutes in seconds

def get_video_formats(url):
    with YoutubeDL() as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats", [])

    quality_options = []
    for fmt in formats:
        has_video = fmt.get("vcodec") != "none"
        has_audio = fmt.get("acodec") != "none"
        if has_video and has_audio:
            height = fmt.get("height")
            format_note = fmt.get("format_note")
            format_id = fmt.get("format_id")
            filesize = fmt.get("filesize", 0)

            # 🔍 Determine readable label
            if format_note:
                label = format_note
            elif height:
                label = f"{height}p"
            else:
                label = format_id  # fallback

            quality_options.append({
                "format_id": str(format_id),
                "label": label,
                "filesize": filesize
            })

    return quality_options

def download(url, format_id):
    sanitized_info = get_video_info(url)

    title = sanitized_info.get("title", "unknown_title")
    sanitized_title = re.sub(r'[\\/*?:"<>|]', '_', title)
    truncated_title = sanitized_title[:10].rstrip("_")

    # Add unique short ID to reduce filename length
    unique_suffix = uuid4().hex[:6]

    # Construct safe output path
    output_path = f"/tmp/{truncated_title}_{unique_suffix}_%(id)s.%(ext)s"
    print(f"[🎯] This is: {output_path}")
    ydl_opts = {
        "outtmpl": output_path,
        "cookies": "cookies.txt",
        "cookies-from-browser": "chrome",
        "format": format_id or "best",
        "verbose": True
    }

    file_paths = []
    with YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=True)

        if "entries" in info_dict:
            for idx, entry in enumerate(info_dict["entries"], start=1):
                filename = ydl.prepare_filename(entry)
                print(f"[🎥 {idx}] Original filename: {filename}")

                random_suffix = random.randint(100, 999)
                unique_filename = filename.replace(f"{sanitized_title}", f"{sanitized_title}_{idx}_{random_suffix}")
                print(f"[🎯 {idx}] Renamed to: {unique_filename}")
                os.rename(filename, unique_filename)
                file_paths.append(unique_filename)
        else:
            file_paths.append(ydl.prepare_filename(info_dict))

    return file_paths
