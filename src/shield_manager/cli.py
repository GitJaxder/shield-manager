# PYTHON_ARGCOMPLETE_OK
"""Command-line interface for shield-manager."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

import argcomplete

from shield_manager import __version__
from shield_manager.apk import ApkError, read_apk_info
from shield_manager.completion import (
    SHELLS,
    complete_devices,
    complete_groups,
    complete_packages,
    remember_packages,
    shell_script,
)
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
    ).completer = complete_devices
    targets.add_argument(
        "-g", "--group", action="append", default=[], metavar="GROUP", help="every device in GROUP"
    ).completer = complete_groups
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
    add.add_argument(
        "-g", "--group", action="append", default=[], metavar="GROUP"
    ).completer = complete_groups

    device_sub.add_parser("list", help="list registered devices")

    remove = device_sub.add_parser("remove", help="forget a registered device")
    remove.add_argument("name").completer = complete_devices

    groups = device_sub.add_parser("set-groups", help="replace the groups a device belongs to")
    groups.add_argument("name").completer = complete_devices
    groups.add_argument("groups", nargs="*", metavar="GROUP").completer = complete_groups

    info = device_sub.add_parser("info", help="connect to a device and show its properties")
    info.add_argument("name").completer = complete_devices

    app = sub.add_parser("app", help="install, update, remove and inspect apps")
    app_sub = app.add_subparsers(dest="action", required=True)

    install = app_sub.add_parser("install", help="install an APK, or update it if present")
    install.add_argument("apk")
    install.add_argument(
        "--allow-downgrade", action="store_true", help="allow replacing a newer installed version"
    )
    _add_target_args(install)

    uninstall = app_sub.add_parser("uninstall", help="remove an app by package name")
    uninstall.add_argument("package").completer = complete_packages
    _add_target_args(uninstall)

    version = app_sub.add_parser("version", help="show the installed version of a package")
    version.add_argument("package").completer = complete_packages
    _add_target_args(version)

    app_list = app_sub.add_parser("list", help="list installed apps")
    app_list.add_argument("--system", action="store_true", help="include system packages")
    _add_target_args(app_list)

    fleet = sub.add_parser("fleet", help="keep every Shield's apps matching a reference Shield")
    fleet_sub = fleet.add_subparsers(dest="action", required=True)

    ref = fleet_sub.add_parser("set-reference", help="choose the Shield the others mirror")
    ref.add_argument("name").completer = complete_devices

    fleet_status = fleet_sub.add_parser(
        "status", help="show how each Shield differs from the reference (targets default to all)"
    )
    fleet_status.add_argument(
        "--from", dest="source", metavar="NAME", help="override the reference"
    ).completer = complete_devices
    _add_target_args(fleet_status)

    fleet_sync = fleet_sub.add_parser(
        "sync", help="copy missing and outdated apps from the reference (targets default to all)"
    )
    fleet_sync.add_argument(
        "--from", dest="source", metavar="NAME", help="override the reference"
    ).completer = complete_devices
    fleet_sync.add_argument(
        "--prune", action="store_true", help="also remove apps the reference doesn't have"
    )
    fleet_sync.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="also downgrade apps that are newer than on the reference",
    )
    fleet_sync.add_argument(
        "--dry-run", action="store_true", help="show what would change without changing it"
    )
    _add_target_args(fleet_sync)

    completion = sub.add_parser(
        "completion", help="print the Tab-completion script for your shell (see README)"
    )
    completion.add_argument("shell", choices=SHELLS)

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


class _RecordingConnection:
    """Passes everything through to a device connection, noting app names it lists.

    The names feed Tab completion, so it never has to connect to a Shield itself.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def shell(self, command: str, **kwargs):
        out = self._conn.shell(command, **kwargs)
        if command.startswith("pm list packages"):
            remember_packages(
                line.split()[0].removeprefix("package:")
                for line in str(out).splitlines()
                if line.startswith("package:")
            )
        return out


def _connect(device: Device) -> _RecordingConnection:
    from shield_manager import adb

    return _RecordingConnection(adb.connect(device))


def _for_each_device(devices: list[Device], action: Callable[[object], str]) -> int:
    """Run action against each device in turn, printing one result line per device.

    Returns 1 if any device failed, so one unreachable Shield doesn't hide the rest.
    """
    failed = 0
    for device in devices:
        try:
            conn = _connect(device)
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
            v = deploy.install(
                conn,
                args.apk,
                info.package,
                info.version_code,
                allow_downgrade=args.allow_downgrade,
            )
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


_DRIFT_LABELS = {
    "install": "missing",
    "update": "outdated",
    "newer": "newer than reference",
    "extra": "not on reference",
}


def _describe(drift) -> str:
    label = _DRIFT_LABELS[drift.change.value]
    if drift.change.value in ("update", "newer"):
        return f"{drift.package}: {label} ({drift.device_version} vs {drift.reference_version})"
    return f"{drift.package}: {label}"


def _run_fleet(args: argparse.Namespace, registry: Registry) -> int:
    from shield_manager import fleet

    if args.action == "set-reference":
        registry.set_reference(args.name)
        print(f"{args.name} is now the reference; other Shields will mirror its apps")
        return 0

    source = args.source or registry.reference
    if not source:
        print(
            "error: no reference Shield; choose one with: shield-manager fleet set-reference NAME",
            file=sys.stderr,
        )
        return 2
    reference = registry.get(source)
    picked = args.device or args.group or args.all
    targets = registry.resolve(args.device, args.group, args.all) if picked else registry.list()

    try:
        if args.action == "status":
            reports = fleet.status(reference, targets, _connect)
        else:
            reports = fleet.sync(
                reference,
                targets,
                _connect,
                prune=args.prune,
                allow_downgrade=args.allow_downgrade,
                dry_run=args.dry_run,
            )
    except Exception as e:  # target failures are caught per device; this is the reference
        print(f"error: can't read apps from reference {reference.name}: {e}", file=sys.stderr)
        return 1

    print(f"Reference: {reference.name}")
    if not reports:
        print("No other Shields to compare. Add one with: shield-manager device add NAME HOST")
    problems = 0
    for report in reports:
        if report.error:
            problems += 1
            print(f"{report.device.name}: FAILED: {report.error}", file=sys.stderr)
            continue
        if args.action == "status" or args.dry_run:
            if report.in_sync:
                print(f"{report.device.name}: in sync")
                continue
            problems += 1
            print(f"{report.device.name}: {len(report.drift)} differences")
            for d in report.drift:
                print(f"  {_describe(d)}")
            continue
        if not report.drift:
            outcome = "already in sync"
        elif report.applied or report.failed:
            outcome = "synced"
        else:
            outcome = "no changes made"
        print(f"{report.device.name}: {outcome}")
        for package, outcome in report.applied.items():
            print(f"  {package}: {outcome}")
        for package, error in report.failed.items():
            problems += 1
            print(f"  {package}: FAILED: {error}", file=sys.stderr)
        for d in report.drift:
            if d.package in report.applied or d.package in report.failed:
                continue
            hint = "--allow-downgrade" if d.change.value == "newer" else "--prune"
            print(f"  {_describe(d)}, left as is (use {hint})")
    return 1 if problems else 0


def main(argv: Sequence[str] | None = None, registry: Registry | None = None) -> int:
    parser = build_parser()
    argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)
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
        elif args.command == "fleet":
            return _run_fleet(args, registry)
        elif args.command == "completion":
            print(shell_script(args.shell))
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
