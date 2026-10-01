# shield-manager

Deploy apps, packages, and updates to Nvidia Shield devices over network ADB.

## Status

Early. The aim is a small home MDM: every Shield mirrors the same set of apps. Today it can
register Shields, pick one as the reference, report how the others drift from it, and sync
them to match. It can also install, update, remove and list apps on one device, a group, or
all of them.

## Requirements

- A computer on the same home network as your Shields (Windows, macOS or Linux).
- Python 3.10 or newer. Check by running `python3 --version` (on Windows, `py --version`).
  If it's missing or older, install it from <https://www.python.org/downloads/>. On Windows,
  tick **Add python.exe to PATH** in the installer.
- On each Shield, turn on network debugging:
  1. Open **Settings → Device Preferences → About**.
  2. Select **Build** seven times, until it says you are a developer.
  3. Go back to **Device Preferences → Developer options** and turn on **Network debugging**.
  4. Note the Shield's IP address. Network debugging shows it once it's on, and it's also under
     **Settings → Network & Internet** when you select your network.

## Install

There is no released package yet, so you install shield-manager from this repository's source.

1. Get the source code. Use either option:
   - **Download:** on <https://github.com/GitJaxder/shield-manager>, click **Code → Download
     ZIP**. Unzip it. You'll get a folder called `shield-manager-main`.
   - **Git:** run `git clone https://github.com/GitJaxder/shield-manager.git`. You'll get a
     folder called `shield-manager`.

2. Open a terminal (on Windows, PowerShell) in that folder. For example:

   ```sh
   cd ~/Downloads/shield-manager-main
   ```

3. Create a private Python environment for the tool and switch to it.

   macOS / Linux:

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   Windows (PowerShell):

   ```powershell
   py -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

   If PowerShell says running scripts is disabled, run
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then try again.

4. Install shield-manager and its dependencies:

   ```sh
   pip install .
   ```

5. Check that it works:

   ```sh
   shield-manager --version
   ```

   This should print `shield-manager 0.1.0`.

The `shield-manager` command only works while the environment from step 3 is active. In a new
terminal, `cd` back into the folder and run the `activate` line from step 3 again.

**Updating:** download the ZIP again (or run `git pull` in the folder). Then, with the
environment active, run `pip install .` again. Your devices and settings are kept, because
they live in `~/.config/shield-manager/`, not in the source folder.

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

## Web UI

```sh
shield-manager web            # then open http://127.0.0.1:8765/
```

The **Apps** tab is an app-store-style screen listing every app on any of your Shields, with
its name and icon (read from the Shield), search, and filters for apps that are missing or
out of date somewhere. Tap an app to install or update it on a Shield; it's copied from a
Shield that already has the newest version. Tick the checkbox on several apps to **Sync** them
(install or update them on every Shield that's missing or behind) or **Remove** them from the
Shields you pick, all at once. A banner offers **Sync now** when Shields have
fallen behind the reference. Sync never removes apps. The **Shields** tab adds, groups and
removes Shields, picks the reference, and installs an APK file you upload.

Installs run in the background, so closing the page doesn't stop them. App names and icons
are cached in `~/.config/shield-manager/app-cache/`.

The page uses only relative URLs, so it works behind a path prefix such as Home Assistant's
ingress. It listens on localhost by default and has no login: `--host 0.0.0.0` exposes it to
your network, and `--allow-from IP` limits it to one client, such as a reverse proxy.

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

## Tab completion

Press Tab to complete commands, device names, group names and app package names. For example,
`shield-manager app uninstall org.x` then Tab becomes `shield-manager app uninstall org.xbmc.kodi`.

Set it up once, in a terminal where the environment from install step 3 is active. Use the
steps for your shell. If you're not sure which shell you have, run `echo $SHELL`. On Windows
it's PowerShell, and on a Mac it's usually zsh.

**bash:**

```sh
shield-manager completion bash > ~/.shield-manager-completion.bash
echo 'source ~/.shield-manager-completion.bash' >> ~/.bashrc
```

**zsh:**

```sh
shield-manager completion zsh > ~/.shield-manager-completion.zsh
echo 'autoload -U compinit && compinit' >> ~/.zshrc
echo 'source ~/.shield-manager-completion.zsh' >> ~/.zshrc
```

If your `~/.zshrc` already runs `compinit` (Oh My Zsh does), skip the middle line.

**fish:**

```sh
mkdir -p ~/.config/fish/completions
shield-manager completion fish > ~/.config/fish/completions/shield-manager.fish
```

**PowerShell:**

```powershell
shield-manager completion powershell | Out-File -Encoding utf8 $HOME\shield-manager-completion.ps1
if (!(Test-Path $PROFILE)) { New-Item -Type File -Force $PROFILE }
Add-Content $PROFILE '. $HOME\shield-manager-completion.ps1'
```

Then open a new terminal and activate the environment again. Completion works whenever the
`shield-manager` command does.

App names are completed from the apps shield-manager has already seen on your Shields, so
completing never has to wait for a device. To fill the list, run this once (and again
whenever you've installed new apps):

```sh
shield-manager app list --all
```

`fleet status` and `fleet sync` also add to the list. After updating shield-manager, run the
first command for your shell again.

## Development

Follow the install steps, but in step 4 run `pip install -e ".[dev]"` instead, so code changes
take effect without reinstalling and the test tools are installed. Then:

```sh
ruff check . && ruff format --check .
pytest
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
