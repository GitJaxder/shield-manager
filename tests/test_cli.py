import zipfile

import pytest

from shield_manager.cli import main
from shield_manager.registry import Device, Registry
from tests.axml import build_manifest
from tests.fakes import FakeConnection
from tests.test_apk import ATTRS, STRINGS


@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path / "devices.json")


def test_device_add_and_list(registry, capsys):
    assert main(["device", "add", "den", "10.0.0.2"], registry=registry) == 0
    assert main(["device", "list"], registry=registry) == 0
    out = capsys.readouterr().out
    assert "den\t10.0.0.2:5555" in out


def test_list_empty_shows_hint(registry, capsys):
    assert main(["device", "list"], registry=registry) == 0
    assert "No devices registered" in capsys.readouterr().out


def test_add_duplicate_fails(registry, capsys):
    main(["device", "add", "den", "10.0.0.2"], registry=registry)
    assert main(["device", "add", "den", "10.0.0.3"], registry=registry) == 1
    assert "already registered" in capsys.readouterr().err


def test_remove_missing_fails(registry, capsys):
    assert main(["device", "remove", "nope"], registry=registry) == 1
    assert "no device named 'nope'" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "shield-manager" in capsys.readouterr().out


@pytest.fixture
def fleet(registry, monkeypatch):
    """Two registered devices whose ADB connections are fakes."""
    from shield_manager import adb

    registry.add(Device("den", "10.0.0.2", groups=("upstairs",)))
    registry.add(Device("garage", "10.0.0.4"))
    conns = {
        "den": FakeConnection(),
        "garage": FakeConnection(),
    }

    def connect(device):
        if device.name not in conns:
            raise ConnectionRefusedError("unreachable")
        return conns[device.name]

    monkeypatch.setattr(adb, "connect", connect)
    return conns


def test_device_add_with_group_and_set_groups(registry, capsys):
    main(["device", "add", "den", "10.0.0.2", "-g", "kids"], registry=registry)
    main(["device", "list"], registry=registry)
    assert "den\t10.0.0.2:5555\t[kids]" in capsys.readouterr().out
    main(["device", "set-groups", "den", "living", "upstairs"], registry=registry)
    assert registry.get("den").groups == ("living", "upstairs")


def test_app_requires_targets(registry, capsys):
    assert main(["app", "version", "com.example.tv"], registry=registry) == 2
    assert "--device, --group or --all" in capsys.readouterr().err


def test_app_uninstall_by_group(registry, fleet, capsys):
    fleet["den"].installed["com.example.tv"] = (42, "")
    assert main(["app", "uninstall", "com.example.tv", "-g", "upstairs"], registry=registry) == 0
    assert fleet["den"].commands == ["pm uninstall com.example.tv"]
    assert fleet["garage"].commands == []
    assert fleet["den"].closed
    assert "den: removed com.example.tv" in capsys.readouterr().out


def test_app_install_all(registry, fleet, tmp_path, capsys):
    apk = tmp_path / "app.apk"
    with zipfile.ZipFile(apk, "w") as zf:
        zf.writestr("AndroidManifest.xml", build_manifest(ATTRS, STRINGS))
    for conn in fleet.values():
        conn.responses["pm install"] = "Success"
        conn.installed["com.example.tv"] = (42, "1.4.2")
    assert main(["app", "install", str(apk), "--all"], registry=registry) == 0
    out = capsys.readouterr().out
    assert "com.example.tv 1.4.2 (versionCode 42)" in out
    assert "garage: installed 1.4.2 (versionCode 42)" in out
    assert "2/2 devices succeeded" in out


def test_one_unreachable_device_fails_without_stopping_others(registry, fleet, capsys):
    registry.add(Device("attic", "10.0.0.9"))
    assert main(["app", "version", "com.example.tv", "--all"], registry=registry) == 1
    captured = capsys.readouterr()
    assert "attic: FAILED: unreachable" in captured.err
    assert "den: not installed" in captured.out
    assert "2/3 devices succeeded" in captured.out


def test_app_unknown_group(registry, capsys):
    assert main(["app", "list", "-g", "nope"], registry=registry) == 1
    assert "no devices in group 'nope'" in capsys.readouterr().err


@pytest.fixture
def mirrored(registry, fleet):
    fleet["den"].installed.update({"kodi": (200, ""), "plex": (50, "")})
    fleet["garage"].installed.update({"kodi": (190, ""), "old.game": (1, "")})
    return fleet


def test_fleet_needs_a_reference(registry, fleet, capsys):
    assert main(["fleet", "status"], registry=registry) == 2
    assert "fleet set-reference" in capsys.readouterr().err


def test_fleet_status(registry, mirrored, capsys):
    main(["fleet", "set-reference", "den"], registry=registry)
    assert registry.reference == "den"
    assert main(["fleet", "status"], registry=registry) == 1
    out = capsys.readouterr().out
    assert "garage: 3 differences" in out
    assert "kodi: outdated (190 vs 200)" in out
    assert "plex: missing" in out
    assert "old.game: not on reference" in out


def test_fleet_sync(registry, mirrored, capsys):
    main(["fleet", "set-reference", "den"], registry=registry)
    assert main(["fleet", "sync"], registry=registry) == 0
    out = capsys.readouterr().out
    assert "kodi: updated to 200" in out
    assert "plex: installed 50" in out
    assert "old.game: not on reference, left as is (use --prune)" in out
    assert main(["fleet", "sync", "--prune"], registry=registry) == 0
    assert main(["fleet", "status"], registry=registry) == 0
    assert "garage: in sync" in capsys.readouterr().out


def test_reference_survives_other_registry_changes(registry, fleet):
    registry.set_reference("den")
    registry.add(Device("attic", "10.0.0.9"))
    assert registry.reference == "den"
    registry.remove("den")
    assert registry.reference is None


def test_fleet_unreachable_reference(registry, fleet, capsys):
    registry.add(Device("attic", "10.0.0.9"))
    assert main(["fleet", "status", "--from", "attic"], registry=registry) == 1
    assert "can't read apps from reference attic: unreachable" in capsys.readouterr().err


def test_install_shows_progress_phases_when_not_a_terminal(registry, fleet, tmp_path, capsys):
    apk = tmp_path / "app.apk"
    with zipfile.ZipFile(apk, "w") as zf:
        zf.writestr("AndroidManifest.xml", build_manifest(ATTRS, STRINGS))
    fleet["den"].responses["pm install"] = "Success"
    fleet["den"].installed["com.example.tv"] = (42, "1.4.2")
    assert main(["app", "install", str(apk), "-d", "den"], registry=registry) == 0
    err = capsys.readouterr().err.splitlines()
    assert err == [
        "Copying com.example.tv to den - 0%",
        "Installing com.example.tv on den",
    ]


def test_progress_line_rewrites_in_place_on_a_terminal():
    import io

    from shield_manager.cli import _ProgressLine
    from shield_manager.deploy import Phase, Progress

    class Tty(io.StringIO):
        def isatty(self):
            return True

    stream = Tty()
    line = _ProgressLine(stream)
    line(Progress("org.xbmc.kodi", Phase.DOWNLOADING, 40, 100, device="den", source="lounge"))
    line(Progress("org.xbmc.kodi", Phase.INSTALLING, device="den"))
    line.clear()
    out = stream.getvalue()
    assert out.startswith("\rDownloading org.xbmc.kodi from lounge - 40%")
    assert "\rInstalling org.xbmc.kodi on den" in out
    assert out.endswith("\r")


def test_app_store_page_opens_the_play_store(registry, fleet, capsys):
    assert main(["app", "store-page", "org.xbmc.kodi", "-d", "den"], registry=registry) == 0
    assert any("market://details?id=org.xbmc.kodi" in c for c in fleet["den"].commands)
    assert "press Install" in capsys.readouterr().out


def test_fleet_updates_lists_and_installs(registry, fleet, monkeypatch, tmp_path, capsys):
    import json

    from shield_manager import sources
    from tests.fakes import FakeHttp, real_apk

    pkg = "org.xbmc.kodi"
    copy = real_apk(tmp_path / "den.apk", pkg, 100, "1.0", cert=b"dev")
    fleet["den"].installed[pkg] = (100, "1.0")
    fleet["den"].apk_files[pkg] = copy
    new = real_apk(tmp_path / "new.apk", pkg, 110, "1.1", cert=b"dev")
    url = "https://github.com/dl/kodi.apk"
    releases = [{"tag_name": "1.1", "assets": [{"name": "kodi.apk", "browser_download_url": url}]}]
    http = FakeHttp(
        {
            "https://api.github.com/repos/xbmc/xbmc/releases?per_page=10": json.dumps(
                releases
            ).encode(),
            url: new,
        }
    )
    monkeypatch.setattr(
        sources.Downloader, "from_config", classmethod(lambda cls, d: cls(http, {pkg: "xbmc/xbmc"}))
    )

    assert main(["fleet", "updates"], registry=registry) == 0
    out = capsys.readouterr().out
    assert f"{pkg}: 1.0 -> 1.1 (GitHub) on den" in out
    assert "fleet updates --install" in out

    assert main(["fleet", "updates", "--install"], registry=registry) == 0
    assert f"den: {pkg} updated to 1.1 (from GitHub)" in capsys.readouterr().out
    assert fleet["den"].installed[pkg][0] == 110


def test_app_download_page_links_the_build_each_shield_runs(registry, fleet, monkeypatch, capsys):
    from shield_manager import sources
    from tests.fakes import FakeHttp

    pkg = "org.xbmc.kodi"
    fleet["den"].installed[pkg] = (100, "21.1")
    fleet["den"].abis = ["armeabi-v7a", "armeabi"]
    page = "https://www.apkmirror.com/apk/xbmc/kodi/kodi-21-1-release/"
    http = FakeHttp(redirects={sources.morphe_url(pkg, "21.1", "armeabi-v7a"): page})
    monkeypatch.setattr(sources.Downloader, "from_config", classmethod(lambda cls, d: cls(http)))
    assert main(["app", "download-page", pkg, "-d", "den"], registry=registry) == 0
    out = capsys.readouterr().out
    assert f"den: 21.1 for armeabi-v7a: {page}" in out
    assert "shield-manager app install FILE" in out
