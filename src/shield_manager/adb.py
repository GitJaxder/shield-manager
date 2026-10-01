"""Thin wrapper around adb-shell for talking to a Shield over network ADB."""

from __future__ import annotations

from pathlib import Path

from adb_shell.adb_device import AdbDeviceTcp
from adb_shell.auth.keygen import keygen
from adb_shell.auth.sign_pythonrsa import PythonRSASigner

from shield_manager.registry import Device, default_config_dir

# The first connection from a new key shows an "Allow USB debugging?" prompt on the
# Shield; this timeout leaves time to accept it on the TV.
AUTH_TIMEOUT_S = 30.0


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
    conn = AdbDeviceTcp(device.host, device.port, default_transport_timeout_s=9.0)
    conn.connect(rsa_keys=[signer or load_signer()], auth_timeout_s=AUTH_TIMEOUT_S)
    return conn


def get_props(conn: AdbDeviceTcp) -> dict[str, str]:
    """Return the device properties most useful for identifying a Shield."""
    keys = {
        "model": "ro.product.model",
        "android": "ro.build.version.release",
        "build": "ro.build.display.id",
        "serial": "ro.serialno",
    }
    return {label: str(conn.shell(f"getprop {prop}")).strip() for label, prop in keys.items()}
