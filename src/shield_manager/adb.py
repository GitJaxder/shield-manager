"""Thin wrapper around adb-shell for talking to a Shield over network ADB."""

from __future__ import annotations

from pathlib import Path

from adb_shell.adb_device import AdbDevice, AdbDeviceTcp
from adb_shell.auth.keygen import keygen
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from adb_shell.transport.tcp_transport import TcpTransport

from shield_manager.registry import Device, default_config_dir

# The first connection from a new key shows an "Allow USB debugging?" prompt on the
# Shield; this timeout leaves time to accept it on the TV.
AUTH_TIMEOUT_S = 30.0


class FullWriteTcpTransport(TcpTransport):
    """A TcpTransport whose writes always send every byte.

    adb-shell's bulk_write calls send() once on a non-blocking socket and drops whatever
    didn't fit in the socket buffer. Shell commands are small enough not to notice, but a
    file push sends up to 1 MB per ADB packet, so most of it was lost and the Shield sat
    waiting for the rest: the copy froze after its first packet.
    """

    def bulk_write(self, data, transport_timeout_s):
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            sent += super().bulk_write(view[sent:], transport_timeout_s)
        return sent


class ShieldAdb(AdbDeviceTcp):
    """AdbDeviceTcp over a FullWriteTcpTransport."""

    def __init__(self, host: str, port: int, default_transport_timeout_s: float | None = None):
        AdbDevice.__init__(self, FullWriteTcpTransport(host, port), default_transport_timeout_s)


def load_signer(key_path: Path | None = None) -> PythonRSASigner:
    """Load the ADB key pair, generating one on first use."""
    key_path = key_path or default_config_dir() / "adbkey"
    if not key_path.exists():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        keygen(str(key_path))
    return PythonRSASigner(
        Path(f"{key_path}.pub").read_text(),
        key_path.read_text(),
    )


def connect(device: Device, signer: PythonRSASigner | None = None) -> AdbDeviceTcp:
    conn = ShieldAdb(device.host, device.port, default_transport_timeout_s=9.0)
    conn.connect(rsa_keys=[signer or load_signer()], auth_timeout_s=AUTH_TIMEOUT_S)
    return conn


def get_props(conn: AdbDeviceTcp) -> dict[str, str]:
    """Return the device properties most useful for identifying a Shield."""
    keys = {
        "model": "ro.product.model",
        "android": "ro.build.version.release",
        "build": "ro.build.display.id",
        "cpu": "ro.product.cpu.abilist",
        "serial": "ro.serialno",
    }
    return {label: str(conn.shell(f"getprop {prop}")).strip() for label, prop in keys.items()}
