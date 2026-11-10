"""Drop-box leecher: drain AUTOLEECH_DIR/queue.txt into real leech tasks.

Producer (scraper/cron/manual) only appends URLs to queue.txt. This monitor
dispatches them one slot at a time, retries failures, and dead-letters a link
after AUTOLEECH_MAX_TRIES so a broken URL never blocks the queue.
"""

import os
from asyncio import Lock, sleep
from os import statvfs
from datetime import datetime, timedelta

from aiofiles import open as aiopen
from aiofiles.os import makedirs, path as aiopath
from apscheduler.triggers.interval import IntervalTrigger

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .. import LOGGER, bot_loop, task_dict, task_dict_lock
from ..core.config_manager import Config
from ..core.telegram_manager import TgClient
from ..helper.ext_utils.leech_history import filter_done, load_history
from ..helper.telegram_helper.message_utils import send_message, send_rss
from .mirror_leech import leech

# own scheduler: rss_monitor() pauses the shared one when rss_dict is empty
scheduler = AsyncIOScheduler(event_loop=bot_loop)
_lock = Lock()
_inflight = {}  # link -> dispatched_at


def _path(name):
    return os.path.join(Config.AUTOLEECH_DIR, name)


def _queue_path():
    return _path("queue.txt")


def _done_path():
    return _path("done.txt")


def _dead_path():
    return _path("dead.txt")


def _tries_path():
    return _path("tries.txt")


async def _read_lines(path):
    if not await aiopath.exists(path):
        return []
    async with aiopen(path, "r") as f:
        return [
            line.strip()
            for line in await f.readlines()
            if line.strip() and not line.strip().startswith("#")
        ]


async def _append(path, text):
    async with _lock:
        async with aiopen(path, "a") as f:
            await f.write(f"{text}\n")


async def _read_tries():
    tries = {}
    for line in await _read_lines(_tries_path()):
        link, _, count = line.partition("\t")
        try:
            tries[link.strip()] = int(count)
        except ValueError:
            continue
    return tries


async def _write_tries(tries):
    async with _lock:
        async with aiopen(_tries_path(), "w") as f:
            for link, count in tries.items():
                await f.write(f"{link}\t{count}\n")


async def _ensure_dir():
    if not await aiopath.exists(Config.AUTOLEECH_DIR):
        await makedirs(Config.AUTOLEECH_DIR, exist_ok=True)


async def _chat_target():
    """Return (chat_id, thread_id) for the synthetic command message."""
    raw = Config.AUTOLEECH_CHAT or Config.LEECH_DUMP_CHAT
    if not raw:
        return None, None
    if "|" in raw:
        chat, thread = raw.split("|", 1)
    else:
        chat, thread = raw, ""
    chat = int(chat) if chat.lstrip("-").isdigit() else chat
    thread = int(thread) if str(thread).lstrip("-").isdigit() else None
    return chat, thread


async def _active_count():
    async with task_dict_lock:
        return len(task_dict)


async def _reap():
    """Resolve dispatched mids that left task_dict: success or failure."""
    if not _inflight:
        return
    history = await load_history()
    async with task_dict_lock:
        active = {
            getattr(st, 'listener', None).link
            for st in task_dict.values()
            if getattr(getattr(st, 'listener', None), 'link', None)
        }
    finished = [link for link in _inflight if link not in active]
    if not finished:
        return
    tries = await _read_tries()
    for link in finished:
        _inflight.pop(link, None)
        if link in history:
            await _append(_done_path(), link)
            tries.pop(link, None)
            LOGGER.info(f"AutoLeech done: {link}")
        else:
            count = tries.get(link, 0) + 1
            if count >= Config.AUTOLEECH_MAX_TRIES:
                await _append(_dead_path(), link)
                tries.pop(link, None)
                LOGGER.warning(f"AutoLeech dead after {count} tries: {link}")
            else:
                tries[link] = count
                LOGGER.warning(f"AutoLeech failed ({count}): {link}")
    await _write_tries(tries)


def _free_gb():
    st = statvfs("/")
    return st.f_bavail * st.f_frsize / (1024**3)


async def autoleech_monitor():
    if not Config.AUTOLEECH:
        return
    try:
        await _ensure_dir()
        await _reap()

        slots = (Config.QUEUE_ALL or 3) - await _active_count()
        if slots <= 0:
            return
        free = _free_gb()
        if free < Config.AUTOLEECH_MIN_FREE_GB:
            LOGGER.warning(
                f"AutoLeech paused: only {free:.1f} GB free "
                f"(min {Config.AUTOLEECH_MIN_FREE_GB} GB)"
            )
            return

        done = set(await _read_lines(_done_path()))
        dead = set(await _read_lines(_dead_path()))
        queued = await _read_lines(_queue_path())
        if not queued:
            return

        pending = [link for link in queued if link not in done and link not in dead]
        pending = list(dict.fromkeys(pending))
        pending = [link for link in pending if link not in _inflight]
        pending = await filter_done(pending)
        if not pending:
            return

        chat, thread = await _chat_target()
        if chat is None:
            LOGGER.error("AutoLeech: AUTOLEECH_CHAT / LEECH_DUMP_CHAT kosong, tidak bisa dispatch.")
            return

        user = await TgClient.bot.get_users(Config.OWNER_ID)
        for link in pending[:slots]:
            msg = await send_rss(f"/l {link}", chat, thread)
            if isinstance(msg, str):
                LOGGER.error(f"AutoLeech: gagal kirim command untuk {link}: {msg}")
                break
            msg.from_user = user
            _inflight[link] = datetime.now()
            await leech(TgClient.bot, msg)
            LOGGER.info(f"AutoLeech dispatch: {link}")
            await sleep(Config.AUTOLEECH_DISPATCH_GAP)
    except Exception as e:
        LOGGER.error(f"AutoLeech monitor error: {e}")


async def autoleech_status(_, message):
    await _ensure_dir()
    queued = await _read_lines(_queue_path())
    done = await _read_lines(_done_path())
    dead = await _read_lines(_dead_path())
    tries = await _read_tries()
    await send_message(
        message,
        "<b>AutoLeech</b>\n"
        f"Enabled: <code>{Config.AUTOLEECH}</code>\n"
        f"Queue: <code>{len(queued)}</code>\n"
        f"Running: <code>{len(_inflight)}</code>\n"
        f"Done: <code>{len(done)}</code>\n"
        f"Dead: <code>{len(dead)}</code>\n"
        f"Retrying: <code>{len(tries)}</code>\n"
        f"Free disk: <code>{_free_gb():.1f} GB</code> "
        f"(min {Config.AUTOLEECH_MIN_FREE_GB})\n"
        f"Dir: <code>{Config.AUTOLEECH_DIR}</code>",
    )


def add_job():
    if not scheduler.running:
        scheduler.start()
    scheduler.add_job(
        autoleech_monitor,
        trigger=IntervalTrigger(seconds=Config.AUTOLEECH_INTERVAL),
        id="autoleech",
        name="AutoLeech",
        misfire_grace_time=30,
        max_instances=1,
        next_run_time=datetime.now() + timedelta(seconds=30),
        replace_existing=True,
    )


add_job()
