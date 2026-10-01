import pytest

from shield_manager import deploy
from shield_manager.apk import ApkInfo
from tests.fakes import FakeConnection

INFO = ApkInfo("com.example.tv", 42, "1.4.2")


def test_installed_version():
    conn = FakeConnection(installed={"com.example.tv": (41, "1.4.1 beta")})
    assert deploy.installed_version(conn, "com.example.tv") == deploy.InstalledVersion(
        41, "1.4.1 beta"
    )


def test_installed_version_missing():
    assert deploy.installed_version(FakeConnection(), "com.example.tv") is None


def test_install_pushes_installs_cleans_up_and_verifies():
    conn = FakeConnection(responses={"pm install": "Performing Streamed Install\nSuccess\n"})
    conn.installed["com.example.tv"] = (42, "1.4.2")
    version = deploy.install(conn, "app.apk", INFO)
    assert version == deploy.InstalledVersion(42, "1.4.2")
    assert conn.pushed == [("app.apk", "/data/local/tmp/com.example.tv.apk")]
    assert "pm install -r /data/local/tmp/com.example.tv.apk" in conn.commands
    assert "rm -f /data/local/tmp/com.example.tv.apk" in conn.commands


def test_install_allow_downgrade_passes_flag():
    conn = FakeConnection(responses={"pm install": "Success"}, installed={INFO.package: (42, "")})
    deploy.install(conn, "app.apk", INFO, allow_downgrade=True)
    assert any(c.startswith("pm install -r -d ") for c in conn.commands)


def test_install_failure_reports_pm_output_and_still_cleans_up():
    conn = FakeConnection(responses={"pm install": "Failure [INSTALL_FAILED_VERSION_DOWNGRADE]"})
    with pytest.raises(deploy.DeployError, match="VERSION_DOWNGRADE"):
        deploy.install(conn, "app.apk", INFO)
    assert conn.commands[-1].startswith("rm -f ")


def test_install_detects_version_mismatch():
    conn = FakeConnection(responses={"pm install": "Success"}, installed={INFO.package: (40, "")})
    with pytest.raises(deploy.DeployError, match="versionCode 40"):
        deploy.install(conn, "app.apk", INFO)


def test_uninstall():
    conn = FakeConnection(responses={"pm uninstall": "Success"})
    deploy.uninstall(conn, "com.example.tv")
    assert conn.commands == ["pm uninstall com.example.tv"]


def test_uninstall_failure():
    conn = FakeConnection(responses={"pm uninstall": "Failure [DELETE_FAILED_INTERNAL_ERROR]"})
    with pytest.raises(deploy.DeployError):
        deploy.uninstall(conn, "com.example.tv")


def test_list_packages_third_party_by_default():
    conn = FakeConnection(responses={"pm list packages": "package:b.app\npackage:a.app\n"})
    assert deploy.list_packages(conn) == ["a.app", "b.app"]
    assert conn.commands == ["pm list packages -3"]
