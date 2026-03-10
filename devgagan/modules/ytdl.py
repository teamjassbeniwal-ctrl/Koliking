# ---------------------------------------------------
# File Name: ytdl.py (Pyrogram-only version)
# Description: A Pyrogram bot for downloading videos from YouTube and other sites
# Author: Gagan
# GitHub: https://github.com/devgaganin/
# Telegram: https://t.me/team_spy_pro
# YouTube: https://youtube.com/@dev_gagan
# Created: 2025-01-11
# Last Modified: 2025-03-10
# Version: 3.0.0
# License: MIT License
# ---------------------------------------------------

import yt_dlp
import os
import tempfile
import time
import asyncio
import random
import string
import requests
import logging
import math
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from devgagan import app  # Pyrogram client
from devgagan.utils.func import get_video_metadata, screenshot
from devgagantools import fast_upload
from concurrent.futures import ThreadPoolExecutor
import aiohttp
import aiofiles
from config import YT_COOKIES, INSTA_COOKIES
from mutagen.id3 import ID3, TIT2, TPE1, COMM, APIC
from mutagen.mp3 import MP3

logger = logging.getLogger(__name__)

thread_pool = ThreadPoolExecutor()
ongoing_downloads = {}
cancel_downloads = {}  # Track cancellation requests

# Helper functions
def get_random_string(length=7):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

async def check_cancelled(user_id):
    """Check if user cancelled the download"""
    return user_id in cancel_downloads and cancel_downloads[user_id]

def d_thumbnail(thumbnail_url, save_path):
    try:
        response = requests.get(thumbnail_url, stream=True)
        response.raise_for_status()
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return save_path
    except Exception as e:
        logger.error(f"Failed to download thumbnail: {e}")
        return None

async def download_thumbnail_async(url, path):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                with open(path, 'wb') as f:
                    f.write(await response.read())

async def extract_audio_async(ydl_opts, url):
    def sync_extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)
    return await asyncio.get_event_loop().run_in_executor(thread_pool, sync_extract)

def download_video(url, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

async def fetch_video_info(url, ydl_opts, progress_message, check_duration_and_size):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=False)

        if check_duration_and_size:
            duration = info_dict.get('duration', 0)
            if duration and duration > 3 * 3600:
                await progress_message.edit_text("**__Video is longer than 3 hours. Download aborted...__**")
                return None

            estimated_size = info_dict.get('filesize_approx', 0)
            if estimated_size and estimated_size > 2 * 1024 * 1024 * 1024:
                await progress_message.edit_text("**__Video size is larger than 2GB. Aborting download.__**")
                return None

        return info_dict

# Progress callback for fast_upload
user_progress = {}
def progress_callback(done, total, chat_id, user_id):
    if user_id in cancel_downloads and cancel_downloads[user_id]:
        raise Exception("Download cancelled by user")

    if chat_id not in user_progress:
        user_progress[chat_id] = {
            'previous_done': 0,
            'previous_time': time.time()
        }

    user_data = user_progress[chat_id]

    percent = (done / total) * 100

    completed_blocks = int(percent // 10)
    remaining_blocks = 10 - completed_blocks
    progress_bar = "█" * completed_blocks + "░" * remaining_blocks

    done_mb = done / (1024 * 1024)
    total_mb = total / (1024 * 1024)

    speed = done - user_data['previous_done']
    elapsed_time = time.time() - user_data['previous_time']

    if elapsed_time > 0:
        speed_bps = speed / elapsed_time
        speed_mbps = (speed_bps * 8) / (1024 * 1024)
    else:
        speed_mbps = 0

    if speed_bps > 0:
        remaining_time = (total - done) / speed_bps
    else:
        remaining_time = 0

    remaining_time_min = remaining_time / 60

    final = (
        f"╭────────────────────╮\n"
        f"│    **__Uploading...__**   │\n"
        f"├────────────────────┤\n"
        f"│ {progress_bar}\n\n"
        f"│ **__Progress:__** {percent:.2f}%\n"
        f"│ **__Done:__** {done_mb:.2f} MB / {total_mb:.2f} MB\n"
        f"│ **__Speed:__** {speed_mbps:.2f} Mbps\n"
        f"│ **__Time Remaining:__** {remaining_time_min:.2f} min\n"
        f"│ **__Use /cancel to stop__**\n"
        f"╰────────────────────╯\n\n"
        f"**__Powered by Team JB__**"
    )

    user_data['previous_done'] = done
    user_data['previous_time'] = time.time()

    return final

def format_duration(seconds):
    if not seconds:
        return "Unknown"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes}:{seconds:02d}"

# Cancel command
@app.on_message(filters.command("cancel"))
async def cancel_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in ongoing_downloads and ongoing_downloads[user_id]:
        cancel_downloads[user_id] = True
        await message.reply_text("**__Cancelling your download... Please wait.__**")
    else:
        await message.reply_text("**__You don't have any ongoing download.__**")

# Audio download command
@app.on_message(filters.command("adl"))
async def adl_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in ongoing_downloads and ongoing_downloads[user_id]:
        await message.reply_text("**You already have an ongoing download. Please wait until it completes!**")
        return

    if len(message.command) < 2:
        await message.reply_text("**Usage:** `/adl <video-link>`\n\nPlease provide a valid video link!")
        return

    url = message.command[1]
    ongoing_downloads[user_id] = True
    if user_id in cancel_downloads:
        cancel_downloads.pop(user_id, None)

    try:
        if "instagram.com" in url:
            await process_audio(message, url, cookies_env_var="INSTA_COOKIES")
        elif "youtube.com" in url or "youtu.be" in url:
            await process_audio(message, url, cookies_env_var="YT_COOKIES")
        else:
            await process_audio(message, url)
    except Exception as e:
        await message.reply_text(f"**An error occurred:** `{e}`")
    finally:
        ongoing_downloads.pop(user_id, None)

async def process_audio(message: Message, url: str, cookies_env_var=None):
    user_id = message.from_user.id

    # Check if playlist
    if "playlist" in url or "&list=" in url:
        await message.reply_text("**__Playlist detected! Downloading all audio tracks...__**")
        await process_audio_playlist(message, url, cookies_env_var)
        return

    cookies = None
    if cookies_env_var:
        cookies = cookies_env_var

    temp_cookie_path = None
    if cookies:
        with tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.txt') as temp_cookie_file:
            temp_cookie_file.write(cookies)
            temp_cookie_path = temp_cookie_file.name

    random_filename = f"@team_spy_pro_{user_id}"
    download_path = f"{random_filename}.mp3"

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f"{random_filename}.%(ext)s",
        'cookiefile': '/app/cookies/youtube.txt',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192'
        }],
        'quiet': False,
        'noplaylist': True,
        'js_runtimes': {'node': {}},
        'remote_components': ['ejs:github'],
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0'
        }
    }

    progress_message = await message.reply_text("**__Starting audio extraction...__**")

    try:
        if await check_cancelled(user_id):
            await progress_message.edit_text("**__Download cancelled by user.__**")
            return

        info_dict = await extract_audio_async(ydl_opts, url)

        if await check_cancelled(user_id):
            await progress_message.edit_text("**__Download cancelled by user.__**")
            if os.path.exists(download_path):
                os.remove(download_path)
            return

        title = info_dict.get('title', 'Extracted Audio')
        await progress_message.edit_text("**__Editing metadata...__**")

        if os.path.exists(download_path):
            def edit_metadata():
                audio_file = MP3(download_path, ID3=ID3)
                try:
                    audio_file.add_tags()
                except Exception:
                    pass
                audio_file.tags["TIT2"] = TIT2(encoding=3, text=title)
                audio_file.tags["TPE1"] = TPE1(encoding=3, text="Team SPY")
                audio_file.tags["COMM"] = COMM(encoding=3, lang="eng", desc="Comment", text="Processed by Team SPY")

                thumbnail_url = info_dict.get('thumbnail')
                if thumbnail_url:
                    thumbnail_path = os.path.join(tempfile.gettempdir(), "thumb.jpg")
                    asyncio.run(download_thumbnail_async(thumbnail_url, thumbnail_path))
                    with open(thumbnail_path, 'rb') as img:
                        audio_file.tags["APIC"] = APIC(
                            encoding=3, mime='image/jpeg', type=3, desc='Cover', data=img.read()
                        )
                    os.remove(thumbnail_path)
                audio_file.save()

            await asyncio.to_thread(edit_metadata)

        chat_id = message.chat.id
        if os.path.exists(download_path):
            if await check_cancelled(user_id):
                await progress_message.edit_text("**__Download cancelled by user.__**")
                if os.path.exists(download_path):
                    os.remove(download_path)
                return

            await progress_message.delete()
            prog = await client.send_message(chat_id, "**__Starting Upload...__**")
            try:
                uploaded = await fast_upload(
                    client, download_path,
                    reply=prog,
                    name=None,
                    progress_bar_function=lambda done, total: progress_callback(done, total, chat_id, user_id)
                )
                if await check_cancelled(user_id):
                    await prog.delete()
                    await message.reply_text("**__Upload cancelled by user.__**")
                    if os.path.exists(download_path):
                        os.remove(download_path)
                    return

                await client.send_audio(
                    chat_id,
                    audio=uploaded,
                    caption=f"**{title}**\n\n**__Powered by Team SPY__**",
                    title=title,
                    performer="Team SPY"
                )
            except Exception as e:
                if "cancelled by user" in str(e).lower():
                    await message.reply_text("**__Upload cancelled successfully.__**")
                else:
                    raise e
            finally:
                if prog:
                    await prog.delete()
        else:
            await message.reply_text("**__Audio file not found after extraction!__**")

    except Exception as e:
        logger.exception("Error during audio extraction or upload")
        await message.reply_text(f"**__An error occurred: {e}__**")
    finally:
        if os.path.exists(download_path):
            os.remove(download_path)
        if temp_cookie_path and os.path.exists(temp_cookie_path):
            os.remove(temp_cookie_path)
        if user_id in cancel_downloads:
            cancel_downloads.pop(user_id, None)

async def process_audio_playlist(message: Message, url: str, cookies_env_var=None):
    user_id = message.from_user.id
    progress_msg = await message.reply_text("**__Extracting playlist information...__**")

    try:
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'force_generic_extractor': False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if 'entries' in info:
            playlist_title = info.get('title', 'Playlist')
            total_videos = len(info['entries'])

            await progress_msg.edit_text(
                f"**__Playlist: {playlist_title}__**\n"
                f"**__Total tracks: {total_videos}__**\n"
                f"**__Starting download of all tracks...__**"
            )

            downloaded = 0
            failed = 0

            for i, entry in enumerate(info['entries']):
                if await check_cancelled(user_id):
                    await message.reply_text(
                        f"**__Playlist download cancelled by user.__**\n"
                        f"**__Downloaded: {downloaded} | Failed: {failed}__**"
                    )
                    return

                video_url = f"https://youtube.com/watch?v={entry['id']}"
                try:
                    await process_audio(message, video_url, cookies_env_var)
                    downloaded += 1
                    await progress_msg.edit_text(
                        f"**__Progress: {downloaded}/{total_videos} downloaded__**\n"
                        f"**__Failed: {failed}__**"
                    )
                except Exception as e:
                    if "cancelled by user" not in str(e).lower():
                        failed += 1
                        logger.error(f"Failed to download {video_url}: {e}")

            await message.reply_text(
                f"**__Playlist download complete!__**\n"
                f"**__Success: {downloaded} | Failed: {failed}__**"
            )
        else:
            await message.reply_text("**__No playlist found. Processing as single video...__**")
            await process_audio(message, url, cookies_env_var)

    except Exception as e:
        await message.reply_text(f"**__Error processing playlist: {e}__**")

# Video download command
@app.on_message(filters.command("dl"))
async def dl_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in ongoing_downloads and ongoing_downloads[user_id]:
        await message.reply_text("**You already have an ongoing download. Please wait until it completes!**")
        return

    if len(message.command) < 2:
        await message.reply_text("**Usage:** `/dl <video-link>`\n\nPlease provide a valid video link!")
        return

    url = message.command[1]

    # Check if playlist
    if "playlist" in url or "&list=" in url:
        await message.reply_text("**__Playlist detected! Downloading all videos...__**")
        await process_video_playlist(message, url, None)
        return

    ongoing_downloads[user_id] = True
    if user_id in cancel_downloads:
        cancel_downloads.pop(user_id, None)

    try:
        if "instagram.com" in url:
            await process_video(message, url, "INSTA_COOKIES", check_duration_and_size=False)
        elif "youtube.com" in url or "youtu.be" in url:
            await process_video(message, url, "YT_COOKIES", check_duration_and_size=True)
        else:
            await process_video(message, url, None, check_duration_and_size=False)

    except Exception as e:
        await message.reply_text(f"**An error occurred:** `{e}`")
    finally:
        ongoing_downloads.pop(user_id, None)

async def process_video(message: Message, url: str, cookies_env_var, check_duration_and_size=False):
    user_id = message.from_user.id
    logger.info(f"Received link: {url}")

    cookies = None
    if cookies_env_var:
        cookies = cookies_env_var

    random_filename = get_random_string() + ".mp4"
    download_path = os.path.abspath(random_filename)

    temp_cookie_path = None
    if cookies:
        with tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.txt') as temp_cookie_file:
            temp_cookie_file.write(cookies)
            temp_cookie_path = temp_cookie_file.name

    thumbnail_file = None
    metadata = {'width': None, 'height': None, 'duration': None, 'thumbnail': None}

    ydl_opts = {
        'outtmpl': download_path,
        'format': 'bv*+ba/b',
        'cookiefile': '/app/cookies/youtube.txt',
        'writethumbnail': True,
        'verbose': True,
        'noplaylist': True,
        'js_runtimes': {'node': {}},
        'remote_components': ['ejs:github'],
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0'
        }
    }

    progress_message = await message.reply_text("**__Starting download...__**")

    try:
        if await check_cancelled(user_id):
            await progress_message.edit_text("**__Download cancelled by user.__**")
            return

        info_dict = await fetch_video_info(url, ydl_opts, progress_message, check_duration_and_size)
        if not info_dict:
            return

        if await check_cancelled(user_id):
            await progress_message.edit_text("**__Download cancelled by user.__**")
            return

        await asyncio.to_thread(download_video, url, ydl_opts)

        if await check_cancelled(user_id):
            await progress_message.edit_text("**__Download cancelled by user.__**")
            if os.path.exists(download_path):
                os.remove(download_path)
            return

        title = info_dict.get('title', 'Powered by Team SPY')
        k = await get_video_metadata(download_path)

        metadata['width'] = k['width'] or 1280
        metadata['height'] = k['height'] or 720
        metadata['duration'] = int(k['duration']) if k['duration'] else 0

        thumbnail_url = info_dict.get('thumbnail')
        THUMB = None

        if thumbnail_url:
            thumbnail_file = os.path.join(tempfile.gettempdir(), get_random_string() + ".jpg")
            downloaded_thumb = d_thumbnail(thumbnail_url, thumbnail_file)
            if downloaded_thumb:
                THUMB = downloaded_thumb

        if not THUMB:
            THUMB = await screenshot(download_path, metadata['duration'], user_id)

        chat_id = message.chat.id
        SIZE = 2 * 1024 * 1024 * 1024  # 2GB
        caption = f"**{title}**\n\n**Duration:** {format_duration(metadata['duration'])}"

        if os.path.exists(download_path) and os.path.getsize(download_path) > SIZE:
            prog = await client.send_message(chat_id, "**__Starting Upload (Large File)...__**")
            await split_and_upload_file(client, chat_id, download_path, caption, user_id)
            if prog:
                await prog.delete()
        elif os.path.exists(download_path):
            if await check_cancelled(user_id):
                await progress_message.edit_text("**__Download cancelled by user.__**")
                if os.path.exists(download_path):
                    os.remove(download_path)
                return

            await progress_message.delete()
            prog = await client.send_message(chat_id, "**__Starting Upload...__**")

                        try:
                uploaded = await fast_upload(
                    client, download_path,
                    reply=prog,
                    progress_bar_function=lambda done, total: progress_callback(done, total, chat_id, user_id)
                )

                if await check_cancelled(user_id):
                    await prog.delete()
                    await message.reply_text("**__Upload cancelled by user.__**")
                    if os.path.exists(download_path):
                        os.remove(download_path)
                    return

                await client.send_video(
                    chat_id,
                    video=uploaded,
                    caption=caption,
                    supports_streaming=True,
                    duration=metadata['duration'],
                    width=metadata['width'],
                    height=metadata['height'],
                    thumb=THUMB if THUMB and os.path.exists(THUMB) else None
                )
            except Exception as e:
                if "cancelled by user" in str(e).lower():
                    await message.reply_text("**__Upload cancelled successfully.__**")
                else:
                    raise e
            finally:
                if prog:
                    await prog.delete()
        else:
            await message.reply_text("**__File not found after download. Something went wrong!__**")

    except Exception as e:
        if "cancelled by user" in str(e).lower():
            await message.reply_text("**__Process cancelled successfully.__**")
        else:
            logger.exception("An error occurred during download or upload.")
            await message.reply_text(f"**__An error occurred: {e}__**")
    finally:
        # Cleanup
        if os.path.exists(download_path):
            try:
                os.remove(download_path)
            except:
                pass
        if temp_cookie_path and os.path.exists(temp_cookie_path):
            try:
                os.remove(temp_cookie_path)
            except:
                pass
        if thumbnail_file and os.path.exists(thumbnail_file):
            try:
                os.remove(thumbnail_file)
            except:
                pass
        if user_id in cancel_downloads:
            cancel_downloads.pop(user_id, None)


# -------------------------------------------------------------------
# Playlist processing for videos
# -------------------------------------------------------------------
async def process_video_playlist(message: Message, url: str, cookies_env_var):
    user_id = message.from_user.id
    progress_msg = await message.reply_text("**__Extracting playlist information...__**")

    try:
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'force_generic_extractor': False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if 'entries' in info:
            playlist_title = info.get('title', 'Playlist')
            total_videos = len(info['entries'])

            await progress_msg.edit_text(
                f"**__Playlist: {playlist_title}__**\n"
                f"**__Total videos: {total_videos}__**\n"
                f"**__Starting download of all videos...__**\n"
                f"**__Use /cancel to stop__**"
            )

            downloaded = 0
            failed = 0

            for i, entry in enumerate(info['entries']):
                if await check_cancelled(user_id):
                    await message.reply_text(
                        f"**__Playlist download cancelled by user.__**\n"
                        f"**__Downloaded: {downloaded} | Failed: {failed}__**"
                    )
                    return

                video_url = f"https://youtube.com/watch?v={entry['id']}"
                try:
                    await process_video(message, video_url, cookies_env_var, check_duration_and_size=True)
                    downloaded += 1
                    await progress_msg.edit_text(
                        f"**__Progress: {downloaded}/{total_videos} downloaded__**\n"
                        f"**__Failed: {failed}__**"
                    )
                except Exception as e:
                    if "cancelled by user" not in str(e).lower():
                        failed += 1
                        logger.error(f"Failed to download {video_url}: {e}")

            await message.reply_text(
                f"**__Playlist download complete!__**\n"
                f"**__Success: {downloaded} | Failed: {failed}__**"
            )
        else:
            await message.reply_text("**__No playlist found. Processing as single video...__**")
            await process_video(message, url, cookies_env_var, check_duration_and_size=True)

    except Exception as e:
        await message.reply_text(f"**__Error processing playlist: {e}__**")


# -------------------------------------------------------------------
# Split and upload large files ( >2GB )
# -------------------------------------------------------------------
async def split_and_upload_file(client: Client, chat_id, file_path, caption, user_id):
    """Split and upload large files with cancellation support"""
    if not os.path.exists(file_path):
        await client.send_message(chat_id, "File not found!")
        return

    file_size = os.path.getsize(file_path)
    start = await client.send_message(chat_id, f"File size: {file_size / (1024 * 1024):.2f} MB")

    PART_SIZE = int(1.9 * 1024 * 1024 * 1024)  # 1.9GB
    CHUNK_SIZE = 5 * 1024 * 1024  # 5MB

    base_name, file_ext = os.path.splitext(file_path)

    part_number = 0
    written = 0
    part_file = f"{base_name}.part{str(part_number).zfill(3)}{file_ext}"

    async with aiofiles.open(file_path, mode="rb") as f:
        async with aiofiles.open(part_file, mode="wb") as part_f:
            while True:
                if await check_cancelled(user_id):
                    await client.send_message(chat_id, "**__Split upload cancelled by user.__**")
                    if os.path.exists(part_file):
                        os.remove(part_file)
                    return

                chunk = await f.read(CHUNK_SIZE)
                if not chunk:
                    break

                await part_f.write(chunk)
                written += len(chunk)

                if written >= PART_SIZE:
                    await part_f.close()

                    edit = await client.send_message(chat_id, f"Uploading part {part_number + 1}...")
                    part_caption = f"{caption}\n\n**Part : {part_number + 1}**"

                    # Generate thumbnail for this part
                    thumb_file = await screenshot(part_file, 1, user_id)

                    # Send video part
                    await client.send_video(
                        chat_id,
                        video=part_file,
                        caption=part_caption,
                        thumb=thumb_file,
                        supports_streaming=True,
                        progress=progress_bar,
                        progress_args=("Uploading...", edit, time.time(), user_id)
                    )

                    # Clean up
                    if thumb_file and os.path.exists(thumb_file):
                        os.remove(thumb_file)
                    os.remove(part_file)

                    # Prepare next part
                    part_number += 1
                    written = 0
                    part_file = f"{base_name}.part{str(part_number).zfill(3)}{file_ext}"
                    part_f = await aiofiles.open(part_file, mode="wb")

    # Upload remaining last part
    if os.path.exists(part_file) and os.path.getsize(part_file) > 0:
        if await check_cancelled(user_id):
            await client.send_message(chat_id, "**__Split upload cancelled by user.__**")
            if os.path.exists(part_file):
                os.remove(part_file)
            return

        edit = await client.send_message(chat_id, f"Uploading part {part_number + 1}...")
        part_caption = f"{caption}\n\n**Part : {part_number + 1}**"

        thumb_file = await screenshot(part_file, 1, user_id)

        await client.send_video(
            chat_id,
            video=part_file,
            caption=part_caption,
            thumb=thumb_file,
            supports_streaming=True,
            progress=progress_bar,
            progress_args=("Uploading...", edit, time.time(), user_id)
        )

        if thumb_file and os.path.exists(thumb_file):
            os.remove(thumb_file)
        os.remove(part_file)

    await start.delete()
    os.remove(file_path)


# -------------------------------------------------------------------
# Progress bar helper for split uploads
# -------------------------------------------------------------------
async def progress_bar(current, total, ud_type, message, start, user_id):
    if await check_cancelled(user_id):
        raise Exception("Upload cancelled by user")

    now = time.time()
    diff = now - start

    if round(diff % 10) == 0 or current == total:
        percentage = (current * 100) / total
        speed = current / diff if diff else 0
        elapsed_time = round(diff * 1000)
        time_to_completion = round((total - current) / speed) * 1000 if speed else 0
        estimated_total_time = elapsed_time + time_to_completion

        elapsed_time_str = time_formatter(elapsed_time)
        estimated_total_time_str = time_formatter(estimated_total_time)

        progress = "".join(["█" for _ in range(math.floor(percentage / 10))]) + \
                   "".join(["░" for _ in range(10 - math.floor(percentage / 10))])

        progress_text = (
            f"╭────────────────────╮\n"
            f"│    **{ud_type}**   │\n"
            f"├────────────────────┤\n"
            f"│ {progress}\n\n"
            f"Completed: {humanbytes(current)}/{humanbytes(total)}\n"
            f"Bytes: {percentage:.2f}%\n"
            f"Speed: {humanbytes(speed)}/s\n"
            f"ETA: {estimated_total_time_str}\n"
            f"│ **__Use /cancel to stop__**\n"
            f"╰────────────────────╯"
        )

        try:
            await message.edit_text(text=progress_text)
        except:
            pass


def humanbytes(size):
    if not size:
        return ""
    power = 2**10
    n = 0
    power_labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power and n < 4:
        size /= power
        n += 1
    return f"{round(size, 2)} {power_labels[n]}"


def time_formatter(milliseconds):
    seconds, milliseconds = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")
    if milliseconds and not parts:
        parts.append(f"{milliseconds}ms")

    return ' '.join(parts) if parts else "0s"
            
