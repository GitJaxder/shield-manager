"""Persistent registry of known Shield devices, stored as JSON."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

DEFAULT_ADB_PORT = 5555
_KEEP = object()


def default_config_dir() -> Path:
    override = os.environ.get("SHIELD_MANAGER_HOME")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "shield-manager"


@dataclass(frozen=True)
class Device:
    name: str
    host: str
    port: int = DEFAULT_ADB_PORT
    groups: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        # JSON gives us lists; keep groups hashable, sorted and de-duplicated.
        object.__setattr__(self, "groups", tuple(sorted(set(self.groups))))

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class DeviceExistsError(Exception):
    pass


class DeviceNotFoundError(Exception):
    pass


class GroupNotFoundError(Exception):
    pass


class Registry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_dir() / "devices.json"

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def _load(self) -> dict[str, Device]:
        return {d["name"]: Device(**d) for d in self._read().get("devices", [])}

    def _save(self, devices: dict[str, Device], reference: object = _KEEP) -> None:
        """Write devices, keeping the stored reference unless a new one is passed."""
        if reference is _KEEP:
            reference = self._read().get("reference")
        if reference not in devices:
            reference = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "reference": reference,
            "devices": [
                {**asdict(d), "groups": list(d.groups)}
                for d in sorted(devices.values(), key=lambda d: d.name)
            ],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self.path)

    @property
    def reference(self) -> str | None:
        """Name of the device the others mirror, if one has been chosen."""
        return self._read().get("reference")

    def set_reference(self, name: str) -> None:
        devices = self._load()
        if name not in devices:
            raise DeviceNotFoundError(name)
        self._save(devices, reference=name)

    def list(self) -> list[Device]:
        return sorted(self._load().values(), key=lambda d: d.name)

    def get(self, name: str) -> Device:
        devices = self._load()
        if name not in devices:
            raise DeviceNotFoundError(name)
        return devices[name]

    def add(self, device: Device) -> None:
        devices = self._load()
        if device.name in devices:
            raise DeviceExistsError(device.name)
        devices[device.name] = device
        self._save(devices)

    def set_groups(self, name: str, groups: list[str]) -> Device:
        devices = self._load()
        if name not in devices:
            raise DeviceNotFoundError(name)
        devices[name] = replace(devices[name], groups=tuple(groups))
        self._save(devices)
        return devices[name]

    def resolve(
        self, names: list[str] | None = None, groups: list[str] | None = None, all_: bool = False
    ) -> list[Device]:
        """Return the devices selected by name, by group, or all of them, without duplicates."""
        devices = self._load()
        if all_:
            return sorted(devices.values(), key=lambda d: d.name)
        selected: dict[str, Device] = {}
        for name in names or []:
            if name not in devices:
                raise DeviceNotFoundError(name)
            selected[name] = devices[name]
        for group in groups or []:
            members = [d for d in devices.values() if group in d.groups]
            if not members:
                raise GroupNotFoundError(group)
            selected.update((d.name, d) for d in members)
        return sorted(selected.values(), key=lambda d: d.name)

    def remove(self, name: str) -> None:
        devices = self._load()
        if name not in devices:
            raise DeviceNotFoundError(name)
        del devices[name]
        self._save(devices)
