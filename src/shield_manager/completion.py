"""Shell Tab completion for device, group and app package names.

App names come from a local cache that the CLI fills whenever it reads a Shield's app
list, so completing never has to connect to a device.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from shield_manager.registry import Registry, default_config_dir

SHELLS = ("bash", "zsh", "fish", "powershell")


def package_cache_path() -> Path:
    return default_config_dir() / "packages.json"


def known_packages(path: Path | None = None) -> list[str]:
    path = path or package_cache_path()
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def remember_packages(packages: Iterable[str], path: Path | None = None) -> None:
    path = path or package_cache_path()
    merged = sorted(set(known_packages(path)) | set(packages))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=0) + "\n")
    except OSError:
        pass  # completion is a convenience; never fail a command over it


def complete_devices(prefix: str, **_) -> list[str]:
    return [d.name for d in Registry().list() if d.name.startswith(prefix)]


def complete_groups(prefix: str, **_) -> list[str]:
    groups = {g for d in Registry().list() for g in d.groups}
    return sorted(g for g in groups if g.startswith(prefix))


def complete_packages(prefix: str, **_) -> list[str]:
    return [p for p in known_packages() if p.startswith(prefix)]


def shell_script(shell: str) -> str:
    import argcomplete

    return argcomplete.shellcode(["shield-manager"], shell=shell)
