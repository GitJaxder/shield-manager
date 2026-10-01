import pytest

from shield_manager import osupdate
from tests.fakes import FakeConnection


def getprop(device="mdarcy", version="9.2.4", **extra):
    props = {
        "ro.product.device": device,
        "ro.product.model": "SHIELD Android TV",
        "ro.build.version.release": "11",
        "ro.build.display.id": "RQ1A.210105.003",
        "ro.build.version.security_patch": "2026-01-05",
        **({"ro.build.version.shield": version} if version else {}),
        **extra,
    }
    return "".join(f"[{k}]: [{v}]\n" for k, v in props.items())


NOTIFICATIONS = """\
  NotificationRecord(0x01: pkg=com.plexapp.android user=UserHandle{0} id=3
      extras={
        android.title=String (Now playing)
  NotificationRecord(0x02: pkg=com.nvidia.ota user=UserHandle{0} id=1
      extras={
        android.title=String (System upgrade available)
        android.text=String (SHIELD Experience 9.2.5 is ready to install)
"""


def shield(props=None, notifications="", ota_enabled=True):
    return FakeConnection(
        responses={
            "getprop": props if props is not None else getprop(),
            "pm list packages -e com.nvidia.ota": (
                "package:com.nvidia.ota\n" if ota_enabled else ""
            ),
            "dumpsys notification": notifications,
        }
    )


def test_read_os_reports_version_hardware_and_no_update():
    info = osupdate.read_os(shield())
    assert info == osupdate.OsInfo(
        hardware="mdarcy",
        model="SHIELD TV Pro (2019)",
        version="9.2.4",
        android="11",
        build="RQ1A.210105.003",
        security_patch="2026-01-05",
    )


def test_read_os_picks_up_the_upgrade_notification():
    info = osupdate.read_os(shield(notifications=NOTIFICATIONS))
    assert info.update_notice == (
        "System upgrade available - SHIELD Experience 9.2.5 is ready to install"
    )


def test_read_os_notices_a_disabled_updater():
    assert osupdate.read_os(shield(ota_enabled=False)).updater_enabled is False


@pytest.mark.parametrize(
    "props,expected",
    [
        ({"ro.nv.build.version": "9.1.1"}, "9.1.1"),
        ({"ro.vendor.nvidia.shield.version": "9.2"}, "9.2"),
        ({"ro.build.version.release": "11"}, None),  # Android's version isn't it
        ({"ro.build.version.shield": "unknown"}, None),
    ],
)
def test_find_version(props, expected):
    assert osupdate.find_version(props) == expected


def status(name, hardware, version):
    info = osupdate.OsInfo(hardware, hardware, version, "11", "b", "")
    return osupdate.OsStatus(name, info)


def test_compare_flags_shields_behind_a_twin_of_the_same_hardware():
    statuses = osupdate.compare(
        [
            status("den", "mdarcy", "9.2.4"),
            status("living", "mdarcy", "9.1.10"),
            status("bedroom", "sif", "8.2.3"),  # no twin: never "behind"
            osupdate.OsStatus("garage", error="unreachable"),
        ]
    )
    by = {s.device: s for s in statuses}
    assert by["living"].behind == "9.2.4" and by["living"].update_available
    assert by["den"].behind is None and not by["den"].update_available
    assert by["bedroom"].behind is None and by["bedroom"].newest == "8.2.3"
    assert "FAILED: unreachable" in by["garage"].describe()


def test_check_reads_every_device_and_keeps_going():
    class D:
        def __init__(self, name):
            self.name = name

    conns = {"den": shield(), "living": shield(getprop(version="9.1.0"))}

    def connect(device):
        if device.name not in conns:
            raise ConnectionRefusedError("unreachable")
        return conns[device.name]

    statuses = osupdate.check([D("den"), D("living"), D("gone")], connect)
    assert [s.behind for s in statuses] == [None, "9.2.4", None]
    assert statuses[2].error == "unreachable"
    assert all(c.closed for c in conns.values())


def test_open_update_screen_wakes_and_opens_system_upgrade():
    conn = FakeConnection(responses={"am start": "Starting: Intent { ... }"})
    assert osupdate.open_update_screen(conn) == "System upgrade"
    assert conn.commands == [
        "input keyevent KEYCODE_WAKEUP",
        "am start -a android.settings.SYSTEM_UPDATE_SETTINGS",
    ]


def test_open_update_screen_falls_back_to_about():
    class Conn(FakeConnection):
        def shell(self, command, **kwargs):
            self.commands.append(command)
            if "SYSTEM_UPDATE" in command:
                return "Error: Activity not started, unable to resolve Intent"
            return "Starting: Intent"

    assert osupdate.open_update_screen(Conn()) == "About"
