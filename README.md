# shield-manager

Deploy apps, packages, and updates to Nvidia Shield devices over network ADB.

## Status

Early. The aim is a small home MDM: every Shield mirrors the same set of apps. Today it can
register Shields, pick one as the reference, report how the others drift from it, and sync
them to match. It can also install, update, remove and list apps on one device, a group, or
all of them.

## Requirements

- Python 3.10+
- On each Shield: **Settings → Device Preferences → About**, tap **Build** seven times to
  enable developer options, then turn on **Developer options → Network debugging**.

## Install

```sh
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

```sh
shield-manager device add living-room 192.168.1.20
shield-manager device list
shield-manager device info living-room   # accept the debugging prompt on the TV the first time
shield-manager device remove living-room
```

Groups let one command reach several Shields:

```sh
shield-manager device add den 192.168.1.21 --group upstairs
shield-manager device set-groups living-room downstairs   # replace a device's groups
```

Apps (`-d NAME`, `-g GROUP` and `--all` pick targets and can be combined):

```sh
shield-manager app install kodi.apk --all          # installs, or updates if already present
shield-manager app install kodi.apk -g upstairs --allow-downgrade
shield-manager app version org.xbmc.kodi --all     # installed version per device
shield-manager app list -d living-room             # third-party apps (--system for all)
shield-manager app uninstall org.xbmc.kodi -d den
```

After an install the tool checks that each device reports the APK's `versionCode`. Devices are
handled one at a time; an unreachable device is reported and the rest still run, and the
command exits non-zero if any device failed.

The device list and the ADB key live in `~/.config/shield-manager/` (override with
`SHIELD_MANAGER_HOME`).

## Mirroring a reference Shield

Pick the Shield whose apps the others should match, then check and sync:

```sh
shield-manager fleet set-reference living-room
shield-manager fleet status              # exits 1 if any Shield differs
shield-manager fleet sync --dry-run      # what sync would change
shield-manager fleet sync                # copy missing and outdated apps from the reference
```

`sync` pulls each app's APK files (including Play Store split APKs) from the reference once
and installs them on every Shield that needs them. It never removes or downgrades anything
unless asked: `--prune` removes apps the reference doesn't have (this deletes their data) and
`--allow-downgrade` replaces versions newer than the reference's. Use `--from NAME` for a
one-off reference, or `-d`/`-g`/`--all` to limit which Shields are checked.

Apps copied this way are sideloaded, so paid apps that check their Play Store licence may
refuse to run until they are installed from the Play Store on that Shield.

## Development

```sh
ruff check . && ruff format --check .
pytest
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
