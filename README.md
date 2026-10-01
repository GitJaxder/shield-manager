# shield-manager

Deploy apps, packages, and updates to Nvidia Shield devices over network ADB.

## Status

Early scaffold. Today it can register Shields and read their properties; app installs,
group deploys, and a package catalog are next.

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

The device list and the ADB key live in `~/.config/shield-manager/` (override with
`SHIELD_MANAGER_HOME`).

## Development

```sh
ruff check . && ruff format --check .
pytest
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
