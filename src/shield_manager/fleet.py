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
from shield_manager.apk import ApkError, read_apk_info, signers
from shield_manager.deploy import Connection, Phase, Progress, ProgressCallback
from shield_manager.registry import Device
from shield_manager.sources import Downloader, SourceUnavailable, Wanted, version_key

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


GITHUB, APKPURE = "GitHub", "APKPure"
DOWNLOADS = (GITHUB, APKPURE)


def source_order(
    shields: list[str],
    target_abis: list[str],
    abis: dict[str, list[str]],
    package: str,
    downloads: Downloader | None = None,
) -> list[str]:
    """Every source to try for one target, best first.

    Shields whose copy the target can likely run come first, then downloads (GitHub, when
    the app's repository is known, then APKPure), then the remaining Shields.
    """
    ranked = rank_sources(shields, target_abis, abis)
    if downloads is None:
        return ranked
    likely = [n for n in ranked if (abis.get(n) or [""])[0] in target_abis]
    online = ([GITHUB] if downloads.github_repo(package) else []) + [APKPURE]
    return likely + online + [n for n in ranked if n not in likely]


Pull = Callable[[str, str, Path, ProgressCallback | None], list[Path]]


class Fetcher:
    """One app's APK files from Shields or downloads, each fetched at most once.

    pull(shield, package, dest, progress) copies the app off a Shield. Downloads are only
    used when they're signed by the same developer as a copy on one of holders (the
    Shields that have the app), so a tampered download is never installed.
    """

    def __init__(
        self,
        package: str,
        version_code: int,
        holders: list[str],
        pull: Pull,
        tmp: Path,
        downloads: Downloader | None = None,
        update_to: str | None = None,
    ):
        self.package, self.version_code, self.holders = package, version_code, holders
        self.pull, self.tmp, self.downloads = pull, tmp, downloads
        # When set, downloads fetch this newer version name, and any versionCode at least
        # version_code is accepted (see check_updates).
        self.update_to = update_to
        self.cache: dict[object, list[Path]] = {}

    def get(
        self, source: str, target_abis: list[str], progress: ProgressCallback | None = None
    ) -> list[Path]:
        if source not in DOWNLOADS:
            if source not in self.cache:
                self.cache[source] = self.pull(
                    source, self.package, self.tmp / source / self.package, progress
                )
            return self.cache[source]
        key = (source, tuple(target_abis))
        if key not in self.cache:
            self.cache[key] = self._download(source, target_abis, progress)
        return self.cache[key]

    def _download(
        self, source: str, target_abis: list[str], progress: ProgressCallback | None
    ) -> list[Path]:
        if self.downloads is None:
            raise SourceUnavailable("downloads are turned off")
        trusted, version_name = self._trusted()
        if not trusted:
            raise SourceUnavailable(
                "can't check who signed a download, because the copy on your Shields has no "
                "modern (v2+) signature"
            )
        if self.update_to:
            wanted = Wanted(self.package, self.version_code, self.update_to, target_abis, True)
        else:
            wanted = Wanted(self.package, self.version_code, version_name, target_abis)
        dest = self.tmp / source.lower() / self.package / "-".join(target_abis)
        on_bytes = None
        if progress:
            transfer = deploy.ByteProgress(self.package, Phase.DOWNLOADING, progress, source)
            on_bytes = transfer.update
        fetch = self.downloads.github if source == GITHUB else self.downloads.apkpure
        paths = fetch(wanted, dest, on_bytes)
        for path in paths:
            if not signers(path) & trusted:
                raise SourceUnavailable(
                    f"its copy is signed by a different developer than the one on your Shields"
                    f" ({path.name}), so it wasn't installed"
                )
        return paths

    def _trusted(self) -> tuple[set[str], str]:
        """Signing certificates and version name of the copy on a Shield that has the app."""
        errors = []
        for holder in self.holders:
            try:
                paths = self.get(holder, [])
            except Exception as e:
                errors.append(f"{holder}: {e}")
                continue
            certs = set().union(*(signers(p) for p in paths)) if paths else set()
            version_name = ""
            for path in paths:
                try:
                    version_name = read_apk_info(path).version_name
                    break
                except ApkError:
                    continue
            return certs, version_name
        raise SourceUnavailable("couldn't read the copy on any Shield: " + "; ".join(errors))


def install_first_compatible(
    conn: Connection,
    package: str,
    version_code: int,
    sources: list[str],
    fetch: Callable[[str], list[Path]],
    **install_kwargs,
) -> tuple[deploy.InstalledVersion, str]:
    """Install an app from the first source whose copy this Shield can run.

    fetch(source) returns that source's APK files, raising (e.g. SourceUnavailable) when
    it has none to offer. Returns the installed version and the source used; raises
    IncompatibleAppError, saying what each source lacked, if none worked.
    """
    wrong_cpu, reasons = [], []
    for source in sources:
        try:
            paths = fetch(source)
        except Exception as e:  # unreachable Shield, nothing to download: try the next one
            reasons.append(f"{source}: {e}")
            continue
        try:
            return deploy.install(conn, paths, package, version_code, **install_kwargs), source
        except deploy.IncompatibleAppError:
            wrong_cpu.append(source)
    runs = ", ".join(deploy.device_abis(conn)) or "a different CPU type"
    if wrong_cpu:
        reasons.insert(
            0,
            f"the copies on {', '.join(wrong_cpu)} are built for a different CPU type than "
            f"it runs ({runs})",
        )
    hint = f"`shield-manager app store-page {package} -d <shield>` opens the page there"
    raise deploy.IncompatibleAppError(
        f"not compatible with this Shield: {'; '.join(reasons)}. Install it on this Shield "
        f"from the Play Store instead ({hint})."
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
    downloads: Downloader | None = None,
) -> list[DeviceReport]:
    """Make each target's apps match the reference.

    Missing and outdated apps are copied from the reference. Apps that are newer on a
    target are left alone unless allow_downgrade is set, and apps the reference doesn't
    have are only removed when prune is set, because removing an app deletes its data.

    When a target can't run any Shield's copy of an app (Shields of different models run
    different CPU types), downloads, if given, are tried before giving up.

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

    def pull(
        source: str, package: str, dest: Path, on_progress: ProgressCallback | None
    ) -> list[Path]:
        with _connected(connect, by_name[source]) as src_conn:
            return deploy.pull_app(
                src_conn, package, dest, progress=_from_source(on_progress, source)
            )

    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
        fetchers: dict[str, Fetcher] = {}

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
                            holders = [reference.name, *others]
                            fetcher = fetchers.setdefault(
                                d.package,
                                Fetcher(
                                    d.package,
                                    d.reference_version,
                                    holders,
                                    pull,
                                    Path(tmp),
                                    downloads,
                                ),
                            )
                            fetcher.holders = holders
                            sources = holders
                            if others or downloads:
                                sources = source_order(
                                    holders,
                                    abis[name],
                                    {n: abis_of(n) for n in holders},
                                    d.package,
                                    downloads,
                                )
                            _, source = install_first_compatible(
                                conn,
                                d.package,
                                d.reference_version,
                                sources,
                                lambda src, f=fetcher, a=abis[name], cb=on_progress: f.get(
                                    src, a, cb
                                ),
                                allow_downgrade=d.change is Change.NEWER,
                                progress=on_progress,
                            )
                            verb = _PAST_TENSE[d.change]
                            via = ""
                            if source in DOWNLOADS:
                                via = f" (downloaded from {source})"
                            elif source != reference.name:
                                via = f" (copied from {source})"
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


@dataclass
class Update:
    """A newer version of an installed app that GitHub or APKPure offers."""

    package: str
    installed_name: str
    installed_code: int
    latest_name: str
    source: str  # GITHUB or APKPURE
    shields: list[str]  # Shields with the app installed
    # Filled in by apply_updates: Shield -> outcome, and Shields that failed.
    applied: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)


def check_updates(
    devices: list[Device], connect: Connector, downloads: Downloader
) -> tuple[list[Update], dict[str, str]]:
    """Find installed apps with a newer version on GitHub or APKPure.

    Each app is checked once, at the newest version on any Shield. Version names are
    compared by their numbers ("1.10" is newer than "1.9"). Returns the updates and
    {Shield: error} for Shields that couldn't be read.
    """
    versions: dict[str, dict[str, int]] = {}
    abis: dict[str, list[str]] = {}
    errors = {}
    for device in devices:
        try:
            with _connected(connect, device) as conn:
                versions[device.name] = deploy.package_versions(conn)
                abis[device.name] = deploy.device_abis(conn)
        except Exception as e:
            errors[device.name] = str(e) or type(e).__name__
    by_name = {d.name: d for d in devices}
    updates = []
    for package in sorted({p for v in versions.values() for p in v}):
        holders = [n for n, v in versions.items() if package in v]
        newest = max(versions[n][package] for n in holders)
        holder = next(n for n in holders if versions[n][package] == newest)
        try:
            with _connected(connect, by_name[holder]) as conn:
                installed = deploy.installed_version(conn, package)
        except Exception:
            continue
        if not installed or not version_key(installed.version_name):
            continue
        latest = downloads.latest(package, abis[holder])
        if latest and version_key(latest[0]) > version_key(installed.version_name):
            updates.append(
                Update(package, installed.version_name, newest, latest[0], latest[1], holders)
            )
    return updates, errors


def apply_updates(
    updates: list[Update],
    devices: list[Device],
    connect: Connector,
    downloads: Downloader,
    cache_dir: Path | None = None,
    progress: ProgressCallback | None = None,
) -> list[Update]:
    """Install each update on every Shield that has the app, with the build for its CPU
    type, only when the download is signed like the installed copy."""
    by_name = {d.name: d for d in devices}

    def pull(
        source: str, package: str, dest: Path, on_progress: ProgressCallback | None
    ) -> list[Path]:
        with _connected(connect, by_name[source]) as src_conn:
            return deploy.pull_app(src_conn, package, dest)

    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
        for update in updates:
            fetcher = Fetcher(
                update.package,
                update.installed_code + 1,
                update.shields,
                pull,
                Path(tmp),
                downloads,
                update_to=update.latest_name,
            )
            sources = [update.source, *(s for s in DOWNLOADS if s != update.source)]
            for name in update.shields:
                on_progress = _for_device(progress, name)
                try:
                    with _connected(connect, by_name[name]) as conn:
                        target_abis = deploy.device_abis(conn)
                        reasons = []
                        for source in sources:
                            try:
                                paths = fetcher.get(source, target_abis, on_progress)
                                info = read_apk_info(paths[0])
                                deploy.install(
                                    conn,
                                    paths,
                                    update.package,
                                    info.version_code,
                                    progress=on_progress,
                                )
                            except (SourceUnavailable, deploy.IncompatibleAppError) as e:
                                reasons.append(f"{source}: {e}")
                                continue
                            update.applied[name] = f"updated to {info.version_name} (from {source})"
                            break
                        else:
                            raise SourceUnavailable("; ".join(reasons))
                except Exception as e:
                    update.failed[name] = str(e) or type(e).__name__
                    if on_progress:
                        on_progress(
                            Progress(update.package, Phase.FAILED, message=update.failed[name])
                        )
    return updates
