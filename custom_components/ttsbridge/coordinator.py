"""Polling coordinator for TTS Bridge.

Deliberately thin per the plan: fetches status on an interval and exposes it
to entities. Does not know how to restart the device or manage the webhook -
that's RecoveryManager's job, landing in a later step. Raising in
_async_update_data is what drives entities' `available` state via
CoordinatorEntity's default behavior (available = coordinator.last_update_success) -
that's real connectivity tracking, not hand-rolled staleness math.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BridgeApiError, BridgeConnectionError
from .bridge import AnnouncementBridge
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Push (webhook) is the primary, near-instant signal once that's wired up in
# a later step - this poll interval is a backstop for "did the pointer get
# lost / did the device restart," not the main responsiveness mechanism, so
# it doesn't need to be aggressive.
UPDATE_INTERVAL = timedelta(seconds=30)


class TtsBridgeCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinates polling of one bridge device."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, bridge: AnnouncementBridge) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=UPDATE_INTERVAL,
        )
        self._bridge = bridge

    @property
    def bridge(self) -> AnnouncementBridge:
        return self._bridge

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self._bridge.async_get_status()
        except (BridgeConnectionError, BridgeApiError) as err:
            raise UpdateFailed(f"Error communicating with TTS Bridge: {err}") from err
