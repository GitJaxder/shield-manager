import zipfile
from pathlib import Path

from shield_manager.apk import TYPE_INT_DEC, TYPE_STRING, ApkError, parse_manifest
from tests.axml import build_manifest


class FakeConnection:
    """Stands in for an AdbDeviceTcp with a simulated package manager.

    APK files used with it contain "package:versionCode" as text, so installs can update
    the simulated device. Entries in responses override the reply to any command that
    starts with that prefix. With abis (CPU types, preferred first), the device reports
    them and the APKs pulled from it are zips with native code for its first one, the way
    the Play Store delivers apps.
    """

    def __init__(self, responses=None, installed=None, splits=None, abis=None, apk_files=None):
        self.responses = responses or {}
        self.installed = dict(installed or {})  # package -> (version_code, version_name)
        self.splits = splits or {}  # package -> extra split APK names
        self.commands = []
        self.pushed = []
        self.pulled = []
        self.staged = {}  # remote path -> (package, version_code)
        self.closed = False
        self.abis = list(abis or [])
        self.apk_files = apk_files or {}  # package -> real APK file served as its base.apk

    def push(self, local_path, device_path, progress_callback=None, **kwargs):
        self.pushed.append((local_path, device_path))
        local = Path(local_path)
        if progress_callback and local.exists():
            _report_chunks(progress_callback, device_path, local.stat().st_size)
        if local.exists() and zipfile.is_zipfile(local):
            with zipfile.ZipFile(local) as zf:
                names = zf.namelist()
                text = zf.read("stub").decode() if "stub" in names else ""
                if "AndroidManifest.xml" in names and not text:
                    try:
                        info = parse_manifest(zf.read("AndroidManifest.xml"))
                        text = f"{info.package}:{info.version_code}"
                    except ApkError:
                        pass
        else:
            text = local.read_bytes().decode(errors="replace") if local.exists() else ""
        if text.count(":") == 1:
            package, code = text.split(":")
            self.staged[device_path] = (package, int(code))

    def _remote_content(self, device_path):
        package = Path(device_path).parent.name.removesuffix("-1")
        return f"{package}:{self.installed[package][0]}"

    def pull(self, device_path, local_path, progress_callback=None, **kwargs):
        self.pulled.append(device_path)
        package = Path(device_path).parent.name.removesuffix("-1")
        if package in self.apk_files and device_path.endswith("/base.apk"):
            Path(local_path).write_bytes(Path(self.apk_files[package]).read_bytes())
            return
        content = self._remote_content(device_path)
        if self.abis:
            with zipfile.ZipFile(local_path, "w") as zf:
                zf.writestr("stub", content)
                zf.writestr(f"lib/{self.abis[0]}/libapp.so", b"\x7fELF")
        else:
            Path(local_path).write_text(content)
        if progress_callback:
            _report_chunks(progress_callback, device_path, Path(local_path).stat().st_size)

    def _apply(self, remotes):
        for remote in remotes:
            if remote in self.staged:
                package, code = self.staged[remote]
                self.installed[package] = (code, "")

    def shell(self, command, **kwargs):
        self.commands.append(command)
        for prefix, response in self.responses.items():
            if command.startswith(prefix):
                return response
        args = command.split()
        if command == "getprop ro.product.cpu.abilist":
            return ",".join(self.abis)
        if command.startswith("dumpsys package "):
            package = args[-1]
            if package in self.installed:
                code, name = self.installed[package]
                return (
                    f"Packages:\n  Package [{package}] (abc):\n"
                    f"    versionCode={code} minSdk=21 targetSdk=33\n"
                    f"    versionName={name}\n"
                )
            return "Dexopt state:\n"
        if command.startswith("pm list packages -3 --show-versioncode"):
            return "".join(
                f"package:{p} versionCode:{code}\n" for p, (code, _) in self.installed.items()
            )
        if command.startswith("pm list packages"):
            return "".join(f"package:{p}\n" for p in self.installed)
        if command.startswith("stat -c %s "):
            return "".join(f"{len(self._remote_content(p))}\n" for p in args[3:])
        if command.startswith("pm path "):
            package = args[-1]
            if package not in self.installed:
                return ""
            names = ["base.apk", *self.splits.get(package, [])]
            return "".join(f"package:/data/app/~~x/{package}-1/{n}\n" for n in names)
        if command.startswith("pm install-create"):
            self.session_files = []
            return "Success: created install session [7]"
        if command.startswith("pm install-write"):
            self.session_files.append(args[-1])
            return "Success: streamed 1 bytes"
        if command.startswith("pm install-commit"):
            self._apply(self.session_files)
            return "Success"
        if command.startswith("pm install "):
            self._apply([args[-1]])
            return "Success"
        if command.startswith("pm uninstall "):
            if self.installed.pop(args[-1], None) is None:
                return "Failure [DELETE_FAILED_INTERNAL_ERROR]"
            return "Success"
        return ""

    def close(self):
        self.closed = True


def _report_chunks(callback, path, size):
    """Report a transfer in two chunks, the way adb-shell reports each chunk sent."""
    first = size // 2
    callback(path, first, size)
    callback(path, size - first, size)


def make_apk(path, package, version_code):
    """Write a stand-in APK file that FakeConnection understands."""
    Path(path).write_text(f"{package}:{version_code}")
    return Path(path)


def _lp(*items):
    """uint32-length-prefixed concatenation, as in the APK signing block."""
    import struct

    return b"".join(struct.pack("<I", len(i)) + i for i in items)


def sign_apk(path, *certs, block_id=0x7109871A):
    """Add an APK Signature Scheme v2-style block naming certs (DER bytes) to a zip file.

    Only the parts apk.signers reads are filled in; digests and signatures are empty.
    """
    import struct

    data = Path(path).read_bytes()
    eocd = data.rfind(b"PK\x05\x06")
    (cd_offset,) = struct.unpack_from("<I", data, eocd + 16)
    signers = [_lp(_lp(_lp(), _lp(*certs), _lp()), _lp(), b"") for _ in certs[:1]]
    value = _lp(_lp(*signers))
    pair = struct.pack("<QI", len(value) + 4, block_id) + value
    size = len(pair) + 8 + 16
    block = struct.pack("<Q", size) + pair + struct.pack("<Q", size) + b"APK Sig Block 42"
    eocd_record = bytearray(data[eocd:])
    struct.pack_into("<I", eocd_record, 16, cd_offset + len(block))
    Path(path).write_bytes(data[:cd_offset] + block + data[cd_offset:eocd] + bytes(eocd_record))
    return Path(path)


def real_apk(path, package, version_code, version_name="1.0", abis=(), cert=None):
    """Write an APK with a real binary manifest, native code for abis, and (if cert is
    given) a signing block naming that certificate."""
    strings = ["versionCode", "versionName", "package", "manifest", package, version_name]
    attrs = [
        ("versionCode", TYPE_INT_DEC, version_code),
        ("versionName", TYPE_STRING, 5),
        ("package", TYPE_STRING, 4),
    ]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("AndroidManifest.xml", build_manifest(attrs, strings))
        for abi in abis:
            zf.writestr(f"lib/{abi}/libapp.so", b"\x7fELF")
    if cert:
        sign_apk(path, cert)
    return Path(path)


class FakeHttp:
    """Stands in for sources.Http: serves bytes by URL and records requests."""

    def __init__(self, pages=None, redirects=None):
        self.pages = dict(pages or {})  # url -> bytes (or a Path to serve)
        self.redirects = dict(redirects or {})  # url -> where it redirects to
        self.requests = []  # (url, headers)

    def _body(self, url):
        if url not in self.pages:
            raise OSError(f"404 {url}")
        body = self.pages[url]
        return Path(body).read_bytes() if isinstance(body, Path) else body

    def get(self, url, headers=None):
        self.requests.append((url, headers or {}))
        return self._body(url)

    def save(self, url, dest, progress=None):
        self.requests.append((url, {}))
        data = self._body(url)
        Path(dest).write_bytes(data)
        if progress:
            progress(len(data), len(data))

    def final_url(self, url):
        self.requests.append((url, {}))
        if url not in self.redirects:
            raise OSError(f"404 {url}")
        return self.redirects[url]
