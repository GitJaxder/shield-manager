"""Find apps online when no Shield has a copy another Shield can run.

Shields of different models run different CPU types, and the Play Store gives each one
only the native code for its own. When no Shield of the right model has an app:

- GitHub releases are downloaded automatically, for open-source apps whose repository is
  known (GITHUB_APPS, plus any listed in <config>/app-sources.json). Callers must check
  that a download is signed by the same developer as the copy already on a Shield
  (apk.signers) before installing it; fleet.Fetcher does.
- For every other app, download_page() gives a link to the page for the right build, the
  way Morphe Manager does: Morphe's server redirects package~version~CPU type to that
  build's page on APKMirror (or Uptodown, APKPure, APKCombo), and a web search limited to
  those sites stands in when it can't. Someone downloads the file there by hand.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from shield_manager import bundle
from shield_manager.apk import ApkError, read_apk_info

# Open-source apps people put on Shields, by package name.
GITHUB_APPS = {
    "com.teamsmart.videomanager.tv": "yuliskov/SmartTube",
    "com.liskovsoft.smarttubetv.beta": "yuliskov/SmartTube",
    "io.homeassistant.companion.android": "home-assistant/android",
}
# File name patterns for built-in repos that publish several apps or variants.
GITHUB_ASSETS = {
    "com.teamsmart.videomanager.tv": "stable",
    "com.liskovsoft.smarttubetv.beta": "beta",
}

# Morphe Manager's lookup: GET <url><package>~<version or "any">~<CPU type> redirects to the
# download page for that build.
MORPHE_SEARCH_URL = "https://api.morphe.software/v2/web-search/"
DOWNLOAD_SITES = (
    "(site:apkmirror.com OR site:uptodown.com OR site:apkpure.com OR site:apkcombo.com)"
)
PAGE_TIMEOUT_S = 8

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
    # True when updating: any versionCode >= version_code from the newest release will do.
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

    def final_url(self, url: str) -> str:
        """Where url ends up after its redirects."""
        req = self._request(url, None)
        req.method = "HEAD"
        with urllib.request.urlopen(req, timeout=PAGE_TIMEOUT_S) as res:
            return res.geturl()

    @staticmethod
    def _request(url: str, headers: dict[str, str] | None) -> urllib.request.Request:
        return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})


@dataclass(frozen=True)
class GitHubSource:
    """Where an app's releases are published on GitHub, like an Obtainium source."""

    repo: str  # "owner/name"
    # A regular expression release file names must match (case-insensitive), for repos
    # that publish several apps or variants; None takes every .apk.
    asset: str | None = None
    builtin: bool = False  # one of GITHUB_APPS rather than one you set


SOURCES_FILE = "app-sources.json"
_REPO = re.compile(r"^(?:https?://)?(?:www\.)?(?:github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def parse_repo(text: str) -> str:
    """ "owner/name" from "owner/name" or a github.com URL; ValueError otherwise."""
    text = text.strip()
    match = _REPO.match(text) or _REPO.match("/".join(text.split("/")[:5]))
    if not match:
        raise ValueError(f"not a GitHub repository: {text!r} (use owner/name or its URL)")
    return f"{match.group(1)}/{match.group(2)}"


@dataclass
class Downloader:
    http: Http = field(default_factory=Http)
    # package -> "owner/name" or a GitHubSource
    github_apps: dict[str, str | GitHubSource] = field(default_factory=dict)
    # Where set_github_source() saves; None keeps changes in memory only.
    config_dir: Path | None = None

    @classmethod
    def from_config(cls, config_dir: Path) -> Downloader:
        """Built-in GitHub apps plus the sources in <config>/app-sources.json, e.g.
        {"github": {"org.example.app": "owner/name",
                    "org.other.app": {"repo": "owner/name", "asset": "tv.*\\.apk"},
                    "com.teamsmart.videomanager.tv": null}}  (null hides a built-in)."""
        apps: dict[str, str | GitHubSource] = {
            pkg: GitHubSource(repo, GITHUB_ASSETS.get(pkg), builtin=True)
            for pkg, repo in GITHUB_APPS.items()
        }
        for pkg, value in _read_sources(config_dir).items():
            if value is None:
                apps.pop(pkg, None)
            elif isinstance(value, str):
                apps[pkg] = GitHubSource(value)
            else:
                apps[pkg] = GitHubSource(value["repo"], value.get("asset"))
        return cls(github_apps=apps, config_dir=config_dir)

    def github_source(self, package: str) -> GitHubSource | None:
        source = self.github_apps.get(package)
        return GitHubSource(source) if isinstance(source, str) else source

    def github_repo(self, package: str) -> str | None:
        source = self.github_source(package)
        return source.repo if source else None

    def github_sources(self) -> dict[str, GitHubSource]:
        """Every app with a GitHub source, by package name."""
        return {pkg: self.github_source(pkg) for pkg in sorted(self.github_apps)}

    def set_github_source(self, package: str, repo: str, asset: str | None = None) -> GitHubSource:
        """Use repo's releases (owner/name or URL) for package, optionally only files whose
        names match asset, and save it. Raises ValueError for a bad repo or pattern."""
        if asset:
            try:
                re.compile(asset)
            except re.error as e:
                raise ValueError(f"not a valid file name pattern: {asset!r} ({e})") from e
        source = GitHubSource(parse_repo(repo), asset or None)
        self.github_apps[package] = source
        self._save(package, {"repo": source.repo, "asset": source.asset} if asset else source.repo)
        return source

    def remove_github_source(self, package: str) -> bool:
        """Stop using GitHub for package (built-in sources too). False if it had none."""
        if package not in self.github_apps:
            return False
        del self.github_apps[package]
        self._save(package, None if package in GITHUB_APPS else _DELETE)
        return True

    def _save(self, package: str, value: object) -> None:
        if self.config_dir is None:
            return
        path = self.config_dir / SOURCES_FILE
        data = json.loads(path.read_text()) if path.exists() else {}
        github = data.setdefault("github", {})
        if value is _DELETE:
            github.pop(package, None)
        else:
            github[package] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp.replace(path)

    def github(
        self, wanted: Wanted, dest: Path, progress: ProgressBytes | None = None
    ) -> list[Path]:
        source = self.github_source(wanted.package)
        if not source:
            raise SourceUnavailable("no GitHub repository known for it")
        repo = source.repo
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
            assets = release.get("assets", [])
            if source.asset:
                pattern = re.compile(source.asset, re.IGNORECASE)
                assets = [a for a in assets if pattern.search(a.get("name", ""))]
            for asset in rank_assets(assets, wanted.abis):
                if tried == GITHUB_MAX_DOWNLOADS:
                    break
                tried += 1
                path = dest / Path(asset["name"]).name
                try:
                    self.http.save(asset["browser_download_url"], path, progress)
                    paths = (
                        bundle.unpack(path, dest / f"{path.stem}-apks")
                        if bundle.is_bundle(path)
                        else [path]
                    )
                    info = read_apk_info(paths[0])
                except (OSError, ApkError):
                    continue
                if info.package != wanted.package:
                    continue
                if wanted.newer:
                    if info.version_code >= wanted.version_code:
                        return paths
                    raise SourceUnavailable("its newest release isn't newer than yours")
                if info.version_code < wanted.version_code:
                    # Releases are newest first, so older ones won't have it either.
                    raise SourceUnavailable(
                        f"no release of {repo} has version {wanted.version_code}"
                    )
                if info.version_code == wanted.version_code:
                    return paths
                break  # a newer release; try the next one
        raise SourceUnavailable(f"no recent release of {repo} has version {wanted.version_code}")

    def latest(self, package: str, abis: list[str]) -> tuple[str, str] | None:
        """The newest version name available for an app and where: (name, "GitHub"), or
        None when its GitHub repository isn't known or has no release. abis is unused for
        now; it's kept so other sources can be added."""
        repo = self.github_repo(package)
        if not repo:
            return None
        try:
            releases = json.loads(
                self.http.get(
                    f"https://api.github.com/repos/{repo}/releases?per_page=10",
                    {"Accept": "application/vnd.github+json"},
                )
            )
        except Exception:
            return None
        tags = [
            r["tag_name"].removeprefix("v")
            for r in releases
            if not r.get("draft") and not r.get("prerelease") and r.get("tag_name")
        ]
        return (tags[0], "GitHub") if tags else None

    def download_page(self, package: str, version_name: str | None, abi: str) -> str:
        """A link to the download page for this build of an app (the newest when
        version_name is None), for a Shield whose main CPU type is abi."""
        lookup = morphe_url(package, version_name, abi)
        try:
            page = self.http.final_url(lookup)
        except Exception:
            return web_search_url(package, version_name, abi)
        host = urllib.parse.urlsplit(page).hostname or ""
        if host.endswith("morphe.software"):  # no redirect: it found nothing
            return web_search_url(package, version_name, abi)
        return page


# Download sites Morphe's server sends people to, by host.
PAGE_SITES = {
    "apkmirror.com": "APKMirror",
    "uptodown.com": "Uptodown",
    "apkpure.com": "APKPure",
    "apkcombo.com": "APKCombo",
}


def page_site(url: str) -> str | None:
    """The download site a download_page() link opens ("APKMirror", ...), or None for a
    web search."""
    host = urllib.parse.urlsplit(url).hostname or ""
    return next(
        (name for h, name in PAGE_SITES.items() if host == h or host.endswith("." + h)), None
    )


def morphe_url(package: str, version_name: str | None, abi: str) -> str:
    return MORPHE_SEARCH_URL + urllib.parse.quote(f"{package}~{version_name or 'any'}~{abi}")


def web_search_url(package: str, version_name: str | None, abi: str) -> str:
    """A web search for the app's build on the download sites Morphe uses."""
    version = f' "{version_name}"' if version_name else ""
    query = f'"{package}"{version} {abi} {DOWNLOAD_SITES}'
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)


_DELETE = object()


def _read_sources(config_dir: Path) -> dict:
    path = config_dir / SOURCES_FILE
    return json.loads(path.read_text()).get("github", {}) if path.exists() else {}


def version_key(name: str) -> tuple[int, ...]:
    """Sort key for version names: their numbers in order, so "1.10" > "1.9"."""
    return tuple(int(n) for n in re.findall(r"\d+", name))


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
        if a.get("name", "").lower().endswith((".apk", *bundle.SUFFIXES[:-1]))
        and (asset_abi(a["name"]) is None or asset_abi(a["name"]) in abis)
    ]

    def key(asset: dict) -> int:
        abi = asset_abi(asset["name"])
        return len(abis) if abi is None else abis.index(abi)

    return sorted(usable, key=key)
