"""What the web UI can do, independent of HTTP so it's easy to test."""

from __future__ import annotations

import inspect
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from functools import partial
from http import HTTPStatus
from pathlib import Path
from typing import Any

from shield_manager import appinfo, deploy, fleet, screen
from shield_manager.apk import ApkError, read_apk_info
from shield_manager.registry import (
    DEFAULT_ADB_PORT,
    Device,
    DeviceExistsError,
    DeviceNotFoundError,
    Registry,
    default_config_dir,
)
from shield_manager.sources import Downloader
from shield_manager.web.jobs import (
    COPYING,
    DONE,
    DOWNLOADING,
    FAILED,
    INSTALLING,
    Job,
    Jobs,
    Step,
)

DEVICE_NAME = re.compile(r"^[\w.-]+$")
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9_.]+$")
# Viewers asking within this many seconds of the last capture share it instead of taking
# another, so several open pages don't multiply the work on the Shields.
SCREEN_MAX_AGE_S = 5.0

Connector = Callable[[Device], Any]


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


def _default_connect(device: Device):
    # Imported lazily so the server starts without loading the ADB stack.
    from shield_manager import adb

    return adb.connect(device)


def _err(e: Exception) -> str:
    return str(e) or type(e).__name__


def _step_reporter(steps: list[Step]) -> Callable[[Any], None]:
    """A deploy progress callback that moves steps through their stages.

    It receives deploy.Progress events (phase, percent); DONE and FAILED are left to the
    caller, which knows how the whole call ended.
    """

    def report(event: Any) -> None:
        phase = getattr(event.phase, "value", event.phase)
        if phase in (DOWNLOADING, COPYING, INSTALLING):
            for step in steps:
                step.update(phase, event.percent)

    return report


def _call_with_progress(fn: Callable, steps: list[Step], *args, **kwargs):
    """Call a deploy function, passing progress= when it accepts one.

    Without it, steps still move through their stages, just without percentages.
    """
    try:
        accepts = "progress" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        accepts = False
    if accepts:
        kwargs["progress"] = _step_reporter(steps)
    elif fn is deploy.install:
        for step in steps:
            step.update(INSTALLING)  # no byte counts to show; go straight to installing
    return fn(*args, **kwargs)


class Api:
    def __init__(
        self,
        registry: Registry,
        connect: Connector | None = None,
        cache_dir: Path | None = None,
        downloads: Downloader | None | str = "default",
    ) -> None:
        self.registry = registry
        self.connect = connect or _default_connect
        # Where apps come from when no Shield has a copy a target can run; None turns
        # downloads off.
        if downloads == "default":
            downloads = Downloader.from_config(registry.path.parent)
        self.downloads = downloads
        self.meta = appinfo.MetaCache(cache_dir or default_config_dir() / "app-cache")
        self.jobs = Jobs()
        self._meta_lock = threading.Lock()
        self._meta_running = False
        # Apps whose details couldn't be read, so they aren't retried on every page load.
        # Kept in memory only, so a restart (or a new version) tries again.
        self._meta_failed: dict[str, int] = {}
        self._screens: dict[str, dict] = {}  # device name -> latest capture
        self._screen_locks: dict[str, threading.Lock] = {}
        # Newer versions on GitHub from the last check, by package.
        self._online: dict[str, fleet.Update] = {}
        self._online_errors: dict[str, str] = {}
        self._online_checked: float | None = None
        self._online_job: Job | None = None

    # -- helpers -------------------------------------------------------------------------

    @contextmanager
    def _connected(self, device: Device):
        conn = self.connect(device)
        try:
            yield conn
        finally:
            conn.close()

    def _on_device(self, device: Device, action: Callable[[Any], Any]) -> dict:
        try:
            with self._connected(device) as conn:
                return {"device": device.name, "ok": True, "result": action(conn)}
        except Exception as e:  # report per device so one bad Shield doesn't hide the rest
            result = {"device": device.name, "ok": False, "error": _err(e)}
            if isinstance(e, deploy.IncompatibleAppError):
                result["incompatible"] = True  # the page offers the Play Store instead
            return result

    def _on_devices(self, devices: list[Device], action: Callable[[Any], Any]) -> list[dict]:
        if not devices:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(devices))) as pool:
            return list(pool.map(lambda d: self._on_device(d, action), devices))

    def _get(self, name: str) -> Device:
        try:
            return self.registry.get(name)
        except DeviceNotFoundError as e:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no device named '{e}'") from e

    def _resolve(self, names: list[str] | None) -> list[Device]:
        if not names:
            raise ApiError(HTTPStatus.BAD_REQUEST, "pick at least one Shield")
        try:
            return self.registry.resolve(names)
        except DeviceNotFoundError as e:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no device named '{e}'") from e

    def _device_dict(self, d: Device) -> dict:
        return {**asdict(d), "address": d.address, "reference": d.name == self.registry.reference}

    # -- devices -------------------------------------------------------------------------

    def list_devices(self) -> list[dict]:
        return [self._device_dict(d) for d in self.registry.list()]

    def add_device(self, body: dict) -> dict:
        name = str(body.get("name", "")).strip()
        host = str(body.get("host", "")).strip()
        if not DEVICE_NAME.match(name):
            raise ApiError(HTTPStatus.BAD_REQUEST, "name may use letters, digits, '.', '_', '-'")
        if not host:
            raise ApiError(HTTPStatus.BAD_REQUEST, "host is required")
        try:
            port = int(body.get("port") or DEFAULT_ADB_PORT)
        except (TypeError, ValueError) as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, "port must be a number") from e
        groups = tuple(str(g).strip() for g in body.get("groups", []) if str(g).strip())
        device = Device(name, host, port, groups)
        try:
            self.registry.add(device)
        except DeviceExistsError as e:
            raise ApiError(HTTPStatus.CONFLICT, f"device '{e}' is already registered") from e
        if not self.registry.reference:
            self.registry.set_reference(name)  # the first Shield is the natural reference
        return self._device_dict(device)

    def remove_device(self, name: str) -> dict:
        self._get(name)
        self.registry.remove(name)
        return {"removed": name}

    def set_groups(self, name: str, body: dict) -> dict:
        self._get(name)
        groups = [str(g).strip() for g in body.get("groups", []) if str(g).strip()]
        return self._device_dict(self.registry.set_groups(name, groups))

    def set_reference(self, body: dict) -> dict:
        name = str(body.get("name", ""))
        self._get(name)
        self.registry.set_reference(name)
        return {"reference": name}

    def device_info(self, name: str) -> dict:
        from shield_manager import adb

        return self._on_device(self._get(name), adb.get_props)

    # -- catalog -------------------------------------------------------------------------

    def catalog(self) -> dict:
        """Every third-party app on any Shield, with each Shield's installed version."""
        devices = self.registry.list()
        reference = self.registry.reference
        inventories = self._on_devices(devices, deploy.package_versions)
        versions: dict[str, dict[str, int]] = {}
        for inv in inventories:
            if inv["ok"]:
                for package, code in inv["result"].items():
                    versions.setdefault(package, {})[inv["device"]] = code

        ref_versions = next(
            (i["result"] for i in inventories if i["device"] == reference and i["ok"]), None
        )
        shields = []
        for d, inv in zip(devices, inventories, strict=True):
            entry: dict[str, Any] = {"name": d.name, "ok": inv["ok"], "error": inv.get("error")}
            entry["reference"] = d.name == reference
            if inv["ok"] and ref_versions is not None and d.name != reference:
                drift = fleet.compare(ref_versions, inv["result"])
                counts = {c.value: 0 for c in fleet.Change}
                for item in drift:
                    counts[item.change.value] += 1
                entry["drift"] = counts
            shields.append(entry)

        cached = self.meta.load()
        apps, stale = [], []
        for package, by_device in sorted(versions.items()):
            newest = max(by_device.values())
            meta = cached.get(package)
            fresh = meta is not None and meta.version_code == newest
            if not fresh and self._meta_failed.get(package) != newest:
                stale.append((package, newest, by_device))
            apps.append(
                {
                    "package": package,
                    "label": meta.label if meta else None,
                    "icon": meta.icon if meta else None,
                    "banner": meta.banner if meta else None,
                    "versions": by_device,
                    "reference_version": (ref_versions or {}).get(package),
                }
            )
        if stale:
            self._start_meta_fetch(stale, reference)
        return {
            "reference": reference,
            "shields": shields,
            "apps": apps,
            "meta_pending": bool(stale) or self._meta_running,
        }

    def meta_index(self) -> dict:
        """Labels and images found so far, polled while names and icons load."""
        index = {
            p: {"label": m.label, "icon": m.icon, "banner": m.banner}
            for p, m in self.meta.load().items()
        }
        return {"apps": index, "pending": self._meta_running}

    def _start_meta_fetch(
        self, stale: list[tuple[str, int, dict[str, int]]], reference: str | None
    ) -> None:
        with self._meta_lock:
            if self._meta_running:
                return
            self._meta_running = True

        # Read each app from a Shield with its newest version, the reference if possible,
        # so each Shield is connected to once.
        by_source: dict[str, list[tuple[str, int]]] = {}
        for package, newest, by_device in stale:
            holders = [d for d, code in by_device.items() if code == newest]
            source = reference if reference in holders else sorted(holders)[0]
            by_source.setdefault(source, []).append((package, newest))

        def run() -> None:
            try:
                self.meta.directory.mkdir(parents=True, exist_ok=True)
                for source, packages in by_source.items():
                    try:
                        device = self.registry.get(source)
                        with self._connected(device) as conn:
                            for package, code in packages:
                                try:
                                    meta = appinfo.fetch_meta(
                                        conn, package, code, self.meta.directory
                                    )
                                except Exception:
                                    self._meta_failed[package] = code
                                    continue
                                self.meta.put(meta)
                    except Exception:  # Shield unreachable
                        self._meta_failed.update(packages)
            finally:
                self._meta_running = False

        threading.Thread(target=run, name="app-meta", daemon=True).start()

    def image(self, name: str) -> Path:
        known = {n for m in self.meta.load().values() for n in (m.icon, m.banner) if n is not None}
        path = self.meta.image_path(name) if name in known else None
        if path is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "no such image")
        return path

    # -- actions (run as background jobs) ------------------------------------------------

    def _packages(self, body: dict) -> list[str]:
        raw = body.get("packages")
        if raw is None:
            raw = [body.get("package", "")]
        if not isinstance(raw, list):
            raise ApiError(HTTPStatus.BAD_REQUEST, "packages must be a list")
        packages = list(dict.fromkeys(str(p).strip() for p in raw))
        if not packages or not all(PACKAGE_NAME.match(p) for p in packages):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid package name")
        return packages

    def _label(self, package: str) -> str:
        meta = self.meta.load().get(package)
        return meta.label if meta and meta.label else package

    def _copy_apps(self, job: Job, plan: list[tuple[str, list[str], int, list[Device]]]) -> None:
        """Copy each (package, source Shields, versionCode, targets) in plan, step by step.

        Sources are Shields holding that version, preferred first. Each target tries them in
        the best order for its CPU types (fleet.source_order), with GitHub downloads
        before Shields whose copy it likely can't run. Every app/Shield pair gets a
        progress step up front, so the page shows the whole queue.
        """
        names = {
            n for _, sources, _, devices in plan for n in [*sources, *(d.name for d in devices)]
        }
        abis = {
            r["device"]: r["result"]
            for r in self._on_devices(
                [self.registry.get(n) for n in sorted(names)], deploy.device_abis
            )
            if r["ok"]
        }

        def ordered(package: str, sources: list[str], device: Device) -> list[str]:
            target = abis.get(device.name, [])
            return fleet.source_order(sources, target, abis, package, self.downloads)

        steps = {
            (package, device.name): job.add_step(
                package, self._label(package), device.name, ordered(package, sources, device)[0]
            )
            for package, sources, _, devices in plan
            for device in devices
        }
        for package, sources, version, devices in plan:
            mine = [steps[(package, d.name)] for d in devices]
            label = self._label(package)
            try:
                with tempfile.TemporaryDirectory(prefix="shield-manager-") as tmp:
                    fetcher = fleet.Fetcher(
                        package, version, sources, self._pull, Path(tmp), self.downloads
                    )
                    for device, step in zip(devices, mine, strict=True):
                        job.progress = f"Installing {label} on {device.name}"
                        fetch = partial(self._fetch, job, fetcher, step, abis.get(device.name, []))
                        order = ordered(package, sources, device)
                        result = self._on_device(
                            device,
                            lambda c, p=package, v=version, o=order, f=fetch, st=step: asdict(
                                fleet.install_first_compatible(
                                    c, p, v, o, f, progress=_step_reporter([st])
                                )[0]
                            ),
                        )
                        if result["ok"]:
                            step.update(DONE)
                        else:
                            step.update(FAILED, error=result["error"])
                            step.store = result.get("incompatible", False)
                        job.results.append({"package": package, **result})
            except Exception as e:
                for step in mine:
                    if step.stage not in (DONE, FAILED):
                        step.update(FAILED, error=_err(e))
                job.results.append({"package": package, "ok": False, "error": _err(e)})
        job.progress = ""

    def _fetch(
        self, job: Job, fetcher: fleet.Fetcher, step: Step, target_abis: list[str], source: str
    ) -> list[Path]:
        """An app's APK files from a Shield or a download, shown on step as it goes."""
        step.source = source
        job.progress = f"Downloading {self._label(fetcher.package)} from {source}"
        step.update(DOWNLOADING)
        paths = fetcher.get(source, target_abis, _step_reporter([step]))
        step.update(COPYING)
        return paths

    def _pull(self, source: str, package: str, dest: Path, progress: Any) -> list[Path]:
        with self._connected(self.registry.get(source)) as conn:
            return deploy.pull_app(conn, package, dest, progress=progress)

    def install_from_shield(self, body: dict) -> dict:
        """Copy apps from the Shield that has the newest version (preferring the reference).

        Takes "packages" (or a single "package") and optional "devices"; with no devices, every
        Shield that's missing an app or has an older version gets it.
        """
        packages = self._packages(body)
        names = body.get("devices")
        targets = self._resolve(names) if names else self.registry.list()
        reference = self.registry.reference

        def work(job: Job) -> None:
            job.progress = "Checking your Shields"
            inventories = {
                r["device"]: r["result"]
                for r in self._on_devices(self.registry.list(), deploy.package_versions)
                if r["ok"]
            }
            plan = []
            for package in packages:
                holders = {d: inv[package] for d, inv in inventories.items() if package in inv}
                if not holders:
                    job.results.append(
                        {"package": package, "ok": False, "error": "not on any reachable Shield"}
                    )
                    continue
                newest = max(holders.values())
                todo = [d for d in targets if holders.get(d.name) != newest]
                candidates = sorted(d for d, c in holders.items() if c == newest)
                candidates.sort(key=lambda d: d != reference)  # reference first if it has it
                if todo:
                    plan.append((package, candidates, newest, todo))
            self._copy_apps(job, plan)

        what = self._label(packages[0]) if len(packages) == 1 else f"{len(packages)} apps"
        where = ", ".join(d.name for d in targets) if names else "every Shield"
        return self.jobs.start(f"Install {what} on {where}", work).to_dict()

    def install_upload(self, apk_path: Path, names: list[str], allow_downgrade: bool) -> dict:
        """Install an uploaded APK. Takes ownership of apk_path's directory."""
        try:
            targets = self._resolve(names)
            info = read_apk_info(apk_path)
        except (ApiError, ApkError) as e:
            shutil.rmtree(apk_path.parent, ignore_errors=True)
            if isinstance(e, ApkError):
                raise ApiError(HTTPStatus.BAD_REQUEST, str(e)) from e
            raise

        def work(job: Job) -> None:
            try:
                label = self._label(info.package)
                steps = [job.add_step(info.package, label, d.name) for d in targets]
                for device, step in zip(targets, steps, strict=True):
                    job.progress = f"Installing on {device.name}"
                    step.update(COPYING)
                    result = self._on_device(
                        device,
                        lambda c, st=step: asdict(
                            _call_with_progress(
                                deploy.install,
                                [st],
                                c,
                                apk_path,
                                info.package,
                                info.version_code,
                                allow_downgrade=allow_downgrade,
                            )
                        ),
                    )
                    if result["ok"]:
                        step.update(DONE)
                    else:
                        step.update(FAILED, error=result["error"])
                    job.results.append(result)
                job.progress = ""
            finally:
                shutil.rmtree(apk_path.parent, ignore_errors=True)

        title = f"Install {info.package} {info.version_name}".strip()
        return {"apk": asdict(info), **self.jobs.start(title, work).to_dict()}

    def sync(self, body: dict) -> dict:
        """Bring Shields up to date with the reference. Never removes apps."""
        reference_name = self.registry.reference
        if not reference_name:
            raise ApiError(HTTPStatus.BAD_REQUEST, "choose a reference Shield first")
        reference = self._get(reference_name)
        names = body.get("devices")
        targets = self._resolve(names) if names else self.registry.list()

        def work(job: Job) -> None:
            job.progress = f"Checking {reference.name}"
            # Check every Shield, not just the targets: ones already up to date can stand in
            # for the reference as the source of a copy.
            everyone = fleet.status(reference, self.registry.list(), self.connect)
            chosen = {d.name for d in targets}
            reports = [r for r in everyone if r.device.name in chosen]
            wanted = {fleet.Change.INSTALL, fleet.Change.UPDATE}
            by_package: dict[str, tuple[int, list[Device]]] = {}
            # Shields already on the reference's version can stand in for it as a source.
            drifted = {r.device.name: {d.package for d in r.drift} for r in everyone if not r.error}
            for report in reports:
                if report.error:
                    job.results.append(
                        {"device": report.device.name, "ok": False, "error": report.error}
                    )
                    continue
                for d in report.drift:
                    if d.change in wanted:
                        version, devices = by_package.setdefault(
                            d.package, (d.reference_version, [])
                        )
                        devices.append(report.device)
            plan = [
                (
                    package,
                    [
                        reference.name,
                        *(n for n, theirs in drifted.items() if package not in theirs),
                    ],
                    version,
                    devices,
                )
                for package, (version, devices) in sorted(by_package.items())
            ]
            self._copy_apps(job, plan)

        return self.jobs.start(f"Sync from {reference.name}", work).to_dict()

    def job(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "no such job")
        return job.to_dict()

    def list_jobs(self) -> list[dict]:
        return [j.to_dict() for j in self.jobs.list()]

    # -- updates online ------------------------------------------------------------------

    def _downloads_on(self) -> Downloader:
        if self.downloads is None:
            raise ApiError(HTTPStatus.CONFLICT, "downloads are turned off for this server")
        return self.downloads

    def online_updates(self) -> dict:
        """The last check for newer versions on GitHub."""
        job = self._online_job
        return {
            "enabled": self.downloads is not None,
            "checked": self._online_checked,
            "checking": job.id if job and job.state == "running" else None,
            "updates": {
                u.package: {
                    "installed": u.installed_name,
                    "latest": u.latest_name,
                    "source": u.source,
                    "shields": u.shields,
                }
                for u in self._online.values()
            },
            "errors": self._online_errors,
        }

    def check_online_updates(self) -> dict:
        """Start a check of every installed app against GitHub."""
        downloads = self._downloads_on()
        job = self._online_job
        if job and job.state == "running":
            return job.to_dict()  # one check at a time; the page follows the running one

        def work(job: Job) -> None:
            job.progress = "Checking GitHub for newer versions"
            updates, errors = fleet.check_updates(self.registry.list(), self.connect, downloads)
            self._online = {u.package: u for u in updates}
            self._online_errors, self._online_checked = errors, time.time()
            job.progress = ""
            job.results.append({"ok": True, "updates": len(updates)})

        self._online_job = self.jobs.start("Check for updates online", work)
        return self._online_job.to_dict()

    def install_online_updates(self, body: dict) -> dict:
        """Install the newer online version of apps on every Shield that has them."""
        downloads = self._downloads_on()
        packages = self._packages(body)
        updates = [self._online[p] for p in packages if p in self._online]
        if not updates:
            raise ApiError(HTTPStatus.BAD_REQUEST, "no update online for those apps; check again")

        def work(job: Job) -> None:
            steps = {
                (u.package, name): job.add_step(u.package, self._label(u.package), name, u.source)
                for u in updates
                for name in u.shields
            }

            def report(event: Any) -> None:
                step = steps.get((event.package, event.device))
                if step:
                    _step_reporter([step])(event)

            devices = self.registry.list()
            for update in updates:
                job.progress = f"Updating {self._label(update.package)}"
                update.applied.clear()
                update.failed.clear()  # from an earlier try
                try:
                    fleet.apply_updates([update], devices, self.connect, downloads, progress=report)
                except Exception as e:
                    update.failed.update({n: _err(e) for n in update.shields})
                for name in update.shields:
                    error = update.failed.get(name)
                    if error is None and name not in update.applied:
                        error = "not updated"
                    steps[(update.package, name)].update(
                        DONE if error is None else FAILED, error=error
                    )
                    job.results.append(
                        {
                            "package": update.package,
                            "device": name,
                            "ok": error is None,
                            "error": error,
                        }
                    )
                if not update.failed:
                    self._online.pop(update.package, None)
            job.progress = ""

        what = self._label(updates[0].package) if len(updates) == 1 else f"{len(updates)} apps"
        return self.jobs.start(f"Update {what} from online", work).to_dict()

    def open_store_page(self, body: dict) -> dict:
        """Open an app's Play Store page on one Shield's screen, ready to press Install."""
        package = self._packages(body)
        if len(package) != 1:
            raise ApiError(HTTPStatus.BAD_REQUEST, "pick one app")
        device = self._get(str(body.get("device", "")))
        result = self._on_device(device, lambda c: deploy.open_store_page(c, package[0]))
        if not result["ok"]:
            raise ApiError(HTTPStatus.BAD_GATEWAY, result["error"])
        return {"device": device.name, "package": package[0]}

    def uninstall(self, body: dict) -> dict:
        """Remove apps from the given Shields. Each Shield is connected to once."""
        packages = self._packages(body)
        devices = self._resolve(body.get("devices"))

        def action(conn):
            present = set(deploy.list_packages(conn, include_system=True))
            outcome = {}
            for package in packages:
                if package not in present:
                    continue  # nothing to remove on this Shield
                try:
                    deploy.uninstall(conn, package)
                    outcome[package] = None
                except Exception as e:
                    outcome[package] = _err(e)
            return outcome

        results = []
        for r in self._on_devices(devices, action):
            if not r["ok"]:
                results += [{"package": p, **r} for p in packages]
                continue
            for package, error in r["result"].items():
                entry = {"package": package, "device": r["device"], "ok": error is None}
                results.append(entry if error is None else {**entry, "error": error})
        return {"packages": packages, "results": results}

    # -- screens -------------------------------------------------------------------------

    def now_showing(self) -> list[dict]:
        """What each Shield is showing: the foreground app and a fresh screenshot."""
        devices = self.registry.list()
        if not devices:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(devices))) as pool:
            return list(pool.map(self._now_showing, devices))

    def _now_showing(self, device: Device) -> dict:
        lock = self._screen_locks.setdefault(device.name, threading.Lock())
        with lock:  # a second viewer waits for the capture in progress and reuses it
            last = self._screens.get(device.name)
            if last is None or time.time() - last["captured_at"] >= SCREEN_MAX_AGE_S:
                last = self._screens[device.name] = self._capture(device)
        return {k: v for k, v in last.items() if k != "png"} | {"image": bool(last.get("png"))}

    def _capture(self, device: Device) -> dict:
        entry: dict[str, Any] = {"device": device.name, "captured_at": time.time()}
        try:
            with self._connected(device) as conn:
                activity = screen.current_activity(conn)
                if activity.awake:
                    try:
                        entry["png"] = screen.screenshot(conn)
                    except Exception as e:  # still report the app in front
                        entry["image_error"] = _err(e)
        except Exception as e:
            return {**entry, "ok": False, "error": _err(e)}
        package = activity.package
        meta = self.meta.load().get(package) if package else None
        label = (meta.label if meta else None) or screen.SYSTEM_SCREENS.get(package or "")
        if activity.state == "Dreaming":
            label = "Screensaver"
        return {
            **entry,
            "ok": True,
            "awake": activity.awake,
            "state": activity.state,
            "package": package,
            "label": label,
            "icon": meta.icon if meta else None,
        }

    def screen_image(self, name: str) -> bytes:
        png = self._screens.get(name, {}).get("png")
        if not png:
            raise ApiError(HTTPStatus.NOT_FOUND, "no screenshot yet")
        return png
