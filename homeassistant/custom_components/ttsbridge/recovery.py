"""Watches one bridge's coordinator and reacts to sustained failure / recovery.

Scope, deliberately bounded:
 - Re-registers the webhook when the coordinator recovers (the bridge
   persists its own registration, but a fresh install/data-clear wouldn't
   have one, and a restart could theoretically lose it).
 - Fires a real HA event - NOT a dispatcher signal, since dispatcher is
   Python-internal only and can't be used as an automation trigger - on a
   retry interval for as long as the coordinator stays down, so a YAML
   automation can still own the actual "resurrect the process" step
   (androidtv.adb_command). The event re-fires on an interval rather than
   once per failure transition specifically to preserve the persistent
   retry behavior that's what actually recovered the multi-hour
   TCL-boot-block outage earlier - a one-shot event tied to a `state`
   trigger would lose that.
 - Does NOT reach into androidtv.adb_command directly, and does NOT
   receive the webhook itself (that's still the existing YAML template:
   trigger sensor - "webhook fast-path" is a later, separate build step).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Callable

from homeassistant.core import HomeAssistant, callback

from homeassistant.helpers.event import async_track_time_interval

from .bridge import AnnouncementBridge
from .const import DOMAIN
from .coordinator import TtsBridgeCoordinator

_LOGGER = logging.getLogger(__name__)

EVENT_RECOVERY_NEEDED = "ttsbridge_recovery_needed"
EVENT_RECOVERED = "ttsbridge_recovered"

# Matches the retry cadence the old time_pattern-based YAML automation used
# (150s), so recovery attempts stay just as persistent as before.
RETRY_INTERVAL = timedelta(seconds=150)


class RecoveryManager:
    """Owns webhook re-registration and recovery-needed event signaling for one entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        webhook_url: str | None,
        bridge: AnnouncementBridge,
        coordinator: TtsBridgeCoordinator,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._webhook_url = webhook_url
        self._bridge = bridge
        self._coordinator = coordinator

        # RecoveryManager is only ever constructed after
        # async_config_entry_first_refresh() has already succeeded (see
        # __init__.py), so it's safe to assume the starting state is
        # "available" rather than handling a not-yet-known third state.
        self._was_available: bool = True
        self._retry_cancel: Callable[[], None] | None = None
        self._remove_listener = coordinator.async_add_listener(self._handle_update)

    def unload(self) -> None:
        """Stop listening and cancel any active retry timer. Call from async_unload_entry."""
        self._remove_listener()
        self._stop_retry_timer()

    @callback
    def _handle_update(self) -> None:
        available = self._coordinator.last_update_success
        _LOGGER.debug(
            "RecoveryManager._handle_update: available=%s was_available=%s",
            available,
            self._was_available,
        )

        if available and not self._was_available:
            # Recovered from a sustained failure.
            self._stop_retry_timer()
            self._hass.async_create_task(self._async_on_recovered())
        elif not available and self._was_available:
            # Fresh transition into failure - fire immediately, then keep
            # retrying on an interval for as long as it stays down.
            self._fire_recovery_needed()
            self._start_retry_timer()

        self._was_available = available

    async def _async_on_recovered(self) -> None:
        if self._webhook_url is not None:
            try:
                await self._bridge.async_register_webhook(self._webhook_url)
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "TTS Bridge %s recovered, but re-registering the webhook failed",
                    self._entry_id,
                )
                # Still fire the recovered event below - the process is back
                # up even if this one follow-up call failed, and the YAML
                # self-heal automation's job is done either way.
        _LOGGER.info("TTS Bridge %s recovered", self._entry_id)
        self._hass.bus.async_fire(EVENT_RECOVERED, {"entry_id": self._entry_id})

    def _fire_recovery_needed(self) -> None:
        _LOGGER.warning("TTS Bridge %s unresponsive, requesting recovery", self._entry_id)
        self._hass.bus.async_fire(EVENT_RECOVERY_NEEDED, {"entry_id": self._entry_id})

    def _start_retry_timer(self) -> None:
        if self._retry_cancel is not None:
            return  # already running
        self._retry_cancel = async_track_time_interval(
            self._hass,
            self._retry_tick,
            RETRY_INTERVAL,
            name=f"{DOMAIN}_{self._entry_id}_recovery_retry",
        )

    def _stop_retry_timer(self) -> None:
        if self._retry_cancel is not None:
            self._retry_cancel()
            self._retry_cancel = None

    @callback
    def _retry_tick(self, _now: datetime) -> None:
        # Only re-fire if we're still actually down - the coordinator's own
        # poll may have already recovered us since this tick was scheduled.
        if not self._coordinator.last_update_success:
            self._fire_recovery_needed()
