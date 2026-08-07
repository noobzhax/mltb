import asyncio
import os
import random
import re
import time
from secrets import token_urlsafe
from urllib.parse import urlsplit

import aiofiles
from aiofiles.os import makedirs, remove as aioremove
from aioshutil import rmtree
from httpx import AsyncClient

from .... import LOGGER, task_dict, task_dict_lock
from ...ext_utils.bot_utils import cmd_exec
from ...ext_utils.status_utils import MirrorStatus
from ...ext_utils.task_manager import check_running_tasks, stop_duplicate_check
from ...mirror_leech_utils.status_utils.queue_status import QueueStatus
from ...mirror_leech_utils.status_utils.vidara_status import VidaraStatus
from ...telegram_helper.message_utils import send_status_message

VIDARA_API = "https://vidara-api.pakai.eu.org/api/extract"

_ID_RE = re.compile(r"vidara\.[a-zA-Z0-9.-]+/(?:[a-zA-Z0-9]+/)*([a-zA-Z0-9]+)")


class VidaraDownloader:
    def __init__(self, listener, path):
        self.listener = listener
        self._path = path
        self.processed_bytes = 0
        self.speed = 0
        self.completed_segments = 0
        self.total_segments = 0
        self.is_downloading = False
        self.start_time = None
        self.gid = token_urlsafe(10)
        self.name = ""
        self.filecode = ""
        self._master_url = ""
        self._playlist_url = ""
        self._seg_urls = []
        
        # Speed calculation tracking
        self._last_check_time = 0
        self._last_bytes = 0

    @property
    def estimated_total_size(self):
        if self.completed_segments > 0 and self.total_segments > 0:
            return int(
                (self.processed_bytes / self.completed_segments)
                * self.total_segments
            )
        return 0

    async def _fetch_stream_info(self, client):
        """Call the Vidara extract API; returns dict with streaming_url,
        title, thumbnail, subtitles. Raises ValueError with a user-friendly
        message on any failure."""
        match = _ID_RE.search(self.listener.link)
        if not match:
            raise ValueError("Invalid Vidara URL format")
        filecode = match.group(1)
        self.filecode = filecode

        try:
            resp = await client.post(
                VIDARA_API, json={"filecode": filecode}, timeout=30.0
            )
        except Exception as e:
            raise ValueError(f"Vidara API unreachable: {e}")

        if resp.status_code != 200:
            raise ValueError(
                f"Vidara API error (HTTP {resp.status_code})"
            )

        try:
            data = resp.json()
        except Exception:
            raise ValueError("Vidara API returned invalid JSON")

        if not isinstance(data, dict):
            raise ValueError("Vidara API returned unexpected payload")

        stream_url = (data.get("streaming_url") or "").strip()
        if not stream_url:
            raise ValueError(
                "No streaming_url returned by Vidara API — video may be private or expired"
            )

        return data

    async def _download_segment(self, client, url, index, temp_dir):
        """Download single segment with enhanced error handling for token expiry"""
        if self.listener.is_cancelled:
            return
        
        # Increased retry attempts for better reliability
        for attempt in range(5):
            try:
                async with client.stream(
                    "GET", url, timeout=60.0  # Increased timeout for large segments
                ) as response:
                    status_code = response.status_code
                    
                    # Detect token expiry errors specifically (403, 404, 504)
                    if status_code in (403, 404, 504):
                        if attempt == 4:  # Last attempt
                            error_msg = f"Token expired or segment unavailable (HTTP {status_code}) after 5 retries"
                            LOGGER.error(error_msg)
                            raise Exception(error_msg)
                        
                        # Token expiry detected - need to wait before retry
                        wait_time = 2 ** attempt  # Exponential backoff: 2, 4, 8, 16s
                        LOGGER.warning(
                            f"Segment {index} failed with HTTP {status_code}, "
                            f"retrying in {wait_time}s (attempt {attempt + 1}/5)"
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    
                    if status_code != 200:
                        raise Exception(
                            f"HTTP status {status_code}"
                        )
                    
                    file_path = os.path.join(temp_dir, f"seg_{index:05d}.ts")
                    async with aiofiles.open(file_path, "wb") as f:
                        async for chunk in response.aiter_bytes(
                            chunk_size=32768
                        ):
                            if self.listener.is_cancelled:
                                return
                            await f.write(chunk)
                            self.processed_bytes += len(chunk)
                            # Update speed tracking every 1MB (1_048_576 bytes)
                            if self.processed_bytes - self._last_bytes >= 1_048_576:
                                current_time = time.time()
                                time_diff = current_time - self._last_check_time
                                if time_diff > 0 and self.start_time:
                                    bytes_diff = self.processed_bytes - self._last_bytes
                                    self.speed = bytes_diff / time_diff
                                self._last_check_time = current_time
                                self._last_bytes = self.processed_bytes
                    return
            except asyncio.TimeoutError:
                if attempt == 4:
                    raise
                wait_time = 2 ** attempt
                LOGGER.warning(
                    f"Segment {index} timeout, retrying in {wait_time}s"
                )
                await asyncio.sleep(wait_time)
            except Exception as e:
                if attempt == 4:
                    raise
                wait_time = 2 ** attempt
                LOGGER.warning(
                    f"Segment {index} failed: {str(e)}, retrying in {wait_time}s"
                )
                await asyncio.sleep(wait_time)

    @staticmethod
    def _url_base(url):
        """Directory of a URL, ignoring any query string (tokens may
        contain '/' which breaks naive rsplit('/'))."""
        parts = urlsplit(url)
        path = parts.path.rsplit("/", 1)[0]
        return f"{parts.scheme}://{parts.netloc}{path}"

    async def _refresh_playlist(self, client):
        """Fetch media playlist with proper error handling and quality selection"""
        LOGGER.info("[Vidara] Starting to fetch master playlist...")
        
        # Fetch variant playlist from master
        resp = await client.get(self._master_url, timeout=30.0)
        if resp.status_code not in (200, 206):
            raise ValueError(
                f"Failed to fetch master playlist (HTTP {resp.status_code})"
            )
        content = resp.text
        
        LOGGER.info(f"[Vidara] Master playlist content preview:\n{content[:500]}...")
        
        # Parse master playlist for variants
        variant_entries = []
        variant_lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        
        LOGGER.info(f"[Vidara] Found {len(variant_lines)} lines in master playlist")
        
        # Build base URL for resolving relative paths
        from urllib.parse import urljoin
        try:
            base_url = self._master_url.rsplit('/', 1)[0] + '/'
        except:
            base_url = None
        
        for i, line in enumerate(variant_lines):
            # Check if line is a playlist URL (absolute or relative)
            is_http = line.startswith("http")
            contains_playlist = "/playlist/" in line.lower()
            is_relative_url = line and not line.startswith("#")  # Any non-comment line
            
            if is_http or contains_playlist or is_relative_url:
                # If it's a relative URL, convert to absolute
                actual_url = line
                if is_relative_url and not is_http and base_url:
                    actual_url = urljoin(base_url, line)
                
                variant_entries.append(actual_url)
                LOGGER.info(f"[Vidara] Variant #{i}: {actual_url}")
        
        if not variant_entries:
            raise ValueError("No variant playlists found in master playlist")
        
        LOGGER.info(f"[Vidara] Total valid variant URLs found: {len(variant_entries)}")
        
        # PREFER HIGH QUALITY FIRST: Look for keywords in URL/path
        # Try to detect quality levels (720p, 1080p, etc.)
        preferred_variant = None
        
        # Priority order: 1080p > 720p > 480p > other > last fallback
        quality_keywords = ['1080', 'hd', 'high']
        low_quality_keywords = ['low', '360', '480']
        
        selected_reason = ""
        
        for keyword in quality_keywords:
            for entry in variant_entries:
                if keyword.lower() in entry.lower():
                    preferred_variant = entry
                    selected_reason = f"matched quality '{keyword}'"
                    break
            if preferred_variant:
                break
        
        # If no high quality found, try to avoid low quality
        if not preferred_variant:
            for entry in variant_entries:
                skip = False
                for low_kw in low_quality_keywords:
                    if low_kw.lower() in entry.lower():
                        skip = True
                        break
                if not skip:
                    preferred_variant = entry
                    selected_reason = "no low-quality variant available"
                    break
        
        # Fallback to first or last if still not selected
        if not preferred_variant:
            preferred_variant = variant_entries[-1]
            selected_reason = "no quality detected, using last variant by default"
        
        LOGGER.info(f"[Vidara] Selected variant: {preferred_variant} ({selected_reason})")
        
        variant_url = preferred_variant
        playlist_url = variant_url if variant_url.startswith("http") else (
            self._url_base(self._master_url) + "/" + variant_url
        )
        
        self._playlist_url = playlist_url
        
        # Fetch media playlist with retry for token expiry
        for attempt in range(3):
            try:
                LOGGER.info(f"[Vidara] Fetching media playlist (attempt {attempt+1}/3)...")
                resp = await client.get(playlist_url, timeout=30.0)
                if resp.status_code not in (200, 206):
                    if attempt == 2:
                        raise ValueError(f"Failed to fetch media playlist (HTTP {resp.status_code})")
                    LOGGER.warning(
                        f"[Vidara] Media playlist failed (attempt {attempt+1}/3), "
                        f"retrying in {2**attempt}s..."
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue
                
                # Verify segment count before proceeding
                seg_count = sum(1 for ln in resp.text.splitlines() 
                              if ln.strip() and not ln.startswith('#'))
                
                LOGGER.info(f"[Vidara] ✅ Media playlist fetched successfully!")
                LOGGER.info(f"[Vidara] 📊 Total segments: {seg_count}")
                
                # Show first few segment URLs for debugging
                segments = [ln.strip() for ln in resp.text.splitlines() 
                          if ln.strip() and not ln.startswith('#')][:5]
                if segments:
                    LOGGER.info(f"[Vidara] First 5 segments:")
                    for i, seg in enumerate(segments):
                        LOGGER.info(f"[Vidara]   Segment {i}: {seg}")
                
                return resp.text
            except asyncio.TimeoutError:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)

    async def _download_playlist(self, client, master_url, temp_dir):
        """Download all segments with token refresh and resume capability"""
        self._master_url = master_url
        retry_count = 0
        max_retries = 3
        
        while retry_count < max_retries:
            try:
                # Reset tracking for this retry attempt
                self.completed_segments = 0
                self.processed_bytes = 0
                
                # Fetch fresh playlist (handles token expiry)
                playlist_content = await self._refresh_playlist(client)
                if not playlist_content:
                    raise ValueError("Failed to fetch playlist content")
                
                seg_urls = []
                seg_base = self._url_base(self._playlist_url)
                for ln in playlist_content.splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    seg_urls.append(
                        ln if ln.startswith("http") else seg_base + "/" + ln
                    )
                
                if not seg_urls:
                    raise ValueError("No segments found in media playlist")
                
                self.total_segments = len(seg_urls)
                self._seg_urls = seg_urls
                self._temp_dir = temp_dir
                
                LOGGER.info(f"[Vidara] Downloading {self.total_segments} segments (retry {retry_count + 1}/{max_retries})")
                
                # Resume from last successful segment if available
                start_index = 0
                for i in range(len(seg_urls)):
                    seg_path = os.path.join(temp_dir, f"seg_{i:05d}.ts")
                    if os.path.exists(seg_path):
                        start_index = i + 1
                        self.completed_segments += 1
                        self.processed_bytes += os.path.getsize(seg_path)
                
                if start_index >= self.total_segments:
                    LOGGER.info("[Vidara] 🎯 All segments already downloaded successfully!")
                    return
                
                LOGGER.info(f"[Vidara] ➡️ Resuming from segment {start_index}/{self.total_segments}")
                
                # Download remaining segments with concurrency limit
                sem = asyncio.Semaphore(6)  # Keep 6 parallel connections
                
                async def worker(url, idx):
                    async with sem:
                        await self._download_segment(client, url, idx, temp_dir)
                        if not self.listener.is_cancelled:
                            self.completed_segments += 1
                
                # Only download missing segments (resume capability)
                await asyncio.gather(*(
                    worker(u, i + start_index) 
                    for i, u in enumerate(seg_urls[start_index:])
                ))
                
                # Success - exit retry loop
                break
                
            except Exception as e:
                retry_count += 1
                if retry_count >= max_retries:
                    raise
                
                LOGGER.error(
                    f"Playlist download failed (attempt {retry_count}), "
                    f"completed: {self.completed_segments}/{self.total_segments}, "
                    f"error: {str(e)}. Retrying in 5s..."
                )
                
                # Clean up partial downloads before retry
                for i in range(len(seg_urls)):
                    seg_path = os.path.join(temp_dir, f"seg_{i:05d}.ts")
                    if os.path.exists(seg_path):
                        try:
                            await aioremove(seg_path)
                        except OSError as e:
                            LOGGER.warning(f"Failed to remove segment {i}: {e}")
                
                self._seg_urls = []
                
                # Wait before retry
                await asyncio.sleep(5)

    async def _make_thumbnail(self, video_path, thumb_path):
        """Snapshot 9 frames (3 from start zone, 3 from middle, 3 from end),
        draw a small timestamp on each, tile into a tight 3x3 grid.
        Used only for leech."""
        try:
            out, err, code = await cmd_exec(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", video_path]
            )
            duration = float(out.strip() or 0)
            if duration <= 0:
                return None

            # 3 zones: start 5-25%, middle 40-60%, end 75-95%
            def _zone_random(lo_pct, hi_pct):
                lo, hi = duration * lo_pct, duration * hi_pct
                return sorted(random.uniform(lo, hi) for _ in range(3))

            picks = (
                _zone_random(0.05, 0.25)
                + _zone_random(0.40, 0.60)
                + _zone_random(0.75, 0.95)
            )
            if len(picks) != 9 or any(p <= 0 or p >= duration for p in picks):
                return None

            tmp_dir = os.path.join(os.path.dirname(thumb_path), "frames")
            await makedirs(tmp_dir, exist_ok=True)
            font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

            for i, t in enumerate(picks):
                # format timestamp HH:MM:SS
                h, rem = divmod(int(t), 3600)
                m, s = divmod(rem, 60)
                ts = f"{h:02d}:{m:02d}:{s:02d}"
                # write text to file and use textfile= to avoid filter
                # parsing issues with ':' in text= (ffmpeg 8 is stricter)
                ts_file = os.path.join(tmp_dir, f"ts{i}.txt")
                with open(ts_file, "w") as f:
                    f.write(ts)
                await cmd_exec(
                    [
                        "ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", video_path,
                        "-vframes", "1",
                        "-vf", (
                            f"drawtext=fontfile={font}:textfile={ts_file}:"
                            "x=8:y=h-th-8:fontsize=20:fontcolor=white:"
                            "box=1:boxcolor=black@0.5:boxborderw=4"
                        ),
                        "-q:v", "2", os.path.join(tmp_dir, f"f{i}.jpg"),
                    ]
                )
                try:
                    await aioremove(ts_file)
                except OSError:
                    pass
            frames = [os.path.join(tmp_dir, f"f{i}.jpg") for i in range(9)]
            if not all(os.path.exists(f) for f in frames):
                return None

            # tight 3x3 grid (no gap; scale to uniform height — ffmpeg 6.x
            # hstack requires same height; `gap` option unsupported)
            out, err, code = await cmd_exec(
                [
                    "ffmpeg", "-y",
                    "-i", frames[0], "-i", frames[1], "-i", frames[2],
                    "-i", frames[3], "-i", frames[4], "-i", frames[5],
                    "-i", frames[6], "-i", frames[7], "-i", frames[8],
                    "-filter_complex",
                    "[0]scale=360:-1[a0];[1]scale=360:-1[a1];[2]scale=360:-1[a2];"
                    "[3]scale=360:-1[a3];[4]scale=360:-1[a4];[5]scale=360:-1[a5];"
                    "[6]scale=360:-1[a6];[7]scale=360:-1[a7];[8]scale=360:-1[a8];"
                    "[a0][a1][a2]hstack=3[r0];[a3][a4][a5]hstack=3[r1];"
                    "[a6][a7][a8]hstack=3[r2];[r0][r1][r2]vstack=3",
                    "-q:v", "2", thumb_path,
                ]
            )
            for f in frames:
                try:
                    await aioremove(f)
                except OSError:
                    pass
            await rmtree(tmp_dir, ignore_errors=True)
            if code == 0 and os.path.exists(thumb_path):
                return thumb_path
            return None
        except Exception:
            return None

    async def download(self):
        self.is_downloading = True
        self.start_time = time.time()
        # Initialize speed tracking
        self._last_check_time = self.start_time
        self._last_bytes = 0
        
        match = _ID_RE.search(self.listener.link)
        if not match:
            await self.listener.on_download_error("Invalid Vidara URL format")
            return
        filecode = match.group(1)
        self.filecode = filecode

        temp_dir = os.path.join(self._path, "_vidara_temp")
        await makedirs(temp_dir, exist_ok=True)
        final_path = os.path.join(self._path, f"{filecode}.mp4")

        try:
            async with AsyncClient(
                verify=False, follow_redirects=True, timeout=60.0
            ) as client:
                info = await self._fetch_stream_info(client)
                if self.listener.is_cancelled:
                    return
                master_url = info["streaming_url"]

                title = (info.get("title") or "").strip()
                if title:
                    safe_title = re.sub(r'[<>:"/\\|?*]', "_", title).strip()
                    if safe_title:
                        final_path = os.path.join(self._path, f"{safe_title}.mp4")
                        self.name = os.path.basename(final_path)

                await self._download_playlist(client, master_url, temp_dir)
                if self.listener.is_cancelled:
                    return
                
                # Verify all segments were downloaded successfully
                if hasattr(self, 'total_segments') and self.completed_segments < self.total_segments:
                    missing = self.total_segments - self.completed_segments
                    await self.listener.on_download_error(
                        f"Download incomplete: {missing} segments missing "
                        f"({self.completed_segments}/{self.total_segments})"
                    )
                    return

            # concat semua segmen TS -> output.ts, lalu remux ke mp4
            out_ts = os.path.join(temp_dir, "output.ts")
            concat_list = os.path.join(temp_dir, "concat.txt")
            with open(concat_list, "w") as f:
                for i in range(self.total_segments):
                    f.write(f"file 'seg_{i:05d}.ts'\n")

            concat_cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_list,
                "-c",
                "copy",
                out_ts,
            ]
            res_out, res_err, code = await cmd_exec(concat_cmd)
            if code != 0:
                raise ValueError(
                    f"ffmpeg concat failed (code {code}). Stderr: {res_err}"
                )
            for i in range(self.total_segments):
                seg = os.path.join(temp_dir, f"seg_{i:05d}.ts")
                if os.path.exists(seg):
                    await aioremove(seg)

            out_path = os.path.join(temp_dir, "output.mp4")
            remux_cmd = [
                "ffmpeg",
                "-y",
                "-i",
                out_ts,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                out_path,
            ]
            res_out, res_err, code = await cmd_exec(remux_cmd)
            if code != 0:
                raise ValueError(
                    f"ffmpeg remux failed (code {code}). Stderr: {res_err}"
                )
            await aioremove(out_ts)
            if os.path.exists(out_path):
                os.replace(out_path, final_path)
            else:
                raise ValueError("ffmpeg output missing after remux")
        except Exception as e:
            if not self.listener.is_cancelled:
                await self.listener.on_download_error(f"Vidara download failed: {e}")
            await rmtree(temp_dir, ignore_errors=True)
            return
        finally:
            await rmtree(temp_dir, ignore_errors=True)

        if self.listener.is_cancelled:
            await aioremove(final_path, ignore_errors=True)
            return

        if not self.name:
            self.name = os.path.basename(final_path)
        self.listener.name = self.name
        self.listener.size = os.path.getsize(final_path)

        # khusus leech: snapshot thumbnail dari video.
        # simpan DI LUAR path download supaya uploader (walk) tidak
        # meng-upload file thumb sebagai file terpisah.
        if getattr(self.listener, "is_leech", False):
            thumb_dir = "/tmp/vidara_thumbs"
            await makedirs(thumb_dir, exist_ok=True)
            thumb_path = os.path.join(thumb_dir, f"{self.filecode}_thumb.jpg")
            thumb = await self._make_thumbnail(final_path, thumb_path)
            if thumb:
                self.listener.thumb = thumb
                LOGGER.info(f"Vidara thumbnail: {thumb}")

        self.is_downloading = False
        await self.listener.on_download_complete()

    async def cancel_task(self):
        self.listener.is_cancelled = True
        LOGGER.info(f"Cancelling Vidara Download: {self.listener.name}")
        await self.listener.on_download_error("Download Cancelled by User!")


async def add_vidara_download(listener, path):
    downloader = VidaraDownloader(listener, path)
    gid = downloader.gid

    add_to_queue, event = await check_running_tasks(listener)
    if add_to_queue:
        LOGGER.info(f"Added to Queue/Download: {listener.name}")
        async with task_dict_lock:
            task_dict[listener.mid] = QueueStatus(listener, gid, "dl")
        await listener.on_download_start()
        if listener.multi <= 1 and not listener.is_rss:
            await send_status_message(listener.message)
        await event.wait()
        if listener.is_cancelled:
            return

    async with task_dict_lock:
        task_dict[listener.mid] = VidaraStatus(listener, downloader, gid)

    if add_to_queue:
        LOGGER.info(
            f"Start Queued Download with VidaraDownloader: {listener.name}"
        )
    else:
        LOGGER.info(f"Download with VidaraDownloader: {listener.name}")
        await listener.on_download_start()
        if listener.multi <= 1 and not listener.is_rss:
            await send_status_message(listener.message)

    await downloader.download()
