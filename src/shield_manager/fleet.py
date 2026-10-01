"""Keep every Shield's installed apps in line with a reference Shield.

This module holds the mirroring logic with no printing or argument parsing, so it can be
reused outside the CLI (for example by a Home Assistant integration).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

from shield_manager import deploy
from shield_manager.deploy import Connection, Phase, Progress, ProgressCallback
from shield_manager.registry import Device

Connector = Callable[[Device], Connection]


class Change(Enum):
    INSTALL = "install"  # on the reference, missing here
    UPDATE = "update"  # older here than on the reference
    NEWER = "newer"  # newer here than on the reference
    EXTRA = "extra"  # here, but not on the reference


_PAST_TENSE = {
    Change.INSTALL: "installed",
    Change.UPDATE: "updated to",
    Change.NEWER: "downgraded to",
}


@dataclass(frozen=True)
class Drift:
    package: str
    change: Change
    reference_version: int | None
    device_version: int | None


@dataclass
class DeviceReport:
    device: Device
    drift: list[Drift] = field(default_factory=list)
    error: str | None = None
    # Filled in by sync: package -> outcome message, and packages that failed.
    applied: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def in_sync(self) -> bool:
        return self.error is None and not self.drift


def compare(reference: dict[str, int], device: dict[str, int]) -> list[Drift]:
    """List how a device's {package: versionCode} differs from the reference's."""
    drift = []
    for package in sorted(reference.keys() | device.keys()):
        ref, dev = reference.get(package), device.get(package)
        if dev is None:
            change = Change.INSTALL
        elif ref is None:
            change = Change.EXTRA
        elif dev < ref:
            change = Change.UPDATE
        elif dev > ref:
            change = Change.NEWER
        else:
            continue
        drift.append(Drift(package, change, ref, dev))
    return drift


def _for_device(progress: ProgressCallback | None, name: str) -> ProgressCallback | None:
    if progress is None:
        return None
    return lambda event: progress(replace(event, device=name))


def _from_source(progress: ProgressCallback | None, name: str) -> ProgressCallback | None:
    if progress is None:
        return None
    return lambda event: progress(replace(event, source=name))


@contextmanager
def _connected(connect: Connector, device: Device) -> Iterator[Connection]:
    conn = connect(device)
    try:
        yield conn
    finally:
        close = getattr(conn, "close", None)
        if close:
            close()


def status(reference: Device, targets: list[Device], connect: Connector) -> list[DeviceReport]:
    """Report each target's drift from the reference without changing anything."""
    with _connected(connect, reference) as conn:
        ref_versions = deploy.package_versions(conn)
    reports = []
    for device in targets:
        if device.name == reference.name:
            continue
        report = DeviceReport(device)
        try:
            with _connected(connect, device) as conn:
                report.drift = compare(ref_versions, deploy.package_versions(conn))
        except Exception as e:  # one unreachable Shield shouldn't hide the others
            report.error = str(e) or type(e).__name__
        reports.append(report)
    return reports


def sync(
    reference: Device,
    targets: list[Device],
    connect: Connector,
    prune: bool = False,
    allow_downgrade: bool = False,
    dry_run: bool = False,
    cache_dir: Path | None = None,
    progress: ProgressCallback | None = None,
) -> list[DeviceReport]:
    """Make each target's apps match the reference.

    Missing and outdated apps are copied from the reference. Apps that are newer on a
    target are left alone unless allow_downgrade is set, and apps the reference doesn't
    have are only removed when prune is set, because removing an app deletes its data.

    progress, if given, receives deploy.Progress events with device set to the Shield
    being updated (including while an app is downloaded from the reference for it), and a
    FAILED event when an app can't be installed or removed.
    """
    reports = status(reference, targets, connect)
    wanted = {Change.INSTALL, Change.UPDATE} | ({Change.NEWER} if allow_downgrade else set())
    if dry_run:
        return reports

    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
        pulled: dict[str, list[Path]] = {}

        def apks_for(package: str, on_progress: ProgressCallback | None) -> list[Path]:
            if package not in pulled:
                with _connected(connect, reference) as ref_conn:
                    pulled[package] = deploy.pull_app(
                        ref_conn,
                        package,
                        Path(tmp) / package,
                        progress=_from_source(on_progress, reference.name),
                    )
            return pulled[package]

        for report in reports:
            if report.error:
                continue
            todo = [
                d
                for d in report.drift
                if d.change in wanted or (prune and d.change is Change.EXTRA)
            ]
            if not todo:
                continue
            on_progress = _for_device(progress, report.device.name)
            try:
                with _connected(connect, report.device) as conn:
                    for d in todo:
                        try:
                            if d.change is Change.EXTRA:
                                deploy.uninstall(conn, d.package, progress=on_progress)
                                report.applied[d.package] = "removed"
                            else:
                                deploy.install(
                                    conn,
                                    apks_for(d.package, on_progress),
                                    d.package,
                                    d.reference_version,
                                    allow_downgrade=d.change is Change.NEWER,
                                    progress=on_progress,
                                )
                                verb = _PAST_TENSE[d.change]
                                report.applied[d.package] = f"{verb} {d.reference_version}"
                        except Exception as e:
                            report.failed[d.package] = str(e) or type(e).__name__
                            if on_progress:
                                on_progress(
                                    Progress(
                                        d.package, Phase.FAILED, message=report.failed[d.package]
                                    )
                                )
            except Exception as e:
                report.error = str(e) or type(e).__name__
    return reports
