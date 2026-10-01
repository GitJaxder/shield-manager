"""Select: which Shield the others mirror."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ShieldManagerError
from .coordinator import ShieldManagerConfigEntry, ShieldManagerCoordinator
from .entity import ShieldManagerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ShieldManagerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ReferenceSelect(entry.runtime_data)])


class ReferenceSelect(ShieldManagerEntity, SelectEntity):
    def __init__(self, coordinator: ShieldManagerCoordinator) -> None:
        super().__init__(coordinator, "reference")

    @property
    def options(self) -> list[str]:
        return sorted(self.coordinator.data.shields)

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.reference

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.client.set_reference(option)
        except ShieldManagerError as err:
            raise HomeAssistantError(str(err)) from err
        self.coordinator.reread_shields()
        await self.coordinator.async_request_refresh()
