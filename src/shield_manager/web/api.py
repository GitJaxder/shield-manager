"""What the web UI can do, independent of HTTP so it's easy to test."""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from http import HTTPStatus
from pathlib import Path
from typing import Any

from shield_manager import appinfo, deploy, fleet
from shield_manager.apk import ApkError, read_apk_info
from shield_manager.registry import (
    DEFAULT_ADB_PORT,
    Device,
    DeviceExistsError,
    DeviceNotFoundError,
    Registry,
    default_config_dir,
)
from shield_manager.web.jobs import Job, Jobs

DEVICE_NAME = re.compile(r"^[\w.-]+$")
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9_.]+$")

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


class Api:
    def __init__(
        self,
        registry: Registry,
        connect: Connector | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.registry = registry
        self.connect = connect or _default_connect
        self.meta = appinfo.MetaCache(cache_dir or default_config_dir() / "app-cache")
        self.jobs = Jobs()
        self._meta_lock = threading.Lock()
        self._meta_running = False
        # Apps whose details couldn't be read, so they aren't retried on every page load.
        # Kept in memory only, so a restart (or a new version) tries again.
        self._meta_failed: dict[str, int] = {}

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
            return {"device": device.name, "ok": False, "error": _err(e)}

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

    def install_from_shield(self, body: dict) -> dict:
        """Copy an app from the Shield that has it (the reference if it does) to others."""
        package = str(body.get("package", "")).strip()
        if not PACKAGE_NAME.match(package):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid package name")
        targets = self._resolve(body.get("devices"))
        reference = self.registry.reference

        def work(job: Job) -> None:
            job.progress = "Finding the app on your Shields"
            holders = {}
            for r in self._on_devices(self.registry.list(), deploy.package_versions):
                if r["ok"] and package in r["result"]:
                    holders[r["device"]] = r["result"][package]
            if not holders:
                raise RuntimeError(f"{package} isn't installed on any reachable Shield")
            newest = max(holders.values())
            candidates = [d for d, c in holders.items() if c == newest]
            source = reference if reference in candidates else sorted(candidates)[0]
            with tempfile.TemporaryDirectory(prefix="shield-manager-") as tmp:
                job.progress = f"Copying from {source}"
                with self._connected(self.registry.get(source)) as conn:
                    apks = deploy.pull_app(conn, package, Path(tmp))
                for device in targets:
                    if holders.get(device.name) == newest:
                        job.results.append({"device": device.name, "ok": True, "skipped": True})
                        continue
                    job.progress = f"Installing on {device.name}"
                    result = self._on_device(
                        device,
                        lambda c: asdict(deploy.install(c, apks, package, newest)),
                    )
                    job.results.append(result)
            job.progress = ""

        names = ", ".join(d.name for d in targets)
        meta = self.meta.load().get(package)
        label = meta.label if meta and meta.label else package
        return self.jobs.start(f"Install {label} on {names}", work).to_dict()

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
                for device in targets:
                    job.progress = f"Installing on {device.name}"
                    job.results.append(
                        self._on_device(
                            device,
                            lambda c: asdict(
                                deploy.install(
                                    c,
                                    apk_path,
                                    info.package,
                                    info.version_code,
                                    allow_downgrade=allow_downgrade,
                                )
                            ),
                        )
                    )
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
            job.progress = f"Copying apps from {reference.name}"
            for report in fleet.sync(reference, targets, self.connect):
                result: dict[str, Any] = {"device": report.device.name}
                if report.error:
                    result.update(ok=False, error=report.error)
                else:
                    result.update(
                        ok=not report.failed, applied=report.applied, failed=report.failed
                    )
                job.results.append(result)
            job.progress = ""

        return self.jobs.start(f"Sync from {reference.name}", work).to_dict()

    def job(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "no such job")
        return job.to_dict()

    def list_jobs(self) -> list[dict]:
        return [j.to_dict() for j in self.jobs.list()]

    def uninstall(self, body: dict) -> dict:
        package = str(body.get("package", "")).strip()
        if not PACKAGE_NAME.match(package):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid package name")
        devices = self._resolve(body.get("devices"))

        def action(conn):
            deploy.uninstall(conn, package)
            return {"removed": package}

        return {"package": package, "results": self._on_devices(devices, action)}
