"""Thin wrapper around adb-shell for talking to a Shield over network ADB."""

from __future__ import annotations

import base64
import binascii
import os
import re
from pathlib import Path

from adb_shell.adb_device import AdbDevice, AdbDeviceTcp
from adb_shell.auth.keygen import keygen, write_public_keyfile
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


_PEM = re.compile(r"-----BEGIN ((?:RSA )?PRIVATE KEY)-----(.*?)-----END \1-----", re.DOTALL)


def default_key_path() -> Path:
    return default_config_dir() / "adbkey"


def _keep_private(key_path: Path) -> None:
    """Make the private key readable by its owner only: anyone who can read it controls
    every Shield that trusts it."""
    if key_path.stat().st_mode & 0o077:
        key_path.chmod(0o600)


def load_signer(key_path: Path | None = None) -> PythonRSASigner:
    """Load the ADB key pair, generating one on first use."""
    key_path = key_path or default_key_path()
    if not key_path.exists():
        key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        keygen(str(key_path))
    _keep_private(key_path)
    return PythonRSASigner(
        Path(f"{key_path}.pub").read_text(),
        key_path.read_text(),
    )


def import_key(text: str, key_path: Path | None = None) -> Path:
    """Use an existing ADB private key, such as Home Assistant's Android TV integration's,
    so Shields that already trust it don't ask again. text is the key in PEM form; its line
    breaks may be missing, as when it's pasted into a one-line field. Replaces the current
    key pair and writes the matching public key. Raises ValueError for anything else."""
    key_path = key_path or default_key_path()
    match = _PEM.search(text)
    if not match:
        raise ValueError(
            "that isn't an ADB private key (it starts with -----BEGIN PRIVATE KEY-----)"
        )
    kind, body = match.group(1), "".join(match.group(2).split())
    try:
        base64.b64decode(body, validate=True)
    except binascii.Error as e:
        raise ValueError("the private key is damaged; copy all of it again") from e
    lines = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    pem = f"-----BEGIN {kind}-----\n{lines}\n-----END {kind}-----\n"

    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    new_key, new_pub = Path(f"{key_path}.new"), Path(f"{key_path}.pub.new")
    fd = os.open(new_key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pem)
    try:
        write_public_keyfile(str(new_key), str(new_pub))
    except Exception as e:  # not an RSA key the cryptography library can read
        new_key.unlink()
        new_pub.unlink(missing_ok=True)
        raise ValueError(f"that private key can't be used for ADB: {e}") from e
    new_key.replace(key_path)
    new_pub.replace(Path(f"{key_path}.pub"))
    return key_path


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
