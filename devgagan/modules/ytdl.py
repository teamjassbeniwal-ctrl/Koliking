import yt_dlp
import os
import tempfile
import time
import asyncio
import random
import string
import requests
import logging
import aiohttp
from pyrogram import Client as PyroClient
from telethon import events
from telethon.sync import TelegramClient
from telethon.tl.types import DocumentAttributeVideo
from telethon.tl.functions.messages import EditMessageRequest
from concurrent.futures import ThreadPoolExecutor
import aiofiles
from mutagen.id3 import ID3, TIT2, TPE1, COMM, APIC
from mutagen.mp3 import MP3
import subprocess

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize thread pool
thread_pool = ThreadPoolExecutor(max_workers=4)
ongoing_downloads = {}
user_progress = {}

class DownloadBot:
    def __init__(self, api_id, api_hash, bot_token, session_name="bot_session"):
        self.client = TelegramClient(session_name, api_id, api_hash)
        self.bot_token = bot_token
        
    async def start(self):
        await self.client.start(bot_token=self.bot_token)
        print("Bot started!")
        
        # Register event handlers
        self.client.add_event_handler(self.handle_dl, events.NewMessage(pattern='/dl'))
        self.client.add_event_handler(self.handle_adl, events.NewMessage(pattern='/adl'))
        
        await self.client.run_until_disconnected()

    def d_thumbnail(self, thumbnail_url, save_path):
        """Download thumbnail synchronously"""
        try:
            response = requests.get(thumbnail_url, stream=True, timeout=10)
            response.raise_for_status()
            with open(save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return save_path
        except Exception as e:
            logger.error(f"Failed to download thumbnail: {e}")
            return None

    async def download_thumbnail_async(self, url, path):
        """Download thumbnail asynchronously"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        async with aiofiles.open(path, 'wb') as f:
                            await f.write(await response.read())
                        return path
        except Exception as e:
            logger.error(f"Async thumbnail download failed: {e}")
            return None

    def get_random_string(self, length=7):
        """Generate random string for filenames"""
        characters = string.ascii_letters + string.digits
        return ''.join(random.choice(characters) for _ in range(length))

    async def progress_callback(self, current, total, chat_id, progress_message):
        """Progress callback for uploads"""
        try:
            percent = (current / total) * 100
            bar_length = 20
            filled_length = int(bar_length * current // total)
            bar = '█' * filled_length + '░' * (bar_length - filled_length)
            
            speed = current / (time.time() - getattr(self, 'start_time', time.time()))
            speed_mbps = (speed * 8) / (1024 * 1024)
            
            current_mb = current / (1024 * 1024)
            total_mb = total / (1024 * 1024)
            
            if speed > 0:
                eta = (total - current) / speed
                eta_str = f"{int(eta // 60)}:{int(eta % 60):02d}"
            else:
                eta_str = "Calculating..."
            
            message = (
                f"╭──────────────────╮\n"
                f"│    **Uploading**    \n"
                f"├──────────────────\n"
                f"│ {bar} {percent:.1f}%\n"
                f"│ {current_mb:.2f}MB / {total_mb:.2f}MB\n"
                f"│ Speed: {speed_mbps:.2f} Mbps\n"
                f"│ ETA: {eta_str}\n"
                f"╰──────────────────╯\n\n"
                f"**Powered By Team SPY**"
            )
            
            try:
                await progress_message.edit(message)
            except:
                pass
                
        except Exception as e:
            logger.error(f"Progress callback error: {e}")

    async def extract_audio_async(self, ydl_opts, url):
        """Extract audio asynchronously"""
        def sync_extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)
        
        return await asyncio.get_event_loop().run_in_executor(thread_pool, sync_extract)

    async def process_audio(self, event, url, cookies_env_var=None):
        """Process audio download and upload"""
        user_id = event.sender_id
        
        if user_id in ongoing_downloads:
            await event.reply("**You already have an ongoing download!**")
            return
            
        ongoing_downloads[user_id] = True
        
        try:
            # Get cookies
            cookies = None
            if cookies_env_var == "INSTA_COOKIES":
                cookies = INSTA_COOKIES
            elif cookies_env_var == "YT_COOKIES":
                cookies = YT_COOKIES
            
            # Create temp cookie file
            temp_cookie_path = None
            if cookies:
                with tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.txt') as f:
                    f.write(cookies)
                    temp_cookie_path = f.name
            
            # Setup yt-dlp options
            random_filename = f"audio_{self.get_random_string()}"
            download_path = f"{random_filename}.mp3"
            
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': f"{random_filename}.%(ext)s",
                'cookiefile': temp_cookie_path if temp_cookie_path else None,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192'
                }],
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
            }
            
            # Start download
            progress_msg = await event.reply("**Starting audio extraction...**")
            
            # Get info and download
            info = await self.extract_audio_async(ydl_opts, url)
            title = info.get('title', 'Unknown Title')
            
            await progress_msg.edit("**Processing metadata...**")
            
            # Add metadata if file exists
            if os.path.exists(download_path):
                # Add ID3 tags
                audio = MP3(download_path, ID3=ID3)
                try:
                    audio.add_tags()
                except:
                    pass
                
                audio.tags.add(TIT2(encoding=3, text=title))
                audio.tags.add(TPE1(encoding=3, text="Team SPY"))
                audio.tags.add(COMM(encoding=3, lang='eng', desc='', text='Processed by Team SPY'))
                
                # Add thumbnail if available
                thumbnail_url = info.get('thumbnail')
                if thumbnail_url:
                    thumb_path = f"thumb_{self.get_random_string()}.jpg"
                    await self.download_thumbnail_async(thumbnail_url, thumb_path)
                    
                    if os.path.exists(thumb_path):
                        with open(thumb_path, 'rb') as img:
                            audio.tags.add(APIC(
                                encoding=3,
                                mime='image/jpeg',
                                type=3,
                                desc='Cover',
                                data=img.read()
                            ))
                        os.remove(thumb_path)
                
                audio.save()
            
            # Upload file
            await progress_msg.edit("**Uploading audio...**")
            
            # Simple upload without progress for now
            with open(download_path, 'rb') as audio_file:
                await self.client.send_file(
                    event.chat_id,
                    audio_file,
                    caption=f"**{title}**\n\n**Powered By Team SPY**",
                    attributes=None
                )
            
            await progress_msg.delete()
            
        except Exception as e:
            logger.error(f"Audio processing error: {e}")
            await event.reply(f"**Error:** {str(e)}")
            
        finally:
            # Cleanup
            if 'download_path' in locals() and os.path.exists(download_path):
                os.remove(download_path)
            if temp_cookie_path and os.path.exists(temp_cookie_path):
                os.remove(temp_cookie_path)
            
            ongoing_downloads.pop(user_id, None)

    async def process_video(self, event, url, cookies_env_var=None, check_duration_and_size=False):
        """Process video download and upload"""
        user_id = event.sender_id
        
        if user_id in ongoing_downloads:
            await event.reply("**You already have an ongoing download!**")
            return
            
        ongoing_downloads[user_id] = True
        
        try:
            # Get cookies
            cookies = None
            if cookies_env_var == "INSTA_COOKIES":
                cookies = INSTA_COOKIES
            elif cookies_env_var == "YT_COOKIES":
                cookies = YT_COOKIES
            
            # Create temp cookie file
            temp_cookie_path = None
            if cookies:
                with tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.txt') as f:
                    f.write(cookies)
                    temp_cookie_path = f.name
            
            # Generate random filename
            random_filename = f"video_{self.get_random_string()}"
            download_path = f"{random_filename}.mp4"
            
            # Get video info first
            ydl_info_opts = {
                'quiet': True,
                'no_warnings': True,
                'cookiefile': temp_cookie_path if temp_cookie_path else None,
            }
            
            progress_msg = await event.reply("**Getting video information...**")
            
            # Get video info
            with yt_dlp.YoutubeDL(ydl_info_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                if check_duration_and_size:
                    duration = info.get('duration', 0)
                    if duration > 3 * 3600:  # 3 hours
                        await progress_msg.edit("**Video too long (max 3 hours)!**")
                        return
                    
                    filesize = info.get('filesize_approx') or info.get('filesize', 0)
                    if filesize and filesize > 2 * 1024 * 1024 * 1024:  # 2GB
                        await progress_msg.edit("**Video too large (max 2GB)!**")
                        return
            
            title = info.get('title', 'Unknown Video')
            thumbnail_url = info.get('thumbnail')
            
            # Download options
            ydl_opts = {
                'format': 'best[height<=1080]',
                'outtmpl': download_path,
                'cookiefile': temp_cookie_path if temp_cookie_path else None,
                'quiet': True,
                'no_warnings': True,
                'writethumbnail': True,
                'postprocessors': [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4'
                }],
            }
            
            # Download video
            await progress_msg.edit("**Downloading video...**")
            
            def download_video_sync():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            
            await asyncio.get_event_loop().run_in_executor(thread_pool, download_video_sync)
            
            if not os.path.exists(download_path):
                await progress_msg.edit("**Download failed!**")
                return
            
            # Get video metadata
            try:
                import cv2
                cap = cv2.VideoCapture(download_path)
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                duration = frame_count / fps if fps > 0 else 0
                cap.release()
            except:
                width = info.get('width', 1280)
                height = info.get('height', 720)
                duration = info.get('duration', 0)
            
            # Download or generate thumbnail
            thumb_path = None
            if thumbnail_url:
                thumb_path = f"thumb_{self.get_random_string()}.jpg"
                await self.download_thumbnail_async(thumbnail_url, thumb_path)
            
            # Upload video
            await progress_msg.edit("**Uploading video...**")
            
            # Simple upload
            with open(download_path, 'rb') as video_file:
                await self.client.send_file(
                    event.chat_id,
                    video_file,
                    caption=f"**{title}**\n\n**Powered By Team SPY**",
                    attributes=[
                        DocumentAttributeVideo(
                            duration=int(duration),
                            w=width,
                            h=height,
                            supports_streaming=True
                        )
                    ],
                    thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None
                )
            
            await progress_msg.delete()
            
        except Exception as e:
            logger.error(f"Video processing error: {e}")
            await event.reply(f"**Error:** {str(e)}")
            
        finally:
            # Cleanup
            if 'download_path' in locals() and os.path.exists(download_path):
                os.remove(download_path)
            if 'thumb_path' in locals() and thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)
            if temp_cookie_path and os.path.exists(temp_cookie_path):
                os.remove(temp_cookie_path)
            
            ongoing_downloads.pop(user_id, None)

    async def handle_dl(self, event):
        """Handle /dl command"""
        try:
            parts = event.message.text.split()
            if len(parts) < 2:
                await event.reply("**Usage:** `/dl <video_url>`")
                return
            
            url = parts[1]
            
            if "instagram.com" in url:
                await self.process_video(event, url, "INSTA_COOKIES", check_duration_and_size=False)
            elif "youtube.com" in url or "youtu.be" in url:
                await self.process_video(event, url, "YT_COOKIES", check_duration_and_size=True)
            else:
                await self.process_video(event, url, None, check_duration_and_size=False)
                
        except Exception as e:
            await event.reply(f"**Error:** {str(e)}")

    async def handle_adl(self, event):
        """Handle /adl command"""
        try:
            parts = event.message.text.split()
            if len(parts) < 2:
                await event.reply("**Usage:** `/adl <audio_url>`")
                return
            
            url = parts[1]
            
            if "instagram.com" in url:
                await self.process_audio(event, url, "INSTA_COOKIES")
            elif "youtube.com" in url or "youtu.be" in url:
                await self.process_audio(event, url, "YT_COOKIES")
            else:
                await self.process_audio(event, url, None)
                
        except Exception as e:
            await event.reply(f"**Error:** {str(e)}")

# Main execution
if __name__ == "__main__":
    # Import config
    from config import API_ID, API_HASH, BOT_TOKEN, INSTA_COOKIES, YT_COOKIES
    
    # Create and run bot
    bot = DownloadBot(API_ID, API_HASH, BOT_TOKEN)
    
    # Run bot
    import asyncio
    asyncio.run(bot.start())
