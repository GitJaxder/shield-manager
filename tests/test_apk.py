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
