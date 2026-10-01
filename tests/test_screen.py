from pathlib import Path

import pytest

from shield_manager import screen
from tests.fakes import FakeConnection

PNG = screen.PNG_SIGNATURE + b"rest of the image"

POWER_AWAKE = "  mWakefulness=Awake\n  mWakefulnessChanging=false\n"
WINDOW = (
    "  mCurrentFocus=Window{9f1 u0 com.android.systemui/com.android.systemui.VolumeDialog}\n"
    "  mFocusedApp=ActivityRecord{3c4d u0 org.xbmc.kodi/.Splash t12}\n"
)


class ScreenConnection(FakeConnection):
    def __init__(self, image=PNG, **kwargs):
        super().__init__(**kwargs)
        self.image = image

    def pull(self, device_path, local_path, **kwargs):
        self.pulled.append(device_path)
        if self.image is not None:
            Path(local_path).write_bytes(self.image)


def test_current_activity_prefers_the_focused_app_over_an_overlay():
    conn = FakeConnection(responses={"dumpsys power": POWER_AWAKE, "dumpsys window": WINDOW})
    activity = screen.current_activity(conn)
    assert activity == screen.Activity(awake=True, state="Awake", package="org.xbmc.kodi")


def test_current_activity_falls_back_to_the_focused_window():
    window = "  mCurrentFocus=Window{1a2b u0 com.google.android.tvlauncher/com.x.MainActivity}\n"
    conn = FakeConnection(responses={"dumpsys power": POWER_AWAKE, "dumpsys window": window})
    assert screen.current_activity(conn).package == "com.google.android.tvlauncher"


@pytest.mark.parametrize("state,awake", [("Asleep", False), ("Dozing", False), ("Dreaming", True)])
def test_current_activity_reports_sleep(state, awake):
    conn = FakeConnection(responses={"dumpsys power": f"mWakefulness={state}\n"})
    activity = screen.current_activity(conn)
    assert (activity.state, activity.awake, activity.package) == (state, awake, None)


def test_screenshot_pulls_the_png_and_cleans_up():
    conn = ScreenConnection()
    assert screen.screenshot(conn) == PNG
    assert conn.commands[0] == f"screencap -p {screen.REMOTE_SCREENSHOT}"
    assert conn.pulled == [screen.REMOTE_SCREENSHOT]
    assert conn.commands[-1] == f"rm -f {screen.REMOTE_SCREENSHOT}"


def test_screenshot_reports_what_screencap_said():
    conn = ScreenConnection(image=None, responses={"screencap": "Permission denied"})
    with pytest.raises(screen.ScreenError, match="Permission denied"):
        screen.screenshot(conn)
    assert conn.commands[-1] == f"rm -f {screen.REMOTE_SCREENSHOT}"
