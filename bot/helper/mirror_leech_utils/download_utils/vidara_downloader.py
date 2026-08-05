import asyncio
import os
import random
import re
import time
from secrets import token_urlsafe

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

_ID_RE = re.compile(r"vidara\.(?:so|to)/(?:v|e)/([a-zA-Z0-9]+)")


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
        if self.listener.is_cancelled:
            return
        for attempt in range(3):
            try:
                async with client.stream(
                    "GET", url, timeout=30.0
                ) as response:
                    if response.status_code not in (200, 206):
                        raise Exception(
                            f"HTTP status {response.status_code}"
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
                return
            except Exception as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 * (attempt + 1))

    async def _download_playlist(self, client, master_url, temp_dir):
        resp = await client.get(master_url, timeout=20.0)
        if resp.status_code not in (200, 206):
            raise ValueError(
                f"Failed to fetch master playlist (HTTP {resp.status_code})"
            )
        content = resp.text
        variant_lines = [
            ln.strip() for ln in content.splitlines() if ln.strip()
        ]
        variant_url = variant_lines[-1] if variant_lines else ""

        playlist_url = variant_url if variant_url.startswith("http") else (
            master_url.rsplit("/", 1)[0] + "/" + variant_url
        )
        resp = await client.get(playlist_url, timeout=20.0)
        if resp.status_code not in (200, 206):
            raise ValueError(f"Failed to fetch media playlist (HTTP {resp.status_code})")

        seg_urls = []
        for ln in resp.text.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            seg_urls.append(
                ln if ln.startswith("http") else playlist_url.rsplit("/", 1)[0] + "/" + ln
            )
        if not seg_urls:
            raise ValueError("No segments found in media playlist")

        self.total_segments = len(seg_urls)
        self.completed_segments = 0

        sem = asyncio.Semaphore(6)
        self._temp_dir = temp_dir

        async def worker(url, idx):
            async with sem:
                await self._download_segment(client, url, idx, temp_dir)
                self.completed_segments += 1

        await asyncio.gather(*(worker(u, i) for i, u in enumerate(seg_urls)))
        self._seg_urls = seg_urls

    async def _make_thumbnail(self, video_path, thumb_path):
        """Snapshot 4 random frames from the video and tile them into a
        2x2 grid thumbnail (used only for leech)."""
        try:
            out, err, code = await cmd_exec(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", video_path]
            )
            duration = float(out.strip() or 0)
            if duration <= 0:
                return None
            # 4 random timestamps, spaced apart, avoiding the very edges
            step = max(duration / 10, 1.0)
            low, high = step, duration - step
            if high <= low:
                return None
            picks = sorted(random.uniform(low, high) for _ in range(4))
            times = ",".join(f"{t:.2f}" for t in picks)

            tmp_dir = os.path.join(os.path.dirname(thumb_path), "frames")
            await makedirs(tmp_dir, exist_ok=True)
            for i in range(4):
                await cmd_exec(
                    [
                        "ffmpeg", "-y", "-ss", f"{picks[i]:.2f}", "-i", video_path,
                        "-vframes", "1", "-q:v", "2",
                        os.path.join(tmp_dir, f"f{i}.jpg"),
                    ]
                )
            frames = [os.path.join(tmp_dir, f"f{i}.jpg") for i in range(4)]
            if not all(os.path.exists(f) for f in frames):
                return None

            # tile 2x2 (scale to uniform height first — ffmpeg 6.x hstack
            # requires same height; `gap` option unsupported on this build)
            out, err, code = await cmd_exec(
                [
                    "ffmpeg", "-y",
                    "-i", frames[0], "-i", frames[1], "-i", frames[2], "-i", frames[3],
                    "-filter_complex",
                    "[0]scale=480:-1[a];[1]scale=480:-1[b];[2]scale=480:-1[c];"
                    "[3]scale=480:-1[d];[a][b]hstack[t];[c][d]hstack[bt];"
                    "[t][bt]vstack",
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
                verify=False, follow_redirects=True, timeout=30.0
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

        # khusus leech: snapshot thumbnail dari video
        if getattr(self.listener, "is_leech", False):
            thumb_dir = os.path.join(self._path, "_vidara_thumb")
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
