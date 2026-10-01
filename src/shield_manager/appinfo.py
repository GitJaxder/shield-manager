"""Read an installed app's display name, icon and TV banner from a Shield.

Only the APK's AndroidManifest.xml, resources.arsc and the chosen image files are read,
and they are extracted on the Shield itself, so a 2 GB game costs a few hundred KB to
inspect. Results are cached on disk per package and versionCode.
"""

from __future__ import annotations

import json
import re
import shlex
import struct
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from shield_manager.apk import (
    RES_STRING_POOL_TYPE,
    RES_XML_RESOURCE_MAP_TYPE,
    RES_XML_START_ELEMENT_TYPE,
    RES_XML_TYPE,
    _read_string_pool,
)
from shield_manager.deploy import Connection, DeployError

RES_TABLE_TYPE = 0x0002
RES_TABLE_PACKAGE_TYPE = 0x0200
RES_TABLE_TYPE_TYPE = 0x0201

TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03

FLAG_COMPLEX = 0x0001
FLAG_COMPACT = 0x0008
TYPE_FLAG_SPARSE = 0x01
TYPE_FLAG_OFFSET16 = 0x02
NO_ENTRY = 0xFFFFFFFF

ATTR_LABEL = 0x01010001
ATTR_ICON = 0x01010002
ATTR_BANNER = 0x010103F2

DENSITY_DEFAULT = 0
DENSITY_ANY = 0xFFFE
DENSITY_NONE = 0xFFFF
IMAGE_TYPES = {".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg"}

REMOTE_WORK_DIR = "/data/local/tmp/shield-manager-meta"


@dataclass(frozen=True)
class ResValue:
    density: int
    language: str
    data_type: int
    data: int | str  # a string for TYPE_STRING, otherwise the raw 32-bit value


class ResourceTable:
    """Just enough of resources.arsc to resolve a resource id to its values."""

    def __init__(self, data: bytes) -> None:
        self.values: dict[int, list[ResValue]] = {}
        self._parse(data)

    def _parse(self, data: bytes) -> None:
        chunk_type, header_size, _ = struct.unpack_from("<HHI", data, 0)
        if chunk_type != RES_TABLE_TYPE:
            raise ValueError("not a resource table")
        strings: list[str] = []
        offset = header_size
        while offset + 8 <= len(data):
            chunk_type, _, chunk_size = struct.unpack_from("<HHI", data, offset)
            if chunk_size == 0:
                break
            if chunk_type == RES_STRING_POOL_TYPE:
                strings = _read_string_pool(data, offset)
            elif chunk_type == RES_TABLE_PACKAGE_TYPE:
                self._parse_package(data, offset, chunk_size, strings)
            offset += chunk_size

    def _parse_package(self, data: bytes, start: int, size: int, strings: list[str]) -> None:
        _, header_size, _, package_id = struct.unpack_from("<HHII", data, start)
        offset = start + header_size
        while offset + 8 <= start + size:
            chunk_type, _, chunk_size = struct.unpack_from("<HHI", data, offset)
            if chunk_size == 0:
                break
            if chunk_type == RES_TABLE_TYPE_TYPE:
                self._parse_type(data, offset, package_id, strings)
            offset += chunk_size

    def _parse_type(self, data: bytes, start: int, package_id: int, strings: list[str]) -> None:
        _, header_size, _, type_id, flags, _, entry_count, entries_start = struct.unpack_from(
            "<HHIBBHII", data, start
        )
        config = start + 20
        (language,) = struct.unpack_from("<2s", data, config + 8)
        (density,) = struct.unpack_from("<H", data, config + 14)
        lang = language.rstrip(b"\0").decode("ascii", errors="replace")

        table = start + header_size
        if flags & TYPE_FLAG_SPARSE:
            pairs = struct.unpack_from(f"<{entry_count * 2}H", data, table)
            entries = [(pairs[i], pairs[i + 1] * 4) for i in range(0, len(pairs), 2)]
        elif flags & TYPE_FLAG_OFFSET16:
            raw = struct.unpack_from(f"<{entry_count}H", data, table)
            entries = [(i, o * 4) for i, o in enumerate(raw) if o != 0xFFFF]
        else:
            raw = struct.unpack_from(f"<{entry_count}I", data, table)
            entries = [(i, o) for i, o in enumerate(raw) if o != NO_ENTRY]

        for index, entry_offset in entries:
            pos = start + entries_start + entry_offset
            size_or_key, entry_flags = struct.unpack_from("<HH", data, pos)
            if entry_flags & FLAG_COMPACT:
                data_type, value = entry_flags >> 8, struct.unpack_from("<I", data, pos + 4)[0]
            elif entry_flags & FLAG_COMPLEX:
                continue  # styles, arrays and other bags aren't needed here
            else:
                _, _, data_type, value = struct.unpack_from("<HBBI", data, pos + size_or_key)
            resolved: int | str = value
            if data_type == TYPE_STRING:
                resolved = strings[value] if value < len(strings) else ""
            res_id = (package_id << 24) | (type_id << 16) | index
            self.values.setdefault(res_id, []).append(ResValue(density, lang, data_type, resolved))

    def resolve(self, res_id: int, depth: int = 0) -> list[ResValue]:
        """Every value a resource can take, following references to other resources."""
        out = []
        for v in self.values.get(res_id, []):
            if v.data_type == TYPE_REFERENCE and depth < 8:
                out.extend(self.resolve(int(v.data), depth + 1))
            else:
                out.append(v)
        return out

    def string(self, res_id: int) -> str | None:
        values = [v for v in self.resolve(res_id) if v.data_type == TYPE_STRING]
        for v in sorted(values, key=lambda v: (v.language not in ("", "en"), v.language)):
            return str(v.data)
        return None

    def best_image(self, res_id: int) -> str | None:
        """Path inside the APK of the highest-density bitmap for a drawable resource."""

        def rank(v: ResValue) -> int:
            if v.density in (DENSITY_ANY, DENSITY_NONE):
                return 1
            return v.density or 160

        images = [
            v
            for v in self.resolve(res_id)
            if v.data_type == TYPE_STRING and Path(str(v.data)).suffix in IMAGE_TYPES
        ]
        return str(max(images, key=rank).data) if images else None


def application_attributes(manifest: bytes) -> dict[int | str, tuple[int, int | str]]:
    """Attributes of <application>, keyed by resource id (or name), as (type, value)."""
    file_type, header = struct.unpack_from("<HH", manifest, 0)
    if file_type != RES_XML_TYPE:
        raise ValueError("not binary XML")
    strings: list[str] = []
    resource_ids: list[int] = []
    offset = header
    while offset + 8 <= len(manifest):
        chunk_type, header_size, chunk_size = struct.unpack_from("<HHI", manifest, offset)
        if chunk_size == 0:
            break
        if chunk_type == RES_STRING_POOL_TYPE:
            strings = _read_string_pool(manifest, offset)
        elif chunk_type == RES_XML_RESOURCE_MAP_TYPE:
            count = (chunk_size - header_size) // 4
            resource_ids = list(struct.unpack_from(f"<{count}I", manifest, offset + header_size))
        elif chunk_type == RES_XML_START_ELEMENT_TYPE:
            ext = offset + header_size
            _, name_idx = struct.unpack_from("<II", manifest, ext)
            if name_idx < len(strings) and strings[name_idx] == "application":
                return _element_attributes(manifest, ext, strings, resource_ids)
        offset += chunk_size
    return {}


def _element_attributes(
    data: bytes, ext: int, strings: list[str], resource_ids: list[int]
) -> dict[int | str, tuple[int, int | str]]:
    attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, ext + 8)
    attrs: dict[int | str, tuple[int, int | str]] = {}
    for i in range(attr_count):
        pos = ext + attr_start + i * attr_size
        _, name_idx, raw_idx, _, _, data_type, value = struct.unpack_from("<IIIHBBI", data, pos)
        key: int | str = (
            resource_ids[name_idx]
            if name_idx < len(resource_ids)
            else (strings[name_idx] if name_idx < len(strings) else "")
        )
        if data_type == TYPE_STRING:
            attrs[key] = (TYPE_STRING, strings[value])
        elif raw_idx != NO_ENTRY and raw_idx < len(strings):
            attrs[key] = (TYPE_STRING, strings[raw_idx])
        else:
            attrs[key] = (data_type, value)
    return attrs


@dataclass(frozen=True)
class AppImages:
    label: str | None
    icon_path: str | None  # paths inside the APK
    banner_path: str | None


def describe(manifest: bytes, resources: bytes | None) -> AppImages:
    """Work out an app's label and which image files hold its icon and banner."""
    attrs = application_attributes(manifest)
    table = ResourceTable(resources) if resources else None

    def lookup(attr: int, kind: str) -> str | None:
        if attr not in attrs:
            return None
        data_type, value = attrs[attr]
        if data_type == TYPE_STRING:
            return str(value) if kind == "string" else None
        if data_type == TYPE_REFERENCE and table:
            res_id = int(value)
            return table.string(res_id) if kind == "string" else table.best_image(res_id)
        return None

    return AppImages(
        label=lookup(ATTR_LABEL, "string"),
        icon_path=lookup(ATTR_ICON, "image"),
        banner_path=lookup(ATTR_BANNER, "image"),
    )


@dataclass
class AppMeta:
    package: str
    version_code: int
    label: str | None = None
    icon: str | None = None  # cached file names, relative to the cache directory
    banner: str | None = None


def _base_apk(conn: Connection, package: str) -> str:
    out = str(conn.shell(f"pm path {shlex.quote(package)}"))
    paths = [
        line.removeprefix("package:").strip()
        for line in out.splitlines()
        if line.startswith("package:")
    ]
    if not paths:
        raise DeployError(f"{package} is not installed")
    return next((p for p in paths if p.endswith("/base.apk")), paths[0])


def _extract(conn: Connection, apk: str, members: list[str], dest: Path) -> dict[str, Path]:
    """Unzip members of an APK on the device and pull back the ones that exist."""
    remote_dir = f"{REMOTE_WORK_DIR}/{dest.name}"
    quoted = " ".join(shlex.quote(m) for m in members)
    try:
        conn.shell(
            f"rm -rf {shlex.quote(remote_dir)} && mkdir -p {shlex.quote(remote_dir)} && "
            f"unzip -o -q {shlex.quote(apk)} {quoted} -d {shlex.quote(remote_dir)}"
        )
        listing = str(conn.shell(f"cd {shlex.quote(remote_dir)} && find . -type f"))
        found = {ln.strip().removeprefix("./") for ln in listing.splitlines() if ln.strip()}
        pulled = {}
        for member in members:
            if member in found:
                local = dest / member.replace("/", "_")
                conn.pull(f"{remote_dir}/{member}", str(local))
                pulled[member] = local
        return pulled
    finally:
        conn.shell(f"rm -rf {shlex.quote(remote_dir)}")


def fetch_meta(conn: Connection, package: str, version_code: int, cache_dir: Path) -> AppMeta:
    """Read an installed app's label, icon and banner, saving the images to cache_dir."""
    meta = AppMeta(package, version_code)
    cache_dir.mkdir(parents=True, exist_ok=True)
    apk = _base_apk(conn, package)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / re.sub(r"[^\w.]", "_", package)
        work.mkdir()
        files = _extract(conn, apk, ["AndroidManifest.xml", "resources.arsc"], work)
        if "AndroidManifest.xml" not in files:
            raise DeployError("couldn't read the app's manifest")
        arsc = files.get("resources.arsc")
        info = describe(
            files["AndroidManifest.xml"].read_bytes(), arsc.read_bytes() if arsc else None
        )
        meta.label = info.label
        wanted = [p for p in (info.icon_path, info.banner_path) if p]
        images = _extract(conn, apk, wanted, work) if wanted else {}
        for kind, path in (("icon", info.icon_path), ("banner", info.banner_path)):
            if path and path in images:
                name = f"{package}.{kind}{Path(path).suffix}"
                (cache_dir / name).write_bytes(images[path].read_bytes())
                setattr(meta, kind, name)
    return meta


class MetaCache:
    """App labels and images on disk, keyed by package and refreshed when its version changes."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.index_path = directory / "index.json"

    def load(self) -> dict[str, AppMeta]:
        if not self.index_path.exists():
            return {}
        try:
            raw = json.loads(self.index_path.read_text())
        except json.JSONDecodeError:
            return {}
        return {p: AppMeta(**m) for p, m in raw.items()}

    def get(self, package: str, version_code: int) -> AppMeta | None:
        meta = self.load().get(package)
        return meta if meta and meta.version_code == version_code else None

    def put(self, meta: AppMeta) -> None:
        index = self.load()
        index[meta.package] = meta
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({p: asdict(m) for p, m in sorted(index.items())}, indent=1))
        tmp.replace(self.index_path)

    def image_path(self, name: str) -> Path | None:
        path = self.directory / name
        if path.parent != self.directory or not path.is_file():
            return None
        return path
