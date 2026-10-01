import io
import os
import socket
import stat
import threading
from pathlib import Path

import pytest
from adb_shell.auth.keygen import keygen
from adb_shell.transport.tcp_transport import TcpTransport

from shield_manager import adb
from shield_manager.adb import FullWriteTcpTransport, ShieldAdb
from shield_manager.cli import main

PACKET = b"x" * (1024 * 1024)  # the largest ADB packet a file push sends


def _write_to_slow_reader(transport_class):
    """Write one big packet through transport_class to a peer that reads slowly, the way a
    Shield does while it writes the file to storage. Returns (bytes reported, bytes received)."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    received = bytearray()
    ready = threading.Event()

    def read():
        peer, _ = server.accept()
        ready.wait()
        while chunk := peer.recv(65536):
            received.extend(chunk)
        peer.close()

    reader = threading.Thread(target=read)
    reader.start()
    transport = transport_class("127.0.0.1", server.getsockname()[1])
    transport.connect(9.0)
    transport._connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
    threading.Timer(0.2, ready.set).start()
    try:
        reported = transport.bulk_write(PACKET, 9.0)
    finally:
        ready.set()
        transport.close()
        reader.join(5)
        server.close()
    return reported, len(received)


def test_adb_shell_transport_drops_most_of_a_big_write():
    # Documents the adb-shell bug FullWriteTcpTransport works around.
    reported, received = _write_to_slow_reader(TcpTransport)
    assert reported == received < len(PACKET)


def test_full_write_transport_sends_every_byte():
    assert _write_to_slow_reader(FullWriteTcpTransport) == (len(PACKET), len(PACKET))


def test_connections_use_the_full_write_transport():
    conn = ShieldAdb("10.0.0.5", 5555, default_transport_timeout_s=9.0)
    assert isinstance(conn._io_manager._transport, FullWriteTcpTransport)


def test_new_key_is_readable_by_its_owner_only(tmp_path):
    old = os.umask(0o022)
    try:
        adb.load_signer(tmp_path / "keys" / "adbkey")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "keys" / "adbkey").stat().st_mode) == 0o600


def test_existing_readable_key_is_made_private(tmp_path):
    key = tmp_path / "adbkey"
    keygen(str(key))
    key.chmod(0o644)
    adb.load_signer(key)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_import_key_pasted_on_one_line(tmp_path):
    original = tmp_path / "ha" / "androidtv_adbkey"
    original.parent.mkdir()
    keygen(str(original))
    pasted = original.read_text().replace("\n", " ")
    key = adb.import_key(pasted, tmp_path / "data" / "adbkey")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    pub = Path(f"{key}.pub").read_text()
    assert pub.split()[0] == Path(f"{original}.pub").read_text().split()[0]
    adb.load_signer(key)  # the pair loads as a signer


def test_import_key_rejects_junk_and_keeps_the_current_key(tmp_path):
    key = tmp_path / "adbkey"
    keygen(str(key))
    before = key.read_text()
    with pytest.raises(ValueError):
        adb.import_key("hello", key)
    with pytest.raises(ValueError):
        adb.import_key("-----BEGIN PRIVATE KEY-----QUJD-----END PRIVATE KEY-----", key)
    assert key.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["adbkey", "adbkey.pub"]


def test_cli_key_import(tmp_path, monkeypatch, capsys):
    source = tmp_path / "androidtv_adbkey"
    keygen(str(source))
    monkeypatch.setenv("SHIELD_MANAGER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("sys.stdin", io.StringIO(source.read_text()))
    assert main(["key", "import"]) == 0
    assert (tmp_path / "home" / "adbkey").read_text() == source.read_text()
    monkeypatch.setattr("sys.stdin", io.StringIO("nope"))
    assert main(["key", "import"]) == 1
    assert "isn't an ADB private key" in capsys.readouterr().err
