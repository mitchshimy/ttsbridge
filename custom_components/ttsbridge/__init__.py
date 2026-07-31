"""The TTS Bridge integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError

from .api import BridgeApiClient
from .automations import async_install_automations, async_remove_automations
from .bridge import AnnouncementBridge
from .const import CONF_MEDIA_PLAYER_ENTITY_ID, CONF_TRIGGER_ENTITY_ID, DOMAIN
from .coordinator import TtsBridgeCoordinator
from .recovery import RecoveryManager
from .webhook import (
    async_get_or_create_webhook_id,
    async_get_webhook_url,
    async_register_webhook_receiver,
    async_unregister_webhook_receiver,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.NOTIFY]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TTS Bridge from a config entry."""
    session = async_get_clientsession(hass)
    client = BridgeApiClient(session, entry.data[CONF_HOST], entry.data[CONF_PORT])
    announcement_bridge = AnnouncementBridge(client)
    coordinator = TtsBridgeCoordinator(hass, entry, announcement_bridge)

    # Raises ConfigEntryNotReady automatically on failure (confirmed against
    # HA's actual source before relying on it) - this both validates
    # connectivity AND populates coordinator.data, so there's no need for a
    # separate status() call the way the earlier bare-client version had.
    await coordinator.async_config_entry_first_refresh()

    # The receiver side never depends on network URL resolution - a
    # webhook_id is just a random token, so this always succeeds and the
    # push fast-path is ready the moment the device is told about it
    # (below), whenever that ends up happening.
    webhook_id = async_get_or_create_webhook_id(hass, entry)
    async_register_webhook_receiver(hass, entry, webhook_id, coordinator)

    webhook_url: str | None = None
    try:
        webhook_url = async_get_webhook_url(hass, webhook_id)
    except NoURLAvailableError:
        _LOGGER.warning(
            "No internal HA URL available - can't tell the TTS Bridge device "
            "where to push updates. Set Settings > System > Network > "
            "Internal URL, then reload this integration. Falling back to "
            "30s polling in the meantime."
        )
    else:
        try:
            await announcement_bridge.async_register_webhook(webhook_url)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Could not register webhook with the TTS Bridge device at setup "
                "- will retry automatically once RecoveryManager sees a failure/"
                "recovery cycle. Falling back to 30s polling in the meantime."
            )

    # Always constructed, regardless of whether webhook_url resolved -
    # firing recovery-needed/recovered events for the YAML self-heal
    # automation is a separate concern from webhook registration, and
    # shouldn't be held hostage by it.
    recovery_manager = RecoveryManager(
        hass, entry.entry_id, webhook_url, announcement_bridge, coordinator
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "bridge": announcement_bridge,
        "coordinator": coordinator,
        "recovery_manager": recovery_manager,
        "webhook_id": webhook_id,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Opt-in only (see automations.py / config_flow.py's
    # async_step_setup_automations for why) - key is simply absent from
    # entry.data if the user skipped that step. Must run AFTER the
    # platform forward above: it looks up the notify entity via the
    # entity registry, which doesn't exist until notify.py's
    # async_setup_entry has actually run and registered it.
    media_player_entity_id = entry.data.get(CONF_MEDIA_PLAYER_ENTITY_ID)
    if media_player_entity_id:
        trigger_entity_id = entry.data.get(CONF_TRIGGER_ENTITY_ID)
        try:
            await async_install_automations(
                hass,
                entry.entry_id,
                entry.data[CONF_HOST],
                media_player_entity_id,
                trigger_entity_id,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Could not install automations for entry %s - the integration "
                "itself is still fully set up, this only affects the optional "
                "auto-generated automations file",
                entry.entry_id,
            )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        stored = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if stored:
            if stored.get("recovery_manager") is not None:
                stored["recovery_manager"].unload()
            if stored.get("webhook_id") is not None:
                async_unregister_webhook_receiver(hass, stored["webhook_id"])
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Called on permanent removal (not reload/disable) - clean up the
    auto-generated automations file if one was ever installed for this entry."""
    await async_remove_automations(hass, entry.entry_id)




