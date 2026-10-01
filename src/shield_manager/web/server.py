"""A small local web UI over the shield-manager library.

Uses only the standard library: a JSON API under /api/ and one static page. It binds to
localhost by default because anyone who can reach it can install apps on your Shields.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from shield_manager import deploy
from shield_manager.apk import ApkError, read_apk_info
from shield_manager.registry import (
    DEFAULT_ADB_PORT,
    Device,
    DeviceExistsError,
    DeviceNotFoundError,
    Registry,
)
from shield_manager.web import drift

# Mutating requests must carry this header. Browsers won't send a custom header
# cross-origin without a CORS preflight, which this server never approves, so other
# websites can't drive the API from your browser.
CSRF_HEADER = "X-Shield-Manager"
CSP = "default-src 'self' 'unsafe-inline'; img-src 'self' data:"
MAX_APK_BYTES = 4 * 1024**3
DEVICE_NAME = re.compile(r"^[\w.-]+$")

Connector = Callable[[Device], Any]


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


def _default_connect(device: Device):
    # Imported lazily so the server starts without loading the ADB stack.
    from shield_manager import adb

    return adb.connect(device)


class Api:
    """The UI's operations, independent of HTTP so they're easy to test."""

    def __init__(self, registry: Registry, connect: Connector | None = None) -> None:
        self.registry = registry
        self.connect = connect or _default_connect

    def _on_device(self, device: Device, action: Callable[[Any], Any]) -> dict:
        try:
            conn = self.connect(device)
            try:
                return {"device": device.name, "ok": True, "result": action(conn)}
            finally:
                conn.close()
        except Exception as e:  # report per device so one bad Shield doesn't hide the rest
            return {"device": device.name, "ok": False, "error": str(e) or type(e).__name__}

    def _on_devices(self, devices: list[Device], action: Callable[[Any], Any]) -> list[dict]:
        if not devices:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(devices))) as pool:
            return list(pool.map(lambda d: self._on_device(d, action), devices))

    def _resolve(self, names: list[str] | None) -> list[Device]:
        if not names:
            raise ApiError(HTTPStatus.BAD_REQUEST, "pick at least one device")
        try:
            return self.registry.resolve(names)
        except DeviceNotFoundError as e:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no device named '{e}'") from e

    def _get(self, name: str) -> Device:
        try:
            return self.registry.get(name)
        except DeviceNotFoundError as e:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no device named '{e}'") from e

    def list_devices(self) -> list[dict]:
        return [{**asdict(d), "address": d.address} for d in self.registry.list()]

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
        return {**asdict(device), "address": device.address}

    def remove_device(self, name: str) -> dict:
        self._get(name)
        self.registry.remove(name)
        return {"removed": name}

    def set_groups(self, name: str, body: dict) -> dict:
        self._get(name)
        groups = [str(g).strip() for g in body.get("groups", []) if str(g).strip()]
        d = self.registry.set_groups(name, groups)
        return {**asdict(d), "address": d.address}

    def device_info(self, name: str) -> dict:
        from shield_manager import adb

        return self._on_device(self._get(name), adb.get_props)

    def device_apps(self, name: str) -> dict:
        def action(conn):
            apps = drift.installed_apps(conn)
            return [{"package": p, "version_code": c} for p, c in sorted(apps.items())]

        return self._on_device(self._get(name), action)

    def drift(self, reference: str) -> dict:
        ref = self._get(reference)
        devices = self.registry.list()
        inventories = {r["device"]: r for r in self._on_devices(devices, drift.installed_apps)}
        ref_result = inventories[ref.name]
        if not ref_result["ok"]:
            raise ApiError(
                HTTPStatus.BAD_GATEWAY, f"couldn't read {ref.name}: {ref_result['error']}"
            )
        ref_apps = ref_result["result"]
        report = []
        for d in devices:
            if d.name == ref.name:
                continue
            r = inventories[d.name]
            if r["ok"]:
                entry = drift.compare(d.name, ref_apps, r["result"])
            else:
                entry = drift.DeviceDrift(d.name, error=r["error"])
            report.append({**asdict(entry), "in_sync": entry.in_sync})
        apps = [{"package": p, "version_code": c} for p, c in sorted(ref_apps.items())]
        return {"reference": ref.name, "reference_apps": apps, "devices": report}

    def install(self, apk_path: Path, names: list[str], allow_downgrade: bool) -> dict:
        devices = self._resolve(names)
        try:
            info = read_apk_info(apk_path)
        except ApkError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(e)) from e

        def action(conn):
            v = deploy.install(conn, apk_path, info, allow_downgrade=allow_downgrade)
            return asdict(v)

        return {"apk": asdict(info), "results": self._on_devices(devices, action)}

    def uninstall(self, body: dict) -> dict:
        package = str(body.get("package", "")).strip()
        if not package:
            raise ApiError(HTTPStatus.BAD_REQUEST, "package is required")
        devices = self._resolve(body.get("devices"))

        def action(conn):
            deploy.uninstall(conn, package)
            return {"removed": package}

        return {"package": package, "results": self._on_devices(devices, action)}


class Handler(BaseHTTPRequestHandler):
    server: UiServer
    server_version = "shield-manager"

    def log_message(self, format: str, *args) -> None:
        if self.server.verbose:
            super().log_message(format, *args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        try:
            if not self._host_allowed():
                raise ApiError(HTTPStatus.FORBIDDEN, "unexpected Host header")
            if method == "GET" and url.path in ("/", "/index.html"):
                return self._send_static()
            if not url.path.startswith("/api/"):
                raise ApiError(HTTPStatus.NOT_FOUND, "not found")
            if method != "GET" and self.headers.get(CSRF_HEADER) != "1":
                raise ApiError(HTTPStatus.FORBIDDEN, f"missing {CSRF_HEADER} header")
            payload = self._route(method, url.path[len("/api/") :], parse_qs(url.query))
            self._send_json(HTTPStatus.OK, payload)
        except ApiError as e:
            self._send_json(e.status, {"error": str(e)})
        except Exception as e:  # keep the server up and tell the page what broke
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})

    def _host_allowed(self) -> bool:
        # Guards against DNS rebinding when bound to localhost.
        if not self.server.loopback_only:
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("localhost", "127.0.0.1", "::1")

    def _route(self, method: str, path: str, query: dict[str, list[str]]) -> Any:
        api = self.server.api
        parts = [unquote(p) for p in path.strip("/").split("/")]
        match method, parts:
            case "GET", ["devices"]:
                return api.list_devices()
            case "POST", ["devices"]:
                return api.add_device(self._json_body())
            case "DELETE", ["devices", name]:
                return api.remove_device(name)
            case "PUT", ["devices", name, "groups"]:
                return api.set_groups(name, self._json_body())
            case "GET", ["devices", name, "info"]:
                return api.device_info(name)
            case "GET", ["devices", name, "apps"]:
                return api.device_apps(name)
            case "GET", ["drift"]:
                reference = (query.get("reference") or [""])[0]
                if not reference:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "reference is required")
                return api.drift(reference)
            case "POST", ["install"]:
                names = [n for v in query.get("devices", []) for n in v.split(",") if n]
                allow_downgrade = (query.get("allow_downgrade") or ["0"])[0] == "1"
                with self._uploaded_apk() as apk:
                    return api.install(apk, names, allow_downgrade)
            case "POST", ["uninstall"]:
                return api.uninstall(self._json_body())
        raise ApiError(HTTPStatus.NOT_FOUND, "not found")

    def _content_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad Content-Length") from e

    def _json_body(self) -> dict:
        length = self._content_length()
        if length > 1024**2:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, "body must be JSON") from e
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        return body

    @contextmanager
    def _uploaded_apk(self) -> Iterator[Path]:
        """Stream the raw request body (the APK file) to a temporary file."""
        length = self._content_length()
        if length <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "send the APK as the request body")
        if length > MAX_APK_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "APK too large")
        with tempfile.TemporaryDirectory(prefix="shield-manager-") as tmp:
            path = Path(tmp) / "upload.apk"
            with path.open("wb") as f:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(remaining, 1024**2))
                    if not chunk:
                        raise ApiError(HTTPStatus.BAD_REQUEST, "upload ended early")
                    f.write(chunk)
                    remaining -= len(chunk)
            yield path

    def _send_static(self) -> None:
        page = resources.files("shield_manager.web").joinpath("static/index.html").read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(page)

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class UiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], api: Api, verbose: bool = False) -> None:
        super().__init__(address, Handler)
        self.api = api
        self.verbose = verbose
        self.loopback_only = address[0] in ("127.0.0.1", "localhost", "::1")


def create_server(
    registry: Registry,
    host: str = "127.0.0.1",
    port: int = 8765,
    connect: Connector | None = None,
    verbose: bool = False,
) -> UiServer:
    return UiServer((host, port), Api(registry, connect), verbose=verbose)
