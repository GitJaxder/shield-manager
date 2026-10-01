"""Command-line interface for shield-manager."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from shield_manager import __version__
from shield_manager.registry import (
    DEFAULT_ADB_PORT,
    Device,
    DeviceExistsError,
    DeviceNotFoundError,
    Registry,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shield-manager",
        description="Deploy apps, packages, and updates to Nvidia Shield devices.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    device = sub.add_parser("device", help="manage registered Shield devices")
    device_sub = device.add_subparsers(dest="action", required=True)

    add = device_sub.add_parser("add", help="register a device by network address")
    add.add_argument("name")
    add.add_argument("host")
    add.add_argument("--port", type=int, default=DEFAULT_ADB_PORT)

    device_sub.add_parser("list", help="list registered devices")

    remove = device_sub.add_parser("remove", help="forget a registered device")
    remove.add_argument("name")

    info = device_sub.add_parser("info", help="connect to a device and show its properties")
    info.add_argument("name")

    return parser


def _device_info(device: Device) -> int:
    # Imported lazily so offline commands don't pay for loading the ADB stack.
    from shield_manager import adb

    conn = adb.connect(device)
    try:
        for label, value in adb.get_props(conn).items():
            print(f"{label}: {value}")
    finally:
        conn.close()
    return 0


def main(argv: Sequence[str] | None = None, registry: Registry | None = None) -> int:
    args = build_parser().parse_args(argv)
    registry = registry or Registry()

    try:
        if args.command == "device":
            if args.action == "add":
                registry.add(Device(args.name, args.host, args.port))
                print(f"Added {args.name} ({args.host}:{args.port})")
            elif args.action == "list":
                devices = registry.list()
                if not devices:
                    print(
                        "No devices registered. Add one with: shield-manager device add NAME HOST"
                    )
                for d in devices:
                    print(f"{d.name}\t{d.address}")
            elif args.action == "remove":
                registry.remove(args.name)
                print(f"Removed {args.name}")
            elif args.action == "info":
                return _device_info(registry.get(args.name))
    except DeviceExistsError as e:
        print(f"error: device '{e}' is already registered", file=sys.stderr)
        return 1
    except DeviceNotFoundError as e:
        print(f"error: no device named '{e}'", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
