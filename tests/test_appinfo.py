import zipfile
from pathlib import Path

from shield_manager import appinfo
from shield_manager.appinfo import ATTR_BANNER, ATTR_ICON, ATTR_LABEL, MetaCache
from tests.resources import TYPE_REFERENCE, TYPE_STRING, build_arsc, build_xml

ICON = 0x7F010000  # type 1 (mipmap), entry 0
LABEL = 0x7F020000  # type 2 (string), entry 0
BANNER = 0x7F030000  # type 3 (drawable), entry 0, an alias of entry 1
BANNER_TARGET = 0x7F030001

ARSC_STRINGS = [
    "res/mipmap-mdpi/ic.png",
    "res/mipmap-xxxhdpi/ic.png",
    "res/mipmap-anydpi-v26/ic.xml",
    "Kodi",
    "Kodi (Deutsch)",
    "res/drawable-xhdpi/banner.webp",
]


def manifest():
    strings = ["label", "icon", "banner", "manifest", "application", "package", "org.xbmc.kodi"]
    return build_xml(
        [
            ("manifest", [("package", TYPE_STRING, 6)]),
            (
                "application",
                [
                    ("label", TYPE_REFERENCE, LABEL),
                    ("icon", TYPE_REFERENCE, ICON),
                    ("banner", TYPE_REFERENCE, BANNER),
                ],
            ),
        ],
        strings,
        [ATTR_LABEL, ATTR_ICON, ATTR_BANNER],
    )


def arsc():
    return build_arsc(
        ARSC_STRINGS,
        [
            (1, 160, "", {0: (TYPE_STRING, 0)}),
            (1, 640, "", {0: (TYPE_STRING, 1)}),
            (1, 0xFFFE, "", {0: (TYPE_STRING, 2)}),
            (2, 0, "de", {0: (TYPE_STRING, 4)}),
            (2, 0, "", {0: (TYPE_STRING, 3)}),
            (3, 0, "", {0: (TYPE_REFERENCE, BANNER_TARGET), 1: (TYPE_STRING, 5)}),
        ],
    )


def test_describe_resolves_label_icon_and_banner():
    info = appinfo.describe(manifest(), arsc())
    assert info.label == "Kodi"  # the default-language string, not the German one
    assert info.icon_path == "res/mipmap-xxxhdpi/ic.png"  # highest-density bitmap, not XML
    assert info.banner_path == "res/drawable-xhdpi/banner.webp"  # followed the alias


def test_describe_without_resources_uses_literal_label():
    strings = ["label", "manifest", "application", "My App"]
    xml = build_xml(
        [("manifest", []), ("application", [("label", TYPE_STRING, 3)])], strings, [ATTR_LABEL]
    )
    info = appinfo.describe(xml, None)
    assert info.label == "My App"
    assert info.icon_path is None


class ApkConnection:
    """Simulates just enough of a Shield shell to unzip files from an APK and pull them."""

    def __init__(self, apk: Path):
        self.apk = apk
        self.extracted = {}  # remote path -> bytes
        self.commands = []

    def shell(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("pm path"):
            return "package:/data/app/kodi-1/base.apk\npackage:/data/app/kodi-1/split_x.apk\n"
        if "unzip" in command:
            remote_dir = command.split(" -d ")[-1].strip("'")
            members = command.split("base.apk ")[1].split(" -d ")[0].split()
            with zipfile.ZipFile(self.apk) as zf:
                names = set(zf.namelist())
                for m in (m.strip("'") for m in members):
                    if m in names:
                        self.extracted[f"{remote_dir}/{m}"] = zf.read(m)
            return ""
        if "find . -type f" in command:
            remote_dir = command.split("cd ")[1].split(" &&")[0].strip("'")
            return "".join(
                f"./{p[len(remote_dir) + 1 :]}\n"
                for p in self.extracted
                if p.startswith(remote_dir + "/")
            )
        if command.startswith("rm -rf") and "unzip" not in command:
            self.extracted.clear()
        return ""

    def pull(self, device_path, local_path, **kwargs):
        Path(local_path).write_bytes(self.extracted[device_path])


def test_fetch_meta_extracts_on_device_and_caches(tmp_path):
    apk = tmp_path / "kodi.apk"
    with zipfile.ZipFile(apk, "w") as zf:
        zf.writestr("AndroidManifest.xml", manifest())
        zf.writestr("resources.arsc", arsc())
        zf.writestr("res/mipmap-xxxhdpi/ic.png", b"PNGDATA")
        zf.writestr("res/drawable-xhdpi/banner.webp", b"WEBPDATA")
        zf.writestr("classes.dex", b"x" * 1000)
    conn = ApkConnection(apk)
    cache = tmp_path / "cache"
    meta = appinfo.fetch_meta(conn, "org.xbmc.kodi", 21, cache)
    assert meta.label == "Kodi"
    assert (cache / meta.icon).read_bytes() == b"PNGDATA"
    assert (cache / meta.banner).read_bytes() == b"WEBPDATA"
    assert not any("classes.dex" in c for c in conn.commands)  # only what's needed is read
    assert conn.commands[-1].startswith("rm -rf")  # cleans up on the device

    store = MetaCache(cache)
    store.put(meta)
    assert store.get("org.xbmc.kodi", 21) == meta
    assert store.get("org.xbmc.kodi", 22) is None  # a new version is re-read
    assert store.image_path("../escape") is None
