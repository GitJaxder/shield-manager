import json
import zipfile

import pytest

from shield_manager import sources
from shield_manager.sources import Downloader, SourceUnavailable, Wanted
from tests.fakes import FakeHttp, real_apk

PKG = "com.teamsmart.videomanager.tv"
ARM32 = ["armeabi-v7a", "armeabi"]
RELEASES = "https://api.github.com/repos/yuliskov/SmartTube/releases?per_page=10"


def test_asset_abi():
    assert sources.asset_abi("SmartTube_stable_29.10_arm64-v8a.apk") == "arm64-v8a"
    assert sources.asset_abi("app-x86_64-release.apk") == "x86_64"
    assert sources.asset_abi("app-x86-release.apk") == "x86"
    assert sources.asset_abi("SmartTube_stable_29.10_armeabi-v7a.apk") == "armeabi-v7a"
    assert sources.asset_abi("app-full-release.apk") is None


def test_rank_assets_prefers_own_cpu_type_then_universal():
    assets = [
        {"name": n}
        for n in ("a_universal.apk", "a_arm64-v8a.apk", "a_armeabi-v7a.apk", "notes.txt")
    ]
    assert [a["name"] for a in sources.rank_assets(assets, ARM32)] == [
        "a_armeabi-v7a.apk",
        "a_universal.apk",
    ]


def _release(tmp_path, tag, code, *abis):
    assets = []
    for abi in abis:
        name = f"SmartTube_{tag}_{abi}.apk"
        url = f"https://github.com/dl/{name}"
        real_apk(tmp_path / name, PKG, code, tag, abis=[abi], cert=b"dev")
        assets.append({"name": name, "browser_download_url": url, "path": tmp_path / name})
    return {"tag_name": tag, "draft": False, "assets": assets}


def _github(tmp_path, releases):
    pages = {
        RELEASES: json.dumps(
            [
                {**r, "assets": [{k: v for k, v in a.items() if k != "path"} for a in r["assets"]]}
                for r in releases
            ]
        ).encode()
    }
    for r in releases:
        for a in r["assets"]:
            pages[a["browser_download_url"]] = a["path"]
    return FakeHttp(pages)


def test_github_finds_the_wanted_version_for_the_cpu_type(tmp_path):
    http = _github(
        tmp_path,
        [
            _release(tmp_path, "2.0", 200, "arm64-v8a", "armeabi-v7a"),
            _release(tmp_path, "1.0", 100, "arm64-v8a", "armeabi-v7a"),
        ],
    )
    d = Downloader(http, dict(sources.GITHUB_APPS))
    [path] = d.github(Wanted(PKG, 100, "1.0", ARM32), tmp_path / "out")
    assert path.name == "SmartTube_1.0_armeabi-v7a.apk"
    downloaded = [u for u, _ in http.requests if u.endswith(".apk")]
    assert downloaded == [
        "https://github.com/dl/SmartTube_2.0_armeabi-v7a.apk",  # newer; moved on
        "https://github.com/dl/SmartTube_1.0_armeabi-v7a.apk",
    ]


def test_github_gives_up_once_releases_are_older(tmp_path):
    http = _github(tmp_path, [_release(tmp_path, "1.0", 100, "armeabi-v7a")])
    d = Downloader(http, dict(sources.GITHUB_APPS))
    with pytest.raises(SourceUnavailable, match="version 150"):
        d.github(Wanted(PKG, 150, "1.5", ARM32), tmp_path / "out")


def test_github_without_a_known_repository(tmp_path):
    with pytest.raises(SourceUnavailable, match="no GitHub repository"):
        Downloader(FakeHttp()).github(Wanted("org.other", 1, "1", ARM32), tmp_path)


def test_from_config_adds_repositories(tmp_path):
    (tmp_path / "app-sources.json").write_text('{"github": {"org.other": "me/other"}}')
    d = Downloader.from_config(tmp_path)
    assert d.github_repo("org.other") == "me/other"
    assert d.github_repo(PKG) == "yuliskov/SmartTube"


def _apkpure_body(*entries):
    """Shaped like APKPure's binary app_version response: version name, then a link."""
    body = b"\x0a\x12"
    for version, kind, url in entries:
        body += (
            b"\x1a\x05"
            + version.encode()
            + b":\x08junk\x10"
            + kind
            + b"\x12\x40"
            + url.encode()
            + b"\x00\x01"
        )
    return body


def test_apkpure_link_picks_the_version():
    body = _apkpure_body(
        ("2.0", b"APKJ", "https://download.pureapk.com/b/APK/two?x=1"),
        ("1.0", b"XAPKJ", "https://download.pureapk.com/b/XAPK/one?x=1"),
    )
    assert sources.apkpure_link(body, "1.0") == (
        "XAPK",
        "https://download.pureapk.com/b/XAPK/one?x=1",
    )
    assert sources.apkpure_link(body, "2.0") == (
        "APK",
        "https://download.pureapk.com/b/APK/two?x=1",
    )
    assert sources.apkpure_link(body, "3.0") is None


def test_apkpure_downloads_and_unpacks_an_xapk(tmp_path):
    base = real_apk(tmp_path / f"{PKG}.apk", PKG, 100, "1.0", cert=b"dev")
    arm = real_apk(tmp_path / "config.armeabi_v7a.apk", PKG, 100, "1.0", abis=["armeabi-v7a"])
    arm64 = real_apk(tmp_path / "config.arm64_v8a.apk", PKG, 100, "1.0", abis=["arm64-v8a"])
    xapk = tmp_path / "bundle.xapk"
    with zipfile.ZipFile(xapk, "w") as zf:
        for p in (base, arm, arm64):
            zf.write(p, p.name)
        zf.writestr("manifest.json", "{}")
        zf.writestr("Android/obb/x.obb", b"big")
    url = "https://download.pureapk.com/b/XAPK/one?x=1"
    http = FakeHttp(
        {sources.APKPURE_VERSIONS_URL + PKG: _apkpure_body(("1.0", b"XAPKJ", url)), url: xapk}
    )
    paths = Downloader(http).apkpure(Wanted(PKG, 100, "1.0", ARM32), tmp_path / "out")
    assert [p.name for p in paths] == [f"{PKG}.apk", "config.armeabi_v7a.apk"]
    assert http.requests[0][1]["x-abis"] == "armeabi-v7a,armeabi"


def test_apkpure_rejects_a_different_version(tmp_path):
    apk = real_apk(tmp_path / "a.apk", PKG, 101, "1.0")
    url = "https://download.pureapk.com/b/APK/one?x=1"
    http = FakeHttp(
        {sources.APKPURE_VERSIONS_URL + PKG: _apkpure_body(("1.0", b"APKJ", url)), url: apk}
    )
    with pytest.raises(SourceUnavailable, match="version 101, not 100"):
        Downloader(http).apkpure(Wanted(PKG, 100, "1.0", ARM32), tmp_path / "out")


def test_apkpure_unreachable(tmp_path):
    with pytest.raises(SourceUnavailable, match="couldn't reach APKPure"):
        Downloader(FakeHttp()).apkpure(Wanted(PKG, 100, "1.0", ARM32), tmp_path)
