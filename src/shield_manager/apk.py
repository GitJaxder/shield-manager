"""Read an APK's package name and version from its binary AndroidManifest.xml.

APK manifests are stored in Android's compiled binary XML format (AXML). Only the
attributes of the root <manifest> element are needed, so this parses just enough of
the format to read them instead of pulling in a full APK toolkit.
"""

from __future__ import annotations

import hashlib
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


def native_abis(paths: list[Path]) -> set[str]:
    """CPU ABIs an app ships native code for (e.g. {"arm64-v8a"}), across its base and
    split APKs. Empty means the app has no native code (or the files can't be read), so
    nothing rules it out."""
    abis = set()
    for path in paths:
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        except (OSError, zipfile.BadZipFile):
            continue  # pm install will report a broken APK better than we can
        for name in names:
            parts = name.split("/")
            if len(parts) == 3 and parts[0] == "lib" and parts[2].endswith(".so"):
                abis.add(parts[1])
    return abis


# APK Signature Scheme v2/v3 block ids (https://source.android.com/docs/security/features/apksigning/v2)
_SIGNATURE_BLOCK_IDS = (0x7109871A, 0xF05368C0, 0x1B93AD61)
_SIG_BLOCK_MAGIC = b"APK Sig Block 42"


def signers(path: str | Path) -> set[str]:
    """SHA-256 fingerprints of the certificates an APK is verifiably signed with (scheme v2
    or later).

    Each signer's signature is checked against its public key, and the APK's contents
    against the digest it signed, the way Android does, so a file that only copies a
    developer's (public) certificate gets no fingerprint. Two copies of an app come from the
    same developer when their fingerprints overlap. Returns an empty set for an APK signed
    only with the old v1 scheme, or one that can't be read or verified, so callers treat it
    as unverifiable.
    """
    try:
        data = Path(path).read_bytes()
        return {hashlib.sha256(cert).hexdigest() for cert in _verified_certs(data)}
    except (OSError, struct.error, ValueError, IndexError):
        return set()


# Signature algorithm ids (APK Signature Scheme v2): (hash, padding or curve kind).
_SHA256, _SHA512 = hashlib.sha256, hashlib.sha512
_ALGORITHMS = {
    0x0101: (_SHA256, "pss"),
    0x0102: (_SHA512, "pss"),
    0x0103: (_SHA256, "pkcs1"),
    0x0104: (_SHA512, "pkcs1"),
    0x0201: (_SHA256, "ecdsa"),
    0x0202: (_SHA512, "ecdsa"),
    0x0301: (_SHA256, "dsa"),
}
_CHUNK = 1024 * 1024


def _verified_certs(data: bytes) -> list[bytes]:
    """The first certificate of every signer whose signature and digest check out."""
    eocd = data.rfind(b"PK\x05\x06", max(0, len(data) - 65_557))
    if eocd < 0:
        return []
    (cd_offset,) = struct.unpack_from("<I", data, eocd + 16)
    if cd_offset < 24 or data[cd_offset - 16 : cd_offset] != _SIG_BLOCK_MAGIC:
        return []
    (size,) = struct.unpack_from("<Q", data, cd_offset - 24)
    block_start = cd_offset - size - 8
    if block_start < 0 or struct.unpack_from("<Q", data, block_start)[0] != size:
        raise ValueError("damaged signing block")
    sections = (
        data[:block_start],
        data[cd_offset:eocd],
        _eocd_pointing_at(data, eocd, block_start),
    )
    digests: dict[object, bytes] = {}

    def content_digest(hash_fn) -> bytes:
        if hash_fn not in digests:
            digests[hash_fn] = _content_digest(sections, hash_fn)
        return digests[hash_fn]

    certs = []
    pos, end = block_start + 8, cd_offset - 24
    while pos + 12 <= end:
        length, block_id = struct.unpack_from("<QI", data, pos)
        value = data[pos + 12 : pos + 8 + length]
        if block_id in _SIGNATURE_BLOCK_IDS:
            v3 = block_id != _SIGNATURE_BLOCK_IDS[0]
            for signer in _length_prefixed(_length_prefixed(value, 0)[0]):
                cert = _verify_signer(signer, v3, content_digest)
                if cert is not None:
                    certs.append(cert)
        pos += 8 + length
    return certs


def _eocd_pointing_at(data: bytes, eocd: int, block_start: int) -> bytes:
    """The end-of-central-directory record as it was before the signing block was added:
    its central directory offset is the block's start."""
    record = bytearray(data[eocd:])
    struct.pack_into("<I", record, 16, block_start)
    return bytes(record)


def _content_digest(sections: tuple[bytes, ...], hash_fn) -> bytes:
    """The APK Signature Scheme v2 digest: a hash of the hashes of every 1 MB chunk."""
    chunk_digests = []
    for section in sections:
        for i in range(0, len(section), _CHUNK):
            chunk = section[i : i + _CHUNK]
            chunk_digests.append(hash_fn(b"\xa5" + struct.pack("<I", len(chunk)) + chunk).digest())
    return hash_fn(
        b"\x5a" + struct.pack("<I", len(chunk_digests)) + b"".join(chunk_digests)
    ).digest()


def _verify_signer(signer: bytes, v3: bool, content_digest) -> bytes | None:
    """The signer's certificate if its signature and content digest are valid, else None."""
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import dsa, ec, padding, rsa

    signed_data, rest = _take(signer)
    if v3:
        rest = rest[8:]  # minSdkVersion, maxSdkVersion
    signatures, rest = _take(rest)
    public_key_der, _ = _take(rest)

    supported = {}
    for entry in _length_prefixed(signatures):
        (algorithm,) = struct.unpack_from("<I", entry, 0)
        if algorithm in _ALGORITHMS:
            supported[algorithm] = _take(entry[4:])[0]
    if not supported:
        return None
    algorithm = max(supported, key=lambda a: (_ALGORITHMS[a][0] is _SHA512, a))
    hash_fn, kind = _ALGORITHMS[algorithm]
    hash_alg = hashes.SHA512() if hash_fn is _SHA512 else hashes.SHA256()

    public_key = serialization.load_der_public_key(public_key_der)
    signature = supported[algorithm]
    try:
        if kind == "pss" and isinstance(public_key, rsa.RSAPublicKey):
            salt = hash_alg.digest_size
            pss = padding.PSS(mgf=padding.MGF1(hash_alg), salt_length=salt)
            public_key.verify(signature, signed_data, pss, hash_alg)
        elif kind == "pkcs1" and isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(signature, signed_data, padding.PKCS1v15(), hash_alg)
        elif kind == "ecdsa" and isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, signed_data, ec.ECDSA(hash_alg))
        elif kind == "dsa" and isinstance(public_key, dsa.DSAPublicKey):
            public_key.verify(signature, signed_data, hash_alg)
        else:
            return None
    except InvalidSignature:
        return None

    digests_blob, rest = _take(signed_data)
    certificates, _ = _take(rest)
    signed_digests = {}
    for entry in _length_prefixed(digests_blob):
        (digest_algorithm,) = struct.unpack_from("<I", entry, 0)
        signed_digests[digest_algorithm] = _take(entry[4:])[0]
    if signed_digests.get(algorithm) != content_digest(hash_fn):
        return None

    certs = _length_prefixed(certificates)
    if not certs:
        return None
    cert_key = x509.load_der_x509_certificate(certs[0]).public_key()
    spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if cert_key.public_bytes(*spki) != public_key.public_bytes(*spki):
        return None
    return certs[0]


def _take(blob: bytes) -> tuple[bytes, bytes]:
    """The first uint32-length-prefixed item in blob, and what follows it."""
    (length,) = struct.unpack_from("<I", blob, 0)
    if 4 + length > len(blob):
        raise ValueError("truncated signing block")
    return blob[4 : 4 + length], blob[4 + length :]


def _length_prefixed(blob: bytes, index: int | None = None) -> list[bytes]:
    """The uint32-length-prefixed items in blob, or just the item at index (as a 1-list)."""
    items, pos = [], 0
    while pos + 4 <= len(blob):
        (length,) = struct.unpack_from("<I", blob, pos)
        if pos + 4 + length > len(blob):
            raise ValueError("truncated signing block")
        items.append(blob[pos + 4 : pos + 4 + length])
        pos += 4 + length
    if index is None:
        return items
    return [items[index]]


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
