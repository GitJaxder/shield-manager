import pytest

from shield_manager import settings
from shield_manager.cli import main
from shield_manager.registry import Device, Registry
from tests.fakes import FakeConnection

BACKDROP = "com.google.android.backdrop/com.google.android.backdrop.Backdrop"
PHOTOS = "com.google.android.apps.photos/.Dream"
GBOARD = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
SWIFT = "com.touchtype.swiftkey/com.touchtype.KeyboardService"


def shield(**values):
    conn = FakeConnection(
        settings=values or {("system", "font_scale"): "1.15"},
        dreams=[BACKDROP, PHOTOS],
        keyboards=[GBOARD, SWIFT],
    )
    return conn


def by_id(conn):
    return {c.setting.id: c for c in settings.read(conn)}


def test_catalog_never_touches_connection_or_identity_settings():
    for s in settings.CATALOG:
        assert s.table in settings.TABLES
        assert not any(bad in s.name for bad in ("adb", "wifi", "device_name", "android_id"))
    assert len(settings.BY_ID) == len(settings.CATALOG)
    assert {s.group for s in settings.CATALOG} == set(settings.GROUPS)


def test_read_shows_labels_and_android_defaults():
    conn = shield()
    conn.settings[("secure", "screensaver_components")] = BACKDROP + ",x/y"
    conn.settings[("system", "screen_off_timeout")] = "900000"
    conn.settings[("global", "window_animation_scale")] = "0.5"
    conn.settings[("system", "font_scale")] = "1.15"
    now = by_id(conn)
    assert now["font-size"].display == "Large"
    assert now["screensaver-after"].display == "15 min"
    assert now["window-animation"].display == "0.5x (faster)"
    assert now["hdmi-cec"].display == "On"  # not stored: Android's default
    assert now["clock"].display == "Automatic"
    assert now["screensaver-app"].display == "Ambient Mode"
    photos = [o.value for o in now["screensaver-app"].options]
    assert photos[1] == "com.google.android.apps.photos/com.google.android.apps.photos.Dream"
    assert now["sleep-after"].as_dict()["options"][-1] == {"value": "-1", "label": "Never"}


def test_change_by_label_and_verify():
    conn = shield()
    assert settings.change(conn, "font-size", "largest").display == "Largest"
    assert conn.settings[("system", "font_scale")] == "1.3"
    assert settings.change(conn, "hdmi-cec", "off").value == "0"
    assert settings.change(conn, "clock", "24").display == "24 hour"
    assert settings.change(conn, "screensaver-after", "1 hour").value == str(60 * 60_000)
    assert settings.change(conn, "caption-style", "Yellow on blue").value == "3"
    assert settings.change(conn, "sleep-after", "-1").display == "Never"


def test_unset_clears_a_setting():
    conn = shield()
    conn.settings[("system", "time_12_24")] = "12"
    current = settings.change(conn, "clock", "automatic")
    assert ("system", "time_12_24") not in conn.settings
    assert current.display == "Automatic"


def test_change_refuses_unknown_settings_and_values():
    conn = shield()
    with pytest.raises(settings.SettingsError, match="no setting called"):
        settings.change(conn, "adb", "1")
    with pytest.raises(settings.SettingsError, match="pick one of: Small, Default"):
        settings.change(conn, "font-size", "huge; reboot")
    assert not [c for c in conn.commands if c.startswith("settings put")]


def test_change_reports_a_value_the_shield_did_not_keep():
    conn = shield()
    conn.responses["settings get system font_scale"] = "1.0\n"
    with pytest.raises(settings.SettingsError, match="kept Font size at Default"):
        settings.change(conn, "font-size", "large")


def test_change_reports_a_refusal():
    conn = shield()
    conn.responses["settings put"] = "java.lang.SecurityException: Permission denial"
    with pytest.raises(settings.SettingsError, match="Permission denial"):
        settings.change(conn, "font-size", "large")


def test_keyboard_is_enabled_then_made_default():
    conn = shield()
    current = settings.change(conn, "keyboard", "swiftkey")
    assert current.value == SWIFT
    assert conn.settings[("secure", "default_input_method")] == SWIFT
    enable = conn.commands.index(f"ime enable {SWIFT}")
    assert conn.commands.index(f"ime set {SWIFT}") > enable
    with pytest.raises(settings.SettingsError, match="pick one of"):
        settings.change(conn, "keyboard", "com.missing")


def test_screensaver_from_installed_ones():
    conn = shield()
    settings.change(conn, "screensaver-app", "Google Photos")
    assert conn.settings[("secure", "screensaver_components")].endswith("photos.Dream")


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path / "devices.json")
    reg.add(Device("bedroom", "10.10.20.11"))
    return reg


def test_cli_show_and_set(registry, monkeypatch, capsys):
    from shield_manager import adb

    conn = shield()
    monkeypatch.setattr(adb, "connect", lambda d: conn)
    assert main(["settings", "show", "bedroom"], registry=registry) == 0
    out = capsys.readouterr().out
    assert "Display:\n  Font size: Large  [font-size]" in out
    assert main(["settings", "set", "bedroom", "font-size", "Small"], registry=registry) == 0
    captured = capsys.readouterr()
    assert "bedroom: Font size is now Small" in captured.out
    assert "bedroom: setting Font size" in captured.err
    assert main(["settings", "set", "bedroom", "font-size", "tiny"], registry=registry) == 1
    assert "error: bedroom: Font size can't be 'tiny'" in capsys.readouterr().err
    assert main(["settings", "set", "bedroom", "adb_enabled", "0"], registry=registry) == 2
    assert conn.closed


def test_cli_list_needs_no_shield(capsys):
    assert main(["settings", "list"]) == 0
    out = capsys.readouterr().out
    assert "  font-size              Font size (Small, Default, Large, Largest)" in out
    assert "keyboard" in out and "an installed keyboard" in out
