"""A client for the Shield Manager app's JSON API (the same one its web page uses)."""

from __future__ import annotations

from typing import Any

import aiohttp

# The app refuses changes without this header (it stops other websites driving it).
CSRF_HEADERS = {"X-Shield-Manager": "1"}
# Reading every Shield's app list can take a while when one is asleep or unreachable.
TIMEOUT = aiohttp.ClientTimeout(total=120)


class ShieldManagerError(Exception):
    """The app couldn't be reached or refused a request."""


class ShieldManagerClient:
    def __init__(self, session: aiohttp.ClientSession, url: str) -> None:
        self._session = session
        self.url = url.rstrip("/") + "/"

    async def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        headers = CSRF_HEADERS if method != "GET" else None
        try:
            async with self._session.request(
                method, self.url + "api/" + path, json=body, headers=headers, timeout=TIMEOUT
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = None
                if resp.status >= 400:
                    message = data.get("error") if isinstance(data, dict) else None
                    raise ShieldManagerError(message or f"HTTP {resp.status}")
                return data
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ShieldManagerError(f"can't reach Shield Manager at {self.url}: {err}") from err

    async def devices(self) -> list[dict]:
        return await self._request("GET", "devices")

    async def catalog(self) -> dict:
        return await self._request("GET", "catalog")

    async def app_names(self) -> dict:
        """Names and icons read so far (cheap: the app's cache, not the Shields)."""
        return await self._request("GET", "catalog/meta")

    async def online_updates(self) -> dict:
        return await self._request("GET", "online-updates")

    async def jobs(self) -> list[dict]:
        return await self._request("GET", "jobs")

    async def set_reference(self, name: str) -> dict:
        return await self._request("PUT", "reference", {"name": name})

    async def sync(
        self,
        devices: list[str] | None = None,
        allow_downgrade: bool = False,
        downloads: bool = True,
    ) -> dict:
        body: dict[str, Any] = {"allow_downgrade": allow_downgrade, "downloads": downloads}
        if devices:
            body["devices"] = devices
        return await self._request("POST", "sync", body)

    async def install(self, packages: list[str], devices: list[str] | None = None) -> dict:
        body: dict[str, Any] = {"packages": packages}
        if devices:
            body["devices"] = devices
        return await self._request("POST", "install-from-shield", body)

    async def check_online_updates(self) -> dict:
        return await self._request("POST", "online-updates/check", {})

    async def install_online_updates(self, packages: list[str]) -> dict:
        return await self._request("POST", "online-updates/install", {"packages": packages})
