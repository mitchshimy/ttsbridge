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
   engine, speak_timeout. Both fields can be present together (url wins
   for playback; message is still recorded as a fallback/label on the
   bridge, exactly like BridgeApiClient.announce_audio's text_fallback
   already supports) - not mutually exclusive.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.notify import NotifyEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
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
ATTR_SPEAK_TIMEOUT = "speak_timeout"

SERVICE_ANNOUNCE = "announce"

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
        timeout_ms = kwargs.get(ATTR_SPEAK_TIMEOUT)

        if url:
            return await self._bridge.async_announce_audio(
                url,
                text_fallback=message,
                priority=priority,
                category=category,
                timeout_ms=timeout_ms,
            )
        return await self._bridge.async_announce_text(
            message,
            priority=priority,
            category=category,
            engine=engine,
            timeout_ms=timeout_ms,
        )
