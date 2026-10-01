"""Shield Manager: see and sync the apps on your Nvidia Shields from Home Assistant.

The Shield Manager app (formerly add-on) does the work over ADB and serves the app-store
page in the sidebar. This integration talks to that app's API to add sensors, buttons and
actions you can use in dashboards and automations.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import ShieldManagerClient, ShieldManagerError
from .const import (
    ATTR_ALLOW_DOWNGRADE,
    ATTR_DEVICES,
    ATTR_DOWNLOADS,
    ATTR_PACKAGES,
    CONF_URL,
    DOMAIN,
)
from .coordinator import ShieldManagerConfigEntry, ShieldManagerCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SELECT, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SYNC_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICES): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_ALLOW_DOWNGRADE, default=False): cv.boolean,
        vol.Optional(ATTR_DOWNLOADS, default=True): cv.boolean,
    }
)
INSTALL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PACKAGES): vol.All(cv.ensure_list, [cv.string], vol.Length(min=1)),
        vol.Optional(ATTR_DEVICES): vol.All(cv.ensure_list, [cv.string]),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def sync(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        return await _start(
            coordinator,
            coordinator.client.sync(
                call.data.get(ATTR_DEVICES),
                allow_downgrade=call.data[ATTR_ALLOW_DOWNGRADE],
                downloads=call.data[ATTR_DOWNLOADS],
            ),
        )

    async def install(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        return await _start(
            coordinator,
            coordinator.client.install(call.data[ATTR_PACKAGES], call.data.get(ATTR_DEVICES)),
        )

    hass.services.async_register(
        DOMAIN, "sync", sync, SYNC_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, "install", install, INSTALL_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ShieldManagerConfigEntry) -> bool:
    client = ShieldManagerClient(async_get_clientsession(hass), entry.data[CONF_URL])
    coordinator = ShieldManagerCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ShieldManagerConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _coordinator(hass: HomeAssistant) -> ShieldManagerCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    return entries[0].runtime_data


async def _start(coordinator: ShieldManagerCoordinator, request) -> ServiceResponse:
    """Start a job in the app and return it; the app carries on in the background."""
    try:
        job = await request
    except ShieldManagerError as err:
        raise HomeAssistantError(str(err)) from err
    await coordinator.async_request_refresh()
    return {"job_id": job.get("id"), "title": job.get("title")}
