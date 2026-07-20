"""Async HTTP client for the TTS Bridge Android app.

Capability-oriented on purpose (announce_audio/announce_text rather than a
1:1 mirror of today's REST endpoints), so callers above this layer stay
stable even if the bridge's transport or endpoint layout changes later.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)


class BridgeConnectionError(Exception):
    """Raised when the bridge can't be reached at all (network/timeout)."""


class BridgeApiError(Exception):
    """Raised when the bridge responds, but with an error status."""


class BridgeApiClient:
    """Thin async wrapper around the bridge's HTTP API."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int) -> None:
        self._session = session
        self._base_url = f"http://{host}:{port}"
        self._timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

    # ---- read endpoints ----

    async def status(self) -> dict[str, Any]:
        return await self._get("/status")

    async def queue(self) -> dict[str, Any]:
        return await self._get("/queue")

    async def engines(self) -> dict[str, Any]:
        return await self._get("/engines")

    # ---- control endpoints ----

    async def cancel(self, clear_queue: bool = False) -> dict[str, Any]:
        params = {"clear": "true"} if clear_queue else None
        return await self._post("/stop", params=params)

    async def set_volume(self, level: int) -> dict[str, Any]:
        return await self._post("/volume", json={"level": level})

    async def register_webhook(self, url: str) -> dict[str, Any]:
        return await self._post("/webhook", json={"url": url})

    async def set_default_engine_chain(self, chain: list[str]) -> dict[str, Any]:
        return await self._post("/engines/default", json={"chain": chain})

    async def register_engine(
        self, engine_id: str, base_url: str, json_request: bool = True
    ) -> dict[str, Any]:
        return await self._post(
            "/engines",
            json={"id": engine_id, "baseUrl": base_url, "jsonRequest": json_request},
        )

    # ---- announce endpoints ----

    async def announce_audio(
        self,
        url: str,
        *,
        text_fallback: str | None = None,
        priority: str = "normal",
        category: str = "general",
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Play a pre-rendered audio URL directly.

        An explicit url always bypasses the bridge's own engine-selection
        chain (see Announcement.java / AnnouncementEngine.java) - the caller
        already decided exactly what to play.
        """
        payload: dict[str, Any] = {"url": url, "priority": priority, "category": category}
        if text_fallback:
            payload["text"] = text_fallback
        if timeout_ms:
            payload["timeout"] = timeout_ms
        return await self._post("/announce", json=payload)

    async def announce_text(
        self,
        text: str,
        *,
        priority: str = "normal",
        category: str = "general",
        engine: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Ask the bridge to speak text via its own engine chain (device TTS etc)."""
        payload: dict[str, Any] = {"text": text, "priority": priority, "category": category}
        if engine:
            payload["engine"] = engine
        if timeout_ms:
            payload["timeout"] = timeout_ms
        return await self._post("/announce", json=payload)

    # ---- plumbing ----

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def _post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request("POST", path, json=json, params=params)

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            async with self._session.request(
                method, url, json=json, params=params, timeout=self._timeout
            ) as resp:
                # content_type=None: the bridge's raw HTTP server always sends
                # application/json, but this avoids a spurious ContentTypeError
                # if that ever drifts.
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    _LOGGER.warning(
                        "TTS Bridge returned %s for %s %s: %s", resp.status, method, path, data
                    )
                    raise BridgeApiError(f"{method} {path} failed with {resp.status}: {data}")
                return data
        except (asyncio.TimeoutError, aiohttp.ClientConnectionError) as err:
            raise BridgeConnectionError(f"Could not reach TTS Bridge at {url}") from err
        except aiohttp.ClientError as err:
            raise BridgeApiError(f"Error calling TTS Bridge at {url}: {err}") from err
