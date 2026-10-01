class FakeConnection:
    """Stands in for an AdbDeviceTcp, answering shell commands from a script."""

    def __init__(self, responses=None, installed=None):
        self.responses = responses or {}
        self.installed = dict(installed or {})  # package -> (version_code, version_name)
        self.commands = []
        self.pushed = []
        self.closed = False

    def push(self, local_path, device_path, **kwargs):
        self.pushed.append((local_path, device_path))

    def shell(self, command, **kwargs):
        self.commands.append(command)
        for prefix, response in self.responses.items():
            if command.startswith(prefix):
                return response
        if command.startswith("dumpsys package "):
            package = command.split()[-1]
            if package in self.installed:
                code, name = self.installed[package]
                return (
                    f"Packages:\n  Package [{package}] (abc):\n"
                    f"    versionCode={code} minSdk=21 targetSdk=33\n"
                    f"    versionName={name}\n"
                )
            return "Dexopt state:\n"
        return ""

    def close(self):
        self.closed = True
