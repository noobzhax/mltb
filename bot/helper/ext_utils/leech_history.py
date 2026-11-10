import re

from asyncio import Lock
from os import path as ospath
from aiofiles import open as aiopen
from aiofiles.os import path as aiopath

from ... import LOGGER
from ...core.config_manager import Config

# Not under DOWNLOAD_DIR: clean_all() wipes that dir on every restart.
# Lives in AUTOLEECH_DIR so the docker volume keeps it across recreates.
HISTORY_FILE = ospath.join(Config.AUTOLEECH_DIR, "leech_history.txt")
_lock = Lock()
_history = None

# Same video arrives as vidara.to/v/ID, vidara.so/v/ID or vidara.to/ID.
# Dedup on the id, not the raw URL, or mirrors get uploaded twice.
_VID_RE = re.compile(
    r"^https?://(?:www\.)?vidara\.[a-z]+/(?:v/|e/)?([A-Za-z0-9]{6,})/?$", re.I
)


def link_key(link):
    """Stable identity for a link: vidara:<id> when possible, else the URL."""
    link = link.strip()
    m = _VID_RE.match(link)
    if m:
        return f"vidara:{m.group(1)}"
    return link


async def load_history():
    global _history
    if _history is None:
        _history = set()
        if await aiopath.exists(HISTORY_FILE):
            try:
                async with aiopen(HISTORY_FILE, "r") as f:
                    _history = {
                        link_key(line)
                        for line in await f.readlines()
                        if line.strip()
                    }
            except Exception as e:
                LOGGER.error(f"Leech history read failed: {e}")
    return _history


async def mark_done(link):
    if not link or not link.startswith(("http://", "https://", "magnet:")):
        return
    key = link_key(link)
    history = await load_history()
    if key in history:
        return
    history.add(key)
    async with _lock:
        try:
            async with aiopen(HISTORY_FILE, "a") as f:
                await f.write(f"{key}\n")
        except Exception as e:
            LOGGER.error(f"Leech history write failed: {e}")


async def filter_done(links):
    history = await load_history()
    return [link for link in links if link_key(link) not in history]
