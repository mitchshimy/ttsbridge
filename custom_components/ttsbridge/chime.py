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

IMPORTANT - cache-first, not cache-after: compute_chime_cache_key() and
try_get_cached_url() are cheap (a stat + a disk-existence check, no
network, no engine call) and are meant to be called BEFORE ever resolving
the message through HA's TTS entity. If a combined file already exists
here from a previous run, notify.py skips the resolve step entirely -
which means the actual Homeway/engine call, and whatever quota it costs,
never happens at all for anything that's ever been generated once. This
matters concretely: HA's own config/tts cache turned out to be something
HA manages and can clear on its own (observed directly - a chime-combined
cache here survived fully intact while config/tts was wiped out from
under it), so this directory - not config/tts - is the actually durable
one. async_render() below is only ever reached on a genuine cache miss,
after a real (quota-costing) resolve has already succeeded.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

from .const import (
    CONF_DEGRADED_RESPONSE_DETECTION_ENABLED,
    DEFAULT_DEGRADED_RESPONSE_DETECTION_ENABLED,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CHIME_SOURCE_DIR = "ttsbridge_chimes"          # config/www/ttsbridge_chimes/<chime_id>.mp3 - you supply these
CHIME_CACHE_DIR = "ttsbridge_chimes_cache"      # config/www/ttsbridge_chimes_cache/<hash>.mp3 - we generate these, durable
FFMPEG_TIMEOUT_S = 15

_SIGNATURES_PATH = os.path.join(os.path.dirname(__file__), "known_bad_signatures.json")


SIGNATURE_LOG_DIR = "ttsbridge_signature_log"   # config/www/ttsbridge_signature_log/ - auto-archived samples + log.json
POISON_GENERATION_DIR = "ttsbridge_poison_generations"  # config/www/ttsbridge_poison_generations/ - per-message poison counters


class PoisonedSynthesis(Exception):
    """Raised when a freshly-resolved TTS clip's audio content matches a
    known degraded/quota-limit response instead of genuine synthesis of
    the requested message. Deliberately a distinct type from any other
    failure in this module - a network hiccup or missing chime asset
    should still let the (good) audio play un-chimed as a graceful
    degradation; a poisoned response should NOT play as if it were the
    real message, and must never be written to the durable cache, or
    every future request for this exact message plays this same bad
    clip forever (this is exactly what happened before this check
    existed - see the fix that added this class for the full story)."""

    def __init__(self, signature: str, label: str | None = None):
        self.signature = signature
        self.label = label
        super().__init__(f"Detected known-bad synthesis signature {signature} ({label or 'unlabeled'})")


_known_bad_cache: dict[str, str] | None = None


def _read_known_bad_signatures_sync() -> dict[str, str]:
    try:
        with open(_SIGNATURES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


async def _load_known_bad_signatures(hass: HomeAssistant) -> dict[str, str]:
    """Loaded once per HA run, not per-call - this file rarely changes and
    the check itself needs to be cheap since it runs on every genuine
    cache miss. Runs the actual file read in the executor rather than
    calling open() directly here - a plain synchronous open() inside an
    async function is exactly the kind of blocking-the-event-loop call
    HA's own dev tooling flags and asks for a bug report over. Caching
    is done manually with a plain module-level variable rather than
    @functools.lru_cache, since lru_cache on an async function caches
    the coroutine object itself, not its result - calling a cached
    coroutine a second time doesn't return the cached value, it raises
    (a coroutine can only be awaited once)."""
    global _known_bad_cache
    if _known_bad_cache is None:
        _known_bad_cache = await hass.async_add_executor_job(_read_known_bad_signatures_sync)
    return _known_bad_cache


def _load_signature_log(log_path: str) -> dict:
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save_signature_log(log_dir: str, log_path: str, data: dict) -> None:
    os.makedirs(log_dir, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _archive_sample(log_dir: str, src_path: str, dest_path: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    shutil.copyfile(src_path, dest_path)


def _load_poison_generations(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save_poison_generations(gen_dir: str, path: str, data: dict) -> None:
    os.makedirs(gen_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


async def _get_poison_generation(hass: HomeAssistant, base_cache_key: str) -> int:
    """Current poison generation for this message+engine (base_cache_key,
    from notify.py's sha256(message|ha_tts_target) - NOT chime_cache_key,
    which also folds in the chime and would give every chime variant of
    the same message its own independent, wrong generation count).
    Defaults to 0 - the overwhelming majority of messages have never
    been caught poisoned, and 0 is deliberately excluded from the cache
    key entirely (see compute_chime_cache_key) so their existing cache
    entries - HA-side AND already on every Android device - stay valid
    and unaffected by this system existing at all."""
    gen_dir = hass.config.path("www", POISON_GENERATION_DIR)
    path = os.path.join(gen_dir, "generations.json")
    data = await hass.async_add_executor_job(_load_poison_generations, path)
    return data.get(base_cache_key, 0)


async def _bump_poison_generation(hass: HomeAssistant, base_cache_key: str) -> int:
    """Called whenever _check_signature is about to raise PoisonedSynthesis
    for this message - registered match or auto-detected collision,
    either way. Returns the new generation number.

    Why this needs to exist at all: HA's own durable cache and detection
    system can be perfectly correct, but the Android bridge app keeps its
    OWN independent local SpeechCache, keyed on this exact same
    base_cache_key-derived value (see SpeechCache.java's class docstring
    on the Android side - it deliberately trusts whatever key HA hands
    it rather than computing its own). A device that ever cached a
    poisoned result locally will keep serving that exact stale copy
    forever for this message, completely untouched by anything done
    HA-side - deleting the durable cache file, registering new
    signatures, none of it ever reaches that device's local cache, since
    the app never re-asks HA once it already has a hit for that key.

    Bumping the generation changes what compute_chime_cache_key produces
    for this message from this point on. Since the Android app has no
    way to distinguish "genuinely new content" from "same content, new
    key" - it just sees a key it's never seen before - this transparently
    busts the stale local entry on every device that has one, the next
    time this message is triggered, with zero changes needed on the
    Android side at all."""
    gen_dir = hass.config.path("www", POISON_GENERATION_DIR)
    path = os.path.join(gen_dir, "generations.json")
    data = await hass.async_add_executor_job(_load_poison_generations, path)
    new_gen = data.get(base_cache_key, 0) + 1
    data[base_cache_key] = new_gen
    await hass.async_add_executor_job(_save_poison_generations, gen_dir, path, data)
    return new_gen


def _is_detection_enabled(hass: HomeAssistant) -> bool:
    """Reads the options-flow toggle (Settings > Devices & Services >
    TTS Bridge > Configure) governing whether ANY of the degraded-
    response detection in this module runs at all - known_bad_signatures
    matching, auto collision detection, poison-generation cache-busting,
    all of it. Defaults to enabled if unset (fresh installs, or entries
    that predate this option existing) - the mechanism is generic and
    effectively free when idle, so most users benefit without ever
    needing to know this exists. Explicitly disabled restores the exact
    original behavior: every resolve is trusted and cached as-is,
    identical to how this integration behaved before any of this system
    was built.

    Checked per-call rather than cached, unlike known_bad_signatures -
    this is a plain in-memory dict read off config_entries (no file I/O,
    no executor hop needed), and needs to reflect a toggle flip
    immediately without requiring a restart.

    Multiple config entries (one per physical bridge device) can exist -
    if ANY of them has this explicitly turned off, detection is treated
    as off globally. Poisoning is a property of the TTS ENGINE, not of
    which device is playing the announcement, so a per-device split
    verdict wouldn't mean anything real; erring toward "off wins" means
    a user disabling it on any one device's Configure page reliably gets
    the simpler behavior everywhere, rather than being surprised it's
    still partially active somewhere they didn't check."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if not entry.options.get(
            CONF_DEGRADED_RESPONSE_DETECTION_ENABLED, DEFAULT_DEGRADED_RESPONSE_DETECTION_ENABLED
        ):
            return False
    return True


async def _check_signature(
    hass: HomeAssistant, audio_path: str, message: str | None, base_cache_key: str | None = None
) -> None:
    """Shared by async_check_tts_url and async_render's own internal
    fallback check (reached only when async_render is called without a
    pre-fetched speech_path). Raises PoisonedSynthesis in two cases:

    1. audio_path's content matches an entry already registered in
       known_bad_signatures.json - the original, manually-curated check.

    2. This exact signature has previously been produced by a DIFFERENT
       message than the one just requested. A genuine synthesis of two
       different messages should essentially never produce byte-identical
       audio by chance - seeing the same signature attached to a second,
       different message is strong automatic evidence of a new
       degraded/canned response, even one never manually registered.

    Case 2 is what actually solves the sample-collection problem this
    was built to fix: previously, catching a genuinely new degraded
    variant meant racing to grab the raw file out of HA's own config/tts
    cache before HA cleared it on its own - often too slow. The first
    time an unfamiliar signature shows up, there's nothing to compare it
    against yet, so it's just logged and let through (indistinguishable
    at that point from a real message being spoken for the first time).
    The SECOND time that same signature shows up attached to different
    text, this permanently archives the audio itself under
    SIGNATURE_LOG_DIR - so there's no ephemeral file to race against
    ever again - and refuses the occurrence exactly like a registered
    match. message=None skips this second check entirely (falls back to
    only the known_bad_signatures.json check) - used when a caller
    doesn't have the original message text handy.

    Message text itself is never persisted, only its SHA-256 - keeps the
    log small and avoids accumulating a growing plaintext transcript of
    everything ever spoken."""
    if not _is_detection_enabled(hass):
        return

    signature = await _async_pcm_signature(audio_path)

    known_bad = await _load_known_bad_signatures(hass)
    if signature in known_bad:
        label = known_bad[signature]
        _LOGGER.error(
            "Matched known-bad signature '%s' (%s) - refusing to cache or play this",
            signature, label,
        )
        if base_cache_key:
            new_gen = await _bump_poison_generation(hass, base_cache_key)
            _LOGGER.error(
                "Bumped poison generation for this message to %d - any Android device with a "
                "stale local cache entry for the old key will fetch fresh next time, no "
                "app-side changes needed.", new_gen,
            )
        raise PoisonedSynthesis(signature, label)

    if message is None:
        return

    log_dir = hass.config.path("www", SIGNATURE_LOG_DIR)
    log_path = os.path.join(log_dir, "log.json")
    message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()

    log = await hass.async_add_executor_job(_load_signature_log, log_path)
    entry = log.get(signature)

    if entry is None:
        # First time this signature has ever been seen - nothing to
        # compare against yet, indistinguishable from a real message
        # being spoken for the first time. Just record it, then check
        # the REVERSE direction below before returning.
        log[signature] = {"message_hashes": [message_hash], "sample_path": None}
        await hass.async_add_executor_job(_save_signature_log, log_dir, log_path, log)
        _check_reverse_collision(log, signature, message_hash, known_bad)
        return

    if message_hash in entry["message_hashes"]:
        return  # same message resolving to the same result again - expected, not suspicious

    # COLLISION: this exact audio has now been produced by two DIFFERENT
    # messages - auto-archive a sample (once) and refuse this occurrence.
    sample_path = entry.get("sample_path")
    if not sample_path:
        sample_path = os.path.join(log_dir, f"{signature}.mp3")
        await hass.async_add_executor_job(_archive_sample, log_dir, audio_path, sample_path)
        entry["sample_path"] = sample_path

    entry["message_hashes"].append(message_hash)
    await hass.async_add_executor_job(_save_signature_log, log_dir, log_path, log)

    _LOGGER.error(
        "AUTO-DETECTED a new suspected degraded/limit signature '%s' - identical audio "
        "produced by %d different messages now. Sample permanently archived at %s - "
        "review it and, if confirmed, add '%s' to known_bad_signatures.json.",
        signature, len(entry["message_hashes"]), sample_path, signature,
    )
    if base_cache_key:
        new_gen = await _bump_poison_generation(hass, base_cache_key)
        _LOGGER.error(
            "Bumped poison generation for this message to %d - any Android device with a "
            "stale local cache entry for the old key will fetch fresh next time, no "
            "app-side changes needed.", new_gen,
        )
    raise PoisonedSynthesis(
        signature, f"auto-detected via message collision, sample archived at {sample_path}"
    )


def _check_reverse_collision(log: dict, signature: str, message_hash: str, known_bad: dict) -> None:
    """The other direction from the main collision check above: has this
    exact MESSAGE ever resolved to a DIFFERENT signature before. Weaker
    evidence than the forward check - real speech can legitimately sound
    different across two resolves months apart (e.g. Homeway updating
    its voice model), so this deliberately does NOT raise
    PoisonedSynthesis on its own. It only logs - loudly, at ERROR level,
    impossible to miss - so there's nothing left to hunt down with a
    separate manual scan, but a human still makes the actual call on
    which side (if either) is really degraded.

    Resolves itself with zero noise when the answer is already obvious:
    if the OTHER signature is already a registered known-bad entry, this
    occurrence being a DIFFERENT signature is exactly the expected,
    healthy outcome (proof this resolve is clean) - nothing to flag.
    Only genuinely ambiguous cases (neither side registered) get
    surfaced."""
    other_signatures = {
        other_sig for other_sig, other_entry in log.items()
        if other_sig != signature and message_hash in other_entry.get("message_hashes", [])
    }
    if not other_signatures:
        return

    if signature in known_bad or any(s in known_bad for s in other_signatures):
        return  # one side already confirmed - not ambiguous, nothing to flag

    _LOGGER.error(
        "REVERSE COLLISION: the same message has now resolved to %d different, unregistered "
        "signatures across separate resolves (including this one: '%s') - one of them is "
        "likely a degraded response, but which one can't be determined automatically. "
        "Compare samples and register whichever is confirmed bad in known_bad_signatures.json. "
        "Other signature(s) involved: %s",
        len(other_signatures) + 1, signature, sorted(other_signatures),
    )


async def compute_chime_cache_key(hass: HomeAssistant, base_cache_key: str, chime_id: str) -> str | None:
    """Cheap, no-network. Returns the combined (message+engine+chime) cache
    key, or None if the chime asset itself doesn't exist (logged here, once,
    regardless of which caller - resolve path or cache-check path - hits it
    first).

    base_cache_key is the plain (message, engine) key notify.py already
    computed. The chime's own identity (id + the source file's mtime) gets
    folded in here, so: same message + same chime file -> same key as
    always (repeats still hit cache exactly like before chimes existed);
    but if you ever swap out the chime audio file, every affected entry's
    key changes on its own, which is what naturally busts both this
    module's own combined-file cache below AND the Android-side
    SpeechCache for it - no manual cache-clearing needed when you change
    the chime sound.

    The poison generation (see _bump_poison_generation) is folded in the
    same way, for the same reason, triggered by a different event: a
    message that's never been caught poisoned gets generation 0, which is
    deliberately left OUT of the hash entirely, so every existing cache
    entry - HA-side and already-cached on every Android device - stays
    exactly as it's always been. Only once THIS specific message is
    caught poisoned does its key change going forward, which transparently
    busts the stale entry everywhere it's cached, without needing to know
    or care how many devices have a copy or where.
    """
    chime_path = hass.config.path("www", CHIME_SOURCE_DIR, f"{chime_id}.mp3")
    try:
        chime_mtime = await hass.async_add_executor_job(_stat_mtime, chime_path)
    except FileNotFoundError:
        _LOGGER.warning(
            "Chime '%s' not found at %s - playing announcement without it", chime_id, chime_path
        )
        return None

    generation = await _get_poison_generation(hass, base_cache_key)
    gen_suffix = f":gen={generation}" if generation else ""

    return hashlib.sha256(
        f"{base_cache_key}|chime={chime_id}:{chime_mtime}{gen_suffix}".encode("utf-8")
    ).hexdigest()


async def try_get_cached_url(hass: HomeAssistant, cache_key: str) -> str | None:
    """Cheap, no-network. Returns the /local/... url for an already-combined
    file if it exists on disk, else None. Call this BEFORE resolving the
    message through HA's TTS entity - a hit here means skipping that
    resolve (and its quota cost) entirely."""
    combined_path = os.path.join(hass.config.path("www", CHIME_CACHE_DIR), f"{cache_key}.mp3")
    exists = await hass.async_add_executor_job(os.path.exists, combined_path)
    if not exists:
        return None
    _LOGGER.debug("Chime+TTS combo already cached for key=%s, skipping HA TTS resolve entirely", cache_key)
    return f"{get_url(hass, prefer_external=False, allow_internal=True)}/local/{CHIME_CACHE_DIR}/{cache_key}.mp3"


async def async_check_tts_url(
    hass: HomeAssistant, tts_url: str, message: str | None = None, base_cache_key: str | None = None
) -> str:
    """Downloads tts_url and verifies it isn't a known-degraded response
    (see _check_signature for both checks this performs). Returns the
    local temp file path on success - NOT deleted, caller now owns
    cleanup (either by handing it to async_render's speech_path param,
    which takes over ownership, or by calling async_discard_temp once
    done with it directly). Kept this way, rather than downloading and
    discarding internally, specifically so a chime-applying call only
    ever downloads and hashes the resolved audio ONCE, whether or not a
    chime ends up getting applied - not once here and again inside
    async_render for the same URL.

    message: pass the original requested text when available - enables
    the automatic collision-based detection of never-before-registered
    degraded variants (see _check_signature). Optional/defaults to None
    for callers that don't have it handy, in which case only the
    known_bad_signatures.json check runs.

    base_cache_key: notify.py's sha256(message|ha_tts_target) - pass it
    when available so a poison hit here also bumps that message's poison
    generation (see _bump_poison_generation), which is what busts a
    stale Android-side local cache entry, not just this module's own
    durable one. Optional - omitting it just means a poison hit here
    still refuses correctly, it just won't change the key going forward.

    Raises PoisonedSynthesis on a match (and cleans up its own temp copy
    before raising, since there's no caller to hand it off to in that
    case).

    Returns None instead of raising if the download itself fails for any
    OTHER reason (e.g. Homeway returning a genuine 500 - it produced no
    audio at all this time, not a degraded-but-valid clip - distinct
    from poisoning, and not something this check can evaluate since
    there's no content to hash). This is not a new failure mode this
    check invented - it's the exact same "can't verify, fall through to
    normal handling" degradation async_render's own download step has
    always had; this just needs the same protection now that it's also
    doing an eager download of its own. Callers already treat None the
    same as "not pre-checked" (it's the same default speech_path=None
    represents), so async_render makes its own attempt and falls back
    the same way it always has, and the no-chime path just proceeds with
    the original, unchecked url - matching pre-poison-check behavior for
    a genuine resolve failure exactly."""
    try:
        temp_path = await _async_download_to_temp(hass, tts_url)
    except Exception:  # noqa: BLE001 - NOT poisoning, a genuine fetch failure - degrade, don't crash
        _LOGGER.warning(
            "Couldn't fetch %s to verify it (Homeway may have failed to produce audio at all "
            "this time) - skipping signature check, falling through to normal handling",
            tts_url, exc_info=True,
        )
        return None

    try:
        await _check_signature(hass, temp_path, message, base_cache_key)
    except PoisonedSynthesis:
        await hass.async_add_executor_job(_silent_remove, temp_path)
        raise
    return temp_path


async def async_discard_temp(hass: HomeAssistant, path: str) -> None:
    """Cleans up a temp file returned by async_check_tts_url when the
    caller ends up NOT handing it to async_render (e.g. no chime applies,
    or the chime asset itself is missing) - render takes ownership of
    cleanup itself once a speech_path is passed into it, so this is only
    needed on the branches that never make that call."""
    await hass.async_add_executor_job(_silent_remove, path)


async def async_render(
    hass: HomeAssistant,
    tts_url: str,
    chime_id: str,
    cache_key: str,
    *,
    speech_path: str | None = None,
    message: str | None = None,
    base_cache_key: str | None = None,
) -> tuple[str, str]:
    """The expensive part - only reached on a genuine cache miss, after a
    real HA TTS resolve has already produced `tts_url`. Downloads it,
    concatenates with the chime via ffmpeg, writes the combined file under
    `cache_key`. Returns (url, cache_key) - the combined version on
    success, or (tts_url, cache_key) unchanged if anything here fails, so a
    broken chime step never loses the announcement itself.

    speech_path: pass this when the caller already downloaded and
    signature-checked tts_url via async_check_tts_url - skips the
    redundant re-download and re-check here, and this function takes
    over ownership of cleaning it up. Left as None, this downloads and
    checks tts_url itself (kept as a real, independent check here too,
    not just a fallback - protects any other/future caller of this
    function directly, not only the one path that currently pre-checks).

    message: only used when speech_path is None (i.e. this function is
    doing its own check) - enables the collision-based detection of
    unregistered signatures in _check_signature. Harmless to omit.

    base_cache_key: NOT the same as the `cache_key` parameter above -
    that one is the combined (message+engine+chime) key used for the
    cache filename; this one is notify.py's plain (message+engine) key,
    needed separately so a poison hit found here (only reached when
    speech_path is None, i.e. this function's own independent check) can
    also bump that message's poison generation - see
    _bump_poison_generation and compute_chime_cache_key's docstring for
    why that matters (busting a stale Android-side local cache entry,
    not just this module's own). Harmless to omit.
    """
    chime_path = hass.config.path("www", CHIME_SOURCE_DIR, f"{chime_id}.mp3")
    cache_dir = hass.config.path("www", CHIME_CACHE_DIR)
    combined_path = os.path.join(cache_dir, f"{cache_key}.mp3")

    owns_speech_path = speech_path is None
    try:
        await hass.async_add_executor_job(os.makedirs, cache_dir, 0o755, True)

        if speech_path is None:
            speech_path = await _async_download_to_temp(hass, tts_url)

            # Only reached when nothing pre-checked this URL already -
            # see docstring. Checked BEFORE ffmpeg concat, BEFORE
            # anything touches combined_path - a match (registered or
            # auto-detected via collision, see _check_signature) means
            # this must not be cached (would poison this cache_key
            # permanently) and must not be treated as a normal
            # chime-failed-fall-back-to-raw-url case either, since that
            # would still play the bad audio, just without a chime on it.
            await _check_signature(hass, speech_path, message, base_cache_key)

        await _async_ffmpeg_concat(chime_path, speech_path, combined_path)
    except PoisonedSynthesis:
        raise  # deliberately NOT caught by the except below - see class docstring
    except Exception:  # noqa: BLE001 - any OTHER failure here should fall back, not propagate
        _LOGGER.warning(
            "Failed to prepend chime '%s', playing announcement without it", chime_id, exc_info=True
        )
        return tts_url, cache_key
    finally:
        # Cleans up regardless of who downloaded it - once a speech_path
        # is in hand here (ours or handed in), this function owns
        # clearing it, whether that ownership was implicit (we
        # downloaded it) or explicit (caller handed it off via the
        # speech_path param).
        if speech_path:
            await hass.async_add_executor_job(_silent_remove, speech_path)

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


async def _async_pcm_signature(speech_path: str) -> str:
    """Decodes the raw downloaded speech clip to a normalized PCM stream and
    MD5-hashes it. Deliberately decodes (rather than hashing the compressed
    mp3 bytes directly) so this is robust to encoder/container differences
    - two files with identical audio but different mp3 encoder settings
    would hash differently as raw bytes but identically once decoded to
    PCM. Same sample rate/channel normalization as the concat step below,
    so this stays consistent if that ever changes."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-i", speech_path,
        "-f", "s16le", "-ar", "44100", "-ac", "1", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT_S)
    if proc.returncode != 0:
        # Don't fail the whole render over a signature-check hiccup - log
        # and treat as "couldn't verify", NOT as "confirmed poisoned".
        _LOGGER.warning(
            "Signature check couldn't decode %s (ffmpeg exit %s), skipping poison check "
            "for this render: %s",
            speech_path, proc.returncode, stderr.decode(errors="replace")[-300:],
        )
        return ""
    return hashlib.md5(stdout).hexdigest()


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
