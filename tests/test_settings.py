import pytest

from shield_manager import settings
from shield_manager.cli import main
from shield_manager.registry import Device, Registry
from tests.fakes import FakeConnection

MAPPER = "flar2.homebutton/flar2.homebutton.MyAccessibilityService"


def test_read_parses_every_table():
    conn = FakeConnection(
        settings={
            ("global", "window_animation_scale"): "0.5",
            ("secure", "screensaver_components"): "a/b=c",
            ("system", "font_scale"): "1.15",
        }
    )
    values = settings.read(conn)
    assert values[("global", "window_animation_scale")] == "0.5"
    assert values[("secure", "screensaver_components")] == "a/b=c"  # "=" in a value
    assert values[("system", "font_scale")] == "1.15"


def test_compare_only_chosen_settings_and_blocks_missing_apps():
    ref = {
        ("system", "font_scale"): "1.3",
        ("secure", "enabled_accessibility_services"): MAPPER,
        ("global", "device_name"): "Living room",
    }
    dev = {("system", "font_scale"): "1.0", ("global", "device_name"): "Den"}
    chosen = settings.selected()
    diffs = settings.compare(ref, dev, chosen, installed={"com.other"})
    by_key = {d.key: d for d in diffs}
    assert set(by_key) == {("system", "font_scale"), ("secure", "enabled_accessibility_services")}
    assert by_key[("system", "font_scale")].syncable
    blocked = by_key[("secure", "enabled_accessibility_services")]
    assert not blocked.syncable
    assert "needs flar2.homebutton installed" in blocked.describe()
    ok = settings.compare(ref, dev, chosen, installed={"flar2.homebutton"})
    assert all(d.syncable for d in ok)


def test_other_differences_skip_device_identity_and_counters():
    ref = {
        ("global", "device_name"): "Living room",
        ("secure", "android_id"): "1",
        ("global", "boot_count"): "4",
        ("global", "nvidia_ai_upscaling"): "2",
    }
    dev = {("global", "device_name"): "Den", ("secure", "android_id"): "2"}
    others = settings.other_differences(ref, dev, settings.selected())
    assert [d.key for d in others] == [("global", "nvidia_ai_upscaling")]


def test_parse_key_refuses_device_specific_settings():
    assert settings.parse_key("global/nvidia_ai_upscaling") == ("global", "nvidia_ai_upscaling")
    for bad in ("global/adb_enabled", "secure/android_id", "nope/x", "global/"):
        with pytest.raises(ValueError):
            settings.parse_key(bad)


def test_selected_categories():
    defaults = {s.category for s in settings.selected()}
    assert "surround" not in defaults and "screensaver" in defaults
    only = settings.selected(["surround"], [("global", "x_setting")])
    assert {s.category for s in only} == {"surround", "other"}
    with pytest.raises(ValueError):
        settings.selected(["nope"])


def test_describe_durations():
    timeout = next(s for s in settings.CATALOG if s.name == "screen_off_timeout")
    assert settings.describe_value(timeout, "900000") == "15 min"
    assert settings.describe_value(timeout, "7200000") == "2 h"
    assert settings.describe_value(timeout, "2147483647") == "never"
    assert settings.describe_value(None, None) == "not set"


@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path / "devices.json")


@pytest.fixture
def shields(registry, monkeypatch):
    from shield_manager import adb

    registry.add(Device("living-room", "10.0.0.1"))
    registry.add(Device("bedroom", "10.0.0.2"))
    registry.add(Device("den", "10.0.0.3"))
    registry.set_reference("living-room")
    conns = {
        "living-room": FakeConnection(
            installed={"flar2.homebutton": (1, "")},
            settings={
                ("system", "screen_off_timeout"): "900000",
                ("global", "window_animation_scale"): "0.5",
                ("secure", "enabled_accessibility_services"): MAPPER,
                ("global", "device_name"): "Living room",
                ("global", "encoded_surround_output"): "2",
            },
        ),
        "bedroom": FakeConnection(
            settings={
                ("system", "screen_off_timeout"): "300000",
                ("global", "device_name"): "Bedroom",
            },
        ),
    }

    def connect(device):
        if device.name not in conns:
            raise ConnectionRefusedError("unreachable")
        return conns[device.name]

    monkeypatch.setattr(adb, "connect", connect)
    return conns


def test_status_changes_nothing(registry, shields, capsys):
    assert main(["settings", "status", "-d", "bedroom"], registry=registry) == 1
    out = capsys.readouterr().out
    assert "bedroom: 3 settings differ" in out
    assert "Start screensaver after: 5 min here, 15 min on the reference" in out
    assert "needs flar2.homebutton installed here first" in out
    assert "Surround" not in out  # not a default category
    assert not [c for c in shields["bedroom"].commands if c.startswith("settings put")]


def test_sync_copies_safe_settings_only(registry, shields, capsys):
    assert main(["settings", "sync", "-d", "bedroom"], registry=registry) == 0
    bedroom = shields["bedroom"].settings
    assert bedroom[("system", "screen_off_timeout")] == "900000"
    assert bedroom[("global", "window_animation_scale")] == "0.5"
    assert bedroom[("global", "device_name")] == "Bedroom"
    assert ("secure", "enabled_accessibility_services") not in bedroom  # app missing
    assert ("global", "encoded_surround_output") not in bedroom
    captured = capsys.readouterr()
    assert "Start screensaver after: set to 15 min" in captured.out
    assert "bedroom: setting Window animation scale (2/2) - 100%" in captured.err


def test_sync_reports_unreachable_shield_and_keeps_going(registry, shields, capsys):
    assert main(["settings", "sync"], registry=registry) == 1
    captured = capsys.readouterr()
    assert "den: FAILED: unreachable" in captured.err
    assert "bedroom: settings synced" in captured.out


def test_sync_category_and_dry_run(registry, shields, capsys):
    args = ["settings", "sync", "-d", "bedroom", "-c", "surround", "--dry-run"]
    assert main(args, registry=registry) == 1
    assert "Surround sound: not set here, 2 on the reference" in capsys.readouterr().out
    assert ("global", "encoded_surround_output") not in shields["bedroom"].settings
    assert main(["settings", "sync", "-d", "bedroom", "-c", "surround"], registry=registry) == 0
    assert shields["bedroom"].settings[("global", "encoded_surround_output")] == "2"


def test_status_others_lists_unknown_differences(registry, shields, capsys):
    shields["living-room"].settings[("global", "nvidia_ai_upscaling")] = "2"
    main(["settings", "status", "-d", "bedroom", "--others"], registry=registry)
    out = capsys.readouterr().out
    assert "global/nvidia_ai_upscaling: not set here, 2 on the reference" in out
    assert "device_name" not in out


def test_put_reports_a_setting_the_shield_refused():
    conn = FakeConnection(responses={"settings put": "java.lang.SecurityException: denied\n"})
    with pytest.raises(settings.SettingsError, match="SecurityException"):
        settings.put(conn, ("system", "font_scale"), "1.3")


def test_settings_list_and_bad_key(registry, capsys):
    assert main(["settings", "list"], registry=registry) == 0
    assert "Put device to sleep after (secure/sleep_timeout)" in capsys.readouterr().out
    registry.add(Device("den", "10.0.0.3"))
    registry.set_reference("den")
    assert main(["settings", "status", "--key", "global/adb_enabled"], registry=registry) == 2
    assert "never synced" in capsys.readouterr().err
