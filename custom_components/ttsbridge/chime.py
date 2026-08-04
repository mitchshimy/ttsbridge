"""Optional chime-before-TTS support.

Deliberately scoped tight: only wired into the path in notify.py where we
already resolve a message through an HA tts.* entity ourselves (the
ha_tts_target branch) - that's the only place we (a) know the resolved
audio is genuinely cacheable (see notify.py's cache_key comment) and (b)
have a concrete file to hand ffmpeg rather than an opaque external url of
unknown origin. An explicitly-passed `url` or a plain device-TTS `text`
announcement don't get chime support - not a technical wall, just not
worth the extra surface area for content we can't safely cache anyway.

Never fails the announcement outright. If ffmpeg is missing, the chime
asset doesn't exist, or anything else goes wrong, this logs a warning and
the caller gets back the original (un-chimed) url/cache_key so the actual
announcement still plays. A chime is a nice-to-have; losing it should
never mean losing the message.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

_LOGGER = logging.getLogger(__name__)

CHIME_SOURCE_DIR = "ttsbridge_chimes"          # config/www/ttsbridge_chimes/<chime_id>.mp3 - you supply these
CHIME_CACHE_DIR = "ttsbridge_chimes_cache"      # config/www/ttsbridge_chimes_cache/<hash>.mp3 - we generate these
FFMPEG_TIMEOUT_S = 15


async def async_prepend_chime(
    hass: HomeAssistant,
    tts_url: str,
    chime_id: str,
    base_cache_key: str,
) -> tuple[str, str]:
    """Returns (url, cache_key) - either the chimed version, or the original
    pair unchanged if anything about the chime step failed.

    base_cache_key is the plain (message, engine) key notify.py already
    computed. The chime's own identity (id + the source file's mtime) gets
    folded in here, so: same message + same chime file -> same key as
    always (repeats still hit cache exactly like before chimes existed);
    but if you ever swap out the chime audio file, every affected entry's
    key changes on its own, which is what naturally busts both this
    module's own combined-file cache below AND the Android-side
    SpeechCache for it - no manual cache-clearing needed when you change
    the chime sound.
    """
    chime_path = hass.config.path("www", CHIME_SOURCE_DIR, f"{chime_id}.mp3")

    try:
        chime_mtime = await hass.async_add_executor_job(_stat_mtime, chime_path)
    except FileNotFoundError:
        _LOGGER.warning(
            "Chime '%s' not found at %s - playing announcement without it", chime_id, chime_path
        )
        return tts_url, base_cache_key

    cache_key = hashlib.sha256(
        f"{base_cache_key}|chime={chime_id}:{chime_mtime}".encode("utf-8")
    ).hexdigest()

    cache_dir = hass.config.path("www", CHIME_CACHE_DIR)
    combined_path = os.path.join(cache_dir, f"{cache_key}.mp3")

    try:
        already_cached = await hass.async_add_executor_job(os.path.exists, combined_path)
        if already_cached:
            _LOGGER.debug("Chime+TTS combo already cached for key=%s, skipping ffmpeg", cache_key)
        else:
            await hass.async_add_executor_job(os.makedirs, cache_dir, 0o755, True)
            speech_path = await _async_download_to_temp(hass, tts_url)
            try:
                await _async_ffmpeg_concat(chime_path, speech_path, combined_path)
            finally:
                await hass.async_add_executor_job(_silent_remove, speech_path)
    except Exception:  # noqa: BLE001 - genuinely any failure here should fall back, not propagate
        _LOGGER.warning(
            "Failed to prepend chime '%s', playing announcement without it", chime_id, exc_info=True
        )
        return tts_url, base_cache_key

    combined_url = f"{get_url(hass, prefer_external=False, allow_internal=True)}/local/{CHIME_CACHE_DIR}/{cache_key}.mp3"
    return combined_url, cache_key


def _stat_mtime(path: str) -> int:
    return int(os.stat(path).st_mtime)


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def _async_download_to_temp(hass: HomeAssistant, url: str) -> str:
    """Fetches `url` (the resolved tts_proxy url, same-origin HA content) into
    a temp file. Goes through a real HTTP round-trip rather than reaching
    into HA's internal tts cache file layout directly - that's an
    undocumented implementation detail that could change between HA
    versions, whereas fetching the same url the Android bridge itself
    would fetch is guaranteed stable."""
    session = async_get_clientsession(hass)
    fd, path = await hass.async_add_executor_job(_mktemp)
    os.close(fd)
    async with session.get(url) as resp:
        resp.raise_for_status()
        data = await resp.read()
    await hass.async_add_executor_job(_write_bytes, path, data)
    return path


def _mktemp() -> tuple[int, str]:
    import tempfile

    return tempfile.mkstemp(prefix="ttsbridge_chime_src_", suffix=".audio")


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


async def _async_ffmpeg_concat(chime_path: str, speech_path: str, out_path: str) -> None:
    """ffmpeg's filter_complex concat (not the concat demuxer) - robust to the
    chime and the TTS clip having different codecs/sample rates/channel
    counts, which the demuxer approach can't handle without pre-normalizing
    both inputs first."""
    tmp_out = f"{out_path}.tmp"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i", chime_path,
        "-i", speech_path,
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]",
        "-map", "[out]",
        "-f", "mp3",
        tmp_out,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"ffmpeg timed out after {FFMPEG_TIMEOUT_S}s")

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {stderr.decode(errors='replace')[-500:]}")

    # Atomic rename - a failed/killed run never leaves a corrupt file at the
    # real cache path, same reasoning as SpeechCache.java's temp-then-rename.
    await asyncio.get_running_loop().run_in_executor(None, os.replace, tmp_out, out_path)
