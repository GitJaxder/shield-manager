"""Builds minimal binary XML and resources.arsc payloads for app metadata tests."""

import struct

from tests.axml import NO_INDEX, _string_pool

TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03


def build_xml(elements, strings, resource_ids):
    """elements are (tag, [(attr_name, data_type, value)]) with names indexing strings."""
    chunks = [
        _string_pool(strings, True),
        struct.pack("<HHI", 0x0180, 8, 8 + 4 * len(resource_ids))
        + struct.pack(f"<{len(resource_ids)}I", *resource_ids),
    ]
    for tag, attrs in elements:
        attr_bytes = b""
        for name, data_type, value in attrs:
            raw = value if data_type == TYPE_STRING else NO_INDEX
            attr_bytes += struct.pack(
                "<IIIHBBI", NO_INDEX, strings.index(name), raw, 8, 0, data_type, value
            )
        ext = struct.pack("<IIHHHHHH", NO_INDEX, strings.index(tag), 20, 20, len(attrs), 0, 0, 0)
        body = struct.pack("<II", 1, NO_INDEX) + ext + attr_bytes
        chunks.append(struct.pack("<HHI", 0x0102, 16, 8 + len(body)) + body)
    payload = b"".join(chunks)
    return struct.pack("<HHI", 0x0003, 8, 8 + len(payload)) + payload


def _type_chunk(type_id, density, language, entries, count):
    config = bytearray(64)
    struct.pack_into("<I", config, 0, 64)
    config[8 : 8 + len(language)] = language.encode()
    struct.pack_into("<H", config, 14, density)
    header_size = 20 + len(config)
    offsets, blob = [], b""
    for i in range(count):
        if i not in entries:
            offsets.append(NO_INDEX)
            continue
        data_type, value = entries[i]
        offsets.append(len(blob))
        blob += struct.pack("<HHI", 8, 0, 0) + struct.pack("<HBBI", 8, 0, data_type, value)
    entries_start = header_size + 4 * count
    size = entries_start + len(blob)
    header = struct.pack(
        "<HHIBBHII", 0x0201, header_size, size, type_id, 0, 0, count, entries_start
    )
    return header + bytes(config) + struct.pack(f"<{count}I", *offsets) + blob


def build_arsc(strings, configs, package_id=0x7F):
    """configs are (type_id, density, language, {entry_index: (data_type, value)})."""
    count = max(i for *_, entries in configs for i in entries) + 1
    types = b"".join(_type_chunk(t, d, lang, e, count) for t, d, lang, e in configs)
    pkg_header = struct.pack("<HHII", 0x0200, 288, 288 + len(types), package_id)
    pkg_header += b"\0" * 256 + struct.pack("<IIIII", 0, 0, 0, 0, 0)
    body = _string_pool(strings, True) + pkg_header + types
    return struct.pack("<HHII", 0x0002, 12, 12 + len(body), 1) + body
