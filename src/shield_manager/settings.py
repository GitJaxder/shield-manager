"""View and change one Shield's Android settings.

Settings are read and written with Android's `settings` command (the global, secure and
system tables), which the ADB shell user may change without root. Only a curated list can
be changed: much of what's in those tables belongs to one device (its name, IDs, network
and ADB state), and changing it could break things, including our own connection. Nvidia's
own options (AI upscaling, display modes) live in its apps' private data and need root.

Like fleet.py, this module doesn't print or parse arguments, so the CLI and the web UI
share it.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from shield_manager.deploy import Connection

TABLES = ("global", "secure", "system")

# Kinds of setting. A toggle is "1" or "0"; a choice has a fixed list of values; a
# screensaver or keyboard is picked from the ones installed on the Shield.
TOGGLE, CHOICE, SCREENSAVER, KEYBOARD = "toggle", "choice", "screensaver", "keyboard"

# Clears a setting so Android falls back to its own default (e.g. the language's clock
# format).
UNSET = ""

DREAM_SERVICE = "android.service.dreams.DreamService"


@dataclass(frozen=True)
class Option:
    value: str
    label: str


@dataclass(frozen=True)
class Setting:
    id: str  # what the CLI and API call it, e.g. "font-size"
    table: str
    name: str
    label: str
    group: str
    kind: str = TOGGLE
    options: tuple[Option, ...] = ()
    # What Android uses while the setting isn't stored on the Shield.
    default: str | None = None


ON_OFF = (Option("1", "On"), Option("0", "Off"))
MINUTE = 60_000
ANIMATION = (
    Option("0", "Off"),
    Option("0.5", "0.5x (faster)"),
    Option("1", "1x (normal)"),
    Option("1.5", "1.5x"),
    Option("2", "2x"),
    Option("5", "5x"),
    Option("10", "10x"),
)


def _minutes(*values: int) -> tuple[Option, ...]:
    return tuple(
        Option(str(m * MINUTE), f"{m // 60} hour{'s' * (m > 60)}" if m >= 60 else f"{m} min")
        for m in values
    )


GROUPS = (
    "Screensaver and sleep",
    "Display",
    "Animations",
    "HDMI-CEC",
    "Sound",
    "Keyboard",
    "Captions and accessibility",
)

_S = Setting
CATALOG = (
    _S("screensaver", "secure", "screensaver_enabled", "Screensaver", GROUPS[0], default="1"),
    _S(
        "screensaver-app",
        "secure",
        "screensaver_components",
        "Which screensaver",
        GROUPS[0],
        SCREENSAVER,
    ),
    _S(
        "screensaver-after",
        "system",
        "screen_off_timeout",
        "Start screensaver after",
        GROUPS[0],
        CHOICE,
        _minutes(5, 15, 30, 60, 120),
    ),
    _S(
        "sleep-after",
        "secure",
        "sleep_timeout",
        "Put the Shield to sleep after",
        GROUPS[0],
        CHOICE,
        (*_minutes(30, 60, 180, 360, 720), Option("-1", "Never")),
    ),
    _S(
        "font-size",
        "system",
        "font_scale",
        "Font size",
        GROUPS[1],
        CHOICE,
        (
            Option("0.85", "Small"),
            Option("1.0", "Default"),
            Option("1.15", "Large"),
            Option("1.3", "Largest"),
        ),
        default="1.0",
    ),
    _S(
        "clock",
        "system",
        "time_12_24",
        "Clock format",
        GROUPS[1],
        CHOICE,
        (
            Option(UNSET, "Automatic"),
            Option("12", "12 hour"),
            Option("24", "24 hour"),
        ),
        default=UNSET,
    ),
    _S("auto-time", "global", "auto_time", "Automatic date and time", GROUPS[1], default="1"),
    _S("auto-time-zone", "global", "auto_time_zone", "Automatic time zone", GROUPS[1], default="1"),
    _S(
        "window-animation",
        "global",
        "window_animation_scale",
        "Window animation speed",
        GROUPS[2],
        CHOICE,
        ANIMATION,
        default="1",
    ),
    _S(
        "transition-animation",
        "global",
        "transition_animation_scale",
        "Transition animation speed",
        GROUPS[2],
        CHOICE,
        ANIMATION,
        default="1",
    ),
    _S(
        "animator-duration",
        "global",
        "animator_duration_scale",
        "Animator duration speed",
        GROUPS[2],
        CHOICE,
        ANIMATION,
        default="1",
    ),
    _S("hdmi-cec", "global", "hdmi_control_enabled", "HDMI-CEC control", GROUPS[3], default="1"),
    _S(
        "tv-wakes-shield",
        "global",
        "hdmi_control_auto_wakeup_enabled",
        "Turn on when the TV turns on",
        GROUPS[3],
        default="1",
    ),
    _S(
        "shield-sleeps-tv",
        "global",
        "hdmi_control_auto_device_off_enabled",
        "Turn the TV off when the Shield sleeps",
        GROUPS[3],
        default="1",
    ),
    _S("system-sounds", "system", "sound_effects_enabled", "System sounds", GROUPS[4], default="1"),
    _S(
        "surround",
        "global",
        "encoded_surround_output",
        "Surround sound",
        GROUPS[4],
        CHOICE,
        (
            Option("0", "Automatic"),
            Option("1", "Never"),
            Option("2", "Always"),
            Option("3", "Manual"),
        ),
        default="0",
    ),
    _S("keyboard", "secure", "default_input_method", "Default keyboard", GROUPS[5], KEYBOARD),
    _S(
        "captions", "secure", "accessibility_captioning_enabled", "Captions", GROUPS[6], default="0"
    ),
    _S(
        "caption-size",
        "secure",
        "accessibility_captioning_font_scale",
        "Caption text size",
        GROUPS[6],
        CHOICE,
        (
            Option("0.25", "Very small"),
            Option("0.5", "Small"),
            Option("1.0", "Normal"),
            Option("1.5", "Large"),
            Option("2.0", "Very large"),
        ),
        default="1.0",
    ),
    _S(
        "caption-style",
        "secure",
        "accessibility_captioning_preset",
        "Caption style",
        GROUPS[6],
        CHOICE,
        (
            Option("0", "White on black"),
            Option("1", "Black on white"),
            Option("2", "Yellow on black"),
            Option("3", "Yellow on blue"),
            Option("-1", "Custom"),
        ),
        default="0",
    ),
    _S(
        "high-contrast-text",
        "secure",
        "high_text_contrast_enabled",
        "High contrast text",
        GROUPS[6],
        default="0",
    ),
    _S(
        "color-inversion",
        "secure",
        "accessibility_display_inversion_enabled",
        "Color inversion",
        GROUPS[6],
        default="0",
    ),
)

BY_ID = {s.id: s for s in CATALOG}


class SettingsError(Exception):
    pass


def get(setting_id: str) -> Setting:
    try:
        return BY_ID[setting_id]
    except KeyError:
        raise SettingsError(
            f"no setting called {setting_id!r}; try one of: {', '.join(BY_ID)}"
        ) from None


def _read_tables(conn: Connection) -> dict[tuple[str, str], str]:
    values: dict[tuple[str, str], str] = {}
    for table in {s.table for s in CATALOG}:
        for line in str(conn.shell(f"settings list {table}")).splitlines():
            name, sep, value = line.partition("=")
            if sep and name and " " not in name:
                values[(table, name)] = value
    return values


_COMPONENT = re.compile(r"^\s*([A-Za-z][\w.]*/[\w.$]+)\s*$")


def _components(output: str) -> list[str]:
    seen: list[str] = []
    for line in output.splitlines():
        m = _COMPONENT.match(line)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def _full(component: str) -> str:
    """ "com.a/.Dream" -> "com.a/com.a.Dream", so short and long forms compare equal."""
    package, _, cls = component.partition("/")
    return f"{package}/{package}{cls}" if cls.startswith(".") else component


def screensavers(conn: Connection) -> list[str]:
    """The screensavers (dream services) installed on the Shield, as package/Class."""
    out = conn.shell(f"cmd package query-services --components -a {DREAM_SERVICE}")
    return [_full(c) for c in _components(str(out))]


def keyboards(conn: Connection) -> list[str]:
    """Every keyboard (input method) installed on the Shield, enabled or not."""
    return [_full(c) for c in _components(str(conn.shell("ime list -a -s")))]


# Friendly names for screensavers and keyboards that come with a Shield or are common;
# others show their package name (the web UI swaps in the app's name when it knows it).
KNOWN_APPS = {
    "com.google.android.backdrop": "Ambient Mode",
    "com.google.android.apps.tv.dreamx": "Ambient Mode",
    "com.google.android.apps.photos": "Google Photos",
    "com.android.dreams.basic": "Colors",
    "com.android.dreams.phototable": "Photo Table",
    "com.google.android.inputmethod.latin": "Gboard",
    "com.google.android.leanback.ime": "Leanback keyboard",
    "com.touchtype.swiftkey": "SwiftKey",
}


def _component_options(installed: list[str]) -> tuple[Option, ...]:
    options = []
    for c in installed:
        package = c.partition("/")[0]
        options.append(Option(c, KNOWN_APPS.get(package, package)))
    return tuple(options)


@dataclass(frozen=True)
class Current:
    """One setting as it is on a Shield, with the values it can be changed to."""

    setting: Setting
    value: str | None  # as stored; None when not stored (Android uses its default)
    options: tuple[Option, ...]

    @property
    def effective(self) -> str | None:
        return self.setting.default if self.value is None else self.value

    @property
    def display(self) -> str:
        value = self.effective
        if value is None:
            return "not set"
        for option in self.options:
            if _same(self.setting, option.value, value):
                return option.label
        return value or "not set"

    def as_dict(self) -> dict:
        return {
            "id": self.setting.id,
            "label": self.setting.label,
            "group": self.setting.group,
            "kind": self.setting.kind,
            "value": self.effective,
            "display": self.display,
            "options": [{"value": o.value, "label": o.label} for o in self.options],
        }


def _same(setting: Setting, a: str, b: str) -> bool:
    if setting.kind in (SCREENSAVER, KEYBOARD):
        # Screensavers are stored as a comma-separated list; the first one is shown.
        return _full(a) == _full(b.split(",")[0])
    try:
        return float(a) == float(b)  # "1" and "1.0" are the same font size
    except ValueError:
        return a == b


def _options(setting: Setting, dynamic: dict[str, list[str]]) -> tuple[Option, ...]:
    if setting.kind == TOGGLE:
        return ON_OFF
    if setting.kind in dynamic:
        return _component_options(dynamic[setting.kind])
    return setting.options


def read(conn: Connection) -> list[Current]:
    """Every setting in the catalog as it is on the Shield, in catalog order."""
    values = _read_tables(conn)
    dynamic = {SCREENSAVER: screensavers(conn), KEYBOARD: keyboards(conn)}
    result = []
    for s in CATALOG:
        value = values.get((s.table, s.name))
        if value == "null":
            value = None
        result.append(Current(s, value, _options(s, dynamic)))
    return result


def _simple(text: str) -> str:
    return re.sub(r"[\s_-]+", " ", text.strip().lower())


def _match(setting: Setting, options: tuple[Option, ...], wanted: str) -> str:
    """The stored value for what the user typed: a value, or an option's label
    (any case, spaces or dashes), e.g. "on", "Large", "24-hour"."""
    simple = _simple(wanted)
    for o in options:
        if wanted == o.value or simple == _simple(o.label):
            return o.value
    # "24" for "24 hour", "white" for "White on black", a package name; only if just one
    # option starts that way.
    starts = [o for o in options if simple and _simple(o.label).startswith(simple)]
    if len(starts) == 1:
        return starts[0].value
    choices = ", ".join(o.label for o in options) or "nothing installed"
    raise SettingsError(f"{setting.label} can't be {wanted!r}; pick one of: {choices}")


def change(conn: Connection, setting_id: str, wanted: str) -> Current:
    """Change one setting on the Shield, check it kept the new value, and return it."""
    setting = get(setting_id)
    if setting.kind == SCREENSAVER:
        options = _component_options(screensavers(conn))
    elif setting.kind == KEYBOARD:
        options = _component_options(keyboards(conn))
    else:
        options = _options(setting, {})
    value = _match(setting, options, wanted)
    if setting.kind == KEYBOARD:
        # A keyboard has to be enabled before it can be the default.
        _run(conn, f"ime enable {shlex.quote(value)}", ok=("now enabled", "already enabled"))
        _run(conn, f"ime set {shlex.quote(value)}", ok=("selected",))
    elif value == UNSET:
        _run(conn, f"settings delete {setting.table} {setting.name}", ok=("Deleted",))
    else:
        _run(conn, f"settings put {setting.table} {setting.name} {shlex.quote(value)}")
    now = str(conn.shell(f"settings get {setting.table} {setting.name}")).strip()
    stored = None if now == "null" else now
    current = Current(setting, stored, options)
    if value == UNSET:
        kept = not stored
    else:
        kept = stored is not None and _same(setting, value, stored)
    if not kept:
        raise SettingsError(f"the Shield kept {setting.label} at {current.display}")
    return current


def _run(conn: Connection, command: str, ok: tuple[str, ...] = ()) -> None:
    """Run a command that prints nothing (or one of the ok phrases) when it works."""
    out = str(conn.shell(command)).strip()
    if out and not any(phrase in out for phrase in ok):
        raise SettingsError(out.splitlines()[-1])
