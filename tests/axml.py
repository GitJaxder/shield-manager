"""Builds minimal binary AndroidManifest.xml payloads for tests."""

import struct

TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
NO_INDEX = 0xFFFFFFFF


def _string_pool(strings: list[str], utf8: bool) -> bytes:
    blobs = []
    for s in strings:
        if utf8:
            raw = s.encode("utf-8")
            blobs.append(bytes([len(s), len(raw)]) + raw + b"\0")
        else:
            blobs.append(struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\0\0")
    offsets, pos = [], 0
    for b in blobs:
        offsets.append(pos)
        pos += len(b)
    body = b"".join(blobs)
    body += b"\0" * (-len(body) % 4)
    header_size = 28
    strings_start = header_size + 4 * len(strings)
    size = strings_start + len(body)
    flags = 1 << 8 if utf8 else 0
    header = struct.pack(
        "<HHIIIIII", 0x0001, header_size, size, len(strings), 0, flags, strings_start, 0
    )
    return header + struct.pack(f"<{len(strings)}I", *offsets) + body


def build_manifest(
    attrs: list[tuple[str, int, int]],
    strings: list[str],
    resource_ids: list[int] | None = None,
    utf8: bool = True,
) -> bytes:
    """attrs are (name, data_type, value) where value is a string index or an int."""
    chunks = [_string_pool(strings, utf8)]
    if resource_ids:
        chunks.append(
            struct.pack("<HHI", 0x0180, 8, 8 + 4 * len(resource_ids))
            + struct.pack(f"<{len(resource_ids)}I", *resource_ids)
        )
    attr_bytes = b""
    for name, data_type, value in attrs:
        raw = value if data_type == TYPE_STRING else NO_INDEX
        attr_bytes += struct.pack(
            "<IIIHBBI", NO_INDEX, strings.index(name), raw, 8, 0, data_type, value
        )
    ext = struct.pack("<IIHHHHHH", NO_INDEX, strings.index("manifest"), 20, 20, len(attrs), 0, 0, 0)
    element_body = struct.pack("<II", 1, NO_INDEX) + ext + attr_bytes
    chunks.append(struct.pack("<HHI", 0x0102, 16, 8 + len(element_body)) + element_body)
    payload = b"".join(chunks)
    return struct.pack("<HHI", 0x0003, 8, 8 + len(payload)) + payload
