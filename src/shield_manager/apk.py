"""Read an APK's package name and version from its binary AndroidManifest.xml.

APK manifests are stored in Android's compiled binary XML format (AXML). Only the
attributes of the root <manifest> element are needed, so this parses just enough of
the format to read them instead of pulling in a full APK toolkit.
"""

from __future__ import annotations

import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

RES_STRING_POOL_TYPE = 0x0001
RES_XML_TYPE = 0x0003
RES_XML_START_ELEMENT_TYPE = 0x0102
RES_XML_RESOURCE_MAP_TYPE = 0x0180

UTF8_FLAG = 1 << 8
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
NO_INDEX = 0xFFFFFFFF

# android:versionCode / android:versionName resource ids, used when a build tool has
# stripped attribute names from the string pool.
ATTR_VERSION_CODE = 0x0101021B
ATTR_VERSION_NAME = 0x0101021C


class ApkError(Exception):
    pass


@dataclass(frozen=True)
class ApkInfo:
    package: str
    version_code: int
    version_name: str


def read_apk_info(path: str | Path) -> ApkInfo:
    try:
        with zipfile.ZipFile(path) as zf:
            manifest = zf.read("AndroidManifest.xml")
    except (zipfile.BadZipFile, KeyError) as e:
        raise ApkError(f"{path} is not a valid APK: {e}") from e
    return parse_manifest(manifest)


def parse_manifest(data: bytes) -> ApkInfo:
    try:
        attrs = _manifest_attributes(data)
    except struct.error as e:
        raise ApkError("truncated AndroidManifest.xml") from e
    package = attrs.get("package")
    if not package:
        raise ApkError("manifest has no package attribute")
    version_code = attrs.get("versionCode", 0)
    return ApkInfo(
        package=str(package),
        version_code=int(version_code),
        version_name=str(attrs.get("versionName", "")),
    )


def _manifest_attributes(data: bytes) -> dict[str, str | int]:
    file_type, file_header = struct.unpack_from("<HH", data, 0)
    if file_type != RES_XML_TYPE:
        raise ApkError("AndroidManifest.xml is not binary XML")

    strings: list[str] = []
    resource_ids: list[int] = []
    offset = file_header
    while offset < len(data):
        chunk_type, header_size, chunk_size = struct.unpack_from("<HHI", data, offset)
        if chunk_size == 0:
            break
        if chunk_type == RES_STRING_POOL_TYPE:
            strings = _read_string_pool(data, offset)
        elif chunk_type == RES_XML_RESOURCE_MAP_TYPE:
            count = (chunk_size - header_size) // 4
            resource_ids = list(struct.unpack_from(f"<{count}I", data, offset + header_size))
        elif chunk_type == RES_XML_START_ELEMENT_TYPE:
            # The first element is always <manifest>.
            return _read_attributes(data, offset, header_size, strings, resource_ids)
        offset += chunk_size
    raise ApkError("manifest element not found")


def _read_string_pool(data: bytes, offset: int) -> list[str]:
    _, header_size, _, count, _, flags, strings_start, _ = struct.unpack_from(
        "<HHIIIIII", data, offset
    )
    offsets = struct.unpack_from(f"<{count}I", data, offset + header_size)
    base = offset + strings_start
    utf8 = bool(flags & UTF8_FLAG)
    return [_read_string(data, base + o, utf8) for o in offsets]


def _read_string(data: bytes, pos: int, utf8: bool) -> str:
    if utf8:
        # Character count then byte count, each one or two bytes.
        pos += 2 if data[pos] & 0x80 else 1
        length = data[pos]
        if length & 0x80:
            length = ((length & 0x7F) << 8) | data[pos + 1]
            pos += 2
        else:
            pos += 1
        return data[pos : pos + length].decode("utf-8", errors="replace")
    (length,) = struct.unpack_from("<H", data, pos)
    pos += 2
    if length & 0x8000:
        (low,) = struct.unpack_from("<H", data, pos)
        length = ((length & 0x7FFF) << 16) | low
        pos += 2
    return data[pos : pos + length * 2].decode("utf-16-le", errors="replace")


def _read_attributes(
    data: bytes, offset: int, header_size: int, strings: list[str], resource_ids: list[int]
) -> dict[str, str | int]:
    ext = offset + header_size
    attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, ext + 8)
    attrs: dict[str, str | int] = {}
    for i in range(attr_count):
        pos = ext + attr_start + i * attr_size
        _ns, name_idx, raw_idx, _size, _res0, data_type, value = struct.unpack_from(
            "<IIIHBBI", data, pos
        )
        name = strings[name_idx] if name_idx < len(strings) else ""
        res_id = resource_ids[name_idx] if name_idx < len(resource_ids) else None
        if res_id == ATTR_VERSION_CODE:
            name = "versionCode"
        elif res_id == ATTR_VERSION_NAME:
            name = "versionName"
        if data_type == TYPE_STRING:
            attrs[name] = strings[value]
        elif raw_idx != NO_INDEX:
            attrs[name] = strings[raw_idx]
        elif data_type in (TYPE_INT_DEC, TYPE_INT_HEX):
            attrs[name] = value
    return attrs
