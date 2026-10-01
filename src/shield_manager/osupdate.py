"""Check which Shield Experience (OS) version each Shield runs and whether it's behind.

NVIDIA publishes no machine-readable list of the latest version, and a Shield can't be
told to install an update over ADB. So the check combines what the Shield itself says
(its version, and the "system upgrade" notification it shows when NVIDIA offers one)
with a comparison against the other Shields of the same hardware: one that's behind a
twin is due an update. Installing stays on the TV: open_update_screen brings up the
System upgrade screen so it can be started with the remote.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from shield_manager.deploy import Connection

OTA_PACKAGE = "com.nvidia.ota"

# Shield hardware by ro.product.device. Updates go out per hardware, so versions are
# only compared between Shields of the same kind.
HARDWARE = {
    "foster": "SHIELD TV (2015)",
    "foster_e": "SHIELD TV (2015)",
    "foster_e_hdd": "SHIELD TV Pro (2015)",
    "darcy": "SHIELD TV (2017)",
    "sif": "SHIELD TV (2019)",
    "mdarcy": "SHIELD TV Pro (2019)",
}

# Properties that may hold the Shield Experience version (e.g. "9.2.4"), most specific
# first. Any other NVIDIA/Shield/OTA "version" property with a dotted number is used
# when none of these is set.
VERSION_PROPS = (
    "ro.build.version.shield",
    "ro.nv.build.version",
    "ro.nvidia.build.version",
    "ro.build.shield.version",
    "ro.build.version.ota",
)

_PROP_LINE = re.compile(r"^\[([^\]]+)\]: \[(.*)\]$")
_DOTTED = re.compile(r"^\d+(?:\.\d+){1,3}$")
_NV_KEY = re.compile(r"(?:^|[._])(?:nv|nvidia|shield|ota)(?:[._]|$)")


@dataclass(frozen=True)
class OsInfo:
    hardware: str  # ro.product.device, e.g. "mdarcy"
    model: str  # friendly name, e.g. "SHIELD TV Pro (2019)"
    version: str | None  # Shield Experience version, e.g. "9.2.4", if the Shield reports it
    android: str
    build: str
    security_patch: str
    # The text of the Shield's own "system upgrade" notification, if it's showing one.
    update_notice: str | None = None
    # False when the OTA updater is disabled or removed: this Shield will never update.
    updater_enabled: bool = True


@dataclass
class OsStatus:
    device: str
    info: OsInfo | None = None
    error: str | None = None
    # Newest version seen on another Shield of the same hardware, when this one is older.
    behind: str | None = None
    newest: str | None = None  # newest version among Shields of this hardware

    @property
    def update_available(self) -> bool:
        return bool(self.info and (self.info.update_notice or self.behind))

    def describe(self) -> str:
        if self.info is None:
            return f"FAILED: {self.error}"
        i = self.info
        parts = [
            i.model,
            f"Shield Experience {i.version or 'version unknown'}",
            f"Android {i.android}",
            f"security patch {i.security_patch or 'unknown'}",
            f"build {i.build}",
        ]
        if i.update_notice:
            parts.append(f"UPDATE OFFERED: {i.update_notice}")
        if self.behind:
            parts.append(f"BEHIND: another {i.model} runs {self.behind}")
        if not i.updater_enabled:
            parts.append("updates are switched off (the NVIDIA updater is disabled)")
        if not self.update_available and i.updater_enabled:
            parts.append("up to date as far as the Shield and its twins know")
        return " · ".join(parts)


def parse_props(text: str) -> dict[str, str]:
    """Parse the output of a bare `getprop`."""
    props = {}
    for line in text.splitlines():
        if m := _PROP_LINE.match(line.strip()):
            props[m.group(1)] = m.group(2)
    return props


def find_version(props: dict[str, str]) -> str | None:
    for key in VERSION_PROPS:
        if _DOTTED.match(props.get(key, "").strip()):
            return props[key].strip()
    for key in sorted(props):
        value = props[key].strip()
        if "version" in key and _NV_KEY.search(key) and _DOTTED.match(value):
            return value
    return None


def version_props(props: dict[str, str]) -> dict[str, str]:
    """Every property that looks like it names a version or build, for diagnosing a
    Shield whose Shield Experience version isn't found."""
    return {k: v for k, v in sorted(props.items()) if re.search(r"version|build|nv|shield", k)}


def _update_notice(notifications: str) -> str | None:
    """Return the title (and text) of a notification posted by NVIDIA's updater."""
    for record in re.split(r"\n\s*NotificationRecord\(", notifications):
        if f"pkg={OTA_PACKAGE}" not in record:
            continue
        title = re.search(r"android\.title=(?:String \()?([^)\n]+)", record)
        text = re.search(r"android\.text=(?:String \()?([^)\n]+)", record)
        words = [m.group(1).strip() for m in (title, text) if m and m.group(1).strip()]
        return " - ".join(words) or "an update is waiting on the Shield"
    return None


def read_os(conn: Connection) -> OsInfo:
    props = parse_props(str(conn.shell("getprop")))
    hardware = props.get("ro.product.device", "")
    enabled = str(conn.shell(f"pm list packages -e {OTA_PACKAGE}"))
    notifications = str(conn.shell("dumpsys notification --noredact"))
    return OsInfo(
        hardware=hardware,
        model=HARDWARE.get(hardware) or props.get("ro.product.model") or hardware or "Shield",
        version=find_version(props),
        android=props.get("ro.build.version.release", ""),
        build=props.get("ro.build.display.id", ""),
        security_patch=props.get("ro.build.version.security_patch", ""),
        update_notice=_update_notice(notifications),
        updater_enabled=f"package:{OTA_PACKAGE}" in enabled.split(),
    )


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def compare(statuses: Iterable[OsStatus]) -> list[OsStatus]:
    """Fill in newest/behind by comparing Shields of the same hardware."""
    statuses = list(statuses)
    newest: dict[str, str] = {}
    for s in statuses:
        if s.info and s.info.version:
            best = newest.get(s.info.hardware)
            if best is None or _key(s.info.version) > _key(best):
                newest[s.info.hardware] = s.info.version
    for s in statuses:
        if s.info and s.info.version:
            s.newest = newest[s.info.hardware]
            if _key(s.info.version) < _key(s.newest):
                s.behind = s.newest
    return statuses


def check(devices, connect: Callable) -> list[OsStatus]:
    """Read every device's OS details (one failing doesn't stop the rest) and compare."""
    statuses = []
    for device in devices:
        status = OsStatus(device.name)
        try:
            conn = connect(device)
            try:
                status.info = read_os(conn)
            finally:
                conn.close()
        except Exception as e:  # report per device
            status.error = str(e) or type(e).__name__
        statuses.append(status)
    return compare(statuses)


def open_update_screen(conn: Connection) -> str:
    """Wake the Shield and open its System upgrade screen; falls back to About, which
    has the "System upgrade" entry, if the direct screen isn't there. Returns which
    screen opened."""
    conn.shell("input keyevent KEYCODE_WAKEUP")
    for action, screen in (
        ("android.settings.SYSTEM_UPDATE_SETTINGS", "System upgrade"),
        ("android.settings.DEVICE_INFO_SETTINGS", "About"),
    ):
        out = str(conn.shell(f"am start -a {action}"))
        if "Error" not in out and "unable to resolve" not in out.lower():
            return screen
    raise RuntimeError("the Shield couldn't open its update screen")
