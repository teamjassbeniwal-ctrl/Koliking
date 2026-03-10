# ---------------------------------------------------
# File Name: ytdl.py (Fully fixed – simplified yt-dlp options)
# Description: Download videos/audio from YouTube & other sites
# Author: Gagan
# Version: 4.2.0 (Stable format selection, cookies fix)
# License: MIT
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
import subprocess
import json
from pyrogram import Client, filters
from pyrogram.types import Message
from devgagan import app
from concurrent.futures import ThreadPoolExecutor
import aiohttp
import aiofiles
from mutagen.id3 import ID3, TIT2, TPE1, COMM, APIC
from mutagen.mp3 import MP3

logger = logging.getLogger(__name__)

thread_pool = ThreadPoolExecutor()
ongoing_downloads = {}
cancel_downloads = {}  # Track cancellation requests

# -------------------------------------------------------------------
#  Helper functions
# -------------------------------------------------------------------
def get_random_string(length=7):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

async def check_cancelled(user_id):
    return user_id in cancel_downloads and cancel_downloads[user_id]

def d_thumbnail(thumbnail_url, save_path):
    try:
        r = requests.get(thumbnail_url, stream=True, timeout=15)
        r.raise_for_status()
        with open(save_path, 'wb') as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return save_path
    except Exception as e:
        logger.error(f"Thumbnail download failed: {e}")
        return None

async def download_thumbnail_async(url, path):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                with open(path, 'wb') as f:
                    f.write(await resp.read())

def get_video_metadata(file_path):
    """
    Returns dict with width, height, duration using ffprobe.
    """
    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_streams', file_path
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        data = json.loads(out)
        video_stream = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
        if video_stream:
            width = int(video_stream.get('width', 1280))
            height = int(video_stream.get('height', 720))
            duration = float(video_stream.get('duration', 0))
            return {'width': width, 'height': height, 'duration': duration}
    except Exception as e:
        logger.error(f"ffprobe failed: {e}")
    return {'width': 1280, 'height': 720, 'duration': 0}

async def screenshot(file_path, duration, user_id):
    """
    Extract a frame at 1/3 of video duration and return thumbnail path.
    """
    thumb = os.path.join(tempfile.gettempdir(), f"thumb_{user_id}_{int(time.time())}.jpg")
    seek = min(duration * 0.33, 30) if duration > 0 else 5
    cmd = [
        'ffmpeg', '-i', file_path, '-ss', str(seek),
        '-vframes', '1', '-q:v', '2', thumb, '-y'
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(thumb):
            return thumb
    except Exception as e:
        logger.error(f"Screenshot failed: {e}")
    return None

async def extract_audio_async(ydl_opts, url):
    def sync_extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)
    return await asyncio.get_event_loop().run_in_executor(thread_pool, sync_extract)

def download_video(url, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

async def fetch_video_info(url, ydl_opts, progress_message, check_duration_and_size):
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        await progress_message.edit_text(f"**Info extraction failed:** `{e}`")
        return None

    if check_duration_and_size:
        dur = info.get('duration', 0)
        if dur > 3 * 3600:
            await progress_message.edit_text("**__Video >3h – aborted.__**")
            return None
        size = info.get('filesize_approx', 0)
        if size > 2 * 1024 ** 3:
            await progress_message.edit_text("**__Video >2GB – aborted.__**")
            return None
    return info

# Progress callback for upload
user_progress = {}
def progress_callback(done, total, chat_id, user_id):
    if user_id in cancel_downloads and cancel_downloads[user_id]:
        raise Exception("Download cancelled by user")
    if chat_id not in user_progress:
        user_progress[chat_id] = {'previous_done': 0, 'previous_time': time.time()}
    data = user_progress[chat_id]
    percent = (done / total) * 100
    blocks = int(percent // 10)
    bar = "█" * blocks + "░" * (10 - blocks)
    done_mb = done / 1048576
    total_mb = total / 1048576
    speed = done - data['previous_done']
    elapsed = time.time() - data['previous_time']
    speed_mbps = (speed / elapsed * 8) / 1048576 if elapsed > 0 else 0
    remaining = (total - done) / (speed / elapsed) if speed > 0 else 0
    rem_min = remaining / 60
    text = (
        f"╭────────────────────╮\n│    **__Uploading...__**   │\n├────────────────────┤\n│ {bar}\n\n"
        f"│ **__Progress:__** {percent:.2f}%\n│ **__Done:__** {done_mb:.2f} MB / {total_mb:.2f} MB\n"
        f"│ **__Speed:__** {speed_mbps:.2f} Mbps\n│ **__Time Remaining:__** {rem_min:.2f} min\n"
        f"│ **__Use /cancel to stop__**\n╰────────────────────╯\n\n**__Powered by Team JB__**"
    )
    data['previous_done'] = done
    data['previous_time'] = time.time()
    return text

def format_duration(seconds):
    if not seconds:
        return "Unknown"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def humanbytes(size):
    if not size:
        return ""
    p = 0
    labels = ['B', 'KB', 'MB', 'GB', 'TB']
    while size > 1024 and p < 4:
        size /= 1024
        p += 1
    return f"{round(size, 2)} {labels[p]}"

def time_formatter(ms):
    s, ms = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s:
        parts.append(f"{s}s")
    if ms and not parts:
        parts.append(f"{ms}ms")
    return ' '.join(parts) if parts else "0s"

# -------------------------------------------------------------------
#  Cancel command
# -------------------------------------------------------------------
@app.on_message(filters.command("dcancel"))
async def cancel_handler(client: Client, message: Message):
    uid = message.from_user.id
    if ongoing_downloads.get(uid):
        cancel_downloads[uid] = True
        await message.reply_text("**__Cancelling download...__**")
    else:
        await message.reply_text("**__No ongoing download.__**")

# -------------------------------------------------------------------
#  Audio download
# -------------------------------------------------------------------
@app.on_message(filters.command("adl"))
async def adl_handler(client: Client, message: Message):
    uid = message.from_user.id
    if ongoing_downloads.get(uid):
        await message.reply_text("**You already have an ongoing download.**")
        return
    if len(message.command) < 2:
        await message.reply_text("**Usage:** `/adl <link>`")
        return
    url = message.command[1]

    # Handle playlist
    if "playlist" in url or "&list=" in url:
        await message.reply_text("**__Playlist detected – downloading all...__**")
        await process_audio_playlist(client, message, url)
        return

    ongoing_downloads[uid] = True
    cancel_downloads.pop(uid, None)  # Ensure clean start
    try:
        if "instagram.com" in url:
            await process_audio(client, message, url, is_instagram=True)
        else:  # YouTube or others
            await process_audio(client, message, url, is_instagram=False)
    except Exception as e:
        await message.reply_text(f"**Error:** `{e}`")
    finally:
        ongoing_downloads.pop(uid, None)
        cancel_downloads.pop(uid, None)

async def process_audio(client: Client, message: Message, url: str, is_instagram: bool = False):
    uid = message.from_user.id
    out_path = None
    prog_msg = await message.reply_text("**__Starting audio extraction...__**")

    try:
        # Determine cookie file path
        cookie_file = None
        if is_instagram:
            cookie_path = '/app/cookies/instagram.txt'
            if os.path.exists(cookie_path):
                cookie_file = cookie_path
        else:
            cookie_path = '/app/cookies/youtube.txt'
            if os.path.exists(cookie_path):
                cookie_file = cookie_path

        random_filename = get_random_string()
        out_path = f"{random_filename}.mp3"

        # Simplified yt-dlp options – no extractor_args, just basic format
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': f"{random_filename}.%(ext)s",
            'quiet': True,
            'noplaylist': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
        }
        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file

        # Check cancellation before download
        if await check_cancelled(uid):
            await prog_msg.edit_text("**__Cancelled.__**")
            return

        # Download and extract audio
        info = await extract_audio_async(ydl_opts, url)

        if await check_cancelled(uid):
            await prog_msg.edit_text("**__Cancelled.__**")
            return

        # Verify file exists and is not empty
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise Exception("Downloaded audio file is missing or empty (0 B).")

        title = info.get("title", "Audio")
        await prog_msg.edit_text("**__Editing metadata...__**")

        # Add metadata
        def add_metadata():
            audio = MP3(out_path, ID3=ID3)
            try:
                audio.add_tags()
            except:
                pass
            audio.tags["TIT2"] = TIT2(encoding=3, text=title)
            audio.tags["TPE1"] = TPE1(encoding=3, text="Team SPY")
            audio.tags["COMM"] = COMM(encoding=3, lang="eng", text="Powered by Team SPY")

            thumb_url = info.get("thumbnail")
            if thumb_url:
                thumb_path = os.path.join(tempfile.gettempdir(), f"{get_random_string()}.jpg")
                try:
                    r = requests.get(thumb_url, timeout=10)
                    with open(thumb_path, 'wb') as f:
                        f.write(r.content)
                    with open(thumb_path, 'rb') as img:
                        audio.tags["APIC"] = APIC(
                            encoding=3,
                            mime="image/jpeg",
                            type=3,
                            data=img.read()
                        )
                except Exception as e:
                    logger.error(f"Thumbnail embedding failed: {e}")
                finally:
                    if os.path.exists(thumb_path):
                        os.remove(thumb_path)
            audio.save()

        await asyncio.to_thread(add_metadata)

        if await check_cancelled(uid):
            await prog_msg.edit_text("**__Cancelled.__**")
            return

        await prog_msg.delete()
        prog = await client.send_message(message.chat.id, "**__Uploading...__**")

        try:
            await client.send_audio(
                chat_id=message.chat.id,
                audio=out_path,
                caption=f"**{title}**\n\n__Powered by Team JB__",
                title=title,
                performer="Team JB",
                progress=progress_callback,
                progress_args=(message.chat.id, uid)
            )
        finally:
            await prog.delete()

    except Exception as e:
        logger.exception("Audio error")
        await message.reply_text(f"**__Error: {e}__**")
    finally:
        # Cleanup
        if out_path and os.path.exists(out_path):
            os.remove(out_path)

async def process_audio_playlist(client, message, url):
    uid = message.from_user.id
    ongoing_downloads[uid] = True  # Prevent multiple playlists
    cancel_downloads.pop(uid, None)
    prog = await message.reply_text("**__Extracting playlist...__**")
    try:
        ydl_opts = {'quiet': True, 'extract_flat': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if 'entries' not in info:
            await prog.edit_text("**__No playlist found.__**")
            # Fallback to single video
            await process_audio(client, message, url)
            return
        total = len(info['entries'])
        await prog.edit_text(f"**__Playlist: {info.get('title','')}__**\n**Total: {total}**")
        good = fail = 0
        for entry in info['entries']:
            if await check_cancelled(uid):
                await message.reply_text(f"**__Cancelled. Downloaded: {good}/{total}__**")
                return
            vid_url = f"https://youtube.com/watch?v={entry['id']}"
            try:
                await process_audio(client, message, vid_url)
                good += 1
            except Exception as e:
                fail += 1
                logger.error(f"Failed {vid_url}: {e}")
            await prog.edit_text(f"**__Progress: {good}/{total} downloaded, {fail} failed.__**")
        await message.reply_text(f"**__Playlist done! Success: {good}, Failed: {fail}__**")
    except Exception as e:
        await message.reply_text(f"**__Error: {e}__**")
    finally:
        ongoing_downloads.pop(uid, None)
        cancel_downloads.pop(uid, None)

# -------------------------------------------------------------------
#  Video download
# -------------------------------------------------------------------
@app.on_message(filters.command("dl"))
async def dl_handler(client: Client, message: Message):
    uid = message.from_user.id
    if ongoing_downloads.get(uid):
        await message.reply_text("**You already have an ongoing download.**")
        return
    if len(message.command) < 2:
        await message.reply_text("**Usage:** `/dl <link>`")
        return
    url = message.command[1]

    if "playlist" in url or "&list=" in url:
        await message.reply_text("**__Playlist detected – downloading all...__**")
        await process_video_playlist(client, message, url)
        return

    ongoing_downloads[uid] = True
    cancel_downloads.pop(uid, None)
    try:
        if "instagram.com" in url:
            await process_video(client, message, url, is_instagram=True)
        else:
            await process_video(client, message, url, is_instagram=False)
    except Exception as e:
        await message.reply_text(f"**Error:** `{e}`")
    finally:
        ongoing_downloads.pop(uid, None)
        cancel_downloads.pop(uid, None)

async def process_video(client, message, url, is_instagram=False):
    uid = message.from_user.id
    thumb = None
    downloaded_file = None
    prog_msg = await message.reply_text("**Starting download...**")

    try:
        # Determine cookie file path
        cookie_file = None
        if is_instagram:
            cookie_path = '/app/cookies/instagram.txt'
            if os.path.exists(cookie_path):
                cookie_file = cookie_path
        else:
            cookie_path = '/app/cookies/youtube.txt'
            if os.path.exists(cookie_path):
                cookie_file = cookie_path

        out_name = get_random_string()
        download_path = f"{out_name}.%(ext)s"

        # Simplified yt-dlp options – no extractor_args
        ydl_opts = {
            'outtmpl': download_path,
            'format': 'bestvideo+bestaudio/best',
            'writethumbnail': False,  # we handle thumbnail separately
            'quiet': True,
            'noplaylist': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
        }
        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file

        if await check_cancelled(uid):
            await prog_msg.edit_text("**Cancelled.**")
            return

        # Extract info (with size/duration check for YouTube)
        check_duration = not is_instagram  # only check duration for YouTube
        info = await fetch_video_info(url, ydl_opts, prog_msg, check_duration)
        if not info:
            return

        # Download video
        await asyncio.to_thread(download_video, url, ydl_opts)

        # Find downloaded file
        for f in os.listdir('.'):
            if f.startswith(out_name):
                downloaded_file = os.path.abspath(f)
                break

        if not downloaded_file or not os.path.exists(downloaded_file):
            raise Exception("Downloaded file not found!")

        # Check file size
        if os.path.getsize(downloaded_file) == 0:
            raise Exception("Downloaded file is empty (0 B).")

        # Convert to MP4 if needed
        if not downloaded_file.endswith('.mp4'):
            mp4_path = os.path.abspath(out_name + '.mp4')
            try:
                subprocess.run(
                    ['ffmpeg', '-i', downloaded_file, '-c', 'copy', mp4_path, '-y'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True
                )
                os.remove(downloaded_file)
                downloaded_file = mp4_path
            except Exception as e:
                logger.warning(f"MP4 conversion failed: {e}")

        # Metadata
        title = info.get('title', 'Video')
        meta = get_video_metadata(downloaded_file) or {}
        width = meta.get('width', 1280)
        height = meta.get('height', 720)
        duration = int(meta.get('duration', 0))

        # Thumbnail
        thumb_url = info.get('thumbnail')
        if thumb_url:
            thumb_path = os.path.join(tempfile.gettempdir(), f"{get_random_string()}.jpg")
            dl = d_thumbnail(thumb_url, thumb_path)
            if dl and os.path.exists(dl):
                thumb = dl

        if not thumb:
            thumb = await screenshot(downloaded_file, duration, uid)

        caption = f"{title}\nDuration: {format_duration(duration)}"

        # Large file splitting (>2GB)
        if os.path.getsize(downloaded_file) > 2 * 1024 ** 3:
            prog = await client.send_message(message.chat.id, "**Large file – splitting...**")
            await split_and_upload_file(client, message.chat.id, downloaded_file, caption, uid)
            await prog.delete()
        else:
            await prog_msg.delete()
            prog = await client.send_message(message.chat.id, "**Uploading...**")
            try:
                await client.send_video(
                    chat_id=message.chat.id,
                    video=downloaded_file,
                    caption=caption,
                    supports_streaming=True,
                    duration=duration,
                    width=width,
                    height=height,
                    thumb=thumb if thumb and os.path.exists(thumb) else None,
                    progress=progress_callback,
                    progress_args=(message.chat.id, uid)
                )
            finally:
                await prog.delete()

    except Exception as e:
        logger.exception("Video error")
        await message.reply_text(f"**Error:** `{e}`")
    finally:
        # Cleanup downloaded file
        if downloaded_file and os.path.exists(downloaded_file):
            try:
                os.remove(downloaded_file)
            except:
                pass
        # Also remove any other files starting with out_name (partials)
        for f in os.listdir('.'):
            if f.startswith(out_name):
                try:
                    os.remove(os.path.join('.', f))
                except:
                    pass
        if thumb and os.path.exists(thumb):
            os.remove(thumb)

async def process_video_playlist(client, message, url):
    uid = message.from_user.id
    ongoing_downloads[uid] = True
    cancel_downloads.pop(uid, None)
    prog = await message.reply_text("**__Extracting playlist...__**")
    try:
        ydl_opts = {'quiet': True, 'extract_flat': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if 'entries' not in info:
            await prog.edit_text("**__No playlist found.__**")
            await process_video(client, message, url)
            return
        total = len(info['entries'])
        await prog.edit_text(f"**__Playlist: {info.get('title','')}__**\n**Total: {total}**")
        good = fail = 0
        for entry in info['entries']:
            if await check_cancelled(uid):
                await message.reply_text(f"**__Cancelled. Downloaded: {good}/{total}__**")
                return
            vid_url = f"https://youtube.com/watch?v={entry['id']}"
            try:
                await process_video(client, message, vid_url)
                good += 1
            except Exception as e:
                fail += 1
                logger.error(f"Failed {vid_url}: {e}")
            await prog.edit_text(f"**__Progress: {good}/{total} downloaded, {fail} failed.__**")
        await message.reply_text(f"**__Playlist done! Success: {good}, Failed: {fail}__**")
    except Exception as e:
        await message.reply_text(f"**__Error: {e}__**")
    finally:
        ongoing_downloads.pop(uid, None)
        cancel_downloads.pop(uid, None)

# -------------------------------------------------------------------
#  Split & upload large files (>2GB)
# -------------------------------------------------------------------
async def split_and_upload_file(client, chat_id, file_path, caption, user_id):
    if not os.path.exists(file_path):
        await client.send_message(chat_id, "File missing")
        return
    size = os.path.getsize(file_path)
    start_msg = await client.send_message(chat_id, f"**File size:** {size/1048576:.2f} MB")

    PART_SIZE = int(1.9 * 1024**3)   # 1.9GB
    CHUNK = 5 * 1024**2               # 5MB

    base, ext = os.path.splitext(file_path)
    part_num = 0
    written = 0
    part_file = f"{base}.part{str(part_num).zfill(3)}{ext}"

    async with aiofiles.open(file_path, 'rb') as f:
        async with aiofiles.open(part_file, 'wb') as pf:
            while True:
                if await check_cancelled(user_id):
                    await client.send_message(chat_id, "**__Cancelled.__**")
                    if os.path.exists(part_file):
                        os.remove(part_file)
                    return
                chunk = await f.read(CHUNK)
                if not chunk:
                    break
                await pf.write(chunk)
                written += len(chunk)
                if written >= PART_SIZE:
                    await pf.close()
                    edit = await client.send_message(chat_id, f"Uploading part {part_num+1}...")
                    part_cap = f"{caption}\n\n**Part {part_num+1}**"
                    thumb = await screenshot(part_file, 1, user_id)
                    await client.send_video(
                        chat_id, video=part_file, caption=part_cap,
                        thumb=thumb, supports_streaming=True,
                        progress=progress_callback, progress_args=(chat_id, user_id)
                    )
                    if thumb and os.path.exists(thumb):
                        os.remove(thumb)
                    os.remove(part_file)
                    part_num += 1
                    written = 0
                    part_file = f"{base}.part{str(part_num).zfill(3)}{ext}"
                    pf = await aiofiles.open(part_file, 'wb')

    # Last part
    if os.path.exists(part_file) and os.path.getsize(part_file) > 0:
        if await check_cancelled(user_id):
            await client.send_message(chat_id, "**__Cancelled.__**")
            if os.path.exists(part_file):
                os.remove(part_file)
            return
        edit = await client.send_message(chat_id, f"Uploading part {part_num+1}...")
        part_cap = f"{caption}\n\n**Part {part_num+1}**"
        thumb = await screenshot(part_file, 1, user_id)
        await client.send_video(
            chat_id, video=part_file, caption=part_cap,
            thumb=thumb, supports_streaming=True,
            progress=progress_callback, progress_args=(chat_id, user_id)
        )
        if thumb and os.path.exists(thumb):
            os.remove(thumb)
        os.remove(part_file)

    await start_msg.delete()
    os.remove(file_path)
