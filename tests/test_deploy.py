import zipfile

import pytest

from shield_manager import deploy
from shield_manager.apk import native_abis
from tests.fakes import FakeConnection, make_apk

PKG = "com.example.tv"


def test_installed_version():
    conn = FakeConnection(installed={PKG: (41, "1.4.1 beta")})
    assert deploy.installed_version(conn, PKG) == deploy.InstalledVersion(41, "1.4.1 beta")


def test_installed_version_missing():
    assert deploy.installed_version(FakeConnection(), PKG) is None


def test_install_pushes_installs_cleans_up_and_verifies(tmp_path):
    conn = FakeConnection()
    apk = make_apk(tmp_path / "app.apk", PKG, 42)
    assert deploy.install(conn, apk, PKG, 42) == deploy.InstalledVersion(42, "")
    assert conn.pushed == [(str(apk), f"/data/local/tmp/{PKG}.0.apk")]
    assert f"pm install -r /data/local/tmp/{PKG}.0.apk" in conn.commands
    assert conn.commands[-2] == f"rm -f /data/local/tmp/{PKG}.0.apk"


def test_install_split_apk_uses_a_session(tmp_path):
    conn = FakeConnection()
    base = make_apk(tmp_path / "base.apk", PKG, 42)
    split = tmp_path / "split_config.xhdpi.apk"
    split.write_text("x")
    deploy.install(conn, [base, split], PKG, 42)
    assert conn.commands[0].startswith("pm install-create -r -S ")
    assert any(c.startswith("pm install-write ") and "split_config" in c for c in conn.commands)
    assert "pm install-commit 7" in conn.commands
    assert conn.installed[PKG] == (42, "")


def test_install_allow_downgrade_passes_flag(tmp_path):
    conn = FakeConnection(installed={PKG: (50, "")})
    deploy.install(conn, make_apk(tmp_path / "a.apk", PKG, 42), PKG, 42, allow_downgrade=True)
    assert any(c.startswith("pm install -r -d ") for c in conn.commands)


def test_install_failure_reports_pm_output_and_still_cleans_up():
    conn = FakeConnection(responses={"pm install": "Failure [INSTALL_FAILED_VERSION_DOWNGRADE]"})
    with pytest.raises(deploy.DeployError, match="VERSION_DOWNGRADE"):
        deploy.install(conn, "app.apk", PKG, 42)
    assert conn.commands[-1].startswith("rm -f ")


def test_install_detects_version_mismatch():
    conn = FakeConnection(responses={"pm install": "Success"}, installed={PKG: (40, "")})
    with pytest.raises(deploy.DeployError, match="versionCode 40"):
        deploy.install(conn, "app.apk", PKG, 42)


def test_uninstall():
    conn = FakeConnection(installed={PKG: (1, "")})
    deploy.uninstall(conn, PKG)
    assert conn.commands == [f"pm uninstall {PKG}"]


def test_uninstall_failure():
    with pytest.raises(deploy.DeployError):
        deploy.uninstall(FakeConnection(), PKG)


def test_list_packages_third_party_by_default():
    conn = FakeConnection(responses={"pm list packages": "package:b.app\npackage:a.app\n"})
    assert deploy.list_packages(conn) == ["a.app", "b.app"]
    assert conn.commands == ["pm list packages -3"]


def test_package_versions():
    conn = FakeConnection(installed={"a.app": (3, ""), "b.app": (10, "")})
    assert deploy.package_versions(conn) == {"a.app": 3, "b.app": 10}


def test_split_file_names_never_reach_the_shell_unquoted(tmp_path):
    # A bundle or download names its own split files, so a name must not run commands.
    conn = FakeConnection()
    base = make_apk(tmp_path / "base.apk", PKG, 42)
    split = tmp_path / "split_config.en;reboot;$(id) `id`.apk"
    split.write_text("x")
    deploy.install(conn, [base, split], PKG, 42)
    writes = [c for c in conn.commands if c.startswith("pm install-write ")]
    assert writes[1].split()[5] == "1_split_config.en_reboot___id___id_.apk"
    assert not any(ch in c for c in writes for ch in ";$`")


def test_pull_app_copies_base_and_splits(tmp_path):
    conn = FakeConnection(installed={PKG: (42, "")}, splits={PKG: ["split_config.en.apk"]})
    paths = deploy.pull_app(conn, PKG, tmp_path)
    assert [p.name for p in paths] == ["base.apk", "split_config.en.apk"]


def test_pull_app_missing_package(tmp_path):
    with pytest.raises(deploy.DeployError, match="not installed"):
        deploy.pull_app(FakeConnection(), PKG, tmp_path)


def _phases(events):
    return [(e.phase.value, e.percent) for e in events]


def test_install_reports_progress(tmp_path):
    events = []
    apk = make_apk(tmp_path / "app.apk", PKG, 42)
    deploy.install(FakeConnection(), apk, PKG, 42, progress=events.append)
    assert _phases(events) == [
        ("copying", 0),
        ("copying", 47),  # the fake sends the 17-byte file in two chunks
        ("copying", 100),
        ("installing", None),
        ("done", None),
    ]
    assert events[1].describe() == f"Copying {PKG} - 47%"
    assert events[3].describe() == f"Installing {PKG}"


def test_pull_reports_download_progress_across_splits(tmp_path):
    events = []
    conn = FakeConnection(installed={PKG: (42, "")}, splits={PKG: ["split_config.en.apk"]})
    deploy.pull_app(conn, PKG, tmp_path, progress=events.append)
    assert [e.phase.value for e in events] == ["downloading"] * len(events)
    assert events[0].percent == 0 and events[-1].percent == 100
    assert events[-1].done == events[-1].total > 0


def test_uninstall_reports_progress():
    events = []
    deploy.uninstall(FakeConnection(installed={PKG: (1, "")}), PKG, progress=events.append)
    assert _phases(events) == [("removing", None), ("done", None)]


def test_progress_describe_with_message():
    event = deploy.Progress(PKG, deploy.Phase.FAILED, message="no space")
    assert event.describe() == f"Failed {PKG}: no space"


def test_progress_describe_names_the_shields():
    def text(phase, **kw):
        return deploy.Progress(PKG, phase, **kw).describe()

    dl = deploy.Phase.DOWNLOADING
    assert text(dl, done=40, total=100, source="lounge", device="den") == (
        f"Downloading {PKG} from lounge - 40%"
    )
    assert text(dl, device="lounge") == f"Downloading {PKG} from lounge"
    assert (
        text(deploy.Phase.COPYING, done=1, total=2, device="den") == f"Copying {PKG} to den - 50%"
    )
    assert text(deploy.Phase.INSTALLING, device="den") == f"Installing {PKG} on den"
    assert text(deploy.Phase.REMOVING, device="den") == f"Removing {PKG} from den"


class SlowShield(FakeConnection):
    """A Shield whose package manager and file transfers go quiet for longer than the
    connection's default 9-second socket timeout, as a real install often does."""

    QUIET_S = 30

    def _check(self, kwargs):
        timeout = kwargs.get("transport_timeout_s") or 9.0
        if timeout < self.QUIET_S:
            raise TimeoutError(f"Reading from 10.10.20.11:5555 timed out ({timeout} seconds)")

    def shell(self, command, **kwargs):
        if command.startswith(("pm install", "pm install-commit")):
            self._check(kwargs)
        return super().shell(command, **kwargs)

    def push(self, local_path, device_path, **kwargs):
        self._check(kwargs)
        return super().push(local_path, device_path, **kwargs)

    def pull(self, device_path, local_path, **kwargs):
        self._check(kwargs)
        return super().pull(device_path, local_path, **kwargs)


def test_slow_install_does_not_hit_the_socket_timeout(tmp_path):
    conn = SlowShield()
    apk = make_apk(tmp_path / "app.apk", PKG, 42)
    assert deploy.install(conn, apk, PKG, 42).version_code == 42


def test_slow_split_install_and_pull_do_not_hit_the_socket_timeout(tmp_path):
    source = SlowShield(installed={PKG: (42, "")}, splits={PKG: ["split_config.en.apk"]})
    paths = deploy.pull_app(source, PKG, tmp_path / "pulled")
    target = SlowShield()
    deploy.install(target, paths, PKG, 42)
    assert target.installed[PKG] == (42, "")


def _native_apk(path, *abis):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("AndroidManifest.xml", b"")
        for abi in abis:
            zf.writestr(f"lib/{abi}/libapp.so", b"\x7fELF")
    return path


def test_native_abis_reads_base_and_splits(tmp_path):
    base = _native_apk(tmp_path / "base.apk")
    split = _native_apk(tmp_path / "split_config.arm64_v8a.apk", "arm64-v8a")
    assert native_abis([base]) == set()
    assert native_abis([base, split]) == {"arm64-v8a"}


def test_install_refuses_an_app_built_for_another_cpu_before_copying(tmp_path):
    conn = FakeConnection(responses={"getprop ro.product.cpu.abilist": "armeabi-v7a,armeabi\n"})
    apk = _native_apk(tmp_path / "app.apk", "arm64-v8a")
    with pytest.raises(
        deploy.IncompatibleAppError, match="built for arm64-v8a.*only runs armeabi-v7a"
    ):
        deploy.install(conn, apk, PKG, 42)
    assert not conn.pushed


def test_install_accepts_an_app_the_cpu_runs(tmp_path):
    conn = FakeConnection(
        responses={
            "getprop ro.product.cpu.abilist": "arm64-v8a,armeabi-v7a,armeabi",
            "pm install": "Success",
        },
        installed={PKG: (42, "1.0")},
    )
    apk = _native_apk(tmp_path / "app.apk", "armeabi-v7a", "x86")
    deploy.install(conn, apk, PKG, 42)


def test_install_explains_a_no_matching_abis_failure(tmp_path):
    conn = FakeConnection(
        responses={
            "getprop ro.product.cpu.abilist": "",
            "pm install": "Failure [INSTALL_FAILED_NO_MATCHING_ABIS: Failed to extract native "
            "libraries, res=-113]",
        }
    )
    apk = _native_apk(tmp_path / "app.apk", "arm64-v8a")
    with pytest.raises(deploy.IncompatibleAppError, match="Play Store.*NO_MATCHING_ABIS"):
        deploy.install(conn, apk, PKG, 42)
