"""Polls the Shield Manager app for the state of every Shield."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ShieldManagerClient, ShieldManagerError
from .const import CATALOG_INTERVAL, DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

type ShieldManagerConfigEntry = ConfigEntry[ShieldManagerCoordinator]


@dataclass
class ShieldState:
    """One Shield, as the app last saw it."""

    name: str
    reachable: bool
    reference: bool
    error: str | None = None
    apps: dict[str, int] = field(default_factory=dict)  # package -> versionCode
    # Packages per change from the reference: install, update, newer, extra.
    changes: dict[str, list[str]] = field(default_factory=dict)

    @property
    def behind(self) -> list[str]:
        """Apps the reference has that this Shield is missing or has an older version of."""
        return [*self.changes.get("install", []), *self.changes.get("update", [])]


@dataclass
class ShieldManagerData:
    reference: str | None
    shields: dict[str, ShieldState]
    labels: dict[str, str]  # package -> app name, where the app has read it
    online_updates: dict[str, dict]
    online_checked: datetime | None
    jobs: list[dict]
    catalog_read: datetime

    @property
    def running_job(self) -> dict | None:
        return next((j for j in self.jobs if j.get("state") == "running"), None)

    def label(self, package: str) -> str:
        return self.labels.get(package) or package


class ShieldManagerCoordinator(DataUpdateCoordinator[ShieldManagerData]):
    config_entry: ShieldManagerConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ShieldManagerConfigEntry, client: ShieldManagerClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self._catalog: dict | None = None
        self._catalog_read: datetime | None = None
        self._finished_jobs: set[str] = set()
        self._reread_catalog = True

    def reread_shields(self) -> None:
        """Re-read every Shield's apps on the next refresh (after a change)."""
        self._reread_catalog = True

    async def _async_update_data(self) -> ShieldManagerData:
        try:
            jobs = await self.client.jobs()
            online = await self.client.online_updates()
            # App names are read off the Shields in the background, so they can arrive
            # after the app lists.
            names = await self.client.app_names()
            # Re-read the Shields when they're due, when asked, or when a job (an install,
            # sync or removal) has just finished and changed what's on them.
            finished = {j["id"] for j in jobs if j.get("state") != "running"}
            if finished - self._finished_jobs and self._catalog is not None:
                self._reread_catalog = True
            self._finished_jobs = finished
            now = dt_util.utcnow()
            due = self._catalog_read is None or now - self._catalog_read >= CATALOG_INTERVAL
            if self._catalog is None or due or self._reread_catalog:
                self._catalog = await self.client.catalog()
                self._catalog_read, self._reread_catalog = now, False
        except ShieldManagerError as err:
            raise UpdateFailed(str(err)) from err
        return _parse(self._catalog, names, online, jobs, self._catalog_read or now)


def _parse(
    catalog: dict, names: dict, online: dict, jobs: list[dict], read: datetime
) -> ShieldManagerData:
    shields: dict[str, ShieldState] = {}
    for entry in catalog.get("shields", []):
        shields[entry["name"]] = ShieldState(
            name=entry["name"],
            reachable=bool(entry.get("ok")),
            reference=bool(entry.get("reference")),
            error=entry.get("error"),
            changes=entry.get("changes") or {},
        )
    labels = {p: m["label"] for p, m in (names.get("apps") or {}).items() if m.get("label")}
    for app in catalog.get("apps", []):
        package = app["package"]
        versions: dict[str, Any] = app.get("versions") or {}
        for name, code in versions.items():
            if name in shields:
                shields[name].apps[package] = code
    checked = online.get("checked")
    return ShieldManagerData(
        reference=catalog.get("reference"),
        shields=shields,
        labels=labels,
        online_updates=online.get("updates") or {},
        online_checked=dt_util.utc_from_timestamp(checked) if checked else None,
        jobs=jobs,
        catalog_read=read,
    )
