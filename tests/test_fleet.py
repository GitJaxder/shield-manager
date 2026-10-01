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
