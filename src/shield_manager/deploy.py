"""Install, update, remove and inspect apps on a connected Shield."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

REMOTE_TMP_DIR = "/data/local/tmp"
# Installing a large APK on a Shield can take well over the default shell timeout.
INSTALL_TIMEOUT_S = 300.0


class Connection(Protocol):
    def shell(self, command: str, **kwargs) -> str: ...

    def push(self, local_path: str, device_path: str, **kwargs) -> None: ...

    def pull(self, device_path: str, local_path: str, **kwargs) -> None: ...


class DeployError(Exception):
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


def install(
    conn: Connection,
    apk_paths: str | Path | Sequence[str | Path],
    package: str,
    version_code: int,
    allow_downgrade: bool = False,
) -> InstalledVersion:
    """Install or update an app and confirm the device now reports the expected version.

    Pass several paths for a split APK (a base.apk plus its config splits), as pulled from
    an app installed through the Play Store.
    """
    paths = (
        [Path(apk_paths)] if isinstance(apk_paths, (str, Path)) else [Path(p) for p in apk_paths]
    )
    if not paths:
        raise DeployError("no APK files given")
    remotes = [f"{REMOTE_TMP_DIR}/{package}.{i}.apk" for i in range(len(paths))]
    flags = "-r -d" if allow_downgrade else "-r"
    long_op = {"read_timeout_s": INSTALL_TIMEOUT_S, "timeout_s": INSTALL_TIMEOUT_S}
    try:
        for local, remote in zip(paths, remotes, strict=True):
            conn.push(str(local), remote, read_timeout_s=INSTALL_TIMEOUT_S)
        if len(paths) == 1:
            out = _shell(conn, f"pm install {flags} {shlex.quote(remotes[0])}", **long_op)
        else:
            out = _install_session(conn, paths, remotes, flags, long_op)
    finally:
        conn.shell("rm -f " + " ".join(shlex.quote(r) for r in remotes))
    if "Success" not in out:
        raise DeployError(out or "pm install gave no output")

    version = installed_version(conn, package)
    if version is None or version.version_code != version_code:
        found = version.version_code if version else "nothing"
        raise DeployError(
            f"installed, but device reports versionCode {found} (expected {version_code})"
        )
    return version


def _install_session(
    conn: Connection, paths: list[Path], remotes: list[str], flags: str, long_op: dict
) -> str:
    """Install split APKs together through a pm install session."""
    total = sum(p.stat().st_size for p in paths)
    created = _shell(conn, f"pm install-create {flags} -S {total}")
    match = re.search(r"\[(\d+)\]", created)
    if not match:
        return created
    session = match.group(1)
    for i, (local, remote) in enumerate(zip(paths, remotes, strict=True)):
        size = local.stat().st_size
        out = _shell(
            conn, f"pm install-write -S {size} {session} {i}_{local.name} {shlex.quote(remote)}"
        )
        if "Success" not in out:
            _shell(conn, f"pm install-abandon {session}")
            return out
    return _shell(conn, f"pm install-commit {session}", **long_op)


def _shell(conn: Connection, command: str, **kwargs) -> str:
    return str(conn.shell(command, **kwargs)).strip()


def uninstall(conn: Connection, package: str) -> None:
    out = str(conn.shell(f"pm uninstall {shlex.quote(package)}")).strip()
    if "Success" not in out:
        raise DeployError(out or "pm uninstall gave no output")


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


def pull_app(conn: Connection, package: str, dest_dir: Path) -> list[Path]:
    """Copy an installed app's APK files (base plus any splits) off the device."""
    out = str(conn.shell(f"pm path {shlex.quote(package)}"))
    remotes = [
        line.removeprefix("package:").strip()
        for line in out.splitlines()
        if line.startswith("package:")
    ]
    if not remotes:
        raise DeployError(f"{package} is not installed on the reference device")
    dest_dir.mkdir(parents=True, exist_ok=True)
    local_paths = []
    for remote in remotes:
        local = dest_dir / Path(remote).name
        conn.pull(remote, str(local), read_timeout_s=INSTALL_TIMEOUT_S)
        local_paths.append(local)
    return local_paths
