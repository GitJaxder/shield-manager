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


def rank_sources(
    candidates: list[str], target_abis: list[str], abis: dict[str, list[str]]
) -> list[str]:
    """Order the Shields an app could be copied from, best first for one target.

    A Play Store copy only carries native code for its own Shield's CPU types, so Shields
    with the same CPU types as the target come first, then ones whose main CPU type the
    target also runs. Otherwise the given order (e.g. reference first) is kept.
    """

    def key(name: str) -> tuple[bool, bool]:
        mine = abis.get(name) or []
        return mine != target_abis, not (mine and mine[0] in target_abis)

    return sorted(candidates, key=key)


def install_first_compatible(
    conn: Connection,
    package: str,
    version_code: int,
    sources: list[str],
    fetch: Callable[[str], list[Path]],
    **install_kwargs,
) -> tuple[deploy.InstalledVersion, str]:
    """Install an app from the first source whose copy this Shield can run.

    fetch(source) returns that Shield's APK files. Returns the installed version and the
    source used; raises IncompatibleAppError if no source's copy fits this Shield's CPU.
    """
    tried = []
    for source in sources:
        try:
            return deploy.install(
                conn, fetch(source), package, version_code, **install_kwargs
            ), source
        except deploy.IncompatibleAppError:
            tried.append(source)
    runs = ", ".join(deploy.device_abis(conn)) or "a different CPU type"
    raise deploy.IncompatibleAppError(
        f"not compatible with this Shield: the copies on {', '.join(tried)} are built for a "
        f"different CPU type than it runs ({runs}). Install it on this Shield from the Play "
        "Store instead."
    )


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

    # Other Shields already on the reference's version of an app can stand in for the
    # reference when its copy is built for a CPU type a target doesn't run.
    drifted = {r.device.name: {d.package for d in r.drift} for r in reports if r.error is None}
    by_name = {r.device.name: r.device for r in reports} | {reference.name: reference}
    abis: dict[str, list[str]] = {}

    def abis_of(name: str) -> list[str]:
        if name not in abis:
            try:
                with _connected(connect, by_name[name]) as conn:
                    abis[name] = deploy.device_abis(conn)
            except Exception:
                abis[name] = []
        return abis[name]

    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
        pulled: dict[tuple[str, str], list[Path]] = {}

        def fetch(package: str, source: str, on_progress: ProgressCallback | None) -> list[Path]:
            if (package, source) not in pulled:
                with _connected(connect, by_name[source]) as src_conn:
                    pulled[package, source] = deploy.pull_app(
                        src_conn,
                        package,
                        Path(tmp) / source / package,
                        progress=_from_source(on_progress, source),
                    )
            return pulled[package, source]

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
            name = report.device.name
            on_progress = _for_device(progress, name)
            try:
                with _connected(connect, report.device) as conn:
                    abis[name] = deploy.device_abis(conn)
                    for d in todo:
                        try:
                            if d.change is Change.EXTRA:
                                deploy.uninstall(conn, d.package, progress=on_progress)
                                report.applied[d.package] = "removed"
                                continue
                            others = [
                                n
                                for n, theirs in drifted.items()
                                if n != name and d.package not in theirs
                            ]
                            sources = [reference.name, *others]
                            if others:
                                sources = rank_sources(
                                    sources, abis[name], {n: abis_of(n) for n in sources}
                                )
                            _, source = install_first_compatible(
                                conn,
                                d.package,
                                d.reference_version,
                                sources,
                                lambda src, pkg=d.package, cb=on_progress: fetch(pkg, src, cb),
                                allow_downgrade=d.change is Change.NEWER,
                                progress=on_progress,
                            )
                            verb = _PAST_TENSE[d.change]
                            via = "" if source == reference.name else f" (copied from {source})"
                            report.applied[d.package] = f"{verb} {d.reference_version}{via}"
                            drifted[name].discard(d.package)  # can now be a source too
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
