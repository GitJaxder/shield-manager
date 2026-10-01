import pytest

from shield_manager import fleet
from shield_manager.fleet import Change, Drift
from shield_manager.registry import Device
from tests.fakes import FakeConnection

REF = Device("living-room", "10.0.0.1")
DEN = Device("den", "10.0.0.2")
ATTIC = Device("attic", "10.0.0.3")


def test_compare():
    reference = {"same": 1, "missing": 5, "old": 10, "new": 10}
    device = {"same": 1, "old": 8, "new": 12, "extra": 3}
    assert fleet.compare(reference, device) == [
        Drift("extra", Change.EXTRA, None, 3),
        Drift("missing", Change.INSTALL, 5, None),
        Drift("new", Change.NEWER, 10, 12),
        Drift("old", Change.UPDATE, 10, 8),
    ]


@pytest.fixture
def shields():
    return {
        REF.name: FakeConnection(
            installed={"kodi": (200, ""), "plex": (50, ""), "vlc": (30, "")},
            splits={"plex": ["split_config.xhdpi.apk"]},
        ),
        DEN.name: FakeConnection(
            installed={"kodi": (190, ""), "vlc": (31, ""), "old.game": (1, "")}
        ),
    }


@pytest.fixture
def connect(shields):
    def _connect(device):
        if device.name not in shields:
            raise ConnectionRefusedError("unreachable")
        return shields[device.name]

    return _connect


def test_status_reports_drift_and_skips_the_reference(connect):
    reports = fleet.status(REF, [REF, DEN], connect)
    assert [r.device for r in reports] == [DEN]
    assert {d.package: d.change for d in reports[0].drift} == {
        "kodi": Change.UPDATE,
        "plex": Change.INSTALL,
        "vlc": Change.NEWER,
        "old.game": Change.EXTRA,
    }


def test_status_unreachable_device_is_reported_not_raised(connect):
    reports = fleet.status(REF, [DEN, ATTIC], connect)
    assert reports[1].error == "unreachable"
    assert not reports[1].in_sync


def test_sync_installs_and_updates_but_keeps_newer_and_extra(connect, shields):
    [report] = fleet.sync(REF, [DEN], connect)
    den = shields[DEN.name].installed
    assert den["kodi"][0] == 200
    assert den["plex"][0] == 50
    assert den["vlc"][0] == 31  # newer than reference, left alone
    assert "old.game" in den  # not removed without prune
    assert report.applied == {"kodi": "updated to 200", "plex": "installed 50"}
    assert report.failed == {}
    # Split APKs were pulled from the reference and installed through a session.
    assert any("split_config" in c for c in shields[DEN.name].commands)


def test_sync_prune_and_allow_downgrade(connect, shields):
    [report] = fleet.sync(REF, [DEN], connect, prune=True, allow_downgrade=True)
    assert shields[DEN.name].installed.keys() == {"kodi", "plex", "vlc"}
    assert shields[DEN.name].installed["vlc"][0] == 30
    assert report.applied["old.game"] == "removed"
    assert report.applied["vlc"] == "downgraded to 30"


def test_sync_dry_run_changes_nothing(connect, shields):
    before = dict(shields[DEN.name].installed)
    [report] = fleet.sync(REF, [DEN], connect, dry_run=True)
    assert shields[DEN.name].installed == before
    assert len(report.drift) == 4


def test_sync_pulls_each_app_once_for_many_targets(connect, shields):
    shields["bedroom"] = FakeConnection()
    fleet.sync(REF, [DEN, Device("bedroom", "10.0.0.4")], connect)
    pulled = shields[REF.name].pulled
    assert len(pulled) == len(set(pulled))
    assert shields["bedroom"].installed.keys() == {"kodi", "plex", "vlc"}


def test_sync_records_per_app_failures(connect, shields):
    shields[DEN.name].responses["pm install-create"] = "Failure [INSUFFICIENT_STORAGE]"
    [report] = fleet.sync(REF, [DEN], connect)
    assert "INSUFFICIENT_STORAGE" in report.failed["plex"]
    assert report.applied["kodi"] == "updated to 200"


def test_sync_reports_progress_per_device(connect, shields):
    events = []
    fleet.sync(REF, [DEN], connect, progress=events.append)
    assert {e.device for e in events} == {"den"}
    plex = [e.phase.value for e in events if e.package == "plex"]
    assert plex[0] == "downloading"
    assert plex.index("copying") > plex.index("downloading")
    assert plex[-2:] == ["installing", "done"]
    downloads = [e for e in events if e.phase.value == "downloading"]
    assert {e.source for e in downloads} == {REF.name}
    assert downloads[0].describe().startswith(f"Downloading {downloads[0].package} from {REF.name}")


def test_sync_reports_failures_as_progress(connect, shields):
    shields[DEN.name].responses["pm install-create"] = "Failure [INSUFFICIENT_STORAGE]"
    events = []
    fleet.sync(REF, [DEN], connect, progress=events.append)
    [failed] = [e for e in events if e.phase.value == "failed"]
    assert failed.package == "plex" and "INSUFFICIENT_STORAGE" in failed.message


ARM64 = ["arm64-v8a", "armeabi-v7a", "armeabi"]
ARM32 = ["armeabi-v7a", "armeabi"]


def _mixed_fleet(attic_has_plex):
    shields = {
        REF.name: FakeConnection(installed={"plex": (50, "")}, abis=ARM64),
        DEN.name: FakeConnection(abis=ARM32),
        ATTIC.name: FakeConnection(
            installed={"plex": (50, "")} if attic_has_plex else {}, abis=ARM32
        ),
    }
    return shields, shields.__getitem__


def _by_name(device):
    return device.name


def test_sync_copies_from_a_shield_of_the_same_cpu_type():
    shields, get = _mixed_fleet(attic_has_plex=True)
    [den, _] = fleet.sync(REF, [DEN, ATTIC], lambda d: get(_by_name(d)))
    assert den.applied == {"plex": "installed 50 (copied from attic)"}
    assert shields[DEN.name].installed["plex"][0] == 50
    assert not shields[REF.name].pulled  # the reference's arm64 copy was never needed


def test_sync_reports_an_app_no_shield_has_a_compatible_copy_of():
    shields, get = _mixed_fleet(attic_has_plex=False)
    reports = fleet.sync(REF, [DEN, ATTIC], lambda d: get(_by_name(d)))
    for report in reports:
        error = report.failed["plex"]
        assert error.startswith(f"no copy {report.device.name} can run.")
        assert "1. Your Shields (this one runs" in error
        assert "2. GitHub: downloads are turned off" in error
        assert (
            f"3. Play Store: `shield-manager app store-page plex -d {report.device.name}`" in error
        )
    assert not shields[DEN.name].pushed  # refused before copying anything


def test_rank_sources_prefers_the_same_cpu_types():
    abis = {"ref": ARM64, "attic": ARM32, "odd": ["x86"]}
    assert fleet.rank_sources(["ref", "odd", "attic"], ARM32, abis) == ["attic", "ref", "odd"]
    assert fleet.rank_sources(["attic", "ref"], ARM64, abis) == ["ref", "attic"]


def _download_fleet(tmp_path, github_cert=b"dev"):
    """den (reference, 64-bit) has SmartTube 100 signed by "dev"; attic (32-bit) doesn't.
    GitHub has a 32-bit build of the same version, signed by github_cert."""
    import json

    from shield_manager import sources
    from tests.fakes import FakeHttp, real_apk

    pkg = "com.teamsmart.videomanager.tv"
    ref_copy = real_apk(tmp_path / "ref.apk", pkg, 100, "1.0", abis=["arm64-v8a"], cert=b"dev")
    shields = {
        REF.name: FakeConnection(
            installed={pkg: (100, "1.0")}, abis=ARM64, apk_files={pkg: ref_copy}
        ),
        ATTIC.name: FakeConnection(abis=ARM32),
    }
    asset = real_apk(
        tmp_path / "st_armeabi-v7a.apk", pkg, 100, "1.0", abis=["armeabi-v7a"], cert=github_cert
    )
    url = "https://github.com/dl/st_armeabi-v7a.apk"
    http = FakeHttp(
        {
            "https://api.github.com/repos/yuliskov/SmartTube/releases?per_page=10": json.dumps(
                [{"assets": [{"name": asset.name, "browser_download_url": url}]}]
            ).encode(),
            url: asset,
        }
    )
    return pkg, shields, sources.Downloader(http, dict(sources.GITHUB_APPS))


def test_sync_downloads_a_build_the_target_can_run(tmp_path):
    pkg, shields, downloads = _download_fleet(tmp_path)
    events = []
    [attic] = fleet.sync(
        REF, [ATTIC], lambda d: shields[d.name], downloads=downloads, progress=events.append
    )
    assert attic.applied == {pkg: "installed 100 (downloaded from GitHub)"}
    assert shields[ATTIC.name].installed[pkg][0] == 100
    assert any(e.describe().startswith(f"Downloading {pkg} from GitHub") for e in events)


def test_sync_refuses_a_download_signed_by_someone_else(tmp_path):
    from shield_manager import sources

    pkg, shields, downloads = _download_fleet(tmp_path, github_cert=b"impostor")
    [attic] = fleet.sync(REF, [ATTIC], lambda d: shields[d.name], downloads=downloads)
    error = attic.failed[pkg]
    page = attic.download_pages[pkg]  # Morphe isn't reachable here, so a web search
    assert page == sources.web_search_url(pkg, "1.0", "armeabi-v7a")
    steps = error.splitlines()[1:]
    assert [line.split(":")[0].strip() for line in steps] == [
        "1. Your Shields (this one runs armeabi-v7a, armeabi)",
        "2. GitHub",
        "3. APKMirror",
        "4. Google search",
        "5. Play Store",
    ]
    assert f"the copies on {REF.name} are built for a different CPU type" in steps[0]
    assert "its copy is signed by a different developer" in steps[1]
    assert steps[2].endswith("no download page found")
    assert f"look for it at {page}, then install the file with" in steps[3]
    assert "`shield-manager app install FILE -d attic`" in steps[3]
    assert f"`shield-manager app store-page {pkg} -d attic`" in steps[4]
    assert pkg not in shields[ATTIC.name].installed


def test_sync_links_the_apkmirror_page_when_morphe_finds_one(tmp_path):
    from shield_manager import sources

    pkg, shields, downloads = _download_fleet(tmp_path, github_cert=b"impostor")
    page = "https://www.apkmirror.com/apk/smarttube/smarttube-1-0-release/"
    downloads.http.redirects[sources.morphe_url(pkg, "1.0", "armeabi-v7a")] = page
    [attic] = fleet.sync(REF, [ATTIC], lambda d: shields[d.name], downloads=downloads)
    steps = attic.failed[pkg].splitlines()[1:]
    assert steps[2].strip().startswith(f"3. APKMirror: download it from {page}, then install")
    assert steps[3].strip().startswith("4. Play Store:")
    assert attic.download_pages[pkg] == page


def test_source_order_tries_every_shield_before_github():
    from shield_manager import sources

    d = sources.Downloader(github_apps={"pkg": "o/r"})
    abis = {"ref": ARM64, "attic": ARM32}
    assert fleet.source_order(["ref", "attic"], ARM32, abis, "pkg", d) == [
        "attic",
        "ref",
        "GitHub",
    ]
    assert fleet.source_order(["ref"], ARM32, abis, "pkg", None) == ["ref"]


def _update_fleet(tmp_path, cert=b"dev"):
    """den (64-bit) and attic (32-bit) both have Kodi 1.0 (100) signed by "dev"; its GitHub
    repository's newest release is 1.1, whose universal APK (110) is signed by cert."""
    import json

    from shield_manager import sources
    from tests.fakes import FakeHttp, real_apk

    pkg = "org.xbmc.kodi"
    den_copy = real_apk(tmp_path / "den.apk", pkg, 100, "1.0", ["arm64-v8a"], b"dev")
    attic_copy = real_apk(tmp_path / "attic.apk", pkg, 100, "1.0", ["armeabi-v7a"], b"dev")
    shields = {
        DEN.name: FakeConnection(
            installed={pkg: (100, "1.0")}, abis=ARM64, apk_files={pkg: den_copy}
        ),
        ATTIC.name: FakeConnection(
            installed={pkg: (100, "1.0")}, abis=ARM32, apk_files={pkg: attic_copy}
        ),
    }
    new = real_apk(tmp_path / "new.apk", pkg, 110, "1.1", ["arm64-v8a", "armeabi-v7a"], cert)
    url = "https://github.com/dl/kodi-1.1.apk"
    release = {
        "tag_name": "v1.1",
        "assets": [{"name": "kodi-1.1.apk", "browser_download_url": url}],
    }
    http = FakeHttp(
        {
            "https://api.github.com/repos/xbmc/xbmc/releases?per_page=10": json.dumps(
                [release]
            ).encode(),
            url: new,
        }
    )
    return pkg, shields, sources.Downloader(http, {pkg: "xbmc/xbmc"})


def test_check_updates_finds_a_newer_version(tmp_path):
    pkg, shields, downloads = _update_fleet(tmp_path)

    def connect(device):
        if device.name not in shields:
            raise ConnectionRefusedError("unreachable")
        return shields[device.name]

    updates, errors = fleet.check_updates(
        [DEN, ATTIC, Device("gone", "10.0.0.9")], connect, downloads
    )
    assert errors == {"gone": "unreachable"}
    [u] = updates
    assert (u.package, u.installed_name, u.latest_name, u.source) == (pkg, "1.0", "1.1", "GitHub")
    assert u.shields == ["den", "attic"]


def test_apply_updates_installs_on_every_shield(tmp_path):
    pkg, shields, downloads = _update_fleet(tmp_path)
    connect = shields.__getitem__
    updates, _ = fleet.check_updates([DEN, ATTIC], lambda d: connect(d.name), downloads)
    [u] = fleet.apply_updates(updates, [DEN, ATTIC], lambda d: connect(d.name), downloads)
    assert u.failed == {}
    assert u.applied == {
        "den": "updated to 1.1 (from GitHub)",
        "attic": "updated to 1.1 (from GitHub)",
    }
    assert shields[DEN.name].installed[pkg][0] == shields[ATTIC.name].installed[pkg][0] == 110


def test_apply_updates_refuses_a_download_signed_by_someone_else(tmp_path):
    pkg, shields, downloads = _update_fleet(tmp_path, cert=b"impostor")
    connect = shields.__getitem__
    updates, _ = fleet.check_updates([DEN, ATTIC], lambda d: connect(d.name), downloads)
    [u] = fleet.apply_updates(updates, [DEN, ATTIC], lambda d: connect(d.name), downloads)
    assert set(u.failed) == {"den", "attic"}
    assert "signed by a different developer" in u.failed["den"]
    assert shields[DEN.name].installed[pkg][0] == 100


def test_check_updates_skips_apps_that_are_current(tmp_path):
    pkg, shields, downloads = _update_fleet(tmp_path)
    for conn in shields.values():
        conn.installed[pkg] = (110, "1.1")
    updates, _ = fleet.check_updates([DEN, ATTIC], lambda d: shields[d.name], downloads)
    assert updates == []
