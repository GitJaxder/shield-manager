from pathlib import Path


class FakeConnection:
    """Stands in for an AdbDeviceTcp with a simulated package manager.

    APK files used with it contain "package:versionCode" as text, so installs can update
    the simulated device. Entries in responses override the reply to any command that
    starts with that prefix.
    """

    def __init__(self, responses=None, installed=None, splits=None):
        self.responses = responses or {}
        self.installed = dict(installed or {})  # package -> (version_code, version_name)
        self.splits = splits or {}  # package -> extra split APK names
        self.commands = []
        self.pushed = []
        self.pulled = []
        self.staged = {}  # remote path -> (package, version_code)
        self.closed = False

    def push(self, local_path, device_path, **kwargs):
        self.pushed.append((local_path, device_path))
        local = Path(local_path)
        text = local.read_bytes().decode(errors="replace") if local.exists() else ""
        if text.count(":") == 1:
            package, code = text.split(":")
            self.staged[device_path] = (package, int(code))

    def pull(self, device_path, local_path, **kwargs):
        self.pulled.append(device_path)
        package = Path(device_path).parent.name.removesuffix("-1")
        Path(local_path).write_text(f"{package}:{self.installed[package][0]}")

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


def make_apk(path, package, version_code):
    """Write a stand-in APK file that FakeConnection understands."""
    Path(path).write_text(f"{package}:{version_code}")
    return Path(path)
