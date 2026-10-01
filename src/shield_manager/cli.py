"""Command-line interface for shield-manager."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from shield_manager import __version__
from shield_manager.apk import ApkError, read_apk_info
from shield_manager.registry import (
    DEFAULT_ADB_PORT,
    Device,
    DeviceExistsError,
    DeviceNotFoundError,
    GroupNotFoundError,
    Registry,
)


def _add_target_args(parser: argparse.ArgumentParser) -> None:
    targets = parser.add_argument_group("targets (pick at least one)")
    targets.add_argument(
        "-d", "--device", action="append", default=[], metavar="NAME", help="a device by name"
    )
    targets.add_argument(
        "-g", "--group", action="append", default=[], metavar="GROUP", help="every device in GROUP"
    )
    targets.add_argument("--all", action="store_true", help="every registered device")


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
    add.add_argument("-g", "--group", action="append", default=[], metavar="GROUP")

    device_sub.add_parser("list", help="list registered devices")

    remove = device_sub.add_parser("remove", help="forget a registered device")
    remove.add_argument("name")

    groups = device_sub.add_parser("set-groups", help="replace the groups a device belongs to")
    groups.add_argument("name")
    groups.add_argument("groups", nargs="*", metavar="GROUP")

    info = device_sub.add_parser("info", help="connect to a device and show its properties")
    info.add_argument("name")

    app = sub.add_parser("app", help="install, update, remove and inspect apps")
    app_sub = app.add_subparsers(dest="action", required=True)

    install = app_sub.add_parser("install", help="install an APK, or update it if present")
    install.add_argument("apk")
    install.add_argument(
        "--allow-downgrade", action="store_true", help="allow replacing a newer installed version"
    )
    _add_target_args(install)

    uninstall = app_sub.add_parser("uninstall", help="remove an app by package name")
    uninstall.add_argument("package")
    _add_target_args(uninstall)

    version = app_sub.add_parser("version", help="show the installed version of a package")
    version.add_argument("package")
    _add_target_args(version)

    app_list = app_sub.add_parser("list", help="list installed apps")
    app_list.add_argument("--system", action="store_true", help="include system packages")
    _add_target_args(app_list)

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


def _for_each_device(devices: list[Device], action: Callable[[object], str]) -> int:
    """Run action against each device in turn, printing one result line per device.

    Returns 1 if any device failed, so one unreachable Shield doesn't hide the rest.
    """
    from shield_manager import adb

    failed = 0
    for device in devices:
        try:
            conn = adb.connect(device)
            try:
                print(f"{device.name}: {action(conn)}")
            finally:
                conn.close()
        except Exception as e:  # report per device and keep going
            failed += 1
            print(f"{device.name}: FAILED: {e}", file=sys.stderr)
    if len(devices) > 1:
        print(f"{len(devices) - failed}/{len(devices)} devices succeeded")
    return 1 if failed else 0


def _run_app(args: argparse.Namespace, registry: Registry) -> int:
    from shield_manager import deploy

    if not (args.device or args.group or args.all):
        print("error: pick targets with --device, --group or --all", file=sys.stderr)
        return 2
    devices = registry.resolve(args.device, args.group, args.all)
    if not devices:
        print("error: no devices registered", file=sys.stderr)
        return 1

    if args.action == "install":
        info = read_apk_info(args.apk)
        print(f"{info.package} {info.version_name} (versionCode {info.version_code})")

        def action(conn):
            v = deploy.install(conn, args.apk, info, allow_downgrade=args.allow_downgrade)
            return f"installed {v.version_name} (versionCode {v.version_code})"

    elif args.action == "uninstall":

        def action(conn):
            deploy.uninstall(conn, args.package)
            return f"removed {args.package}"

    elif args.action == "version":

        def action(conn):
            v = deploy.installed_version(conn, args.package)
            return "not installed" if v is None else f"{v.version_name} ({v.version_code})"

    else:  # list

        def action(conn):
            packages = deploy.list_packages(conn, include_system=args.system)
            return f"{len(packages)} packages\n" + "\n".join(f"  {p}" for p in packages)

    return _for_each_device(devices, action)


def main(argv: Sequence[str] | None = None, registry: Registry | None = None) -> int:
    args = build_parser().parse_args(argv)
    registry = registry or Registry()

    try:
        if args.command == "device":
            if args.action == "add":
                registry.add(Device(args.name, args.host, args.port, tuple(args.group)))
                print(f"Added {args.name} ({args.host}:{args.port})")
            elif args.action == "list":
                devices = registry.list()
                if not devices:
                    print(
                        "No devices registered. Add one with: shield-manager device add NAME HOST"
                    )
                for d in devices:
                    groups = f"\t[{', '.join(d.groups)}]" if d.groups else ""
                    print(f"{d.name}\t{d.address}{groups}")
            elif args.action == "remove":
                registry.remove(args.name)
                print(f"Removed {args.name}")
            elif args.action == "set-groups":
                d = registry.set_groups(args.name, args.groups)
                print(f"{d.name} groups: {', '.join(d.groups) or '(none)'}")
            elif args.action == "info":
                return _device_info(registry.get(args.name))
        elif args.command == "app":
            return _run_app(args, registry)
    except DeviceExistsError as e:
        print(f"error: device '{e}' is already registered", file=sys.stderr)
        return 1
    except DeviceNotFoundError as e:
        print(f"error: no device named '{e}'", file=sys.stderr)
        return 1
    except GroupNotFoundError as e:
        print(f"error: no devices in group '{e}'", file=sys.stderr)
        return 1
    except ApkError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
