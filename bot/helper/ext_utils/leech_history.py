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


async def load_history():
    global _history
    if _history is None:
        _history = set()
        if await aiopath.exists(HISTORY_FILE):
            try:
                async with aiopen(HISTORY_FILE, "r") as f:
                    _history = {line.strip() for line in await f.readlines() if line.strip()}
            except Exception as e:
                LOGGER.error(f"Leech history read failed: {e}")
    return _history


async def mark_done(link):
    if not link or not link.startswith(("http://", "https://", "magnet:")):
        return
    history = await load_history()
    if link in history:
        return
    history.add(link)
    async with _lock:
        try:
            async with aiopen(HISTORY_FILE, "a") as f:
                await f.write(f"{link}\n")
        except Exception as e:
            LOGGER.error(f"Leech history write failed: {e}")


async def filter_done(links):
    history = await load_history()
    return [link for link in links if link not in history]
