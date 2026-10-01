"""Install, update, remove and inspect apps on a connected Shield."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from shield_manager.apk import ApkInfo

REMOTE_TMP_DIR = "/data/local/tmp"
# Installing a large APK on a Shield can take well over the default shell timeout.
INSTALL_TIMEOUT_S = 300.0


class Connection(Protocol):
    def shell(self, command: str, **kwargs) -> str: ...

    def push(self, local_path: str, device_path: str, **kwargs) -> None: ...


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
    conn: Connection, apk_path: str | Path, info: ApkInfo, allow_downgrade: bool = False
) -> InstalledVersion:
    """Install or update an APK and confirm the device now reports its version."""
    remote = f"{REMOTE_TMP_DIR}/{info.package}.apk"
    conn.push(str(apk_path), remote, read_timeout_s=INSTALL_TIMEOUT_S)
    flags = "-r -d" if allow_downgrade else "-r"
    try:
        out = str(
            conn.shell(
                f"pm install {flags} {shlex.quote(remote)}",
                read_timeout_s=INSTALL_TIMEOUT_S,
                timeout_s=INSTALL_TIMEOUT_S,
            )
        ).strip()
    finally:
        conn.shell(f"rm -f {shlex.quote(remote)}")
    if "Success" not in out:
        raise DeployError(out or "pm install gave no output")

    version = installed_version(conn, info.package)
    if version is None or version.version_code != info.version_code:
        found = version.version_code if version else "nothing"
        raise DeployError(
            f"installed, but device reports versionCode {found} (expected {info.version_code})"
        )
    return version


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
