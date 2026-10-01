"""Set up Shield Manager: found automatically from the app, or by its address."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import ShieldManagerClient, ShieldManagerError
from .const import CONF_APP_SLUG, CONF_URL, DEFAULT_PORT, DOMAIN


class ShieldManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

    async def _check(self, url: str) -> str | None:
        """Return an error key, or None when the app answers at url."""
        try:
            await ShieldManagerClient(async_get_clientsession(self.hass), url).devices()
        except ShieldManagerError:
            return "cannot_connect"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            if not (error := await self._check(url)):
                return self.async_create_entry(title="Shield Manager", data={CONF_URL: url})
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_URL, default=f"http://127.0.0.1:{DEFAULT_PORT}"): str}
            ),
            errors=errors,
        )

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The Shield Manager app announced itself."""
        config = discovery_info.config
        url = f"http://{config['host']}:{config.get('port', DEFAULT_PORT)}"
        self._discovered = {CONF_URL: url, CONF_APP_SLUG: discovery_info.slug}
        await self.async_set_unique_id(DOMAIN)
        # The app's address changes if it's reinstalled from another repository.
        self._abort_if_unique_id_configured(updates=self._discovered)
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (error := await self._check(self._discovered[CONF_URL])):
                return self.async_create_entry(title="Shield Manager", data=self._discovered)
            errors["base"] = error
        return self.async_show_form(step_id="hassio_confirm", errors=errors)
