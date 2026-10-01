import http.client
import json
import threading
import zipfile

import pytest

from shield_manager.registry import Device, Registry
from shield_manager.web import drift
from shield_manager.web.server import create_server
from tests.axml import build_manifest
from tests.fakes import FakeConnection
from tests.test_apk import ATTRS, STRINGS


def versions(apps):
    return "\n".join(f"package:{p} versionCode:{c}" for p, c in apps.items())


def test_installed_apps_parses_version_codes():
    conn = FakeConnection(
        responses={"pm list packages -3": "package:a.b versionCode:12\npackage:c.d\njunk\n"}
    )
    assert drift.installed_apps(conn) == {"a.b": 12, "c.d": 0}


def test_compare():
    ref = {"kodi": 20, "plex": 5, "yt": 3}
    got = drift.compare("den", ref, {"kodi": 19, "yt": 4, "game": 1})
    assert [a.package for a in got.missing] == ["plex"]
    assert [(a.package, a.version_code) for a in got.outdated] == [("kodi", 19)]
    assert [a.package for a in got.newer] == ["yt"]
    assert [a.package for a in got.extra] == ["game"]
    assert not got.in_sync
    assert drift.compare("den", ref, {**ref, "game": 1}).in_sync  # extras don't count


@pytest.fixture
def ui(tmp_path):
    registry = Registry(tmp_path / "devices.json")
    registry.add(Device("den", "10.0.0.2", groups=("upstairs",)))
    registry.add(Device("living", "10.0.0.3"))
    conns = {
        "den": FakeConnection(responses={"pm list packages": versions({"kodi": 20, "plex": 5})}),
        "living": FakeConnection(responses={"pm list packages": versions({"kodi": 19})}),
    }

    def connect(device):
        if device.name not in conns:
            raise ConnectionRefusedError("unreachable")
        return conns[device.name]

    server = create_server(registry, port=0, connect=connect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        hdrs = {"X-Shield-Manager": "1", **(headers or {})}
        payload = raw if raw is not None else (json.dumps(body) if body is not None else None)
        conn.request(method, path, body=payload, headers=hdrs)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        ctype = res.getheader("Content-Type", "")
        return res.status, json.loads(data) if ctype.startswith("application/json") else data

    request.registry, request.conns = registry, conns
    yield request
    server.shutdown()
    server.server_close()


def test_serves_page(ui):
    status, page = ui("GET", "/")
    assert status == 200
    assert b"Shield Manager" in page


def test_device_crud(ui):
    status, created = ui("POST", "/api/devices", {"name": "attic", "host": "10.0.0.9"})
    assert status == 200 and created["address"] == "10.0.0.9:5555"
    assert ui("POST", "/api/devices", {"name": "attic", "host": "x"})[0] == 409
    assert ui("POST", "/api/devices", {"name": "bad name", "host": "x"})[0] == 400
    status, d = ui("PUT", "/api/devices/attic/groups", {"groups": ["b", "a"]})
    assert d["groups"] == ["a", "b"]
    assert ui("DELETE", "/api/devices/attic")[0] == 200
    assert [d["name"] for d in ui("GET", "/api/devices")[1]] == ["den", "living"]
    assert ui("DELETE", "/api/devices/attic")[0] == 404


def test_mutations_need_csrf_header(ui):
    status, body = ui(
        "POST", "/api/devices", {"name": "x", "host": "y"}, headers={"X-Shield-Manager": ""}
    )
    assert status == 403
    assert "x" not in [d.name for d in ui.registry.list()]


def test_rejects_foreign_host_header(ui):
    assert ui("GET", "/api/devices", headers={"Host": "evil.example"})[0] == 403


def test_drift(ui):
    ui.registry.add(Device("attic", "10.0.0.9"))
    status, report = ui("GET", "/api/drift?reference=den")
    assert status == 200
    by_name = {d["device"]: d for d in report["devices"]}
    assert by_name["living"]["missing"] == [{"package": "plex", "version_code": 5}]
    assert by_name["living"]["outdated"] == [{"package": "kodi", "version_code": 19}]
    assert not by_name["living"]["in_sync"]
    assert by_name["attic"]["error"] == "unreachable"
    assert all(c.closed for c in ui.conns.values())


def test_drift_unreachable_reference(ui):
    ui.registry.add(Device("attic", "10.0.0.9"))
    status, body = ui("GET", "/api/drift?reference=attic")
    assert status == 502 and "unreachable" in body["error"]


def test_install_upload(ui, tmp_path):
    apk = tmp_path / "app.apk"
    with zipfile.ZipFile(apk, "w") as zf:
        zf.writestr("AndroidManifest.xml", build_manifest(ATTRS, STRINGS))
    for conn in ui.conns.values():
        conn.responses["pm install"] = "Success"
        conn.installed["com.example.tv"] = (42, "1.4.2")
    status, body = ui("POST", "/api/install?devices=den,living", raw=apk.read_bytes())
    assert status == 200
    assert body["apk"]["package"] == "com.example.tv"
    assert [r["ok"] for r in body["results"]] == [True, True]
    assert ui.conns["den"].pushed[0][1] == "/data/local/tmp/com.example.tv.apk"


def test_install_rejects_non_apk(ui):
    status, body = ui("POST", "/api/install?devices=den", raw=b"not a zip")
    assert status == 400 and "not a valid APK" in body["error"]


def test_uninstall_reports_per_device(ui):
    ui.conns["den"].responses["pm uninstall"] = "Success"
    ui.conns["living"].responses["pm uninstall"] = "Failure [DELETE_FAILED_INTERNAL_ERROR]"
    status, body = ui("POST", "/api/uninstall", {"package": "kodi", "devices": ["den", "living"]})
    assert status == 200
    assert [r["ok"] for r in body["results"]] == [True, False]
    assert "DELETE_FAILED" in body["results"][1]["error"]
