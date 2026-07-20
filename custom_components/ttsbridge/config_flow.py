"""Config flow for TTS Bridge.

Note on the reconfigure step: async_step_reconfigure / _get_reconfigure_entry /
async_update_reload_and_abort are current-generation HA config_flow helpers
for editing an existing entry in place (added a couple of years back). These
specific method names were NOT independently source-verified this session
the way the tts/media_source internals were - unlike that risk, a wrong
method name here fails loudly and immediately (an AttributeError the moment
you click "Reconfigure" in the UI) rather than silently doing the wrong
thing, so it's a safe thing to test-and-fix rather than pre-verify further.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BridgeApiClient, BridgeApiError, BridgeConnectionError
from .const import DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Optional(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): int,
        }
    )


async def _validate_connection(hass, host: str, port: int) -> None:
    """Raise BridgeConnectionError/BridgeApiError if the bridge isn't reachable."""
    session = async_get_clientsession(hass)
    client = BridgeApiClient(session, host, port)
    status = await client.status()
    _LOGGER.debug("TTS Bridge connectivity check ok for %s:%s -> %s", host, port, status)


class TtsBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for TTS Bridge."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            try:
                await _validate_connection(self.hass, host, port)
            except BridgeConnectionError:
                errors["base"] = "cannot_connect"
            except BridgeApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating TTS Bridge connection")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"TTS Bridge ({host})",
                    data={CONF_HOST: host, CONF_PORT: port},
                )

        return self.async_show_form(step_id="user", data_schema=_schema(), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Let a changed IP be edited in place instead of delete-and-re-add."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            try:
                await _validate_connection(self.hass, host, port)
            except BridgeConnectionError:
                errors["base"] = "cannot_connect"
            except BridgeApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating TTS Bridge connection")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data={CONF_HOST: host, CONF_PORT: port}
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(entry.data),
            errors=errors,
        )
