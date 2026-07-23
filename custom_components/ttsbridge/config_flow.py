"""Config flow for TTS Bridge.

Note on the reconfigure step: async_step_reconfigure / _get_reconfigure_entry /
async_update_reload_and_abort are current-generation HA config_flow helpers
for editing an existing entry in place (added a couple of years back). These
specific method names were NOT independently source-verified this session
the way the tts/media_source internals were - unlike that risk, a wrong
method name here fails loudly and immediately (an AttributeError the moment
you click "Reconfigure" in the UI) rather than silently doing the wrong
thing, so it's a safe thing to test-and-fix rather than pre-verify further.

Note on async_step_setup_automations: writing automation YAML files needs
a real entry_id (used for automation uniqueness and to filter recovery
events per-device), which only exists after async_create_entry() returns
- config_flow steps run before the entry exists. So this step only
collects the choice (which media_player, or skip) and stores it in
entry.data; the actual file write happens in async_setup_entry
(__init__.py), which does have the real entry by then.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BridgeApiClient, BridgeApiError, BridgeConnectionError
from .const import CONF_MEDIA_PLAYER_ENTITY_ID, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

ATTR_MEDIA_PLAYER_ENTITY = "media_player_entity_id"

# Matches AnnouncementService.java's fully-qualified component name -
# exactly what RecoveryManager's YAML automation already sends successfully.
START_SERVICE_COMMAND = "am start-foreground-service -n dev.local.ttsbridge/.AnnouncementService"

# Give the process a moment to actually bind its HTTP server before retrying -
# same reasoning as the delay already used in the YAML automations.
START_SERVICE_SETTLE_SECONDS = 5


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Optional(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): int,
        }
    )


def _media_player_schema(default_entity_id: str | None = None) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                ATTR_MEDIA_PLAYER_ENTITY, default=default_entity_id
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="media_player", integration="androidtv")
            ),
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

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int | None = None
        # Carried forward from async_step_start_service if that path was
        # taken, so async_step_setup_automations can pre-fill it instead
        # of asking for the same entity twice.
        self._known_media_player_entity_id: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            self._host = host
            self._port = port

            try:
                await _validate_connection(self.hass, host, port)
            except (BridgeConnectionError, BridgeApiError):
                # Don't just fail here - offer to start it via ADB first,
                # the same way RecoveryManager's automation already does
                # successfully, before making the user go do that by hand.
                return await self.async_step_start_service()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating TTS Bridge connection")
                errors["base"] = "unknown"
            else:
                return await self.async_step_setup_automations()

        return self.async_show_form(step_id="user", data_schema=_schema(), errors=errors)

    async def async_step_start_service(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Shown after an initial connection failure - offers to start the
        bridge remotely via androidtv.adb_command before giving up."""
        errors: dict[str, str] = {}

        if user_input is not None:
            entity_id = user_input.get(ATTR_MEDIA_PLAYER_ENTITY)
            if entity_id:
                try:
                    await self.hass.services.async_call(
                        "androidtv",
                        "adb_command",
                        {"command": START_SERVICE_COMMAND},
                        target={"entity_id": entity_id},
                        blocking=True,
                    )
                except ServiceNotFound:
                    errors["base"] = "androidtv_not_available"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("adb_command failed while trying to start the bridge")
                    errors["base"] = "start_failed"
                else:
                    await asyncio.sleep(START_SERVICE_SETTLE_SECONDS)
                    try:
                        await _validate_connection(self.hass, self._host, self._port)
                    except (BridgeConnectionError, BridgeApiError):
                        errors["base"] = "still_cannot_connect"
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Unexpected error validating TTS Bridge connection")
                        errors["base"] = "unknown"
                    else:
                        # We already know the right media_player entity -
                        # carry it forward so the next step can pre-fill it.
                        self._known_media_player_entity_id = entity_id
                        return await self.async_step_setup_automations()
            else:
                # Declined - no entity picked, surface the original failure
                # rather than looping on this step forever.
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="start_service",
            data_schema=_media_player_schema(),
            errors=errors,
            description_placeholders={"host": self._host or "", "port": str(self._port or "")},
        )

    async def async_step_setup_automations(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Opt-in: install the recommended power-on/self-heal automations.

        Deliberately a separate, explicit step rather than automatic -
        this writes a YAML file into the user's automations/ folder,
        which is a different kind of action than anything else this
        integration does (see automations.py's module docstring for why
        that distinction matters). The actual file write happens later,
        in async_setup_entry, once a real entry_id exists.
        """
        if user_input is not None:
            entity_id = user_input.get(ATTR_MEDIA_PLAYER_ENTITY)
            data: dict[str, Any] = {CONF_HOST: self._host, CONF_PORT: self._port}
            if entity_id:
                data[CONF_MEDIA_PLAYER_ENTITY_ID] = entity_id
            return self.async_create_entry(title=f"TTS Bridge ({self._host})", data=data)

        return self.async_show_form(
            step_id="setup_automations",
            data_schema=_media_player_schema(self._known_media_player_entity_id),
        )

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

