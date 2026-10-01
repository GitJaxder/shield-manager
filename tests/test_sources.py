import json

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


def test_version_key_orders_by_numbers():
    assert sources.version_key("1.10") > sources.version_key("1.9")
    assert sources.version_key("v2.0-tv") > sources.version_key("1.99.9")
    assert sources.version_key("beta") == ()


def test_latest_reads_the_newest_github_release():
    http = FakeHttp(
        {
            RELEASES: json.dumps(
                [{"tag_name": "v3.0-beta", "prerelease": True}, {"tag_name": "v2.1"}]
            ).encode()
        }
    )
    assert Downloader(http, dict(sources.GITHUB_APPS)).latest(PKG, ARM32) == ("2.1", "GitHub")
    assert Downloader(http).latest(PKG, ARM32) is None  # repository not known
    assert Downloader(FakeHttp(), dict(sources.GITHUB_APPS)).latest(PKG, ARM32) is None


def test_download_page_follows_morphes_redirect():
    lookup = sources.MORPHE_SEARCH_URL + "org.xbmc.kodi~21.1~armeabi-v7a"
    page = "https://www.apkmirror.com/apk/xbmc/kodi/kodi-21-1-release/"
    http = FakeHttp(redirects={lookup: page})
    assert Downloader(http).download_page("org.xbmc.kodi", "21.1", "armeabi-v7a") == page
    newest = sources.MORPHE_SEARCH_URL + "org.xbmc.kodi~any~arm64-v8a"
    http = FakeHttp(redirects={newest: page})
    assert Downloader(http).download_page("org.xbmc.kodi", None, "arm64-v8a") == page


def test_download_page_falls_back_to_a_web_search():
    pkg, abi = "org.xbmc.kodi", "armeabi-v7a"
    search = sources.web_search_url(pkg, "21.1", abi)
    assert search.startswith("https://www.google.com/search?q=")
    assert "site%3Aapkmirror.com" in search and "%2221.1%22" in search
    # Morphe unreachable
    assert Downloader(FakeHttp()).download_page(pkg, "21.1", abi) == search
    # Morphe answered without redirecting: it found nothing
    lookup = sources.morphe_url(pkg, "21.1", abi)
    http = FakeHttp(redirects={lookup: lookup})
    assert Downloader(http).download_page(pkg, "21.1", abi) == search


def test_github_newer_takes_the_newest_release(tmp_path):
    http = _github(tmp_path, [_release(tmp_path, "2.0", 200, "armeabi-v7a")])
    d = Downloader(http, dict(sources.GITHUB_APPS))
    [path] = d.github(Wanted(PKG, 101, "2.0", ARM32, newer=True), tmp_path / "out")
    assert path.name == "SmartTube_2.0_armeabi-v7a.apk"
    with pytest.raises(SourceUnavailable, match="isn't newer"):
        d.github(Wanted(PKG, 201, "2.0", ARM32, newer=True), tmp_path / "out2")


def test_page_site_names_the_download_site():
    assert sources.page_site("https://www.apkmirror.com/apk/x/") == "APKMirror"
    assert sources.page_site("https://smarttube.en.uptodown.com/android/download/1-x") == "Uptodown"
    assert sources.page_site(sources.web_search_url("a.b", None, "x86")) is None
