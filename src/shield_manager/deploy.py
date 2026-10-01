"""Install, update, remove and inspect apps on a connected Shield."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol

from shield_manager.apk import native_abis

REMOTE_TMP_DIR = "/data/local/tmp"
# Installing a large APK on a Shield can take well over the default shell timeout.
INSTALL_TIMEOUT_S = 300.0
# adb-shell gives up when the socket is silent for transport_timeout_s, whatever
# read_timeout_s says, and the connection's default is only a few seconds. A Shield sends
# nothing while pm installs an app or while it writes a large file, so every long step
# raises both limits.
_LONG = {"transport_timeout_s": INSTALL_TIMEOUT_S, "read_timeout_s": INSTALL_TIMEOUT_S}


class Connection(Protocol):
    def shell(self, command: str, **kwargs) -> str: ...

    def push(self, local_path: str, device_path: str, **kwargs) -> None: ...

    def pull(self, device_path: str, local_path: str, **kwargs) -> None: ...


class DeployError(Exception):
    pass


class IncompatibleAppError(DeployError):
    """The app's native code is built for a CPU type the Shield doesn't run."""


class Phase(str, Enum):
    DOWNLOADING = "downloading"  # copying an app's APK files off a (reference) Shield
    COPYING = "copying"  # copying APK files onto the Shield being installed to
    INSTALLING = "installing"  # the Shield's package manager is installing the app
    REMOVING = "removing"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class Progress:
    """One step of an install, update or removal, for progress displays.

    done and total are bytes for DOWNLOADING and COPYING and 0 otherwise. device (the
    Shield being changed) and source (the Shield an app is downloaded from) are set by
    callers that know them, such as fleet.sync and the CLI.
    """

    package: str
    phase: Phase
    done: int = 0
    total: int = 0
    device: str | None = None
    message: str = ""
    source: str | None = None

    @property
    def percent(self) -> int | None:
        if self.phase in (Phase.DOWNLOADING, Phase.COPYING) and self.total:
            return min(100, self.done * 100 // self.total)
        return None

    def describe(self) -> str:
        """Software-Center-style text, e.g. "Downloading org.xbmc.kodi from den - 40%"."""
        text = f"{self.phase.value.capitalize()} {self.package}"
        where = self.source or self.device if self.phase is Phase.DOWNLOADING else self.device
        if where:
            text += f" {_PREPOSITION[self.phase]} {where}"
        if self.percent is not None:
            text += f" - {self.percent}%"
        if self.message:
            text += f": {self.message}"
        return text


_PREPOSITION = {
    Phase.DOWNLOADING: "from",
    Phase.COPYING: "to",
    Phase.INSTALLING: "on",
    Phase.REMOVING: "from",
    Phase.DONE: "on",
    Phase.FAILED: "on",
}

ProgressCallback = Callable[[Progress], None]


class _Transfer:
    """Turns adb-shell's per-chunk callbacks into whole-percent Progress events."""

    def __init__(self, package: str, phase: Phase, total: int, progress: ProgressCallback):
        self.base = Progress(package, phase, 0, total)
        self.progress = progress
        self.done = 0
        self.last_percent = -1
        self._emit()

    def chunk(self, _path: str, size: int, _file_total: int) -> None:
        self.done += size
        self._emit()

    def _emit(self) -> None:
        event = replace(self.base, done=min(self.done, self.base.total))
        if event.percent != self.last_percent:
            self.last_percent = event.percent
            self.progress(event)


def _ignore(_: Progress) -> None:
    pass


@dataclass(frozen=True)
class InstalledVersion:
    version_code: int
    version_name: str


def installed_version(conn: Connection, package: str) -> InstalledVersion | None:
    """Return the installed version of a package, or None if it isn't installed."""
    out = str(conn.shell(f"dumpsys package {shlex.quote(package)}"))
    code = re.search(r"versionCode=(\d+)", out)
    if not code:
        return None
    name = re.search(r"versionName=(.*)", out)
    return InstalledVersion(int(code.group(1)), name.group(1).strip() if name else "")


def device_abis(conn: Connection) -> list[str]:
    """CPU ABIs the device runs, preferred first (e.g. ["arm64-v8a", "armeabi-v7a"])."""
    out = str(conn.shell("getprop ro.product.cpu.abilist")).strip()
    return [abi.strip() for abi in out.split(",") if abi.strip()]


def _check_abis(conn: Connection, paths: list[Path]) -> None:
    needed = native_abis(paths)
    supported = device_abis(conn) if needed else []
    if supported and not needed & set(supported):
        raise IncompatibleAppError(_abi_message(needed, supported))


def _abi_message(needed: set[str] | None, supported: list[str]) -> str:
    built = f"built for {', '.join(sorted(needed))}" if needed else "built for another CPU type"
    runs = f" only runs {', '.join(supported)}" if supported else " can't run it"
    return (
        f"this copy of the app is {built}, but this Shield{runs}. Install it on this "
        "Shield from the Play Store, or copy it from a Shield of the same model."
    )


def install(
    conn: Connection,
    apk_paths: str | Path | Sequence[str | Path],
    package: str,
    version_code: int,
    allow_downgrade: bool = False,
    progress: ProgressCallback | None = None,
) -> InstalledVersion:
    """Install or update an app and confirm the device now reports the expected version.

    Pass several paths for a split APK (a base.apk plus its config splits), as pulled from
    an app installed through the Play Store. progress, if given, receives COPYING events
    with byte counts, then INSTALLING, then DONE.
    """
    progress = progress or _ignore
    paths = (
        [Path(apk_paths)] if isinstance(apk_paths, (str, Path)) else [Path(p) for p in apk_paths]
    )
    if not paths:
        raise DeployError("no APK files given")
    remotes = [f"{REMOTE_TMP_DIR}/{package}.{i}.apk" for i in range(len(paths))]
    flags = "-r -d" if allow_downgrade else "-r"
    long_op = {**_LONG, "timeout_s": INSTALL_TIMEOUT_S}
    _check_abis(conn, paths)
    try:
        copy = _Transfer(package, Phase.COPYING, _local_size(paths), progress)
        for local, remote in zip(paths, remotes, strict=True):
            conn.push(str(local), remote, progress_callback=copy.chunk, **_LONG)
        progress(Progress(package, Phase.INSTALLING))
        if len(paths) == 1:
            out = _shell(conn, f"pm install {flags} {shlex.quote(remotes[0])}", **long_op)
        else:
            out = _install_session(conn, paths, remotes, flags, long_op)
    finally:
        conn.shell("rm -f " + " ".join(shlex.quote(r) for r in remotes))
    if "INSTALL_FAILED_NO_MATCHING_ABIS" in out:
        raise IncompatibleAppError(f"{_abi_message(native_abis(paths), device_abis(conn))} ({out})")
    if "Success" not in out:
        raise DeployError(out or "pm install gave no output")

    version = installed_version(conn, package)
    if version is None or version.version_code != version_code:
        found = version.version_code if version else "nothing"
        raise DeployError(
            f"installed, but device reports versionCode {found} (expected {version_code})"
        )
    progress(Progress(package, Phase.DONE))
    return version


def _install_session(
    conn: Connection, paths: list[Path], remotes: list[str], flags: str, long_op: dict
) -> str:
    """Install split APKs together through a pm install session."""
    total = sum(p.stat().st_size for p in paths)
    created = _shell(conn, f"pm install-create {flags} -S {total}", **long_op)
    match = re.search(r"\[(\d+)\]", created)
    if not match:
        return created
    session = match.group(1)
    for i, (local, remote) in enumerate(zip(paths, remotes, strict=True)):
        size = local.stat().st_size
        out = _shell(
            conn,
            f"pm install-write -S {size} {session} {i}_{local.name} {shlex.quote(remote)}",
            **long_op,
        )
        if "Success" not in out:
            _shell(conn, f"pm install-abandon {session}")
            return out
    return _shell(conn, f"pm install-commit {session}", **long_op)


def _shell(conn: Connection, command: str, **kwargs) -> str:
    return str(conn.shell(command, **kwargs)).strip()


def uninstall(conn: Connection, package: str, progress: ProgressCallback | None = None) -> None:
    progress = progress or _ignore
    progress(Progress(package, Phase.REMOVING))
    out = str(conn.shell(f"pm uninstall {shlex.quote(package)}")).strip()
    if "Success" not in out:
        raise DeployError(out or "pm uninstall gave no output")
    progress(Progress(package, Phase.DONE))


def list_packages(conn: Connection, include_system: bool = False) -> list[str]:
    flag = "" if include_system else " -3"
    out = str(conn.shell(f"pm list packages{flag}"))
    return sorted(
        line.removeprefix("package:").strip()
        for line in out.splitlines()
        if line.startswith("package:")
    )


def package_versions(conn: Connection) -> dict[str, int]:
    """Return {package: versionCode} for every third-party app on the device."""
    out = str(conn.shell("pm list packages -3 --show-versioncode"))
    versions = {}
    for line in out.splitlines():
        match = re.match(r"package:(\S+)\s+versionCode:(\d+)", line.strip())
        if match:
            versions[match.group(1)] = int(match.group(2))
    return versions


def pull_app(
    conn: Connection, package: str, dest_dir: Path, progress: ProgressCallback | None = None
) -> list[Path]:
    """Copy an installed app's APK files (base plus any splits) off the device.

    progress, if given, receives DOWNLOADING events with byte counts.
    """
    out = str(conn.shell(f"pm path {shlex.quote(package)}"))
    remotes = [
        line.removeprefix("package:").strip()
        for line in out.splitlines()
        if line.startswith("package:")
    ]
    if not remotes:
        raise DeployError(f"{package} is not installed on the reference device")
    dest_dir.mkdir(parents=True, exist_ok=True)
    total = _remote_size(conn, remotes) if progress else 0
    download = _Transfer(package, Phase.DOWNLOADING, total, progress or _ignore)
    local_paths = []
    for remote in remotes:
        local = dest_dir / Path(remote).name
        conn.pull(remote, str(local), progress_callback=download.chunk, **_LONG)
        local_paths.append(local)
    return local_paths


def _local_size(paths: list[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.exists())


def _remote_size(conn: Connection, remotes: list[str]) -> int:
    """Total size in bytes of files on the device, or 0 if it can't be read."""
    out = str(conn.shell("stat -c %s " + " ".join(shlex.quote(r) for r in remotes)))
    sizes = [int(line) for line in out.split() if line.isdigit()]
    return sum(sizes) if len(sizes) == len(remotes) else 0
