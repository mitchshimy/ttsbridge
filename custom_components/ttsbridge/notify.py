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

SERVICE_ANNOUNCE = "announce"
HA_TTS_ENTITY_PREFIX = "tts."

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

        ha_tts_target = tts_engine or (
            engine if engine and engine.startswith(HA_TTS_ENTITY_PREFIX) else None
        )
        cache_key = None
        if not url and message and ha_tts_target:
            url = await self._async_resolve_ha_tts_url(ha_tts_target, message)
            # Only safe to compute here: we resolved this url ourselves from
            # (message, ha_tts_target), so we know identical inputs mean
            # identical audio content - HA's own tts cache already guarantees
            # that (see the persistent-cache design discussion). An
            # explicitly-passed `url` (the branch below) carries no such
            # guarantee, so it's deliberately left uncached rather than
            # risking a stale clip being served for genuinely different audio.
            #
            # NOT the same as the url itself: HA's tts_proxy issues a fresh
            # token per resolve even on its own cache hit, so this has to be
            # derived from the (message, engine) pair instead, not the url.
            cache_key = hashlib.sha256(
                f"{message}|{ha_tts_target}".encode("utf-8")
            ).hexdigest()

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
