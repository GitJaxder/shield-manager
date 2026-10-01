"""See what a Shield is showing: a screenshot and the app in the foreground.

Screenshots of DRM-protected video (Netflix, Disney+ and most paid streaming apps) come
back black; the foreground app is still reported, so the two together say what's on.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from shield_manager.deploy import REMOTE_TMP_DIR, Connection

REMOTE_SCREENSHOT = f"{REMOTE_TMP_DIR}/shield-manager-screen.png"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Lines such as "mCurrentFocus=Window{1a2b u0 com.plexapp.android/com.plexapp...Main}" or
# "mFocusedApp=ActivityRecord{3c4d u0 org.xbmc.kodi/.Splash t12}".
_FOCUS = re.compile(r"\bu\d+ ([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/")
_WAKEFULNESS = re.compile(r"mWakefulness=(\w+)")


# Friendly names for the system screens most often in front, which aren't in the app list.
SYSTEM_SCREENS = {
    "com.google.android.tvlauncher": "Home screen",
    "com.google.android.leanbacklauncher": "Home screen",
    "com.android.tv.settings": "Settings",
    "com.google.android.backdrop": "Screensaver",
    "com.android.vending": "Play Store",
}


class ScreenError(Exception):
    pass


@dataclass(frozen=True)
class Activity:
    awake: bool  # False while the Shield is asleep (screenshots are then black)
    state: str  # Android's wakefulness: Awake, Asleep, Dreaming (screensaver) or Dozing
    package: str | None  # the app in the foreground, if one could be found


def current_activity(conn: Connection) -> Activity:
    """Report whether the Shield is awake and which app is in the foreground."""
    power = str(conn.shell("dumpsys power | grep mWakefulness="))
    match = _WAKEFULNESS.search(power)
    state = match.group(1) if match else "Unknown"
    window = str(conn.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'"))
    # Prefer the focused app over the focused window: a dialog or the volume overlay can
    # hold window focus while the app underneath is still what's on screen.
    lines = sorted(window.splitlines(), key=lambda ln: "mFocusedApp" not in ln)
    package = next((m.group(1) for ln in lines if (m := _FOCUS.search(ln))), None)
    return Activity(awake=state not in ("Asleep", "Dozing"), state=state, package=package)


def screenshot(conn: Connection) -> bytes:
    """Capture the screen as PNG bytes.

    The capture is written to a file on the Shield and pulled back over ADB's file
    transfer, which keeps the binary intact (piping it through the shell can mangle it).
    """
    out = str(conn.shell(f"screencap -p {REMOTE_SCREENSHOT}"))
    try:
        with tempfile.TemporaryDirectory(prefix="shield-manager-") as tmp:
            local = Path(tmp) / "screen.png"
            conn.pull(REMOTE_SCREENSHOT, str(local))
            data = local.read_bytes() if local.exists() else b""
    finally:
        conn.shell(f"rm -f {REMOTE_SCREENSHOT}")
    if not data.startswith(PNG_SIGNATURE):
        raise ScreenError(out.strip() or "the Shield didn't return a screenshot")
    return data
