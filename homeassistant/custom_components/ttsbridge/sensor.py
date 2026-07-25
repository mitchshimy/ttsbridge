"""Sensor platform for TTS Bridge."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_PORT, DOMAIN
from .coordinator import TtsBridgeCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TtsBridgeCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([TtsBridgeStatusSensor(coordinator, entry)])


class TtsBridgeStatusSensor(CoordinatorEntity[TtsBridgeCoordinator], SensorEntity):
    """Reports the bridge's current playback state (IDLE/SPEAKING/BUSY)."""

    _attr_has_entity_name = True
    _attr_name = "Status"
    _attr_icon = "mdi:television-speaker"

    def __init__(self, coordinator: TtsBridgeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="ttsbridge (self-hosted)",
            model="Android TTS Bridge",
            configuration_url=f"http://{entry.data[CONF_HOST]}:{entry.data.get(CONF_PORT, DEFAULT_PORT)}/status",
        )

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("state")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data or {}
        return {
            "current": data.get("current"),
            "queue_size": data.get("queueSize"),
            "volume": data.get("volume"),
        }
