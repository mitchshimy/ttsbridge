"""Notify platform for TTS Bridge.

Two ways to reach the same entity, deliberately kept separate rather than
overloading one:

 - notify.send_message (via notify.<device>): kept genuinely minimal -
   message only. This is what makes it interoperable with HA's notify
   groups and anything else written against the standard notify interface.
   NotifyEntity.async_send_message's real signature only supports
   message/title (confirmed against source) - there's no data: passthrough
   the way the old legacy BaseNotificationService platform had, so this
   entity does NOT try to carry url/priority/category/engine through here.

 - ttsbridge.announce (a custom entity service, registered on this
   platform): carries everything the Announcement Director actually needs
   - message OR url (at least one required, enforced at the schema level
   via cv.has_at_least_one_key so a malformed call fails instantly in HA
   rather than after a round trip to the device), priority, category,
   engine, speak_timeout. Both message and url can be present together
   (url wins for playback; message is still recorded as a fallback/label
   on the bridge, exactly like BridgeApiClient.announce_audio's
   text_fallback already supports) - not mutually exclusive.

The engine can be reached two ways, kept as two separate fields rather
than overloading one - an HA entity selector scoped to the tts domain
(the thing that gives you the tts.speak-style dropdown) and free text
can't coexist in a single schema field, that's a hard HA constraint, not
a style choice:

 - `engine` (free text): "device", or any id registered via POST
   /engines (bridge-native, resolved by AnnouncementEngine's own
   EngineRegistry/fallback chain) -> passed straight through to
   announce_text(engine=...). Also still accepts a manually-typed
   "tts.<entity_id>" value for backward compatibility, resolved the same
   way as tts_engine below.
 - `tts_engine` (real entity selector, domain: tts): pick any installed
   HA TTS entity - Piper, Google Translate, Homeway Sage, etc. - from an
   actual dropdown instead of memorizing/typing its id. Resolved via HA's
   media_source mechanism into a URL, then handed to announce_audio().
   This is the in-process equivalent of the Director script's
   get_tts_url REST call - confirmed against real HA source
   (generate_media_source_id's actual signature) and empirically
   verified against tts.piper, tts.google_translate_en_com, and
   tts.homeway_sage_free_text_to_speech specifically (not just
   genuinely-different engines from docs examples) before this was
   built. If both tts_engine and engine are given, tts_engine wins, on
   the theory that picking from a dropdown is a more deliberate choice
   than whatever engine happened to be set to.

   media_source.async_resolve_media() has been observed to return a bare
   relative path (e.g. "/api/tts_proxy/xxx.mp3") rather than an absolute
   URL when called without a target entity_id (which we deliberately pass
   as None here, since there's no real media_player involved). A relative
   path is meaningless to the bridge, which is a completely separate
   Android device fetching this URL over the network - so it's rebuilt
   into an absolute URL via get_url() before being handed off. This was
   the actual cause of "engine works, HA reports success, but nothing
   plays and nothing logs an error anywhere" - the failure happens on the
   Android side, opening a URI that was never valid to begin with, well
   after HA had already declared the call a success.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from urllib.parse import urlencode

import voluptuous as vol

from homeassistant.components import media_source
from homeassistant.components.notify import NotifyEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import chime
from .bridge import AnnouncementBridge
from .const import DOMAIN
from .coordinator import TtsBridgeCoordinator

_LOGGER = logging.getLogger(__name__)

ATTR_MESSAGE = "message"
ATTR_URL = "url"
ATTR_PRIORITY = "priority"
ATTR_CATEGORY = "category"
ATTR_ENGINE = "engine"
ATTR_TTS_ENGINE = "tts_engine"
ATTR_SPEAK_TIMEOUT = "speak_timeout"
ATTR_CHIME = "chime"
ATTR_CLEAR_QUEUE = "clear_queue"

SERVICE_ANNOUNCE = "announce"
SERVICE_CANCEL = "cancel"
SERVICE_CHECK_CACHE = "check_cache"
HA_TTS_ENTITY_PREFIX = "tts."

CANCEL_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Optional(ATTR_CLEAR_QUEUE, default=True): cv.boolean,
    }
)

CHECK_CACHE_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Required(ATTR_TTS_ENGINE): cv.entity_id,
        vol.Optional(ATTR_CHIME): cv.string,
    }
)

ANNOUNCE_SCHEMA = vol.All(
    cv.make_entity_service_schema(
        {
            vol.Optional(ATTR_MESSAGE): cv.string,
            vol.Optional(ATTR_URL): cv.string,
            vol.Optional(ATTR_PRIORITY, default="normal"): vol.In(
                ["emergency", "high", "normal", "low"]
            ),
            vol.Optional(ATTR_CATEGORY, default="general"): cv.string,
            vol.Optional(ATTR_ENGINE): cv.string,
            vol.Optional(ATTR_TTS_ENGINE): cv.entity_id,
            vol.Optional(ATTR_SPEAK_TIMEOUT): vol.Coerce(int),
            vol.Optional(ATTR_CHIME): cv.string,
        }
    ),
    cv.has_at_least_one_key(ATTR_MESSAGE, ATTR_URL),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator: TtsBridgeCoordinator = stored["coordinator"]
    bridge: AnnouncementBridge = stored["bridge"]

    async_add_entities([TtsBridgeNotifyEntity(coordinator, bridge, entry)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_ANNOUNCE,
        ANNOUNCE_SCHEMA,
        "async_announce",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        SERVICE_CANCEL,
        CANCEL_SCHEMA,
        "async_cancel",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        SERVICE_CHECK_CACHE,
        CHECK_CACHE_SCHEMA,
        "async_check_cache",
        supports_response=SupportsResponse.ONLY,
    )


class TtsBridgeNotifyEntity(CoordinatorEntity[TtsBridgeCoordinator], NotifyEntity):
    """notify.send_message (minimal) + ttsbridge.announce (rich) for one bridge device."""

    _attr_has_entity_name = True
    _attr_name = "Announce"

    def __init__(
        self, coordinator: TtsBridgeCoordinator, bridge: AnnouncementBridge, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._bridge = bridge
        self._attr_unique_id = f"{entry.entry_id}_notify"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Standard notify interface - deliberately minimal, see module docstring."""
        await self._bridge.async_announce_text(message)

    async def async_announce(self, **kwargs: Any) -> dict[str, Any]:
        """ttsbridge.announce entity service - see module docstring for the full contract."""
        url = kwargs.get(ATTR_URL)
        message = kwargs.get(ATTR_MESSAGE)
        priority = kwargs.get(ATTR_PRIORITY, "normal")
        category = kwargs.get(ATTR_CATEGORY, "general")
        engine = kwargs.get(ATTR_ENGINE)
        tts_engine = kwargs.get(ATTR_TTS_ENGINE)
        timeout_ms = kwargs.get(ATTR_SPEAK_TIMEOUT)
        chime_id = kwargs.get(ATTR_CHIME)

        ha_tts_target = tts_engine or (
            engine if engine and engine.startswith(HA_TTS_ENTITY_PREFIX) else None
        )
        cache_key = None
        if not url and message and ha_tts_target:
            # Same reasoning as always: only safe to compute a cache_key
            # here because we're resolving this url ourselves from
            # (message, ha_tts_target) - identical inputs mean identical
            # audio content, guaranteed by HA's own tts cache. NOT the
            # url itself: HA's tts_proxy issues a fresh token per resolve
            # even on its own cache hit, so this has to be derived from
            # the (message, engine) pair instead.
            cache_key = hashlib.sha256(
                f"{message}|{ha_tts_target}".encode("utf-8")
            ).hexdigest()

            chime_cache_key = None
            if chime_id:
                chime_cache_key = await chime.compute_chime_cache_key(
                    self.hass, cache_key, chime_id
                )

            cached_url = None
            if chime_cache_key:
                cached_url = await chime.try_get_cached_url(self.hass, chime_cache_key)

            if cached_url:
                # Already rendered this exact (message, engine, chime)
                # combination in a previous run - skip the HA TTS resolve
                # entirely, which means skipping whatever it would have
                # cost against the engine's quota. This is what makes a
                # cleared config/tts (which, unlike this cache, HA appears
                # to manage and can clear on its own) a non-event for
                # anything that's ever been generated once.
                url, cache_key = cached_url, chime_cache_key
            else:
                url = await self._async_resolve_ha_tts_url(ha_tts_target, message)

                # Downloaded and checked exactly once here, then handed
                # off to async_render (which takes over ownership of
                # cleanup) whenever a chime actually applies, or cleaned
                # up directly by us in the finally below when it
                # doesn't - replaces two earlier, narrower attempts at
                # this same coverage (one only handling chime_id being
                # falsy, one relying solely on async_render's own
                # internal check) that each independently downloaded and
                # checked the same URL a second time on the path where a
                # chime DOES apply.
                try:
                    speech_path = await chime.async_check_tts_url(
                        self.hass, url, message, base_cache_key=cache_key
                    )
                except chime.PoisonedSynthesis as err:
                    raise self._poisoned_synthesis_error(err, ha_tts_target) from err

                try:
                    if chime_id:
                        if chime_cache_key:
                            try:
                                url, cache_key = await chime.async_render(
                                    self.hass, url, chime_id, chime_cache_key,
                                    speech_path=speech_path, message=message,
                                    base_cache_key=cache_key,
                                )
                                speech_path = None  # async_render now owns cleanup
                            except chime.PoisonedSynthesis as err:
                                speech_path = None  # async_render's own finally already cleaned it up
                                raise self._poisoned_synthesis_error(err, ha_tts_target) from err
                        else:
                            _LOGGER.warning(
                                "chime='%s' could not be applied (see prior warning for why) - "
                                "playing announcement without it",
                                chime_id,
                            )
                finally:
                    if speech_path:
                        await chime.async_discard_temp(self.hass, speech_path)
        elif chime_id:
            _LOGGER.warning(
                "chime='%s' requested but this announcement isn't going through an HA "
                "tts.* entity (needs tts_engine, or engine set to a tts.* id) - playing "
                "without the chime. Chimes are only supported on that path.",
                chime_id,
            )

        if url:
            _LOGGER.info("Calling announce_audio with url=%s cache_key=%s", url, cache_key)
            result = await self._bridge.async_announce_audio(
                url,
                text_fallback=message,
                priority=priority,
                category=category,
                timeout_ms=timeout_ms,
                cache_key=cache_key,
            )
            _LOGGER.info("announce_audio result: %s", result)
            return result
        return await self._bridge.async_announce_text(
            message,
            priority=priority,
            category=category,
            engine=engine,
            timeout_ms=timeout_ms,
        )

    async def async_cancel(self, **kwargs: Any) -> dict[str, Any]:
        """ttsbridge.cancel entity service. Stops whatever's currently playing;
        clear_queue (default True) also drops everything still queued behind
        it, so a runaway batch of announcements doesn't keep working through
        its backlog after you've asked it to stop."""
        clear_queue = kwargs.get(ATTR_CLEAR_QUEUE, True)
        result = await self._bridge.async_cancel(clear_queue=clear_queue)
        _LOGGER.info("cancel result (clear_queue=%s): %s", clear_queue, result)
        return result

    async def async_check_cache(self, **kwargs: Any) -> dict[str, Any]:
        """ttsbridge.check_cache entity service. Answers 'is this exact
        (message, tts_engine, chime) combination already cached?' without
        ever calling the engine - same hashing chime.compute_chime_cache_key
        / chime.try_get_cached_url that async_announce uses internally, just
        stopped before the resolve step rather than falling through to it.

        This exists because a caller (e.g. tts_announce trying both a raw
        SSML and a tag-stripped variant of the same message, preferring
        whichever's already cached) can't safely "try announce and see if it
        was a cache hit" - a miss there has *already* triggered a real,
        quota-costing synthesis as a side effect by the time you'd know.
        This lets that decision happen first, for free.

        Returns {"cached": False} on any miss - unknown engine, no chime
        asset, whatever. Only {"cached": True, "url": ...} is a real hit.
        """
        message = kwargs[ATTR_MESSAGE]
        ha_tts_target = kwargs[ATTR_TTS_ENGINE]
        chime_id = kwargs.get(ATTR_CHIME)

        cache_key = hashlib.sha256(f"{message}|{ha_tts_target}".encode("utf-8")).hexdigest()

        if not chime_id:
            # No chime support outside the chime path - see chime.py's
            # module docstring for why this stays scoped tight. Nothing to
            # check.
            return {"cached": False}

        chime_cache_key = await chime.compute_chime_cache_key(self.hass, cache_key, chime_id)
        if not chime_cache_key:
            return {"cached": False}

        cached_url = await chime.try_get_cached_url(self.hass, chime_cache_key)
        if not cached_url:
            return {"cached": False}

        return {"cached": True, "url": cached_url}

    @staticmethod
    def _poisoned_synthesis_error(err: "chime.PoisonedSynthesis", ha_tts_target: str) -> HomeAssistantError:
        """Shared by both call sites that catch chime.PoisonedSynthesis (the
        chime path in async_render, and the no-chime path via
        async_check_tts_url) - keeps the two error messages identical
        without duplicating the wording in two places that could drift
        apart from each other over time."""
        return HomeAssistantError(
            f"Homeway returned a known-degraded response for '{ha_tts_target}' "
            f"(signature {err.signature}: {err.label}) - refusing to play or cache it"
        )

    async def _async_resolve_ha_tts_url(self, engine_entity_id: str, message: str) -> str:
        """Render `message` through an installed HA TTS entity, in-process - no
        token, no network round-trip to ourselves, unlike the REST
        /api/tts_get_url equivalent this replaces.

        media_source.async_resolve_media() can return a bare relative path
        (no scheme/host) rather than an absolute URL - meaningless to the
        bridge, a separate device fetching this over the network - so it's
        rebuilt into an absolute URL via get_url() when that happens.
        """
        identifier = f"{engine_entity_id}?{urlencode({'message': message})}"
        media_content_id = media_source.generate_media_source_id("tts", identifier)
        try:
            play_media = await media_source.async_resolve_media(self.hass, media_content_id, None)
        except media_source.Unresolvable as err:
            raise HomeAssistantError(
                f"Could not render message through {engine_entity_id}: {err}"
            ) from err

        url = play_media.url
        if url.startswith("/"):
            base_url = get_url(self.hass, prefer_external=False, allow_internal=True)
            _LOGGER.info("get_url() resolved base_url=%s for relative path %s", base_url, url)
            url = f"{base_url}{url}"

        _LOGGER.info(
            "Resolved %s via %s -> %s (mime=%s)",
            engine_entity_id,
            media_content_id,
            url,
            play_media.mime_type,
        )
        return url
