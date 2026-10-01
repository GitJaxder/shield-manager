"""Persistent registry of known Shield devices, stored as JSON."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_ADB_PORT = 5555


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

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class DeviceExistsError(Exception):
    pass


class DeviceNotFoundError(Exception):
    pass


class Registry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_dir() / "devices.json"

    def _load(self) -> dict[str, Device]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        return {d["name"]: Device(**d) for d in data.get("devices", [])}

    def _save(self, devices: dict[str, Device]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"devices": [asdict(d) for d in sorted(devices.values(), key=lambda d: d.name)]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self.path)

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

    def remove(self, name: str) -> None:
        devices = self._load()
        if name not in devices:
            raise DeviceNotFoundError(name)
        del devices[name]
        self._save(devices)
