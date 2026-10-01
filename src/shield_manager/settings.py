"""Keep every Shield's Android settings in line with a reference Shield.

Settings are read and written with Android's `settings` command (the global, secure and
system tables), which the ADB shell user may change without root. Only a curated list of
settings is synced: much of what's in those tables belongs to one device (its name, IDs,
network and ADB state), and copying it would break things, including our own connection.

Like fleet.py, this module doesn't print or parse arguments, so it can be reused outside
the CLI (for example by a Home Assistant integration).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from shield_manager.deploy import Connection
from shield_manager.registry import Device

Connector = Callable[[Device], Connection]
Key = tuple[str, str]  # (table, name), e.g. ("secure", "screensaver_enabled")

TABLES = ("global", "secure", "system")


@dataclass(frozen=True)
class Setting:
    table: str
    name: str
    label: str
    category: str
    # "ms" for durations in milliseconds, shown as minutes/hours.
    unit: str = ""
    # The value names app components (package/Class); it's only set on a Shield that has
    # every one of those apps installed, so it never points at a missing app.
    components: bool = False

    @property
    def key(self) -> Key:
        return (self.table, self.name)


@dataclass(frozen=True)
class Category:
    name: str
    description: str
    default: bool = True  # synced unless asked for specific categories


CATEGORIES = [
    Category("screensaver", "Screensaver and sleep timers"),
    Category("animations", "Animation speeds (Developer options)"),
    Category("date-time", "Clock format and automatic date and time"),
    Category("text", "Font size"),
    Category("captions", "Caption style"),
    Category("accessibility", "Accessibility options and services (e.g. Button Mapper)"),
    Category("keyboard", "Enabled keyboards and the default keyboard"),
    Category("hdmi-cec", "HDMI-CEC control of the TV"),
    Category("sound", "System sounds"),
    Category(
        "surround",
        "Surround sound output formats (depends on each Shield's TV or receiver)",
        default=False,
    ),
]

_C = Setting
# Applied in this order, so e.g. a keyboard is enabled before it's made the default.
CATALOG = [
    _C("secure", "screensaver_enabled", "Screensaver on", "screensaver"),
    _C("secure", "screensaver_components", "Screensaver", "screensaver", components=True),
    _C("secure", "screensaver_activate_on_sleep", "Screensaver when asleep", "screensaver"),
    _C("secure", "screensaver_activate_on_dock", "Screensaver when docked", "screensaver"),
    _C("system", "screen_off_timeout", "Start screensaver after", "screensaver", "ms"),
    _C("secure", "sleep_timeout", "Put device to sleep after", "screensaver", "ms"),
    _C("secure", "attentive_timeout", "Turn off when idle after", "screensaver", "ms"),
    _C("global", "window_animation_scale", "Window animation scale", "animations"),
    _C("global", "transition_animation_scale", "Transition animation scale", "animations"),
    _C("global", "animator_duration_scale", "Animator duration scale", "animations"),
    _C("system", "time_12_24", "Clock format (12/24 hour)", "date-time"),
    _C("global", "auto_time", "Automatic date and time", "date-time"),
    _C("global", "auto_time_zone", "Automatic time zone", "date-time"),
    _C("system", "font_scale", "Font size", "text"),
    _C("secure", "accessibility_captioning_enabled", "Captions on", "captions"),
    _C("secure", "accessibility_captioning_locale", "Caption language", "captions"),
    _C("secure", "accessibility_captioning_preset", "Caption style preset", "captions"),
    _C("secure", "accessibility_captioning_font_scale", "Caption text size", "captions"),
    _C("secure", "accessibility_captioning_typeface", "Caption font", "captions"),
    _C("secure", "accessibility_captioning_edge_type", "Caption edge type", "captions"),
    _C("secure", "accessibility_captioning_edge_color", "Caption edge color", "captions"),
    _C("secure", "accessibility_captioning_foreground_color", "Caption text color", "captions"),
    _C("secure", "accessibility_captioning_background_color", "Caption background", "captions"),
    _C("secure", "accessibility_captioning_window_color", "Caption window color", "captions"),
    _C("secure", "high_text_contrast_enabled", "High contrast text", "accessibility"),
    _C("secure", "accessibility_display_inversion_enabled", "Color inversion", "accessibility"),
    _C(
        "secure",
        "enabled_accessibility_services",
        "Accessibility services",
        "accessibility",
        components=True,
    ),
    _C("secure", "accessibility_enabled", "Accessibility on", "accessibility"),
    _C("secure", "enabled_input_methods", "Enabled keyboards", "keyboard", components=True),
    _C("secure", "default_input_method", "Default keyboard", "keyboard", components=True),
    _C("global", "hdmi_control_enabled", "HDMI-CEC", "hdmi-cec"),
    _C("global", "hdmi_control_auto_wakeup_enabled", "Turn on with the TV", "hdmi-cec"),
    _C("global", "hdmi_control_auto_device_off_enabled", "Turn the TV off with it", "hdmi-cec"),
    _C("system", "sound_effects_enabled", "System sounds", "sound"),
    _C("global", "encoded_surround_output", "Surround sound", "surround"),
    _C(
        "global",
        "encoded_surround_output_enabled_formats",
        "Surround formats",
        "surround",
    ),
]

_BY_KEY = {s.key: s for s in CATALOG}

# Never synced, even when asked for by name: they identify one device, keep it on the
# network, or keep ADB (and so shield-manager) working.
NEVER = re.compile(
    r"android_id|device_name|bluetooth|wifi|adb_|development_settings|_mac|mac_|"
    r"boot_count|ssaid|install_non_market|lock_screen|lockscreen|netstats|ntp_|"
    r"network|mobile_data|airplane|captive_portal|tether|vpn|usb_|"
    r"(^|_)id$|_token|_uuid|serial|unique",
    re.IGNORECASE,
)

# Settings whose values change by themselves (counters, timestamps, caches), left out of
# the "other differences" list because they always differ.
_NOISE = re.compile(
    r"_time(stamp)?s?$|_count$|_version$|last_|_cache|_seen|_shown|"
    r"^skip_first_use|^user_setup|^device_provisioned|_sequence|_checkin|_ms$",
    re.IGNORECASE,
)


class SettingsError(Exception):
    pass


def parse_key(text: str) -> Key:
    """Turn "secure/screensaver_enabled" into ("secure", "screensaver_enabled")."""
    table, _, name = text.partition("/")
    if table not in TABLES or not name:
        raise ValueError(f"{text!r} isn't TABLE/NAME, with TABLE one of {', '.join(TABLES)}")
    if NEVER.search(name):
        raise ValueError(f"{text} belongs to one Shield (or keeps it connected); never synced")
    return (table, name)


def describe_value(setting: Setting | None, value: str | None) -> str:
    if value is None:
        return "not set"
    if setting and setting.unit == "ms" and value.lstrip("-").isdigit():
        ms = int(value)
        if ms < 0 or ms >= 2**31 - 1:
            return "never"
        minutes = ms / 60000
        if minutes >= 60 and minutes % 60 == 0:
            return f"{int(minutes // 60)} h"
        if minutes >= 1:
            return f"{minutes:g} min"
        return f"{ms // 1000} s"
    return value or '""'


def read(conn: Connection) -> dict[Key, str]:
    """Every setting in the global, secure and system tables."""
    values: dict[Key, str] = {}
    for table in TABLES:
        out = str(conn.shell(f"settings list {table}"))
        for line in out.splitlines():
            name, sep, value = line.partition("=")
            if sep and name and " " not in name:
                values[(table, name)] = value
    return values


def installed_packages(conn: Connection) -> set[str]:
    """Every package on the Shield, system apps included."""
    out = str(conn.shell("pm list packages"))
    return {line[8:].strip() for line in out.splitlines() if line.startswith("package:")}


def component_packages(value: str) -> set[str]:
    """The apps named by a list of components, e.g.
    "com.a/.Service:com.b/com.b.Ime;123" -> {"com.a", "com.b"}."""
    return set(re.findall(r"([A-Za-z][\w.]*)/", value))


@dataclass(frozen=True)
class SettingDiff:
    key: Key
    reference: str | None  # None: not set on the reference
    device: str | None  # None: not set on this Shield
    setting: Setting | None = None  # None for settings outside the catalog
    # Why it won't be synced, e.g. an app it names isn't installed here.
    blocked: str = ""

    @property
    def label(self) -> str:
        return self.setting.label if self.setting else f"{self.key[0]}/{self.key[1]}"

    @property
    def syncable(self) -> bool:
        return self.reference is not None and not self.blocked

    def describe(self) -> str:
        """e.g. "Start screensaver after: 5 min here, 15 min on the reference"."""
        here = describe_value(self.setting, self.device)
        there = describe_value(self.setting, self.reference)
        text = f"{self.label}: {here} here, {there} on the reference"
        if self.reference is None:
            text += " (left as is)"
        if self.blocked:
            text += f" ({self.blocked})"
        return text


def selected(categories: Iterable[str] | None = None, keys: Iterable[Key] = ()) -> list[Setting]:
    """The catalog settings in the given categories (default: the default ones), plus
    any extra keys, in the order they're applied."""
    known = {c.name: c for c in CATEGORIES}
    if categories:
        unknown = [c for c in categories if c not in known]
        if unknown:
            raise ValueError(f"no settings category {unknown[0]!r}; try {', '.join(known)}")
        wanted = set(categories)
    else:
        wanted = {c.name for c in CATEGORIES if c.default}
    chosen = [s for s in CATALOG if s.category in wanted]
    for key in keys:
        if key in _BY_KEY:
            if _BY_KEY[key] not in chosen:
                chosen.append(_BY_KEY[key])
        else:
            chosen.append(Setting(key[0], key[1], f"{key[0]}/{key[1]}", "other"))
    return chosen


def compare(
    reference: dict[Key, str],
    device: dict[Key, str],
    chosen: list[Setting],
    installed: set[str] | None = None,
) -> list[SettingDiff]:
    """How the chosen settings differ on a Shield from the reference, in apply order.

    installed (the Shield's packages) blocks settings that name an app it doesn't have.
    """
    diffs = []
    for s in chosen:
        ref, dev = reference.get(s.key), device.get(s.key)
        if ref == dev:
            continue
        blocked = ""
        if s.components and ref and installed is not None:
            missing = sorted(component_packages(ref) - installed)
            if missing:
                blocked = f"needs {', '.join(missing)} installed here first"
        diffs.append(SettingDiff(s.key, ref, dev, s, blocked))
    return diffs


def other_differences(
    reference: dict[Key, str], device: dict[Key, str], chosen: list[Setting]
) -> list[SettingDiff]:
    """Settings outside the chosen ones that differ, minus device-specific ones and
    counters. Useful to find settings worth adding (e.g. Nvidia's own)."""
    skip = {s.key for s in chosen}
    diffs = []
    for key in sorted(reference.keys() | device.keys()):
        if key in skip or NEVER.search(key[1]) or _NOISE.search(key[1]):
            continue
        ref, dev = reference.get(key), device.get(key)
        if ref != dev:
            diffs.append(SettingDiff(key, ref, dev, _BY_KEY.get(key)))
    return diffs


def put(conn: Connection, key: Key, value: str) -> None:
    """Set one setting and check the Shield kept it."""
    table, name = key
    if NEVER.search(name):
        raise SettingsError(f"{table}/{name} is never changed by shield-manager")
    out = str(conn.shell(f"settings put {table} {name} {shlex.quote(value)}")).strip()
    if out:  # success prints nothing; errors (e.g. a SecurityException) print a message
        raise SettingsError(out.splitlines()[-1])
    now = str(conn.shell(f"settings get {table} {name}")).strip()
    if now != value.strip():
        raise SettingsError(f"the Shield kept {describe_value(_BY_KEY.get(key), now)}")


@dataclass(frozen=True)
class SettingsProgress:
    """One step of a settings sync, e.g. "den: setting Font size (3/7) - 42%"."""

    device: str
    label: str
    index: int  # 1-based
    total: int
    failed: str = ""

    @property
    def percent(self) -> int:
        return self.index * 100 // self.total if self.total else 100

    def describe(self) -> str:
        text = f"{self.device}: setting {self.label} ({self.index}/{self.total})"
        text += f" - {self.percent}%"
        if self.failed:
            text += f": FAILED: {self.failed}"
        return text


@dataclass
class SettingsReport:
    device: Device
    diffs: list[SettingDiff] = field(default_factory=list)
    others: list[SettingDiff] = field(default_factory=list)
    error: str | None = None
    # Filled in by sync: label -> new value, and label -> error.
    applied: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def in_sync(self) -> bool:
        return self.error is None and not self.diffs


def _close(conn: Connection) -> None:
    close = getattr(conn, "close", None)
    if close:
        close()


def _read_reference(reference: Device, connect: Connector) -> dict[Key, str]:
    conn = connect(reference)
    try:
        return read(conn)
    finally:
        _close(conn)


def sync(
    reference: Device,
    targets: list[Device],
    connect: Connector,
    categories: Iterable[str] | None = None,
    keys: Iterable[Key] = (),
    dry_run: bool = False,
    others: bool = False,
    progress: Callable[[SettingsProgress], None] | None = None,
) -> list[SettingsReport]:
    """Set each target's chosen settings to the reference's values.

    Settings the reference doesn't have are left as is, as are ones naming apps a target
    doesn't have (sync apps first). With dry_run nothing changes, which is how status
    works; with others, each report also lists the other settings that differ.
    """
    chosen = selected(categories, keys)
    ref_values = _read_reference(reference, connect)
    reports = []
    for device in targets:
        if device.name == reference.name:
            continue
        report = SettingsReport(device)
        reports.append(report)
        try:
            conn = connect(device)
        except Exception as e:  # one unreachable Shield shouldn't hide the others
            report.error = str(e) or type(e).__name__
            continue
        try:
            values = read(conn)
            report.diffs = compare(ref_values, values, chosen, installed_packages(conn))
            if others:
                report.others = other_differences(ref_values, values, chosen)
            if dry_run:
                continue
            todo = [d for d in report.diffs if d.syncable]
            for i, diff in enumerate(todo, 1):
                step = SettingsProgress(device.name, diff.label, i, len(todo))
                try:
                    put(conn, diff.key, diff.reference or "")
                    report.applied[diff.label] = describe_value(diff.setting, diff.reference)
                except Exception as e:
                    report.failed[diff.label] = str(e) or type(e).__name__
                    step = SettingsProgress(device.name, diff.label, i, len(todo), str(e))
                if progress:
                    progress(step)
        except Exception as e:
            report.error = str(e) or type(e).__name__
        finally:
            _close(conn)
    return reports


def status(
    reference: Device,
    targets: list[Device],
    connect: Connector,
    categories: Iterable[str] | None = None,
    keys: Iterable[Key] = (),
    others: bool = False,
) -> list[SettingsReport]:
    """Report how each target's settings differ from the reference, changing nothing."""
    return sync(reference, targets, connect, categories, keys, dry_run=True, others=others)
