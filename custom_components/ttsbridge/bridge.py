"""Application-level logic for one TTS Bridge device.

Per the plan, this is deliberately separate from the coordinator: the
coordinator's only job is "fetch data on a schedule," while this is where
"what does it mean for this bridge to be healthy" lives. RecoveryManager
(the restart/re-register sequence) and webhook registration hook into this
class in a later step - for now it's a thin pass-through to BridgeApiClient,
giving the coordinator/sensor something stable to depend on while the rest
gets built incrementally.
"""

from __future__ import annotations

from typing import Any

from .api import BridgeApiClient


class AnnouncementBridge:
    """Coordinates the API client (and, later, RecoveryManager/webhooks)."""

    def __init__(self, api_client: BridgeApiClient) -> None:
        self._api = api_client

    async def async_get_status(self) -> dict[str, Any]:
        """Fetch current status. Raises BridgeConnectionError/BridgeApiError on failure -
        deliberately not swallowed here, so the coordinator's own error handling
        (which drives entity `available` state) is the single place that decides
        what a failure means."""
        return await self._api.status()

    async def async_announce_audio(self, url: str, **kwargs: Any) -> dict[str, Any]:
        return await self._api.announce_audio(url, **kwargs)

    async def async_announce_text(self, text: str, **kwargs: Any) -> dict[str, Any]:
        return await self._api.announce_text(text, **kwargs)

    async def async_register_webhook(self, url: str) -> dict[str, Any]:
        return await self._api.register_webhook(url)
