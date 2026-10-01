import socket
import threading

from adb_shell.transport.tcp_transport import TcpTransport

from shield_manager.adb import FullWriteTcpTransport, ShieldAdb

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
