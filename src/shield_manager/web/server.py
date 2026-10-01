"""A small web UI over the shield-manager library.

Uses only the standard library: a JSON API under api/ and one static page. Every URL the
page uses is relative, so it also works behind a path prefix such as Home Assistant's
ingress. It binds to localhost by default because anyone who can reach it can install
apps on your Shields.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from shield_manager.appinfo import IMAGE_TYPES
from shield_manager.registry import Registry
from shield_manager.sources import Downloader
from shield_manager.web.api import Api, ApiError, Connector

# Mutating requests must carry this header. Browsers won't send a custom header
# cross-origin without a CORS preflight, which this server never approves, so other
# websites can't drive the API from your browser.
CSRF_HEADER = "X-Shield-Manager"
CSP = "default-src 'self' 'unsafe-inline'; img-src 'self' data:"
MAX_APK_BYTES = 4 * 1024**3
LOOPBACK = ("127.0.0.1", "localhost", "::1")


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
            allowed = self.server.allowed_clients
            if allowed and self.client_address[0] not in allowed:
                raise ApiError(HTTPStatus.FORBIDDEN, "client not allowed")
            if not self._host_allowed():
                raise ApiError(HTTPStatus.FORBIDDEN, "unexpected Host header")
            if method == "GET" and url.path in ("/", "/index.html"):
                return self._send_static()
            if not url.path.startswith("/api/"):
                raise ApiError(HTTPStatus.NOT_FOUND, "not found")
            if method != "GET" and self.headers.get(CSRF_HEADER) != "1":
                raise ApiError(HTTPStatus.FORBIDDEN, f"missing {CSRF_HEADER} header")
            path = url.path[len("/api/") :]
            if method == "GET" and path.startswith("images/"):
                return self._send_image(unquote(path[len("images/") :]))
            if method == "GET" and path.startswith("screens/"):
                png = self.server.api.screen_image(unquote(path[len("screens/") :]))
                return self._send_bytes(png, "image/png")
            self._send_json(HTTPStatus.OK, self._route(method, path, parse_qs(url.query)))
        except ApiError as e:
            self._send_json(e.status, {"error": str(e)})
        except Exception as e:  # keep the server up and tell the page what broke
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})

    def _host_allowed(self) -> bool:
        # Guards against DNS rebinding when bound to localhost.
        if not self.server.loopback_only:
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in LOOPBACK

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
            case "GET", ["now-showing"]:
                return api.now_showing()
            case "PUT", ["reference"]:
                return api.set_reference(self._json_body())
            case "GET", ["catalog"]:
                return api.catalog()
            case "GET", ["catalog", "meta"]:
                return api.meta_index()
            case "POST", ["install-from-shield"]:
                return api.install_from_shield(self._json_body())
            case "POST", ["install"]:
                names = [n for v in query.get("devices", []) for n in v.split(",") if n]
                allow_downgrade = (query.get("allow_downgrade") or ["0"])[0] == "1"
                return api.install_upload(self._receive_apk(), names, allow_downgrade)
            case "POST", ["sync"]:
                return api.sync(self._json_body())
            case "GET", ["online-updates"]:
                return api.online_updates()
            case "POST", ["online-updates", "check"]:
                return api.check_online_updates()
            case "POST", ["online-updates", "install"]:
                return api.install_online_updates(self._json_body())
            case "POST", ["store-page"]:
                return api.open_store_page(self._json_body())
            case "POST", ["uninstall"]:
                return api.uninstall(self._json_body())
            case "GET", ["jobs"]:
                return api.list_jobs()
            case "GET", ["jobs", job_id]:
                return api.job(job_id)
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

    def _receive_apk(self) -> Path:
        """Stream the raw request body (the APK file) into a new temporary directory."""
        length = self._content_length()
        if length <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "send the APK as the request body")
        if length > MAX_APK_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "APK too large")
        path = Path(tempfile.mkdtemp(prefix="shield-manager-")) / "upload.apk"
        with path.open("wb") as f:
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(remaining, 1024**2))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining:
            path.unlink()
            path.parent.rmdir()
            raise ApiError(HTTPStatus.BAD_REQUEST, "upload ended early")
        return path

    def _send_static(self) -> None:
        page = resources.files("shield_manager.web").joinpath("static/index.html").read_bytes()
        self._send_bytes(page, "text/html; charset=utf-8", {"Content-Security-Policy": CSP})

    def _send_image(self, name: str) -> None:
        path = self.server.api.image(name)
        content_type = IMAGE_TYPES.get(path.suffix, "image/png")
        self._send_bytes(path.read_bytes(), content_type, cache=True)

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        self._send_bytes(json.dumps(payload).encode(), "application/json", status=status)

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
        cache: bool = False,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


class UiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        api: Api,
        verbose: bool = False,
        allowed_clients: Iterable[str] = (),
    ) -> None:
        super().__init__(address, Handler)
        self.api = api
        self.verbose = verbose
        self.loopback_only = address[0] in LOOPBACK
        self.allowed_clients = frozenset(allowed_clients)


def create_server(
    registry: Registry,
    host: str = "127.0.0.1",
    port: int = 8765,
    connect: Connector | None = None,
    verbose: bool = False,
    allowed_clients: Iterable[str] = (),
    cache_dir: Path | None = None,
    downloads: Downloader | None | str = "default",
) -> UiServer:
    """Build the UI server. allowed_clients, if given, limits which IPs may connect.

    downloads is where apps come from when no Shield has a copy a target can run: the
    built-in GitHub and APKPure sources by default, or None to turn downloads off.
    """
    api = Api(registry, connect, cache_dir, downloads)
    return UiServer((host, port), api, verbose=verbose, allowed_clients=allowed_clients)
