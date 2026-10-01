"""Binary sensors: whether the app can reach each Shield."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ShieldManagerConfigEntry, ShieldManagerCoordinator
from .entity import ShieldEntity, add_shield_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ShieldManagerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    add_shield_entities(
        coordinator, async_add_entities, lambda name: [ReachableSensor(coordinator, name)]
    )


class ReachableSensor(ShieldEntity, BinarySensorEntity):
    """On when the app could connect to the Shield over ADB at its last read."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: ShieldManagerCoordinator, shield: str) -> None:
        super().__init__(coordinator, shield, "reachable")

    @property
    def is_on(self) -> bool | None:
        state = self.state_data
        return state.reachable if state else None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        state = self.state_data
        return {"error": state.error} if state and state.error else {}
