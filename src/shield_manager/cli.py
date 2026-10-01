"""Command-line interface for shield-manager."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace

from shield_manager import __version__
from shield_manager.apk import ApkError
from shield_manager.bundle import app_info
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

    store = app_sub.add_parser(
        "store-page", help="open an app's Play Store page on the Shield, ready to press Install"
    )
    store.add_argument("package")
    _add_target_args(store)

    page = app_sub.add_parser(
        "download-page",
        help="print a link to the download page for the build of an app each Shield can run",
    )
    page.add_argument("package")
    page.add_argument(
        "--version",
        metavar="NAME",
        help="the version name to look for (default: the one installed, else the newest)",
    )
    _add_target_args(page)

    app_list = app_sub.add_parser("list", help="list installed apps")
    app_list.add_argument("--system", action="store_true", help="include system packages")
    _add_target_args(app_list)

    fleet = sub.add_parser("fleet", help="keep every Shield's apps matching a reference Shield")
    fleet_sub = fleet.add_subparsers(dest="action", required=True)

    ref = fleet_sub.add_parser("set-reference", help="choose the Shield the others mirror")
    ref.add_argument("name")

    fleet_status = fleet_sub.add_parser(
        "status", help="show how each Shield differs from the reference (targets default to all)"
    )
    fleet_status.add_argument(
        "--from", dest="source", metavar="NAME", help="override the reference"
    )
    _add_target_args(fleet_status)

    fleet_sync = fleet_sub.add_parser(
        "sync", help="copy missing and outdated apps from the reference (targets default to all)"
    )
    fleet_sync.add_argument("--from", dest="source", metavar="NAME", help="override the reference")
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
    fleet_sync.add_argument(
        "--no-download",
        action="store_true",
        help="only copy between Shields; never download apps from GitHub",
    )
    _add_target_args(fleet_sync)

    updates = fleet_sub.add_parser(
        "updates",
        help="check GitHub for newer versions of open-source apps (targets default to all)",
    )
    updates.add_argument(
        "--install", action="store_true", help="install the updates found on every Shield"
    )
    _add_target_args(updates)

    source = sub.add_parser(
        "source", help="choose GitHub repositories to download apps and updates from"
    )
    source_sub = source.add_subparsers(dest="action", required=True)
    source_set = source_sub.add_parser(
        "set", help="download an app from a GitHub repository's releases"
    )
    source_set.add_argument("package", help="the app's package name, e.g. org.xbmc.kodi")
    source_set.add_argument("repo", help="owner/name, or the repository's GitHub URL")
    source_set.add_argument(
        "--asset",
        metavar="PATTERN",
        help="only use release files whose names match this regular expression "
        "(for repositories that publish several apps or variants)",
    )
    source_sub.add_parser("list", help="list the apps with a GitHub source")
    source_remove = source_sub.add_parser("remove", help="stop downloading an app from GitHub")
    source_remove.add_argument("package")

    web = sub.add_parser("web", help="serve the web UI")
    web.add_argument("--host", default="127.0.0.1", help="address to bind (default: localhost)")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument(
        "--allow-from",
        action="append",
        default=[],
        metavar="IP",
        help="only accept connections from this IP (repeatable), e.g. a reverse proxy",
    )

    return parser


def _serve_web(args: argparse.Namespace, registry: Registry) -> int:
    from shield_manager.web import create_server

    server = create_server(
        registry, args.host, args.port, verbose=True, allowed_clients=args.allow_from
    )
    host = f"[{args.host}]" if ":" in args.host else args.host
    print(f"Shield Manager UI on http://{host}:{server.server_port}/ (Ctrl+C to stop)")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_from:
        print("warning: anyone who can reach this address can install apps on your Shields")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


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


class _ProgressLine:
    """Shows deploy progress on stderr, e.g. "Copying org.xbmc.kodi to den - 40%".

    On a terminal it's one line rewritten in place; otherwise (logs, pipes) each phase
    is printed once, without the percentage steps.
    """

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stderr
        self.live = self.stream.isatty()
        self.device = ""
        self.width = 0
        self.last = None

    def __call__(self, event) -> None:
        from shield_manager.deploy import Phase

        who = event.device or self.device
        text = replace(event, device=who or None).describe()
        if self.live:
            self.stream.write("\r" + text.ljust(self.width))
            self.stream.flush()
            self.width = len(text)
            return
        key = (who, event.package, event.phase)
        if event.phase is not Phase.DONE and key != self.last:
            print(text, file=self.stream)
            self.last = key

    def clear(self) -> None:
        """Erase the live line so the next printed result starts on a clean line."""
        if self.live and self.width:
            self.stream.write("\r" + " " * self.width + "\r")
            self.stream.flush()
            self.width = 0


def _for_each_device(
    devices: list[Device],
    action: Callable[[object], str],
    progress: _ProgressLine | None = None,
) -> int:
    """Run action against each device in turn, printing one result line per device.

    Returns 1 if any device failed, so one unreachable Shield doesn't hide the rest.
    """
    from shield_manager import adb

    failed = 0
    for device in devices:
        if progress:
            progress.device = device.name
        try:
            conn = adb.connect(device)
            try:
                result = action(conn)
            finally:
                conn.close()
                if progress:
                    progress.clear()
            print(f"{device.name}: {result}")
        except Exception as e:  # report per device and keep going
            failed += 1
            print(f"{device.name}: FAILED: {e}", file=sys.stderr)
    if len(devices) > 1:
        print(f"{len(devices) - failed}/{len(devices)} devices succeeded")
    return 1 if failed else 0


def _run_app(args: argparse.Namespace, registry: Registry) -> int:
    from shield_manager import deploy

    progress = _ProgressLine()

    if not (args.device or args.group or args.all):
        print("error: pick targets with --device, --group or --all", file=sys.stderr)
        return 2
    devices = registry.resolve(args.device, args.group, args.all)
    if not devices:
        print("error: no devices registered", file=sys.stderr)
        return 1

    if args.action == "install":
        info = app_info(args.apk)
        print(f"{info.package} {info.version_name} (versionCode {info.version_code})")

        def action(conn):
            v = deploy.install(
                conn,
                args.apk,
                info.package,
                info.version_code,
                allow_downgrade=args.allow_downgrade,
                progress=progress,
            )
            return f"installed {v.version_name} (versionCode {v.version_code})"

    elif args.action == "uninstall":

        def action(conn):
            deploy.uninstall(conn, args.package, progress=progress)
            return f"removed {args.package}"

    elif args.action == "version":

        def action(conn):
            v = deploy.installed_version(conn, args.package)
            return "not installed" if v is None else f"{v.version_name} ({v.version_code})"

    elif args.action == "store-page":

        def action(conn):
            deploy.open_store_page(conn, args.package)
            return "Play Store page open on the TV; press Install with the remote"

    elif args.action == "download-page":
        from shield_manager.sources import Downloader

        downloads = Downloader.from_config(registry.path.parent)

        def action(conn):
            version = args.version
            if not version:
                installed = deploy.installed_version(conn, args.package)
                version = installed.version_name if installed else None
            abi = (deploy.device_abis(conn) or ["armeabi-v7a"])[0]
            page = downloads.download_page(args.package, version, abi)
            return (
                f"{version or 'newest'} for {abi}: {page}\n"
                "  Download the APK there, then install it with "
                "`shield-manager app install FILE -d <shield>`"
            )

    else:  # list

        def action(conn):
            packages = deploy.list_packages(conn, include_system=args.system)
            return f"{len(packages)} packages\n" + "\n".join(f"  {p}" for p in packages)

    return _for_each_device(devices, action, progress)


def _run_updates(args: argparse.Namespace, registry: Registry) -> int:
    from shield_manager import adb, fleet
    from shield_manager.sources import Downloader

    picked = args.device or args.group or args.all
    devices = registry.resolve(args.device, args.group, args.all) if picked else registry.list()
    if not devices:
        print("error: no devices registered", file=sys.stderr)
        return 1
    downloads = Downloader.from_config(registry.path.parent)
    updates, errors = fleet.check_updates(devices, adb.connect, downloads)
    for name, error in errors.items():
        print(f"{name}: FAILED: {error}", file=sys.stderr)
    if not updates:
        print("Every app is up to date" + (" on the Shields that answered" if errors else ""))
        return 1 if errors else 0
    for u in updates:
        print(
            f"{u.package}: {u.installed_name} -> {u.latest_name} ({u.source}) on "
            + ", ".join(u.shields)
        )
    if not args.install:
        print("Install them with: shield-manager fleet updates --install")
        return 0
    progress = _ProgressLine()
    fleet.apply_updates(updates, devices, adb.connect, downloads, progress=progress)
    progress.clear()
    failed = 0
    for u in updates:
        for name, outcome in u.applied.items():
            print(f"{name}: {u.package} {outcome}")
        for name, error in u.failed.items():
            failed += 1
            print(f"{name}: {u.package} FAILED: {error}", file=sys.stderr)
    return 1 if failed or errors else 0


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


def _run_source(args: argparse.Namespace, registry: Registry) -> int:
    from shield_manager.sources import Downloader

    downloads = Downloader.from_config(registry.path.parent)
    if args.action == "set":
        try:
            src = downloads.set_github_source(args.package, args.repo, args.asset)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        only = f" (files matching {src.asset})" if src.asset else ""
        print(f"{args.package}: GitHub releases of {src.repo}{only}")
        print(
            "Sync uses it when no Shield has a copy a Shield can run, and "
            "`shield-manager fleet updates` checks it for newer versions."
        )
    elif args.action == "remove":
        if not downloads.remove_github_source(args.package):
            print(f"error: {args.package} has no GitHub source", file=sys.stderr)
            return 1
        print(f"{args.package}: no longer downloaded from GitHub")
    else:  # list
        for pkg, src in downloads.github_sources().items():
            notes = [f"files matching {src.asset}"] if src.asset else []
            if src.builtin:
                notes.append("built in")
            extra = f" ({', '.join(notes)})" if notes else ""
            print(f"{pkg}\thttps://github.com/{src.repo}{extra}")
    return 0


def _run_fleet(args: argparse.Namespace, registry: Registry) -> int:
    from shield_manager import adb, fleet
    from shield_manager.sources import Downloader

    if args.action == "set-reference":
        registry.set_reference(args.name)
        print(f"{args.name} is now the reference; other Shields will mirror its apps")
        return 0

    if args.action == "updates":
        return _run_updates(args, registry)

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

    progress = _ProgressLine()
    try:
        if args.action == "status":
            reports = fleet.status(reference, targets, adb.connect)
        else:
            reports = fleet.sync(
                reference,
                targets,
                adb.connect,
                prune=args.prune,
                allow_downgrade=args.allow_downgrade,
                dry_run=args.dry_run,
                progress=progress,
                downloads=None
                if args.no_download
                else Downloader.from_config(registry.path.parent),
            )
            progress.clear()
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
        elif args.command == "fleet":
            return _run_fleet(args, registry)
        elif args.command == "source":
            return _run_source(args, registry)
        elif args.command == "web":
            return _serve_web(args, registry)
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
