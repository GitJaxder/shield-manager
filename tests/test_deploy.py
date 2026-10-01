import pytest

from shield_manager import deploy
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


def test_pull_app_copies_base_and_splits(tmp_path):
    conn = FakeConnection(installed={PKG: (42, "")}, splits={PKG: ["split_config.en.apk"]})
    paths = deploy.pull_app(conn, PKG, tmp_path)
    assert [p.name for p in paths] == ["base.apk", "split_config.en.apk"]


def test_pull_app_missing_package(tmp_path):
    with pytest.raises(deploy.DeployError, match="not installed"):
        deploy.pull_app(FakeConnection(), PKG, tmp_path)
