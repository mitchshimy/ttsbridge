"""The TTS Bridge integration.

This is deliberately minimal right now - config flow + API client only, no
entity platforms yet. Per the build plan, sensor.py/notify.py/the
coordinator land in the next pass once this is confirmed working end to
end (a device can actually be added via the UI and /status succeeds).
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BridgeApiClient, BridgeConnectionError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TTS Bridge from a config entry."""
    session = async_get_clientsession(hass)
    client = BridgeApiClient(session, entry.data[CONF_HOST], entry.data[CONF_PORT])

    try:
        await client.status()
    except BridgeConnectionError as err:
        # Triggers HA's built-in retry-with-backoff for setup, rather than
        # a hard failure - the TV might just be off/rebooting right now.
        raise ConfigEntryNotReady(f"Could not reach TTS Bridge: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = True
    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
