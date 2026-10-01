import http.client
import json
import threading
import time
import zipfile
from types import SimpleNamespace

import pytest

from shield_manager import appinfo
from shield_manager.registry import Device, Registry
from shield_manager.web.server import create_server
from tests.axml import build_manifest
from tests.fakes import FakeConnection
from tests.test_apk import ATTRS, STRINGS


@pytest.fixture
def ui(tmp_path, monkeypatch):
    registry = Registry(tmp_path / "devices.json")
    registry.add(Device("den", "10.0.0.2", groups=("upstairs",)))
    registry.add(Device("living", "10.0.0.3"))
    registry.set_reference("den")
    installed = {
        "den": {"org.xbmc.kodi": (20, "20.0"), "com.plexapp.android": (5, "5")},
        "living": {"org.xbmc.kodi": (19, "19.0"), "com.retroarch": (7, "1.7")},
    }

    def connect(device):
        if device.name not in installed:
            raise ConnectionRefusedError("unreachable")
        conn = FakeConnection(installed=installed[device.name])
        conn.installed = installed[device.name]  # share state across connections
        return conn

    def fake_meta(conn, package, version_code, cache_dir):
        (cache_dir / f"{package}.icon.png").write_bytes(b"PNG")
        return appinfo.AppMeta(
            package, version_code, label=package.split(".")[1].title(), icon=f"{package}.icon.png"
        )

    monkeypatch.setattr(appinfo, "fetch_meta", fake_meta)
    server = create_server(registry, port=0, connect=connect, cache_dir=tmp_path / "cache")
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

    def wait_job(job):
        for _ in range(200):
            status, job = request("GET", f"/api/jobs/{job['id']}")
            if job["state"] != "running":
                return job
            time.sleep(0.02)
        raise AssertionError("job didn't finish")

    request.registry, request.installed, request.wait_job = registry, installed, wait_job
    request.api = server.api
    yield request
    server.shutdown()
    server.server_close()


def test_serves_page_with_relative_urls(ui):
    status, page = ui("GET", "/")
    assert status == 200
    assert b"Shield Manager" in page
    # Must work behind Home Assistant ingress's path prefix.
    assert b'fetch("/api' not in page and b'src="/' not in page


def test_device_crud(ui):
    status, created = ui("POST", "/api/devices", {"name": "attic", "host": "10.0.0.9"})
    assert status == 200 and created["address"] == "10.0.0.9:5555"
    assert ui("POST", "/api/devices", {"name": "attic", "host": "x"})[0] == 409
    assert ui("POST", "/api/devices", {"name": "bad name", "host": "x"})[0] == 400
    status, d = ui("PUT", "/api/devices/attic/groups", {"groups": ["b", "a"]})
    assert d["groups"] == ["a", "b"]
    assert ui("DELETE", "/api/devices/attic")[0] == 200
    assert ui("DELETE", "/api/devices/attic")[0] == 404


def test_set_reference(ui):
    assert ui("PUT", "/api/reference", {"name": "living"})[0] == 200
    assert ui.registry.reference == "living"
    devices = ui("GET", "/api/devices")[1]
    assert [d["name"] for d in devices if d["reference"]] == ["living"]


def test_first_device_becomes_reference(tmp_path):
    from shield_manager.web.api import Api

    api = Api(Registry(tmp_path / "d.json"), connect=lambda d: None, cache_dir=tmp_path)
    api.add_device({"name": "den", "host": "10.0.0.2"})
    assert api.registry.reference == "den"


def test_mutations_need_csrf_header(ui):
    status, _ = ui(
        "POST", "/api/devices", {"name": "x", "host": "y"}, headers={"X-Shield-Manager": ""}
    )
    assert status == 403
    assert "x" not in [d.name for d in ui.registry.list()]


def test_rejects_foreign_host_header(ui):
    assert ui("GET", "/api/devices", headers={"Host": "evil.example"})[0] == 403


def test_allowed_clients(tmp_path):
    server = create_server(Registry(tmp_path / "d.json"), port=0, allowed_clients=["10.9.9.9"])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", "/api/devices")
        assert conn.getresponse().status == 403
    finally:
        server.shutdown()
        server.server_close()


def test_catalog_lists_every_app_with_versions_and_drift(ui):
    ui.registry.add(Device("attic", "10.0.0.9"))
    status, cat = ui("GET", "/api/catalog")
    assert status == 200
    apps = {a["package"]: a for a in cat["apps"]}
    assert apps["org.xbmc.kodi"]["versions"] == {"den": 20, "living": 19}
    assert apps["com.retroarch"]["reference_version"] is None
    shields = {s["name"]: s for s in cat["shields"]}
    assert shields["attic"]["ok"] is False
    assert shields["living"]["drift"] == {"install": 1, "update": 1, "newer": 0, "extra": 1}
    assert shields["den"]["reference"] is True

    # Labels and icons load in the background, then show up in the catalog.
    for _ in range(100):
        meta = ui("GET", "/api/catalog/meta")[1]
        if not meta["pending"] and len(meta["apps"]) == 3:
            break
        time.sleep(0.02)
    assert meta["apps"]["org.xbmc.kodi"]["label"] == "Xbmc"
    cat = ui("GET", "/api/catalog")[1]
    kodi = next(a for a in cat["apps"] if a["package"] == "org.xbmc.kodi")
    assert kodi["label"] == "Xbmc" and not cat["meta_pending"]
    status, image = ui("GET", "/api/images/" + kodi["icon"])
    assert status == 200 and image == b"PNG"
    assert ui("GET", "/api/images/index.json")[0] == 404


def test_install_from_shield_copies_from_the_reference(ui):
    status, job = ui(
        "POST",
        "/api/install-from-shield",
        {"package": "com.plexapp.android", "devices": ["living"]},
    )
    assert status == 200
    job = ui.wait_job(job)
    assert job["state"] == "done", job
    assert ui.installed["living"]["com.plexapp.android"][0] == 5


def test_install_from_shield_uses_newest_copy(ui):
    # retroarch only exists on living; installing it on den copies it from there.
    job = ui.wait_job(
        ui("POST", "/api/install-from-shield", {"package": "com.retroarch", "devices": ["den"]})[1]
    )
    assert job["state"] == "done", job
    assert ui.installed["den"]["com.retroarch"][0] == 7


def test_install_from_shield_rejects_bad_package(ui):
    status, body = ui("POST", "/api/install-from-shield", {"package": "a;rm", "devices": ["den"]})
    assert status == 400


def test_sync_job(ui):
    job = ui.wait_job(ui("POST", "/api/sync", {})[1])
    assert job["state"] == "done", job
    living = ui.installed["living"]
    assert living["org.xbmc.kodi"][0] == 20 and living["com.plexapp.android"][0] == 5
    assert "com.retroarch" in living  # sync never removes apps


def test_install_upload(ui, tmp_path):
    apk = tmp_path / "app.apk"
    with zipfile.ZipFile(apk, "w") as zf:
        zf.writestr("AndroidManifest.xml", build_manifest(ATTRS, STRINGS))
    for name in ("den", "living"):
        ui.installed[name]["com.example.tv"] = (42, "1.4.2")  # what the device reports after
    status, job = ui("POST", "/api/install?devices=den,living", raw=apk.read_bytes())
    assert status == 200 and job["apk"]["package"] == "com.example.tv"
    job = ui.wait_job(job)
    assert [r["ok"] for r in job["results"]] == [True, True], job


def test_install_rejects_non_apk(ui):
    status, body = ui("POST", "/api/install?devices=den", raw=b"not a zip")
    assert status == 400 and "not a valid APK" in body["error"]


def test_uninstall_reports_per_device(ui):
    status, body = ui(
        "POST", "/api/uninstall", {"package": "com.plexapp.android", "devices": ["den", "living"]}
    )
    assert status == 200
    # living never had it, so there's nothing to report for it.
    assert body["results"] == [{"package": "com.plexapp.android", "device": "den", "ok": True}]
    assert "com.plexapp.android" not in ui.installed["den"]


def test_uninstall_several_apps(ui):
    packages = ["org.xbmc.kodi", "com.retroarch", "com.plexapp.android"]
    status, body = ui("POST", "/api/uninstall", {"packages": packages, "devices": ["living"]})
    assert status == 200
    assert {r["package"] for r in body["results"] if r["ok"]} == {"org.xbmc.kodi", "com.retroarch"}
    assert ui.installed["living"] == {}


def test_uninstall_unreachable_shield_reports_each_app(ui):
    ui.registry.add(Device("attic", "10.0.0.9"))
    body = ui("POST", "/api/uninstall", {"packages": ["a.b", "c.d"], "devices": ["attic"]})[1]
    assert [(r["package"], r["ok"]) for r in body["results"]] == [("a.b", False), ("c.d", False)]


def test_install_several_apps_everywhere_they_are_missing(ui):
    packages = ["com.plexapp.android", "com.retroarch", "org.xbmc.kodi"]
    job = ui.wait_job(ui("POST", "/api/install-from-shield", {"packages": packages})[1])
    assert job["state"] == "done", job
    for name in ("den", "living"):
        assert {p: v[0] for p, v in ui.installed[name].items()} == {
            "org.xbmc.kodi": 20,
            "com.plexapp.android": 5,
            "com.retroarch": 7,
        }
    # Only what was missing or behind was installed.
    assert sorted((r["package"], r["device"]) for r in job["results"]) == [
        ("com.plexapp.android", "living"),
        ("com.retroarch", "den"),
        ("org.xbmc.kodi", "living"),
    ]


def test_install_several_reports_apps_on_no_shield(ui):
    job = ui.wait_job(ui("POST", "/api/install-from-shield", {"packages": ["gone.app"]})[1])
    assert job["results"] == [
        {"package": "gone.app", "ok": False, "error": "not on any reachable Shield"}
    ]


def test_packages_must_be_valid(ui):
    assert ui("POST", "/api/uninstall", {"packages": "x", "devices": ["den"]})[0] == 400
    assert (
        ui("POST", "/api/uninstall", {"packages": ["ok.app", "b;ad"], "devices": ["den"]})[0] == 400
    )


def test_unreadable_app_details_are_retried_after_restart_not_every_load(ui, monkeypatch):
    calls = []

    def broken(conn, package, version_code, cache_dir):
        calls.append(package)
        raise RuntimeError("no unzip")

    monkeypatch.setattr(appinfo, "fetch_meta", broken)
    ui("GET", "/api/catalog")
    for _ in range(100):
        if not ui("GET", "/api/catalog/meta")[1]["pending"]:
            break
        time.sleep(0.02)
    first = len(calls)
    assert first == 3
    assert ui("GET", "/api/catalog")[1]["meta_pending"] is False
    assert len(calls) == first
    assert ui.api.meta.load() == {}  # failures aren't written to disk


def test_install_job_reports_a_step_per_app_and_shield(ui):
    body = {"packages": ["com.plexapp.android", "com.retroarch"]}
    job = ui.wait_job(ui("POST", "/api/install-from-shield", body)[1])
    steps = {(s["package"], s["device"]): s for s in job["steps"]}
    assert set(steps) == {("com.plexapp.android", "living"), ("com.retroarch", "den")}
    assert all(s["stage"] == "done" for s in steps.values())


def test_progress_from_core_shows_up_as_percentages(ui, monkeypatch):
    from shield_manager import deploy
    from shield_manager.web import api as web_api

    seen = []
    real_pull, real_install = deploy.pull_app, deploy.install

    def event(phase, percent=None):
        # Shaped like deploy.Progress from the core library's progress API.
        return SimpleNamespace(phase=SimpleNamespace(value=phase), percent=percent)

    def pull_app(conn, package, dest, progress=None):
        progress(event("downloading", 25))
        seen.append(("pull", job_steps()))
        return real_pull(conn, package, dest)

    def install(conn, apks, package, version, allow_downgrade=False, progress=None):
        progress(event("copying", 75))
        seen.append(("push", job_steps()))
        progress(event("installing"))
        seen.append(("install", job_steps()))
        progress(event("done"))
        return real_install(conn, apks, package, version, allow_downgrade=allow_downgrade)

    def job_steps():
        return [(s.stage, s.percent) for j in ui.api.jobs.list() for s in j.steps]

    monkeypatch.setattr(deploy, "pull_app", pull_app)
    monkeypatch.setattr(deploy, "install", install)
    monkeypatch.setattr(web_api.deploy, "pull_app", pull_app)
    monkeypatch.setattr(web_api.deploy, "install", install)
    body = {"packages": ["com.plexapp.android"], "devices": ["living"]}
    job = ui.wait_job(ui("POST", "/api/install-from-shield", body)[1])
    assert job["state"] == "done", job
    assert seen == [
        ("pull", [("downloading", 25)]),
        ("push", [("copying", 75)]),
        ("install", [("installing", None)]),
    ]


def test_sync_job_has_steps(ui):
    job = ui.wait_job(ui("POST", "/api/sync", {})[1])
    assert sorted((s["package"], s["device"], s["stage"]) for s in job["steps"]) == [
        ("com.plexapp.android", "living", "done"),
        ("org.xbmc.kodi", "living", "done"),
    ]


def test_now_showing_reports_each_shield_and_serves_its_screenshot(ui, monkeypatch):
    from shield_manager import screen

    captures = []

    def fake_activity(conn):
        return screen.Activity(True, "Awake", "com.google.android.tvlauncher")

    def fake_screenshot(conn):
        captures.append(conn)
        return screen.PNG_SIGNATURE + b"pixels"

    monkeypatch.setattr(screen, "current_activity", fake_activity)
    monkeypatch.setattr(screen, "screenshot", fake_screenshot)
    status, shown = ui("GET", "/api/now-showing")
    assert status == 200
    by_name = {s["device"]: s for s in shown}
    assert by_name["den"]["ok"] and by_name["den"]["image"]
    assert by_name["den"]["label"] == "Home screen"
    assert "png" not in by_name["den"]

    status, png = ui("GET", "/api/screens/den")
    assert status == 200 and png.startswith(screen.PNG_SIGNATURE)

    # A second look straight away reuses the capture instead of taking another.
    ui("GET", "/api/now-showing")
    assert len(captures) == 2


def test_now_showing_skips_the_screenshot_while_asleep(ui, monkeypatch):
    from shield_manager import screen

    monkeypatch.setattr(
        screen, "current_activity", lambda c: screen.Activity(False, "Asleep", None)
    )
    monkeypatch.setattr(screen, "screenshot", lambda c: pytest.fail("captured while asleep"))
    status, shown = ui("GET", "/api/now-showing")
    assert status == 200
    assert all(s["ok"] and not s["awake"] and not s["image"] for s in shown)
    assert ui("GET", "/api/screens/den")[0] == 404
