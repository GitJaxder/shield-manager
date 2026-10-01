"""Download apps from the internet when no Shield has a copy another Shield can run.

Shields of different models run different CPU types, and the Play Store gives each one
only the native code for its own. When no Shield of the right model has an app, these
sources can fetch a build for it:

- GitHub releases, for open-source apps whose repository is known (GITHUB_APPS, plus any
  listed in <config>/app-sources.json).
- APKPure, through the same unofficial API the open-source apkeep tool uses.

Callers must check that a download is signed by the same developer as the copy already
on a Shield (apk.signers) before installing it; fleet.Fetcher does.
"""

from __future__ import annotations

import json
import re
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from shield_manager.apk import ApkError, read_apk_info

# Open-source apps people put on Shields, by package name.
GITHUB_APPS = {
    "com.teamsmart.videomanager.tv": "yuliskov/SmartTube",
    "com.liskovsoft.smarttubetv.beta": "yuliskov/SmartTube",
    "io.homeassistant.companion.android": "home-assistant/android",
}

APKPURE_VERSIONS_URL = "https://api.pureapk.com/m/v3/cms/app_version?hl=en-US&package_name="
# Headers apkeep sends; x-abis asks for builds for these CPU types.
APKPURE_HEADERS = {"x-cv": "3172501", "x-sv": "29", "x-gp": "1"}
_APKPURE_LINK = rb"(X?APKJ)..(https?://[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b[-a-zA-Z0-9()@:%_+.~#?&/=]*)"

USER_AGENT = "shield-manager"
GITHUB_MAX_DOWNLOADS = 6  # APKs to try across recent releases before giving up

ProgressBytes = Callable[[int, int], None]  # (bytes done, total bytes or 0)


class SourceUnavailable(Exception):
    """This source has no usable copy of the app (not listed, wrong version, offline)."""


@dataclass(frozen=True)
class Wanted:
    package: str
    version_code: int
    version_name: str
    abis: list[str]  # the target Shield's CPU types, preferred first
    # True when updating: any versionCode >= version_code will do, from the newest
    # release (GitHub) or the listing for version_name (APKPure).
    newer: bool = False


class Http:
    """Plain HTTPS over urllib. Tests swap in a fake."""

    def get(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        with urllib.request.urlopen(self._request(url, headers), timeout=60) as res:
            return res.read()

    def save(self, url: str, dest: Path, progress: ProgressBytes | None = None) -> None:
        with urllib.request.urlopen(self._request(url, None), timeout=60) as res:
            total = int(res.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as out:
                while chunk := res.read(1 << 16):
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)

    @staticmethod
    def _request(url: str, headers: dict[str, str] | None) -> urllib.request.Request:
        return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})


@dataclass
class Downloader:
    http: Http = field(default_factory=Http)
    github_apps: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config_dir: Path) -> Downloader:
        """Built-in GitHub apps plus any in <config>/app-sources.json, e.g.
        {"github": {"org.example.app": "owner/repo"}}."""
        apps = dict(GITHUB_APPS)
        path = config_dir / "app-sources.json"
        if path.exists():
            apps.update(json.loads(path.read_text()).get("github", {}))
        return cls(github_apps=apps)

    def github_repo(self, package: str) -> str | None:
        return self.github_apps.get(package)

    def github(
        self, wanted: Wanted, dest: Path, progress: ProgressBytes | None = None
    ) -> list[Path]:
        repo = self.github_repo(wanted.package)
        if not repo:
            raise SourceUnavailable("no GitHub repository known for it")
        try:
            releases = json.loads(
                self.http.get(
                    f"https://api.github.com/repos/{repo}/releases?per_page=10",
                    {"Accept": "application/vnd.github+json"},
                )
            )
        except Exception as e:
            raise SourceUnavailable(f"couldn't list releases of {repo}: {e}") from e
        dest.mkdir(parents=True, exist_ok=True)
        tried = 0
        for release in releases:
            if release.get("draft") or (wanted.newer and release.get("prerelease")):
                continue
            for asset in rank_assets(release.get("assets", []), wanted.abis):
                if tried == GITHUB_MAX_DOWNLOADS:
                    break
                tried += 1
                path = dest / Path(asset["name"]).name
                try:
                    self.http.save(asset["browser_download_url"], path, progress)
                    info = read_apk_info(path)
                except (OSError, ApkError):
                    continue
                if info.package != wanted.package:
                    continue
                if wanted.newer:
                    if info.version_code >= wanted.version_code:
                        return [path]
                    raise SourceUnavailable("its newest release isn't newer than yours")
                if info.version_code < wanted.version_code:
                    # Releases are newest first, so older ones won't have it either.
                    raise SourceUnavailable(
                        f"no release of {repo} has version {wanted.version_code}"
                    )
                if info.version_code == wanted.version_code:
                    return [path]
                break  # a newer release; try the next one
        raise SourceUnavailable(f"no recent release of {repo} has version {wanted.version_code}")

    def apkpure(
        self, wanted: Wanted, dest: Path, progress: ProgressBytes | None = None
    ) -> list[Path]:
        if not wanted.version_name:
            raise SourceUnavailable("the app's version name is unknown")
        try:
            body = self.http.get(
                APKPURE_VERSIONS_URL + wanted.package,
                {**APKPURE_HEADERS, "x-abis": ",".join(wanted.abis)},
            )
        except Exception as e:
            raise SourceUnavailable(f"couldn't reach APKPure: {e}") from e
        match = apkpure_link(body, wanted.version_name)
        if not match:
            raise SourceUnavailable(f"it doesn't list version {wanted.version_name}")
        kind, url = match
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{wanted.package}.{'xapk' if kind == 'XAPK' else 'apk'}"
        try:
            self.http.save(url, path, progress)
        except Exception as e:
            raise SourceUnavailable(f"download failed: {e}") from e
        paths = unpack_xapk(path, dest / "xapk", wanted.abis) if kind == "XAPK" else [path]
        codes = set()
        for p in paths:
            try:
                info = read_apk_info(p)
            except ApkError as e:
                raise SourceUnavailable(f"its download isn't a valid app: {e}") from e
            if info.package != wanted.package:
                raise SourceUnavailable(f"its download is a different app ({info.package})")
            codes.add(info.version_code)
        if wanted.newer and len(codes) == 1 and min(codes) >= wanted.version_code:
            return paths
        if wanted.newer or codes != {wanted.version_code}:
            found = ", ".join(str(c) for c in sorted(codes))
            raise SourceUnavailable(f"its download is version {found}, not {wanted.version_code}")
        return paths

    def latest(self, package: str, abis: list[str]) -> tuple[str, str] | None:
        """The newest version name available for an app and where: (name, "GitHub" or
        "APKPure"), or None if neither has it."""
        found = []
        repo = self.github_repo(package)
        if repo:
            try:
                releases = json.loads(
                    self.http.get(
                        f"https://api.github.com/repos/{repo}/releases?per_page=10",
                        {"Accept": "application/vnd.github+json"},
                    )
                )
                tags = [
                    r["tag_name"].removeprefix("v")
                    for r in releases
                    if not r.get("draft") and not r.get("prerelease") and r.get("tag_name")
                ]
                if tags:
                    found.append((tags[0], "GitHub"))
            except Exception:
                pass
        try:
            body = self.http.get(
                APKPURE_VERSIONS_URL + package, {**APKPURE_HEADERS, "x-abis": ",".join(abis)}
            )
            names = apkpure_versions(body)
            if names:
                found.append((max(names, key=version_key), "APKPure"))
        except Exception:
            pass
        if not found:
            return None
        return max(found, key=lambda f: version_key(f[0]))


def version_key(name: str) -> tuple[int, ...]:
    """Sort key for version names: their numbers in order, so "1.10" > "1.9"."""
    return tuple(int(n) for n in re.findall(r"\d+", name))


def apkpure_versions(body: bytes) -> list[str]:
    """Version names listed in APKPure's app_version response (as apkeep reads them)."""
    found = re.findall(rb"([A-Za-z0-9.-]+):\([0-9a-fA-F]{40,}", body)
    return list(dict.fromkeys(v.decode() for v in found))


def apkpure_link(body: bytes, version_name: str) -> tuple[str, str] | None:
    """Find the download link for version_name in APKPure's app_version response.

    Returns ("APK" or "XAPK", url). Like apkeep, this scans the binary response for the
    version name followed by the first link after it.
    """
    pattern = rb"[^0-9]" + re.escape(version_name.encode()) + rb":.+?" + _APKPURE_LINK
    match = re.search(pattern, body, re.DOTALL)
    if not match:
        return None
    return match.group(1).decode().removesuffix("J"), match.group(2).decode()


# Substrings of release asset names that say which CPU type a build is for. Order matters:
# "x86_64" before "x86", and "arm64" before the 32-bit ARM names.
_ASSET_ABIS = [
    ("arm64", "arm64-v8a"),
    ("aarch64", "arm64-v8a"),
    ("x86_64", "x86_64"),
    ("x86", "x86"),
    ("armeabi-v7a", "armeabi-v7a"),
    ("armv7", "armeabi-v7a"),
    ("arm32", "armeabi-v7a"),
    ("armeabi", "armeabi"),
]


def asset_abi(name: str) -> str | None:
    """The CPU type a release asset is built for, or None for a universal build."""
    lower = name.lower()
    return next((abi for token, abi in _ASSET_ABIS if token in lower), None)


def rank_assets(assets: list[dict], abis: list[str]) -> list[dict]:
    """APK assets this Shield can run: builds for its own CPU types (preferred first), then
    universal builds."""
    usable = [
        a
        for a in assets
        if a.get("name", "").lower().endswith(".apk")
        and (asset_abi(a["name"]) is None or asset_abi(a["name"]) in abis)
    ]

    def key(asset: dict) -> int:
        abi = asset_abi(asset["name"])
        return len(abis) if abi is None else abis.index(abi)

    return sorted(usable, key=key)


def unpack_xapk(path: Path, dest: Path, abis: list[str]) -> list[Path]:
    """Extract the APKs from an XAPK bundle, base first, leaving out CPU-type splits for
    CPU types this Shield doesn't run. Expansion files (OBB) aren't copied."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise SourceUnavailable(f"its download isn't a valid XAPK: {e}") from e
    dest.mkdir(parents=True, exist_ok=True)
    abi_splits = {f"config.{abi.replace('-', '_')}.apk" for abi in _ALL_ABIS}
    wanted_splits = {f"config.{abi.replace('-', '_')}.apk" for abi in abis}
    paths = []
    with zf:
        for name in zf.namelist():
            if "/" in name or not name.endswith(".apk"):
                continue
            if name in abi_splits and name not in wanted_splits:
                continue
            out = dest / name
            out.write_bytes(zf.read(name))
            paths.append(out)
    if not paths:
        raise SourceUnavailable("its XAPK has no APK files")
    return sorted(paths, key=lambda p: p.name.startswith("config."))


_ALL_ABIS = ["arm64-v8a", "armeabi-v7a", "armeabi", "x86", "x86_64"]
