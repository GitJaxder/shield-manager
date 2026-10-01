import zipfile

import pytest

from shield_manager import bundle, deploy
from tests.fakes import FakeConnection, real_apk

PKG = "com.valvesoftware.steamlink"
ABIS = ["arm64_v8a", "armeabi_v7a", "x86", "x86_64"]


def make_apkm(tmp_path, name="steamlink.apkm", code=5000315, layout="apkm"):
    """A bundle like APKMirror's Steam Link one: a base APK plus one split per CPU type."""
    parts = tmp_path / f"{name}-parts"
    parts.mkdir()
    base = real_apk(parts / "base.apk", PKG, code, "1.3.32", cert=b"valve")
    splits = {
        f"split_config.{abi}.apk": real_apk(
            parts / f"split_config.{abi}.apk", PKG, code, "1.3.32", [abi.replace("_", "-")]
        )
        for abi in ABIS
    }
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        if layout == "apks":
            zf.write(base, "splits/base-master.apk")
            zf.write(base, "standalones/standalone-arm64_v8a.apk")
            for split in splits.values():
                abi = split.name.split(".")[1]
                zf.write(split, f"splits/base-{abi}.apk")
        else:
            zf.write(base, "base.apk")
            for n, split in splits.items():
                zf.write(split, n)
            zf.write(base, "split_config.xhdpi.apk")
        zf.writestr("info.json", "{}")
        zf.writestr("icon.png", b"png")
    return path


def test_is_bundle(tmp_path):
    assert bundle.is_bundle(make_apkm(tmp_path))
    assert not bundle.is_bundle(real_apk(tmp_path / "plain.apk", PKG, 1))
    assert not bundle.is_bundle(tmp_path / "missing.apkm")


def test_unpack_puts_the_base_first(tmp_path):
    paths = bundle.unpack(make_apkm(tmp_path), tmp_path / "out")
    assert paths[0].name == "base.apk"
    assert {p.name for p in paths[1:]} == {
        "split_config.arm64_v8a.apk",
        "split_config.armeabi_v7a.apk",
        "split_config.x86.apk",
        "split_config.x86_64.apk",
        "split_config.xhdpi.apk",
    }


def test_unpack_apks_uses_the_splits(tmp_path):
    paths = bundle.unpack(make_apkm(tmp_path, "app.apks", layout="apks"), tmp_path / "out")
    assert paths[0].name == "base-master.apk"
    assert len(paths) == 5  # standalones/ left out


def test_split_abi():
    assert bundle.split_abi("split_config.armeabi_v7a.apk") == "armeabi-v7a"
    assert bundle.split_abi("config.x86_64.apk") == "x86_64"
    assert bundle.split_abi("base-x86.apk") == "x86"
    assert bundle.split_abi("split_config.xhdpi.apk") is None
    assert bundle.split_abi("base.apk") is None


def test_pick_keeps_the_base_and_this_shields_splits(tmp_path):
    paths = bundle.unpack(make_apkm(tmp_path), tmp_path / "out")
    picked = bundle.pick(paths, ["armeabi-v7a", "armeabi"])
    assert [p.name for p in picked] == [
        "base.apk",
        "split_config.armeabi_v7a.apk",
        "split_config.xhdpi.apk",
    ]
    with pytest.raises(bundle.BundleError, match="only runs mips"):
        bundle.pick(paths, ["mips"])


def test_app_info_reads_the_base(tmp_path):
    info = bundle.app_info(make_apkm(tmp_path))
    assert (info.package, info.version_code, info.version_name) == (PKG, 5000315, "1.3.32")


def test_encrypted_bundles_are_refused(tmp_path):
    path = make_apkm(tmp_path)
    data = bytearray(path.read_bytes())
    # Set the "encrypted" flag on every local and central directory entry.
    for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = data.find(sig)
        while i != -1:
            data[i + offset] |= 1
            i = data.find(sig, i + 4)
    path.write_bytes(bytes(data))
    with pytest.raises(bundle.BundleError, match="encrypted"):
        bundle.unpack(path, tmp_path / "out")


def test_install_copies_only_what_the_shield_runs(tmp_path):
    conn = FakeConnection(abis=["armeabi-v7a", "armeabi"])
    conn.installed[PKG] = (5000315, "1.3.32")
    deploy.install(conn, make_apkm(tmp_path), PKG, 5000315)
    pushed = [local.rsplit("/", 1)[-1] for local, _ in conn.pushed]
    assert pushed == ["base.apk", "split_config.armeabi_v7a.apk", "split_config.xhdpi.apk"]


def test_progress_names_the_parts_each_shield_gets(tmp_path):
    from dataclasses import replace

    conn = FakeConnection(abis=["armeabi-v7a", "armeabi"])
    conn.installed[PKG] = (5000315, "1.3.32")
    events = []
    deploy.install(conn, make_apkm(tmp_path), PKG, 5000315, progress=events.append)
    installing = next(e for e in events if e.phase is deploy.Phase.INSTALLING)
    assert installing.parts == "base + armeabi-v7a + xhdpi"
    assert replace(installing, device="bedroom").describe() == (
        f"Installing {PKG} on bedroom (base + armeabi-v7a + xhdpi)"
    )


def test_install_refuses_a_bundle_for_other_cpu_types(tmp_path):
    conn = FakeConnection(abis=["mips"])
    with pytest.raises(deploy.IncompatibleAppError, match="only runs mips"):
        deploy.install(conn, make_apkm(tmp_path), PKG, 5000315)
    assert conn.pushed == []


def test_cli_installs_a_bundle(tmp_path, capsys):
    from shield_manager.cli import main
    from shield_manager.registry import Device, Registry

    registry = Registry(tmp_path / "devices.json")
    registry.add(Device("bedroom", "10.10.20.11"))
    conn = FakeConnection(abis=["armeabi-v7a", "armeabi"], responses={"pm install": "Success"})
    conn.installed[PKG] = (5000315, "1.3.32")
    import shield_manager.adb as adb

    original, adb.connect = adb.connect, lambda d: conn
    try:
        assert main(["app", "install", str(make_apkm(tmp_path)), "-d", "bedroom"], registry) == 0
    finally:
        adb.connect = original
    assert f"{PKG} 1.3.32 (versionCode 5000315)" in capsys.readouterr().out
    assert len(conn.pushed) == 3


def test_github_downloads_a_bundle_asset(tmp_path):
    import json

    from shield_manager.sources import Downloader, Wanted
    from tests.fakes import FakeHttp

    apkm = make_apkm(tmp_path)
    url = "https://github.com/dl/steamlink.apkm"
    releases = [
        {"tag_name": "1.3.32", "assets": [{"name": "steamlink.apkm", "browser_download_url": url}]}
    ]
    http = FakeHttp(
        {
            "https://api.github.com/repos/v/s/releases?per_page=10": json.dumps(releases).encode(),
            url: apkm,
        }
    )
    paths = Downloader(http, {PKG: "v/s"}).github(
        Wanted(PKG, 5000315, "1.3.32", ["armeabi-v7a"]), tmp_path / "dl"
    )
    assert paths[0].name == "base.apk" and len(paths) == 6


def test_unpack_refuses_a_bundle_that_unpacks_too_large(tmp_path, monkeypatch):
    # A zip bomb: small to upload or download, huge once unpacked.
    path = make_apkm(tmp_path)
    monkeypatch.setattr(bundle, "MAX_UNPACKED_BYTES", 1000)
    with pytest.raises(bundle.BundleError, match="more than 4 GB"):
        bundle.unpack(path, tmp_path / "out")
