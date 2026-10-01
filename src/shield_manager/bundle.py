"""APK bundles: an app's base APK and split APKs packed into one file.

Download sites offer apps that ship as split APKs as bundles: APKMirror's .apkm, the
.xapk APKPure and others use, and bundletool's .apks. All of them are zip files of APKs
(.xapk may add OBB expansion files, which aren't copied). A Shield only needs the base
plus the splits for its own CPU type; density and language splits are kept, since the
Shield's package manager picks what it uses.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from pathlib import Path

from shield_manager.apk import ApkError, ApkInfo, read_apk_info

SUFFIXES = (".apkm", ".xapk", ".apks", ".zip")

# CPU type in a split's file name, e.g. split_config.arm64_v8a.apk, base-armeabi_v7a.apk.
_SPLIT_ABI = re.compile(r"(?:^|[._-])(arm64_v8a|armeabi_v7a|armeabi|x86_64|x86)(?=[._-]|$)")
_BASE_NAMES = {"base.apk", "base-master.apk"}


class BundleError(ApkError):
    pass


def is_bundle(path: str | Path) -> bool:
    """True for a zip of APKs rather than an APK (which has an AndroidManifest.xml)."""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return False
    if "AndroidManifest.xml" in names:
        return False
    return path.suffix.lower() in SUFFIXES or any(n.lower().endswith(".apk") for n in names)


def unpack(path: str | Path, dest: Path) -> list[Path]:
    """Extract every APK in a bundle into dest, base first."""
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise BundleError(f"{Path(path).name} isn't a valid APK bundle: {e}") from e
    with zf:
        entries = [i for i in zf.infolist() if i.filename.lower().endswith(".apk")]
        # bundletool's .apks holds split APKs under splits/ and whole-app APKs for old
        # Android versions under standalones/; Shields use the splits.
        if any(i.filename.startswith("splits/") for i in entries):
            entries = [i for i in entries if i.filename.startswith("splits/")]
        if not entries:
            raise BundleError(f"{Path(path).name} has no APK files in it")
        if any(i.flag_bits & 0x1 for i in entries):
            raise BundleError(
                f"{Path(path).name} is encrypted (older APKMirror files only open in the "
                "APKMirror Installer app); download it again or pick another version"
            )
        dest.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, info in enumerate(entries):
            name = Path(info.filename).name
            out = dest / (name if name not in {p.name for p in paths} else f"{i}-{name}")
            out.write_bytes(zf.read(info))
            paths.append(out)
    return sorted(paths, key=lambda p: not _is_base(p.name))


def _is_base(name: str) -> bool:
    lower = name.lower()
    if lower in _BASE_NAMES:
        return True
    return not (lower.startswith(("split_", "config.", "base-")) or ".config." in lower)


def split_abi(name: str) -> str | None:
    """The CPU type a split APK's file name says it's for ("arm64-v8a"), or None."""
    match = _SPLIT_ABI.search(name.lower().removesuffix(".apk"))
    if not match:
        return None
    return {"arm64_v8a": "arm64-v8a", "armeabi_v7a": "armeabi-v7a"}.get(
        match.group(1), match.group(1)
    )


def pick(paths: list[Path], abis: list[str]) -> list[Path]:
    """The APKs a Shield running abis needs: everything but CPU-type splits for other CPU
    types. Raises BundleError when there are CPU-type splits but none for this Shield."""
    kept = [p for p in paths if p == paths[0] or split_abi(p.name) in (None, *abis)]
    split_abis = {split_abi(p.name) for p in paths[1:]} - {None}
    if split_abis and not split_abis & set(abis):
        raise BundleError(
            f"this bundle's native code is for {', '.join(sorted(split_abis))}, but this "
            f"Shield only runs {', '.join(abis) or 'other CPU types'}"
        )
    return kept


def describe_parts(paths: list[Path]) -> str:
    """Which parts of a split app paths are, base first: "base + armeabi-v7a + xhdpi"."""
    names = ["base"]
    for path in paths[1:]:
        label = split_abi(path.name)
        if not label:
            label = path.name.lower().removesuffix(".apk")
            for prefix in ("split_config.", "config.", "base-", "split_"):
                label = label.removeprefix(prefix)
        names.append(label)
    return " + ".join(names)


def expand(paths: list[Path], dest: Path) -> list[Path]:
    """paths with every bundle replaced by the APKs in it."""
    out = []
    for i, path in enumerate(paths):
        out += unpack(path, dest / str(i)) if is_bundle(path) else [path]
    return out


def app_info(path: str | Path) -> ApkInfo:
    """Package name and version of an APK or a bundle (read from its base APK)."""
    if not is_bundle(path):
        return read_apk_info(path)
    with tempfile.TemporaryDirectory() as tmp:
        return read_apk_info(unpack(path, Path(tmp))[0])
