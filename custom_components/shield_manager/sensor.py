"""Sensors: what each Shield has, and what the app is doing."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ShieldManagerConfigEntry, ShieldManagerCoordinator
from .entity import ShieldEntity, ShieldManagerEntity, add_shield_entities

# Lists in attributes are cut short so the state machine stays small.
MAX_LISTED = 50
# A job step's stages while it's moving (see shield_manager.web.jobs).
ACTIVE_STAGES = ("downloading", "copying", "installing", "removing")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ShieldManagerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            UpdatesAvailableSensor(coordinator),
            ActivitySensor(coordinator),
            LastReadSensor(coordinator),
        ]
    )
    add_shield_entities(
        coordinator,
        async_add_entities,
        lambda name: [AppsSensor(coordinator, name), BehindSensor(coordinator, name)],
    )


class AppsSensor(ShieldEntity, SensorEntity):
    """How many third-party apps a Shield has."""

    _attr_native_unit_of_measurement = "apps"

    def __init__(self, coordinator: ShieldManagerCoordinator, shield: str) -> None:
        super().__init__(coordinator, shield, "apps")

    @property
    def native_value(self) -> int | None:
        state = self.state_data
        return len(state.apps) if state and state.reachable else None


class BehindSensor(ShieldEntity, SensorEntity):
    """How many apps a Shield is missing or has older versions of, compared with the
    reference Shield."""

    _attr_native_unit_of_measurement = "apps"

    def __init__(self, coordinator: ShieldManagerCoordinator, shield: str) -> None:
        super().__init__(coordinator, shield, "behind")

    @property
    def native_value(self) -> int | None:
        state = self.state_data
        if state is None or not state.reachable:
            return None
        return 0 if state.reference else len(state.behind)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.state_data
        if state is None:
            return {}
        data = self.coordinator.data
        changes = state.changes
        return {
            "missing": [data.label(p) for p in changes.get("install", [])][:MAX_LISTED],
            "outdated": [data.label(p) for p in changes.get("update", [])][:MAX_LISTED],
            "newer_than_reference": [data.label(p) for p in changes.get("newer", [])][:MAX_LISTED],
            "not_on_reference": [data.label(p) for p in changes.get("extra", [])][:MAX_LISTED],
            "error": state.error,
        }


class UpdatesAvailableSensor(ShieldManagerEntity, SensorEntity):
    """Apps with a newer version on GitHub, from the app's last check."""

    _attr_native_unit_of_measurement = "apps"

    def __init__(self, coordinator: ShieldManagerCoordinator) -> None:
        super().__init__(coordinator, "updates_available")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.online_updates)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {
            "updates": [
                {
                    "app": data.label(package),
                    "package": package,
                    "installed": update.get("installed"),
                    "latest": update.get("latest"),
                }
                for package, update in sorted(data.online_updates.items())
            ][:MAX_LISTED],
            "last_checked": data.online_checked,
        }


class ActivitySensor(ShieldManagerEntity, SensorEntity):
    """What the app is doing right now, e.g. "Copying Kodi to bedroom - 40%"."""

    def __init__(self, coordinator: ShieldManagerCoordinator) -> None:
        super().__init__(coordinator, "activity")

    @property
    def native_value(self) -> str:
        job = self.coordinator.data.running_job
        if job is None:
            return "Idle"
        return _describe(job)[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        job = self.coordinator.data.running_job
        if job is None:
            return {}
        return {"job": job.get("title"), "job_id": job.get("id")}


class LastReadSensor(ShieldManagerEntity, SensorEntity):
    """When the app last read every Shield's apps."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: ShieldManagerCoordinator) -> None:
        super().__init__(coordinator, "last_read")

    @property
    def native_value(self):
        return self.coordinator.data.catalog_read


def _describe(job: dict) -> str:
    """The step in progress, worded like the web page's progress rows."""
    for step in job.get("steps", []):
        if step.get("stage") in ACTIVE_STAGES:
            text = f"{step['stage'].capitalize()} {step.get('label')} on {step.get('device')}"
            if step.get("percent") is not None:
                text += f" - {step['percent']:.0f}%"
            return text
    return job.get("progress") or job.get("title") or "Working"
