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
shield-manager app install steamlink.apkm -d bedroom   # APK bundles work too
shield-manager app install kodi.apk -g upstairs --allow-downgrade
shield-manager app version org.xbmc.kodi --all     # installed version per device
shield-manager app list -d living-room             # third-party apps (--system for all)
shield-manager app uninstall org.xbmc.kodi -d den
```

While it works, installs, removals and `fleet sync` show live progress such as
`den: Downloading org.xbmc.kodi - 40%`, then `Copying`, then `Installing`.

`app install` takes an APK or an APK bundle: APKMirror's `.apkm`, `.xapk`, or bundletool's
`.apks`. From a bundle, each Shield gets the base APK plus the split for its own CPU type
(and the screen and language splits), so one file works on every Shield model. Bundles from
APKMirror that are encrypted (older uploads) can't be installed; download another version.

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
fallen behind the reference. Sync never removes apps. The page also checks GitHub for
newer versions of open-source apps when it opens (at most every 6 hours, or when you press **Check
again**); apps with one show "Update available: 1.0 → 1.1" and appear under **Updates**. Press **Update all** in the banner, or tick apps and press **Update**, to install them as
`fleet updates --install` does (see [Checking for updates](#checking-for-updates)). The **Shields** tab adds, groups and
removes Shields, picks the reference, checks a connection, lists each Shield's apps (system
apps too, if you tick the box), and installs an APK or APK bundle (.apkm, .xapk, .apks) you
upload.

**Sync now** opens a preview of what each Shield would get from the reference, like `fleet
status`. Tick the Shields to sync and, as with `fleet sync`'s options, choose whether to also
downgrade apps that are newer than on the reference, remove apps the reference doesn't have,
or download from GitHub. An app's page has **Get it another way**: open its Play Store page
on a Shield's TV (`app store-page`) or find a download page for that Shield's CPU type
(`app download-page`). Its **GitHub source** section sets, edits or removes the repository
the app's releases come from (owner/name or the repo's URL, plus an optional file-name
pattern), like `source set` and `source remove`. The update check re-runs right after. Cards
name the repo, and the **From GitHub** filter lists every app with a source (`source list`).

Installs run in the background, so closing the page doesn't stop them. While they run, a panel
at the bottom shows each app on each Shield moving through Downloading, Copying and Installing,
with a percentage where the transfer reports one. App names and icons
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

### Mixed Shield models

Different Shield models run different CPU types, and the Play Store gives each Shield only
the version built for its own. So for each Shield, `sync` (and the web UI) works down this
list and stops at the first step that gives it a copy that Shield can run:

1. **Your Shields:** a copy from another Shield, trying Shields of the same model first.
2. **GitHub:** a release, for open-source apps it knows (SmartTube, the Home Assistant app).
   It's only installed when it's signed by the same developer as the copy already on your
   Shields, so a tampered download is refused.
3. **APKMirror:** a link to the page for the right version and CPU type, found the way
   [Morphe](https://github.com/MorpheApp/morphe-manager) does. Morphe's server occasionally
   finds it on Uptodown, APKPure or APKCombo instead.
4. **Google search:** when there's no such page, a link to a web search on those sites.
5. **Play Store:** a command that opens the app's page on that Shield, where you press
   **Install** with the remote.

Steps 1 and 2 happen automatically. If neither works, the error lists what each step found,
for example:

```text
com.valvesoftware.steamlink: FAILED: no copy bedroom can run.
    1. Your Shields (this one runs armeabi-v7a, armeabi): the copies on den are built for a different CPU type
    2. GitHub: it isn't an open-source app it knows the repository of
    3. APKMirror: download it from https://www.apkmirror.com/apk/..., then install the file with `shield-manager app install FILE -d bedroom`
    4. Play Store: `shield-manager app store-page com.valvesoftware.steamlink -d bedroom` opens it
```

To use a download link:

1. Open it on your computer.
2. Download the file. On APKMirror, scroll to the download section and press
   **DOWNLOAD APK**, or **DOWNLOAD APK BUNDLE** for apps that come as a bundle (an `.apkm`
   file). Either works.
3. Install it on the Shield:

   ```sh
   shield-manager app install ~/Downloads/the-file.apkm -d bedroom
   ```

   Or upload it on the web UI's **Shields** tab.

Files you download yourself aren't checked against your Shields' copy, so stick to the page
the link opens. You can also get the link without syncing:

```sh
shield-manager app download-page com.valvesoftware.steamlink -d bedroom
```

Add `--no-download` to `sync` to skip steps 2 to 4.

### Checking for updates

```sh
shield-manager fleet updates             # list open-source apps with a newer version on GitHub
shield-manager fleet updates --install   # install those updates on every Shield
```

This compares each installed open-source app it knows with the newest release on GitHub.
Other apps can't be checked yet. Updates
are installed on every Shield that has the app, with the build for its CPU type, and only
when the download is signed by the same developer as the installed copy.

### Choosing GitHub sources

Like Obtainium, you can tell shield-manager which GitHub repository publishes an app. Sync
then downloads it from there when no Shield has a copy a Shield can run, and
`fleet updates` checks it for newer versions. SmartTube and the Home Assistant app are
built in.

1. Find the app's package name. It's in `shield-manager app list -d living-room`, or in the
   app's Play Store web address after `id=`.
2. Find the repository that publishes the app's APK files under **Releases**, for example
   <https://github.com/yuliskov/SmartTube>.
3. Set it:

   ```sh
   shield-manager source set org.example.app https://github.com/example/app
   ```

   If the repository publishes several apps or variants, add `--asset` with a pattern the
   right files' names match, for example `--asset tv`. The pattern is a regular expression
   and ignores upper and lower case.

4. Check it:

   ```sh
   shield-manager source list
   ```

`shield-manager source remove org.example.app` stops using GitHub for that app. It works for
the built-in ones too. Sources are saved in `~/.config/shield-manager/app-sources.json`.
Downloads are still only installed when they're signed by the same developer as the copy on
your Shields.

## Syncing settings

Settings follow the same reference Shield as apps (set it with `fleet set-reference`). Each
step below is a command to run in the terminal where shield-manager is installed.

1. See which settings can be synced, grouped by category:

   ```sh
   shield-manager settings list
   ```

2. Compare every Shield with the reference. Nothing is changed:

   ```sh
   shield-manager settings status
   ```

   Each differing setting is shown with both values, for example
   `Start screensaver after: 5 min here, 15 min on the reference`.

3. Preview, then copy the reference's settings to the other Shields:

   ```sh
   shield-manager settings sync --dry-run
   shield-manager settings sync
   ```

   It prints each setting as it's set, e.g. `bedroom: setting Font size (3/7) - 42%`, and
   checks that the Shield kept the new value.

By default it syncs the screensaver and sleep timers, animation speeds, clock format, font
size, caption style, accessibility options and services, keyboards, HDMI-CEC options and
system sounds. Surround sound formats depend on the TV or receiver each Shield is plugged
into, so they're only synced when asked for: `shield-manager settings sync -c surround`.
`-c` picks categories (repeatable), and `-d`/`-g`/`--all` and `--from NAME` work as for
`fleet`.

What it won't do:

- Settings that name an app (a screensaver, keyboard or accessibility service such as Button
  Mapper) are only set on Shields that have that app. Run `fleet sync` first.
- A setting the reference has never changed is left as is.
- Settings that belong to one Shield, or keep it on the network and connected to ADB (its
  name, IDs, Wi-Fi, Bluetooth, developer and debugging options), are never synced.
- App data (logins, in-app preferences) isn't synced; Android only lets ADB read it with
  root.

To find other settings worth syncing (for example Nvidia's own), list everything else that
differs, then sync one by name:

```sh
shield-manager settings status --others -d bedroom
shield-manager settings sync --key global/SETTING_NAME
```

## Development

Follow the install steps, but in step 4 run `pip install -e ".[dev]"` instead, so code changes
take effect without reinstalling and the test tools are installed. Then:

```sh
ruff check . && ruff format --check .
pytest
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
