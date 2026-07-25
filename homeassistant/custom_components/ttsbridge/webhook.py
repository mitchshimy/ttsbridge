"""HA-native webhook receiving for TTS Bridge's push updates.

This is the fast path: the bridge POSTs state changes here the instant they
happen (see AnnouncementService.java's pushWebhookUpdate()), and this feeds
straight into the coordinator via async_set_updated_data() - which updates
entities immediately AND resets the poll timer AND clears any prior failure
state, confirmed directly against DataUpdateCoordinator's source before
relying on it. The 30s poll in coordinator.py keeps running underneath
regardless, as the backstop for "the device stopped pushing entirely"
(crashed, network dropped) - so this is push-primary-with-poll-backstop,
not push-only, which is what push-only got wrong the first time around
(see recovery.py's history for why that mattered in practice).

The webhook_id is generated via webhook.async_generate_id() (confirmed to
use secrets.token_hex(32) - genuinely unguessable, not just obscure), and
registered with local_only=True as an extra layer beyond that, since this
device is guaranteed to only ever be reached from the LAN.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp.web import Request, Response
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import CONF_WEBHOOK_ID
from .coordinator import TtsBridgeCoordinator

_LOGGER = logging.getLogger(__name__)


def async_get_or_create_webhook_id(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Return this entry's persisted webhook_id, generating one on first setup."""
    existing = entry.data.get(CONF_WEBHOOK_ID)
    if existing:
        return existing

    new_id = webhook.async_generate_id()
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_WEBHOOK_ID: new_id})
    return new_id


def async_get_webhook_url(hass: HomeAssistant, webhook_id: str) -> str:
    """Build the LAN URL for this webhook - never external, the TV can't assume it can reach that."""
    return webhook.async_generate_url(
        hass, webhook_id, allow_internal=True, allow_external=False, prefer_external=False
    )


@callback
def async_register_webhook_receiver(
    hass: HomeAssistant, entry: ConfigEntry, webhook_id: str, coordinator: TtsBridgeCoordinator
) -> None:
    """Register the HA-side webhook that receives the bridge's pushes."""

    async def _handle(hass: HomeAssistant, webhook_id: str, request: Request) -> Response | None:
        try:
            payload: dict[str, Any] = await request.json()
        except ValueError:
            _LOGGER.warning("TTS Bridge webhook for %s received non-JSON body", entry.entry_id)
            return Response(status=400)

        _LOGGER.debug("TTS Bridge webhook push for %s: %s", entry.entry_id, payload)
        coordinator.async_set_updated_data(payload)
        return None  # HA turns this into a 200 OK automatically

    webhook.async_register(
        hass,
        entry.domain,
        f"TTS Bridge push ({entry.title})",
        webhook_id,
        _handle,
        local_only=True,
        allowed_methods=["POST"],
    )


@callback
def async_unregister_webhook_receiver(hass: HomeAssistant, webhook_id: str) -> None:
    webhook.async_unregister(hass, webhook_id)
