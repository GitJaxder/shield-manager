"""Buttons: sync Shields with the reference, and check for and install app updates."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ShieldManagerError
from .coordinator import ShieldManagerConfigEntry, ShieldManagerCoordinator
from .entity import ShieldEntity, ShieldManagerEntity, add_shield_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ShieldManagerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    client = coordinator.client

    async def install_updates() -> None:
        packages = list(coordinator.data.online_updates)
        if not packages:
            raise HomeAssistantError("No app updates found; check for updates first")
        await client.install_online_updates(packages)

    async_add_entities(
        [
            HubButton(coordinator, "sync_all", client.sync),
            HubButton(coordinator, "check_updates", client.check_online_updates),
            HubButton(coordinator, "install_updates", install_updates),
        ]
    )
    add_shield_entities(
        coordinator, async_add_entities, lambda name: [SyncShieldButton(coordinator, name)]
    )


async def _run(coordinator: ShieldManagerCoordinator, action: Awaitable) -> None:
    try:
        await action
    except ShieldManagerError as err:
        raise HomeAssistantError(str(err)) from err
    # The app works in the background; refresh now so Activity shows it started.
    await coordinator.async_request_refresh()


class HubButton(ShieldManagerEntity, ButtonEntity):
    def __init__(
        self,
        coordinator: ShieldManagerCoordinator,
        key: str,
        action: Callable[[], Awaitable],
    ) -> None:
        super().__init__(coordinator, key)
        self._action = action

    async def async_press(self) -> None:
        await _run(self.coordinator, self._action())


class SyncShieldButton(ShieldEntity, ButtonEntity):
    """Install the apps this Shield is missing or behind on, from the reference."""

    def __init__(self, coordinator: ShieldManagerCoordinator, shield: str) -> None:
        super().__init__(coordinator, shield, "sync")

    async def async_press(self) -> None:
        await _run(self.coordinator, self.coordinator.client.sync([self.shield]))
