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
    assert err == ["den: Copying com.example.tv - 0%", "den: Installing com.example.tv"]


def test_progress_line_rewrites_in_place_on_a_terminal():
    import io

    from shield_manager.cli import _ProgressLine
    from shield_manager.deploy import Phase, Progress

    class Tty(io.StringIO):
        def isatty(self):
            return True

    stream = Tty()
    line = _ProgressLine(stream)
    line(Progress("org.xbmc.kodi", Phase.DOWNLOADING, 40, 100, device="den"))
    line(Progress("org.xbmc.kodi", Phase.INSTALLING, device="den"))
    line.clear()
    out = stream.getvalue()
    assert out.startswith("\rden: Downloading org.xbmc.kodi - 40%")
    assert "\rden: Installing org.xbmc.kodi" in out
    assert out.endswith("\r")
