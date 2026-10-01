"""Compare each Shield's installed apps against a reference Shield.

This is a stopgap for the web UI until the core library's mirror mode lands; once it
does, the UI should call that instead and this module can go.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shield_manager.deploy import Connection


@dataclass(frozen=True)
class AppVersion:
    package: str
    version_code: int


@dataclass
class DeviceDrift:
    device: str
    missing: list[AppVersion] = field(default_factory=list)  # on the reference, not here
    extra: list[AppVersion] = field(default_factory=list)  # here, not on the reference
    outdated: list[AppVersion] = field(default_factory=list)  # older here than the reference
    newer: list[AppVersion] = field(default_factory=list)  # newer here than the reference
    error: str | None = None

    @property
    def in_sync(self) -> bool:
        return self.error is None and not (self.missing or self.outdated or self.newer)


def installed_apps(conn: Connection) -> dict[str, int]:
    """Return third-party package -> versionCode, read in one shell call."""
    out = str(conn.shell("pm list packages -3 --show-versioncode"))
    apps: dict[str, int] = {}
    for line in out.splitlines():
        if not line.startswith("package:"):
            continue
        parts = line.removeprefix("package:").split()
        if not parts:
            continue
        code = 0
        for part in parts[1:]:
            if part.startswith("versionCode:"):
                code = int(part.removeprefix("versionCode:") or 0)
        apps[parts[0]] = code
    return apps


def compare(device: str, reference: dict[str, int], apps: dict[str, int]) -> DeviceDrift:
    drift = DeviceDrift(device)
    for package in sorted(reference.keys() | apps.keys()):
        want, have = reference.get(package), apps.get(package)
        if have is None:
            drift.missing.append(AppVersion(package, want))
        elif want is None:
            drift.extra.append(AppVersion(package, have))
        elif have < want:
            drift.outdated.append(AppVersion(package, have))
        elif have > want:
            drift.newer.append(AppVersion(package, have))
    return drift
