"""Base entities: the Shield Manager hub, and one device per Shield."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_APP_SLUG, DOMAIN
from .coordinator import ShieldManagerCoordinator, ShieldState


class ShieldManagerEntity(CoordinatorEntity[ShieldManagerCoordinator]):
    """An entity of the Shield Manager app as a whole."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ShieldManagerCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        slug = entry.data.get(CONF_APP_SLUG)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Shield Manager",
            manufacturer="shield-manager",
            # The app's page in the sidebar, or the web UI's own address.
            configuration_url=f"homeassistant://hassio/ingress/{slug}"
            if slug
            else coordinator.client.url,
        )


class ShieldEntity(CoordinatorEntity[ShieldManagerCoordinator]):
    """An entity of one Shield."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ShieldManagerCoordinator, shield: str, key: str) -> None:
        super().__init__(coordinator)
        self.shield = shield
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{shield}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{shield}")},
            name=shield,
            manufacturer="Nvidia",
            model="Shield",
            via_device=(DOMAIN, entry_id),
        )

    @property
    def state_data(self) -> ShieldState | None:
        return self.coordinator.data.shields.get(self.shield)

    @property
    def available(self) -> bool:
        return super().available and self.state_data is not None


def add_shield_entities(
    coordinator: ShieldManagerCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    make: Callable[[str], Iterable[Entity]],
) -> None:
    """Add entities for each Shield, now and whenever the app gains a new one."""
    added: set[str] = set()

    @callback
    def add_new() -> None:
        new = [name for name in coordinator.data.shields if name not in added]
        added.update(new)
        async_add_entities([entity for name in new for entity in make(name)])

    add_new()
    coordinator.config_entry.async_on_unload(coordinator.async_add_listener(add_new))
