# shield-manager

Deploy apps, packages, and updates to Nvidia Shield devices over network ADB.

## Status

Early. It can register Shields, organise them into groups, and install, update, remove and
list apps on one device, a group, or all of them. A package catalog is next.

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

## Development

```sh
ruff check . && ruff format --check .
pytest
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
