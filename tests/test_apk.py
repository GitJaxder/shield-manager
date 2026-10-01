import zipfile

import pytest

from shield_manager.apk import (
    ATTR_VERSION_CODE,
    ATTR_VERSION_NAME,
    ApkError,
    ApkInfo,
    parse_manifest,
    read_apk_info,
)
from tests.axml import TYPE_INT_DEC, TYPE_STRING, build_manifest

STRINGS = ["versionCode", "versionName", "package", "manifest", "com.example.tv", "1.4.2"]
ATTRS = [
    ("versionCode", TYPE_INT_DEC, 42),
    ("versionName", TYPE_STRING, 5),
    ("package", TYPE_STRING, 4),
]
EXPECTED = ApkInfo("com.example.tv", 42, "1.4.2")


@pytest.mark.parametrize("utf8", [True, False])
def test_parse_manifest(utf8):
    assert parse_manifest(build_manifest(ATTRS, STRINGS, utf8=utf8)) == EXPECTED


def test_parse_manifest_with_stripped_attribute_names():
    # Some build tools blank android: attribute names and rely on the resource map.
    strings = ["", " ", "package", "manifest", "com.example.tv", "1.4.2"]
    attrs = [("", TYPE_INT_DEC, 42), (" ", TYPE_STRING, 5), ("package", TYPE_STRING, 4)]
    data = build_manifest(attrs, strings, resource_ids=[ATTR_VERSION_CODE, ATTR_VERSION_NAME])
    assert parse_manifest(data) == EXPECTED


def test_read_apk_info(tmp_path):
    apk = tmp_path / "app.apk"
    with zipfile.ZipFile(apk, "w") as zf:
        zf.writestr("AndroidManifest.xml", build_manifest(ATTRS, STRINGS))
    assert read_apk_info(apk) == EXPECTED


def test_not_a_zip(tmp_path):
    bad = tmp_path / "bad.apk"
    bad.write_text("nope")
    with pytest.raises(ApkError):
        read_apk_info(bad)


def test_text_manifest_rejected():
    with pytest.raises(ApkError):
        parse_manifest(b"<?xml version='1.0'?><manifest/>")


def test_signers_reads_v2_and_v3_certificates(tmp_path):
    import hashlib
    import zipfile

    from shield_manager.apk import signers
    from tests.fakes import sign_apk

    for block_id in (0x7109871A, 0xF05368C0):
        path = tmp_path / f"{block_id}.apk"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("AndroidManifest.xml", b"x")
        sign_apk(path, b"cert-a", block_id=block_id)
        assert signers(path) == {hashlib.sha256(b"cert-a").hexdigest()}
        with zipfile.ZipFile(path) as zf:  # still a valid zip
            assert zf.read("AndroidManifest.xml") == b"x"


def test_signers_of_an_unsigned_or_broken_apk_is_empty(tmp_path):
    import zipfile

    from shield_manager.apk import signers

    path = tmp_path / "plain.apk"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("AndroidManifest.xml", b"x")
    assert signers(path) == set()
    (tmp_path / "junk.apk").write_bytes(b"not a zip")
    assert signers(tmp_path / "junk.apk") == set()
    assert signers(tmp_path / "missing.apk") == set()
